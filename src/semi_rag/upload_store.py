from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def upload_root(config: dict) -> Path:
    return Path(config['data_dir']) / 'rag/uploads'


def upload_dir(config: dict, upload_id: str) -> Path:
    if not re.fullmatch(r'[0-9a-f]{64}', upload_id):
        raise ValueError('Invalid upload ID')
    return upload_root(config) / upload_id


def atomic_json(path: Path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.replace(path)


class UploadStore:
    def __init__(self, config):
        self.config = config
        root = upload_root(config)
        root.mkdir(parents=True, exist_ok=True)
        self.database = root / 'jobs.sqlite3'
        with self.connect() as connection:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, status TEXT NOT NULL, data TEXT NOT NULL)')

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.database, timeout=30)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def get(self, upload_id: str):
        with self.connect() as connection:
            row = connection.execute('SELECT data FROM jobs WHERE id=?', (upload_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def list(self):
        with self.connect() as connection:
            rows = connection.execute('SELECT data FROM jobs ORDER BY rowid DESC').fetchall()
        return [json.loads(row[0]) for row in rows]

    def create(self, row: dict):
        row['updated_at'] = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection:
            connection.execute('INSERT INTO jobs VALUES (?,?,?)', (row['id'], row['status'], json.dumps(row, ensure_ascii=False)))
        return row

    def update(self, upload_id: str, **changes):
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            saved = connection.execute('SELECT data FROM jobs WHERE id=?', (upload_id,)).fetchone()
            if not saved:
                raise KeyError(upload_id)
            row = {**json.loads(saved[0]), **changes, 'updated_at': datetime.now(timezone.utc).isoformat()}
            connection.execute('UPDATE jobs SET status=?,data=? WHERE id=?',
                               (row['status'], json.dumps(row, ensure_ascii=False), upload_id))
        return row


def approved_chunks(config: dict) -> list[dict]:
    records = []
    for path in sorted(upload_root(config).glob('*/approved.json')):
        records.extend(json.loads(path.read_text())['chunks'])
    return records
