import base64
import json
import time
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import hashes
from fastapi.testclient import TestClient
from semi_rag.api import create_app
from semi_rag.auth import AuthManager, AuthenticationError, UserContext


class Retriever:
    manifest = {'generation': 'test'}
    chunks = []
    by_id = {}

    def search(self, query, filters, top_k, mode='rerank'):
        return {'hits': [], 'filters': filters}


class Answerer:
    def llm_health(self):
        return {'model': 'test'}

    def close(self):
        pass


def test_dev_auth_permissions_and_owner(tmp_path, monkeypatch):
    monkeypatch.setenv('RAG_AUTH_MODE', 'dev')
    app = create_app({'data_dir': str(tmp_path), 'pdf_dir': str(tmp_path)}, Retriever(), Answerer())
    with TestClient(app) as client:
        assert client.get('/health').status_code == 200
        assert client.get('/conversations').status_code == 401
        assert client.get('/conversations', headers={'accept': 'text/html'}).status_code == 404  # dev has no SSO login
        visitor = {'x-rag-dev-user': 'employee-1'}
        admin = {'x-rag-dev-user': 'admin-1', 'x-rag-dev-role': 'admin'}
        assert client.get('/auth/me', headers=visitor).json()['role'] == 'visitor'
        assert client.get('/uploads', headers=visitor).status_code == 403
        assert client.post('/article-api/sync', headers=visitor, json={}).status_code == 403
        assert client.post('/tasks', headers=visitor, json={'type':'article.sync'}).status_code == 403
        assert client.get('/uploads', headers=admin).status_code == 200
        task = client.post('/tasks', headers=admin, json={'type':'article.sync','payload':{},
            'idempotency_key':'future','run_at':'2099-01-01T00:00:00Z'}).json()
        assert client.get('/tasks/'+task['id'], headers=admin).status_code == 200
        assert client.get('/tasks/'+task['id'], headers=visitor).status_code == 404
        assert client.post('/tasks/'+task['id']+'/cancel', headers=admin).json()['status'] == 'cancelled'
        row = client.post('/conversations', headers=visitor, json={'title': 'mine'}).json()
        assert client.get('/conversations/' + row['id'], headers=visitor).status_code == 200
        assert client.get('/conversations/' + row['id'], headers=admin).status_code == 404


def test_session_signature_and_expiry(monkeypatch):
    monkeypatch.setenv('RAG_AUTH_MODE', 'oidc')
    monkeypatch.setenv('RAG_SESSION_SECRET', 'a' * 40)
    monkeypatch.setenv('RAG_OIDC_ISSUER', 'https://id.example')
    monkeypatch.setenv('RAG_OIDC_CLIENT_ID', 'rag')
    monkeypatch.setenv('RAG_OIDC_REDIRECT_URI', 'https://rag.example/auth/callback')
    auth = AuthManager()
    valid = auth.session_token(UserContext('abc', 'ABC', 'visitor', int(time.time()) + 20, 'sid'))
    assert auth.user_from_session(valid).subject == 'abc'
    with __import__('pytest').raises(AuthenticationError):
        auth.user_from_session(valid[:-2] + 'xx')
    expired = auth.session_token(UserContext('abc', 'ABC', 'visitor', int(time.time()) - 1, 'sid'))
    with __import__('pytest').raises(AuthenticationError, match='expired'):
        auth.user_from_session(expired)


def _b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode()


def test_oidc_login_callback_signature_nonce_role(tmp_path, monkeypatch):
    monkeypatch.setenv('RAG_AUTH_MODE', 'oidc')
    monkeypatch.setenv('RAG_SESSION_SECRET', 'b' * 40)
    monkeypatch.setenv('RAG_OIDC_ISSUER', 'https://id.example')
    monkeypatch.setenv('RAG_OIDC_CLIENT_ID', 'rag')
    monkeypatch.setenv('RAG_OIDC_CLIENT_SECRET', 'secret')
    monkeypatch.setenv('RAG_OIDC_REDIRECT_URI', 'https://rag.example/auth/callback')
    monkeypatch.setenv('RAG_OIDC_ADMIN_GROUPS', 'research-admins')
    auth = AuthManager()
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = private.public_key().public_numbers()
    auth._metadata = {'issuer': auth.issuer, 'authorization_endpoint': 'https://id.example/authorize',
                      'token_endpoint': 'https://id.example/token'}
    auth._jwks = {'keys': [{'kid': 'key-1', 'e': _b64(public.e.to_bytes(3, 'big')),
                             'n': _b64(public.n.to_bytes(256, 'big'))}]}
    auth._cache_until = time.monotonic() + 60

    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, data):
            state = auth._unsigned(state_cookie)
            claims = {'iss': auth.issuer, 'aud': auth.client_id, 'sub': 'user-1',
                      'nonce': state['nonce'], 'exp': int(time.time()) + 600,
                      'iat': int(time.time()), 'groups': [], 'realm_access': {'roles': ['research-admins']}}
            head = _b64(json.dumps({'alg': 'RS256', 'kid': 'key-1'}).encode())
            payload = _b64(json.dumps(claims).encode())
            signature = _b64(private.sign(f'{head}.{payload}'.encode(), padding.PKCS1v15(), hashes.SHA256()))
            class Reply:
                def raise_for_status(self): pass
                def json(self): return {'id_token': f'{head}.{payload}.{signature}'}
            return Reply()

    monkeypatch.setattr('semi_rag.auth.httpx.AsyncClient', lambda **kwargs: FakeClient())
    import asyncio
    location, state_cookie = asyncio.run(auth.login('/agent'))
    state = parse_qs(urlsplit(location).query)['state'][0]
    user, return_to = asyncio.run(auth.callback('code', state, state_cookie))
    assert (user.role, return_to) == ('admin', '/agent')
    with __import__('pytest').raises(AuthenticationError, match='state mismatch'):
        asyncio.run(auth.callback('code', 'wrong', state_cookie))
