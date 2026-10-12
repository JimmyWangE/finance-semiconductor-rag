"""End-to-end checks for the local PostgreSQL/Redis/Keycloak deployment."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import httpx
import psycopg
import redis

from semi_rag.auth import AuthManager, UserContext


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"PASS {message}")


def check_connection_denied(dsn: str, message: str) -> None:
    try:
        psycopg.connect(dsn).close()
    except psycopg.OperationalError:
        print(f"PASS {message}")
        return
    raise AssertionError(message)


def main() -> int:
    origin = os.environ["RAG_PUBLIC_ORIGIN"].rstrip("/")
    issuer = os.environ["RAG_OIDC_ISSUER"].rstrip("/")

    with psycopg.connect(os.environ["RAG_DATABASE_URL"]) as database:
        check(database.execute("SELECT 1").fetchone()[0] == 1, "PostgreSQL finance_rag connection")
        migrations = database.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
        check(migrations >= 3, "PostgreSQL migrations applied")
    with psycopg.connect(os.environ["KEYCLOAK_DATABASE_URL"]) as database:
        check(database.execute("SELECT 1").fetchone()[0] == 1, "PostgreSQL keycloak connection")
    check_connection_denied(os.environ["RAG_DATABASE_URL"].rsplit("/", 1)[0] + "/keycloak",
                            "rag_app cannot connect to keycloak database")
    check_connection_denied(os.environ["KEYCLOAK_DATABASE_URL"].rsplit("/", 1)[0] + "/finance_rag",
                            "keycloak_app cannot connect to finance_rag database")

    cache = redis.Redis.from_url(os.environ["RAG_REDIS_URL"], decode_responses=True)
    check(cache.ping(), "Redis authenticated PING")
    check(cache.config_get("appendonly").get("appendonly") == "yes", "Redis AOF enabled")

    with httpx.Client(timeout=20, follow_redirects=False, trust_env=False) as client:
        ready = client.get("http://127.0.0.1:9000/sso/health/ready")
        check(ready.status_code == 200 and ready.json().get("status") == "UP", "Keycloak ready")

        discovery = client.get(issuer + "/.well-known/openid-configuration")
        check(discovery.status_code == 200, "OIDC discovery reachable through port 189")
        check(discovery.json().get("issuer") == issuer, "OIDC issuer matches application config")

        admin_token = client.post(
            "http://127.0.0.1:8180/sso/realms/master/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": "kc-admin",
                "password": os.environ["KEYCLOAK_ADMIN_PASSWORD"],
            },
        )
        check(admin_token.status_code == 200, "Keycloak bootstrap administrator available")
        admin_headers = {"Authorization": "Bearer " + admin_token.json()["access_token"]}
        admin_base = "http://127.0.0.1:8180/sso/admin/realms/finance"
        realm = client.get(admin_base, headers=admin_headers)
        check(realm.status_code == 200 and realm.json().get("realm") == "finance" and
              realm.json().get("displayName") == "金融半导体分析平台" and
              not realm.json().get("editUsernameAllowed"),
              "Keycloak finance realm branding and username policy")
        clients = client.get(admin_base + "/clients", params={"clientId": "finance-rag"},
                             headers=admin_headers).json()
        expected_callback = origin + "/auth/callback"
        mapper_names = {mapper.get("name") for mapper in clients[0].get("protocolMappers", [])} if clients else set()
        check(len(clients) == 1 and not clients[0].get("directAccessGrantsEnabled") and
              expected_callback in clients[0].get("redirectUris", []) and
              {"realm roles", "finance-rag audience"}.issubset(mapper_names),
              "Keycloak confidential client redirect and grant policy")
        remote_secret = client.get(admin_base + f"/clients/{clients[0]['id']}/client-secret",
                                   headers=admin_headers).json().get("value")
        local_secret = Path(os.environ["RAG_OIDC_CLIENT_SECRET_FILE"]).read_text().strip()
        check(remote_secret == local_secret, "Keycloak client secret matches application secret")
        for username, expected_role in (("admin", "admin"), ("rag-visitor", "visitor")):
            users = client.get(admin_base + "/users", params={"username": username, "exact": "true"},
                               headers=admin_headers).json()
            check(len(users) == 1, f"Keycloak user {username} exists")
            roles = client.get(admin_base + f"/users/{users[0]['id']}/role-mappings/realm",
                               headers=admin_headers).json()
            check(expected_role in {role["name"] for role in roles},
                  f"Keycloak user {username} has {expected_role} role")
            if username == 'admin':
                admin_subject = users[0]['id']
                legacy = client.get(admin_base + "/users", params={"username": "rag-admin", "exact": "true"},
                                    headers=admin_headers).json()
                check(not legacy, "Old admin username removed without deleting the user")

        check(client.get(origin + "/proxy-health").status_code == 200, "Nginx proxy health")
        app_health = client.get(origin + "/health")
        check(app_health.status_code in {200, 503}, "FastAPI health reachable")

        unauth = client.get(origin + "/stats")
        check(unauth.status_code == 401, "Unauthenticated API rejected")
        page = client.get(origin + "/", headers={"Accept": "text/html"})
        check(page.status_code == 302 and page.headers.get("location", "").startswith("/auth/login"),
              "Unauthenticated page redirects to SSO")
        login = client.get(origin + "/auth/login")
        check(login.status_code == 302 and "/sso/realms/finance/protocol/openid-connect/auth" in
              login.headers.get("location", ""), "Application starts OIDC authorization flow")
        login_page = client.get(login.headers["location"])
        check(login_page.status_code == 200 and "username" in login_page.text.lower(),
              "Keycloak login page renders through port 189")

        auth = AuthManager()
        expires = int(time.time()) + 300
        admin_cookie = auth.session_token(UserContext("stack-admin", "Stack Admin", "admin", expires, "verify"))
        visitor_cookie = auth.session_token(UserContext("stack-visitor", "Stack Visitor", "visitor", expires, "verify"))
        admin = {"rag_session": admin_cookie}
        visitor = {"rag_session": visitor_cookie}

        real_admin = {"rag_session": auth.session_token(
            UserContext(admin_subject, "Admin", "admin", expires, "verify-history"))}
        history = client.get(origin + "/agent/jobs", cookies=real_admin)
        check(history.status_code == 200 and isinstance(history.json(), list),
              "Financial Agent history loads without PostgreSQL type errors")
        if history.json():
            record = history.json()[0]
            check(client.get(origin + f"/agent/jobs/{record['id']}", cookies=real_admin).status_code == 200,
                  "Existing admin analysis remains accessible after account rename")

        check(client.get(origin + "/auth/me", cookies=visitor).json().get("role") == "visitor",
              "Visitor session accepted")
        check(client.get(origin + "/uploads", cookies=visitor).status_code == 403,
              "Visitor cannot access upload administration")
        check(client.get(origin + "/uploads", cookies=admin).status_code == 200,
              "Admin can access upload administration")

        metrics = client.get(origin + "/metrics", headers={
            "Authorization": "Bearer " + os.environ["RAG_METRICS_TOKEN"]
        })
        check(metrics.status_code == 200 and "rag_http_requests_total" in metrics.text,
              "Prometheus metrics protected and available")

        task_response = client.post(origin + "/tasks", cookies=admin,
            headers={"Origin": origin, "Content-Type": "application/json"}, json={
                "type": "article.sync",
                "payload": {"source_id": "stack-verification"},
                "idempotency_key": "local-stack-verification-v1",
                "run_at": "2099-01-01T00:00:00Z",
            })
        check(task_response.status_code in {200, 202}, "Persistent task accepted")
        task_id = str(task_response.json()["id"])
        check(client.get(origin + f"/tasks/{task_id}", cookies=visitor).status_code == 404,
              "Task ownership isolation enforced")

    with psycopg.connect(os.environ["RAG_DATABASE_URL"]) as database:
        count = database.execute("SELECT count(*) FROM task_jobs WHERE id=%s", (task_id,)).fetchone()[0]
        check(count == 1, "Task persisted in PostgreSQL")
    check(cache.xlen(os.environ.get("RAG_REDIS_STREAM", "rag:tasks")) >= 1,
          "Task notification persisted in Redis Stream")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"FAIL {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
