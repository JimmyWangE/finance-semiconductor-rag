"""OIDC authentication, signed sessions, and the phase-one role policy."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import httpx
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives import hashes
from cryptography.exceptions import InvalidSignature
from fastapi import Request


SESSION_COOKIE = 'rag_session'
STATE_COOKIE = 'rag_oidc_state'


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode('ascii')


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


def _csv(name: str) -> set[str]:
    return {item.strip() for item in os.getenv(name, '').split(',') if item.strip()}


@dataclass(frozen=True)
class UserContext:
    subject: str
    display_name: str
    role: str
    expires_at: int
    session_id: str


class AuthenticationError(Exception):
    pass


class AuthManager:
    def __init__(self):
        self.mode = os.getenv('RAG_AUTH_MODE', 'legacy').lower()
        if self.mode not in {'oidc', 'legacy', 'dev'}:
            raise ValueError('RAG_AUTH_MODE must be oidc, legacy, or dev')
        self.session_ttl = int(os.getenv('RAG_SESSION_TTL_SECONDS', '28800'))
        self.issuer = os.getenv('RAG_OIDC_ISSUER', '').rstrip('/')
        self.client_id = os.getenv('RAG_OIDC_CLIENT_ID', '')
        self.redirect_uri = os.getenv('RAG_OIDC_REDIRECT_URI', '')
        self.scopes = os.getenv('RAG_OIDC_SCOPES', 'openid profile email groups')
        self.admin_groups = _csv('RAG_OIDC_ADMIN_GROUPS')
        self.admin_subjects = _csv('RAG_OIDC_ADMIN_SUBJECTS')
        self._metadata = None
        self._jwks = None
        self._cache_until = 0.0
        self.secret = self._load_secret()
        if self.mode == 'oidc' and not all((self.issuer, self.client_id, self.redirect_uri, self.secret)):
            raise ValueError('OIDC mode requires issuer, client id, redirect URI, and session secret')

    def _load_secret(self) -> bytes:
        path = os.getenv('RAG_SESSION_SECRET_FILE', '')
        value = Path(path).read_text().strip() if path and Path(path).is_file() else os.getenv('RAG_SESSION_SECRET', '')
        if self.mode == 'oidc' and len(value) < 32:
            raise ValueError('RAG session secret must contain at least 32 characters')
        return value.encode() if value else secrets.token_bytes(32)

    def _signed(self, payload: dict) -> str:
        body = _b64encode(json.dumps(payload, separators=(',', ':'), ensure_ascii=True).encode())
        signature = _b64encode(hmac.new(self.secret, body.encode(), hashlib.sha256).digest())
        return body + '.' + signature

    def _unsigned(self, value: str) -> dict:
        try:
            body, supplied = value.split('.', 1)
            expected = _b64encode(hmac.new(self.secret, body.encode(), hashlib.sha256).digest())
            if not hmac.compare_digest(expected, supplied):
                raise AuthenticationError('invalid session signature')
            payload = json.loads(_b64decode(body))
            if int(payload['exp']) <= int(time.time()):
                raise AuthenticationError('session expired')
            return payload
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise AuthenticationError('invalid session') from exc

    def session_token(self, user: UserContext) -> str:
        return self._signed({'sub': user.subject, 'name': user.display_name, 'role': user.role,
                             'exp': user.expires_at, 'sid': user.session_id})

    def user_from_session(self, token: str) -> UserContext:
        payload = self._unsigned(token)
        if payload.get('role') not in {'visitor', 'admin'} or not payload.get('sub'):
            raise AuthenticationError('invalid role or subject')
        return UserContext(str(payload['sub']), str(payload.get('name') or payload['sub']),
                           payload['role'], int(payload['exp']), str(payload.get('sid', '')))

    def authenticate(self, request: Request) -> UserContext | None:
        if self.mode == 'legacy':
            return None
        if self.mode == 'dev':
            subject = request.headers.get('x-rag-dev-user', '').strip()
            if not subject:
                return None
            role = request.headers.get('x-rag-dev-role', 'visitor').strip()
            if role not in {'visitor', 'admin'}:
                raise AuthenticationError('invalid development role')
            return UserContext(subject, request.headers.get('x-rag-dev-name', subject), role,
                               int(time.time()) + 3600, 'dev')
        token = request.cookies.get(SESSION_COOKIE, '')
        return self.user_from_session(token) if token else None

    async def metadata(self) -> dict:
        if self._metadata and time.monotonic() < self._cache_until:
            return self._metadata
        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
            response = await client.get(self.issuer + '/.well-known/openid-configuration')
            response.raise_for_status()
            metadata = response.json()
            jwks_response = await client.get(metadata['jwks_uri'])
            jwks_response.raise_for_status()
        if metadata.get('issuer', '').rstrip('/') != self.issuer:
            raise AuthenticationError('OIDC issuer mismatch')
        self._metadata, self._jwks = metadata, jwks_response.json()
        self._cache_until = time.monotonic() + 300
        return metadata

    async def login(self, return_to: str = '/') -> tuple[str, str]:
        metadata = await self.metadata()
        if not return_to.startswith('/') or return_to.startswith('//'):
            return_to = '/'
        state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        state_token = self._signed({'state': state, 'nonce': nonce, 'return_to': return_to,
                                    'exp': int(time.time()) + 600})
        query = urlencode({'response_type': 'code', 'client_id': self.client_id,
                           'redirect_uri': self.redirect_uri, 'scope': self.scopes,
                           'state': state, 'nonce': nonce})
        return metadata['authorization_endpoint'] + '?' + query, state_token

    async def callback(self, code: str, state: str, state_cookie: str) -> tuple[UserContext, str]:
        saved = self._unsigned(state_cookie)
        if not hmac.compare_digest(str(saved.get('state', '')), state):
            raise AuthenticationError('OIDC state mismatch')
        metadata = await self.metadata()
        secret_path = os.getenv('RAG_OIDC_CLIENT_SECRET_FILE', '')
        client_secret = (Path(secret_path).read_text().strip() if secret_path and Path(secret_path).is_file()
                         else os.getenv('RAG_OIDC_CLIENT_SECRET', ''))
        async with httpx.AsyncClient(trust_env=False, timeout=15) as client:
            response = await client.post(metadata['token_endpoint'], data={
                'grant_type': 'authorization_code', 'code': code, 'redirect_uri': self.redirect_uri,
                'client_id': self.client_id, 'client_secret': client_secret})
            response.raise_for_status()
        claims = self._verify_id_token(response.json().get('id_token', ''), saved['nonce'])
        groups = claims.get('groups') or []
        if isinstance(groups, str):
            groups = [groups]
        realm_roles = (claims.get('realm_access') or {}).get('roles') or []
        if isinstance(realm_roles, str):
            realm_roles = [realm_roles]
        subject = str(claims['sub'])
        granted = set(groups) | set(realm_roles)
        role = 'admin' if subject in self.admin_subjects or self.admin_groups.intersection(granted) else 'visitor'
        expires = min(int(claims['exp']), int(time.time()) + self.session_ttl)
        user = UserContext(subject, str(claims.get('name') or claims.get('preferred_username') or subject),
                           role, expires, secrets.token_urlsafe(18))
        return user, str(saved.get('return_to', '/'))

    def _verify_id_token(self, token: str, nonce: str) -> dict:
        try:
            header_part, payload_part, signature_part = token.split('.')
            header = json.loads(_b64decode(header_part))
            claims = json.loads(_b64decode(payload_part))
            if header.get('alg') != 'RS256':
                raise AuthenticationError('only RS256 ID tokens are accepted')
            key = next(key for key in self._jwks.get('keys', []) if key.get('kid') == header.get('kid'))
            public_key = rsa.RSAPublicNumbers(int.from_bytes(_b64decode(key['e']), 'big'),
                                               int.from_bytes(_b64decode(key['n']), 'big')).public_key()
            public_key.verify(_b64decode(signature_part), f'{header_part}.{payload_part}'.encode(),
                              padding.PKCS1v15(), hashes.SHA256())
            audience = claims.get('aud', [])
            audience = [audience] if isinstance(audience, str) else audience
            now = int(time.time())
            if claims.get('iss', '').rstrip('/') != self.issuer or self.client_id not in audience:
                raise AuthenticationError('ID token issuer or audience mismatch')
            if len(audience) > 1 and claims.get('azp') != self.client_id:
                raise AuthenticationError('ID token authorized party mismatch')
            if int(claims.get('exp', 0)) <= now or int(claims.get('iat', now + 1)) > now + 60:
                raise AuthenticationError('ID token expired or issued in the future')
            if not hmac.compare_digest(str(claims.get('nonce', '')), nonce):
                raise AuthenticationError('ID token nonce mismatch')
            return claims
        except (ValueError, KeyError, StopIteration, TypeError, InvalidSignature, json.JSONDecodeError) as exc:
            raise AuthenticationError('invalid ID token') from exc


def admin_required(method: str, path: str) -> bool:
    """Central phase-one policy; object ownership remains enforced by stores."""
    if path == '/metrics' or path.startswith(('/rss-admin', '/dash', '/trpc')):
        return True
    if path == '/library' or path == '/library.js':
        return True
    if path.startswith('/uploads'):
        return True
    if method in {'POST', 'PUT', 'PATCH', 'DELETE'} and path.startswith((
            '/article-api/sync', '/article-api/import', '/article-api/sources/',
            '/eastmoney/import', '/report-sources/')):
        return True
    return False
