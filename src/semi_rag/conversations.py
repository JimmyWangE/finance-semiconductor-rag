from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class ConversationConflict(Exception):
    pass


class ConversationStore:
    """Browser-scoped durable conversations; commit a completed turn atomically."""

    MODES = ('rag', 'general')

    def __init__(self, config):
        root = Path(config['data_dir']) / 'rag/conversations'
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / 'history.sqlite3'
        self.max_turns = config.get('conversation_max_turns', 200)
        with self.connect() as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, title TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0, filters TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0,
                    mode TEXT NOT NULL DEFAULT 'rag'
                );
                CREATE INDEX IF NOT EXISTS conversation_owner ON conversations(owner, deleted, updated_at);
                CREATE TABLE IF NOT EXISTS turns (
                    conversation_id TEXT NOT NULL, number INTEGER NOT NULL, request_id TEXT NOT NULL,
                    request TEXT NOT NULL, response TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(conversation_id, number), UNIQUE(conversation_id, request_id)
                );
            ''')
            columns = {row['name'] for row in conn.execute('PRAGMA table_info(conversations)')}
            if 'mode' not in columns:
                conn.execute("ALTER TABLE conversations ADD COLUMN mode TEXT NOT NULL DEFAULT 'rag'")
            conn.execute('CREATE INDEX IF NOT EXISTS conversation_owner_mode '
                         'ON conversations(owner, mode, deleted, updated_at)')

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def now():
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def public(row):
        data = dict(row)
        data.pop('owner', None)
        data.pop('deleted', None)
        data['filters'] = json.loads(data['filters'])
        return data

    @classmethod
    def validate_mode(cls, mode: str):
        if mode not in cls.MODES:
            raise ValueError(f'Unsupported conversation mode: {mode}')

    def create(self, owner: str, title: str = '新会话', mode: str = 'rag'):
        self.validate_mode(mode)
        cid, now = uuid.uuid4().hex, self.now()
        with self.connect() as conn:
            conn.execute('INSERT INTO conversations(id,owner,title,created_at,updated_at,mode) VALUES (?,?,?,?,?,?)',
                         (cid, owner, title, now, now, mode))
        return self.get(owner, cid, mode)

    def list(self, owner: str, offset=0, limit=50, mode: str = 'rag'):
        self.validate_mode(mode)
        with self.connect() as conn:
            total = conn.execute('SELECT COUNT(*) FROM conversations WHERE owner=? AND mode=? AND deleted=0',
                                 (owner, mode)).fetchone()[0]
            rows = conn.execute('SELECT * FROM conversations WHERE owner=? AND mode=? AND deleted=0 '
                                'ORDER BY updated_at DESC,id LIMIT ? OFFSET ?',
                                (owner, mode, limit, offset)).fetchall()
        return {'conversations': [self.public(row) for row in rows], 'total': total}

    def get(self, owner: str, cid: str, mode: str = 'rag'):
        self.validate_mode(mode)
        with self.connect() as conn:
            row = conn.execute('SELECT * FROM conversations WHERE id=? AND owner=? AND mode=? AND deleted=0',
                               (cid, owner, mode)).fetchone()
        if row is None:
            raise KeyError(cid)
        return self.public(row)

    def history(self, owner: str, cid: str, mode: str = 'rag'):
        self.validate_mode(mode)
        with self.connect() as conn:
            conn.execute('BEGIN')
            row = conn.execute('SELECT * FROM conversations WHERE id=? AND owner=? AND mode=? AND deleted=0',
                               (cid, owner, mode)).fetchone()
            if row is None:
                raise KeyError(cid)
            conversation = self.public(row)
            turns = conn.execute('SELECT * FROM turns WHERE conversation_id=? ORDER BY number', (cid,)).fetchall()
        return {**conversation, 'turns': [{'number': row['number'], 'request_id': row['request_id'],
            'created_at': row['created_at'], 'query': json.loads(row['request'])['query'],
            'response': json.loads(row['response'])} for row in turns]}

    def replay(self, owner: str, cid: str, request_id: str, request: dict, mode: str = 'rag'):
        self.get(owner, cid, mode)
        with self.connect() as conn:
            row = conn.execute('SELECT request,response FROM turns WHERE conversation_id=? AND request_id=?', (cid, request_id)).fetchone()
        if not row:
            return None
        if json.loads(row['request']) != request:
            raise ConversationConflict('request_id 已用于其他请求，请生成新的请求编号')
        return json.loads(row['response'])

    def append(self, owner: str, cid: str, revision: int, request_id: str, request: dict,
               response: dict, filters: dict, mode: str = 'rag'):
        self.validate_mode(mode)
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM conversations WHERE id=? AND owner=? AND mode=? AND deleted=0',
                               (cid, owner, mode)).fetchone()
            if row is None:
                raise KeyError(cid)
            if row['revision'] != revision:
                raise ConversationConflict('会话已在其他页面更新，请重新加载后再发送')
            count = conn.execute('SELECT COUNT(*) FROM turns WHERE conversation_id=?', (cid,)).fetchone()[0]
            if count >= self.max_turns:
                raise ConversationConflict('本会话已达到轮数上限，请新建会话')
            response = {**response, 'conversation_id': cid, 'turn_number': count + 1,
                        'conversation_revision': revision + 1, 'request_id': request_id}
            now = self.now()
            conn.execute('INSERT INTO turns VALUES (?,?,?,?,?,?)', (cid, count + 1, request_id,
                json.dumps(request, ensure_ascii=False), json.dumps(response, ensure_ascii=False), now))
            title = request['query'][:60] if row['title'] == '新会话' and count == 0 else row['title']
            conn.execute('UPDATE conversations SET revision=revision+1,title=?,filters=?,updated_at=? WHERE id=?',
                         (title, json.dumps(filters, ensure_ascii=False), now, cid))
        return response

    def change(self, owner: str, cid: str, revision: int, title=None, delete=False, mode: str = 'rag'):
        self.validate_mode(mode)
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM conversations WHERE id=? AND owner=? AND mode=? AND deleted=0',
                               (cid, owner, mode)).fetchone()
            if row is None:
                raise KeyError(cid)
            if row['revision'] != revision:
                raise ConversationConflict('会话已更新，请重新加载')
            conn.execute('UPDATE conversations SET title=?,deleted=?,revision=revision+1,updated_at=? WHERE id=?',
                         (title if title is not None else row['title'], int(delete), self.now(), cid))
        return {'deleted': True} if delete else self.get(owner, cid, mode)
