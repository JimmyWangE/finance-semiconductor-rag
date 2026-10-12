"""Idempotently import legacy Agent/upload task history into PostgreSQL."""
from __future__ import annotations

import argparse, json, os, sqlite3
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

STATUS={'completed':'succeeded','indexed':'succeeded','cancelled':'cancelled','failed':'failed',
        'index_failed':'failed','queued':'queued','queued_index':'queued','running':'retry_wait',
        'parsing':'retry_wait','recognizing':'retry_wait','indexing':'retry_wait','review':'succeeded'}


def uid(kind,legacy):return str(uuid5(NAMESPACE_URL,f'semi-rag:{kind}:{legacy}'))


def insert(db,task_id,kind,owner,status,payload,result,error,created,updated):
    db.execute('''INSERT INTO task_jobs(id,type,owner_id,status,payload,result,error,idempotency_key,
      created_at,updated_at,finished_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
      CASE WHEN %s IN ('succeeded','failed','cancelled') THEN %s::timestamptz END)
      ON CONFLICT(id) DO NOTHING''',(task_id,kind,owner,status,json.dumps(payload),json.dumps(result) if result else None,
      json.dumps({'message':error}) if error else None,f'legacy:{payload["legacy_id"]}',created,updated,status,updated))
    db.execute('''INSERT INTO task_events(task_id,sequence,event_type,payload,created_at)
      VALUES(%s,1,'migrated',%s,%s) ON CONFLICT(task_id,sequence) DO NOTHING''',
      (task_id,json.dumps({'status':status,'legacy':True}),updated))


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--data-dir',type=Path,default=Path('data'));parser.add_argument('--dry-run',action='store_true');args=parser.parse_args()
    records=[]
    agent=args.data_dir/'finance_agent_jobs.sqlite3'
    if agent.exists():
        source=sqlite3.connect(agent);source.row_factory=sqlite3.Row
        for row in source.execute('SELECT * FROM jobs'):
            value=dict(row);payload={'legacy_id':value['id'],'ticker':value['ticker'],'trade_date':value['trade_date'],'analysts':json.loads(value['analysts'])}
            records.append((uid('agent',value['id']),'agent.analysis',value['owner'],STATUS[value['status']],payload,
                json.loads(value['result']) if value['result'] else None,value['error'],value['created_at'],value['updated_at']))
    upload=args.data_dir/'rag/uploads/jobs.sqlite3'
    if upload.exists():
        source=sqlite3.connect(upload)
        for legacy,status,data in source.execute('SELECT id,status,data FROM jobs'):
            value=json.loads(data);kind='document.index' if status in ('queued_index','indexing','index_failed','indexed') else 'document.parse'
            stamp=value.get('updated_at')
            records.append((uid('upload',legacy),kind,value.get('owner','legacy-system'),STATUS.get(status,'failed'),
                {'legacy_id':legacy,'upload_id':legacy},None,value.get('error'),stamp,stamp))
    print(json.dumps({'records':len(records),'by_type':{kind:sum(r[1]==kind for r in records) for kind in sorted({r[1] for r in records})}},ensure_ascii=False))
    if args.dry_run:return
    import psycopg
    with psycopg.connect(os.environ['RAG_DATABASE_URL']) as db:
        with db.transaction():
            for record in records:insert(db,*record)

if __name__=='__main__':main()
