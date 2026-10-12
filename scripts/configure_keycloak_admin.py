"""Update the existing finance realm without recreating users or losing task ownership."""

from __future__ import annotations

import os
from pathlib import Path

import httpx


def main() -> None:
    secrets = Path(__file__).resolve().parents[1] / 'infra/secrets'
    base = 'http://127.0.0.1:8180/sso'
    with httpx.Client(trust_env=False, timeout=20) as client:
        token = client.post(base + '/realms/master/protocol/openid-connect/token', data={
            'grant_type': 'password', 'client_id': 'admin-cli', 'username': 'kc-admin',
            'password': (secrets / 'keycloak_admin_password').read_text().strip(),
        })
        token.raise_for_status()
        headers = {'Authorization': 'Bearer ' + token.json()['access_token']}
        realm_url = base + '/admin/realms/finance'
        users_url = realm_url + '/users'

        current = client.get(users_url, params={'username': 'rag-admin', 'exact': 'true'}, headers=headers)
        current.raise_for_status()
        existing = client.get(users_url, params={'username': 'admin', 'exact': 'true'}, headers=headers)
        existing.raise_for_status()
        old, renamed = current.json(), existing.json()
        if len(old) + len(renamed) != 1:
            raise RuntimeError('expected exactly one existing rag-admin or admin user; no account changed')
        user = (old or renamed)[0]
        user_url = users_url + '/' + user['id']
        realm = client.get(realm_url, headers=headers)
        realm.raise_for_status()
        settings = realm.json()
        if old:
            if not settings.get('editUsernameAllowed'):
                response = client.put(realm_url, headers=headers,
                                      json={**settings, 'editUsernameAllowed': True})
                response.raise_for_status()
            try:
                response = client.put(user_url, headers=headers, json={**user, 'username': 'admin'})
                response.raise_for_status()
            finally:
                if not settings.get('editUsernameAllowed'):
                    response = client.put(realm_url, headers=headers,
                                          json={**settings, 'editUsernameAllowed': False})
                    response.raise_for_status()

        response = client.put(user_url + '/reset-password', headers=headers, json={
            'type': 'password', 'value': 'admin', 'temporary': False,
        })
        response.raise_for_status()

        response = client.put(realm_url, headers=headers, json={
            **settings, 'displayName': '金融半导体分析平台',
        })
        response.raise_for_status()

        password_file = secrets / 'rag_admin_password'
        password_file.write_text('admin\n')
        os.chmod(password_file, 0o600)
        roles = client.get(user_url + '/role-mappings/realm', headers=headers)
        roles.raise_for_status()
        if 'admin' not in {role['name'] for role in roles.json()}:
            raise RuntimeError('renamed user is missing the admin realm role')
        print('Updated realm title and admin account; preserved Keycloak user ID and role.')


if __name__ == '__main__':
    main()
