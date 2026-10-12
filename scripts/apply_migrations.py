"""Apply checksummed PostgreSQL migrations in lexical order."""
import hashlib, os
from pathlib import Path

import psycopg

root=Path(__file__).resolve().parents[1]
dsn=os.environ['RAG_DATABASE_URL']
with psycopg.connect(dsn) as db:
    db.execute('CREATE TABLE IF NOT EXISTS schema_migrations(version text PRIMARY KEY,checksum text NOT NULL,applied_at timestamptz NOT NULL DEFAULT now())')
    for path in sorted((root/'migrations').glob('*.sql')):
        sql=path.read_text(); checksum=hashlib.sha256(sql.encode()).hexdigest()
        old=db.execute('SELECT checksum FROM schema_migrations WHERE version=%s',(path.name,)).fetchone()
        if old:
            if old[0]!=checksum: raise SystemExit(f'checksum mismatch: {path.name}')
            continue
        with db.transaction():
            db.execute(sql)
            db.execute('INSERT INTO schema_migrations(version,checksum) VALUES(%s,%s)',(path.name,checksum))
        print('applied',path.name)
