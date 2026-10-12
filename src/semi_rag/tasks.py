"""Unified durable task state. PostgreSQL is authoritative; Redis is notification only."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4


TERMINAL = {'succeeded', 'failed', 'cancelled'}


def now():
    return datetime.now(timezone.utc)


def iso(value=None):
    return (value or now()).isoformat(timespec='seconds')


class SqliteTaskStore:
    """Single-instance rollback/test backend with the same state contract."""
    def __init__(self, path):
        self.path, self.lock = Path(path), threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS task_jobs(
                id TEXT PRIMARY KEY,type TEXT,owner_id TEXT,status TEXT,payload TEXT,result TEXT,error TEXT,
                idempotency_key TEXT,priority INTEGER,run_at TEXT,attempt INTEGER,max_attempts INTEGER,
                lease_owner TEXT,lease_expires_at TEXT,cancel_requested_at TEXT,parent_task_id TEXT,
                created_at TEXT,started_at TEXT,finished_at TEXT,updated_at TEXT,
                UNIQUE(owner_id,type,idempotency_key));
              CREATE TABLE IF NOT EXISTS task_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT,task_id TEXT,sequence INTEGER,event_type TEXT,payload TEXT,
                created_at TEXT,UNIQUE(task_id,sequence));
              CREATE INDEX IF NOT EXISTS task_claim ON task_jobs(status,run_at,priority,created_at);
              CREATE INDEX IF NOT EXISTS task_owner ON task_jobs(owner_id,created_at);
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db: yield db
        finally: db.close()

    @staticmethod
    def public(row):
        if not row: return None
        value = dict(row)
        for key in ('payload', 'result', 'error'):
            value[key] = json.loads(value[key]) if value.get(key) else None
        return value

    def submit(self, task_type, owner, payload, idempotency_key=None, run_at=None, max_attempts=3, parent_task_id=None):
        with self.lock, self.db() as db:
            if idempotency_key:
                old = db.execute('SELECT * FROM task_jobs WHERE owner_id=? AND type=? AND idempotency_key=?',
                                 (owner, task_type, idempotency_key)).fetchone()
                if old: return self.public(old), False
            task_id, stamp = uuid4().hex, iso()
            db.execute('INSERT INTO task_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (task_id, task_type, owner, 'queued', json.dumps(payload), None, None, idempotency_key, 0,
                 run_at or stamp, 0, max_attempts, None, None, None, parent_task_id, stamp, None, None, stamp))
            self._event(db, task_id, 'submitted', {'status': 'queued'})
            return self.public(db.execute('SELECT * FROM task_jobs WHERE id=?',(task_id,)).fetchone()), True

    def _event(self, db, task_id, kind, payload):
        sequence = db.execute('SELECT coalesce(max(sequence),0)+1 FROM task_events WHERE task_id=?',(task_id,)).fetchone()[0]
        db.execute('INSERT INTO task_events(task_id,sequence,event_type,payload,created_at) VALUES(?,?,?,?,?)',
                   (task_id, sequence, kind, json.dumps(payload, ensure_ascii=False), iso()))

    def get(self, owner, task_id):
        with self.db() as db:
            return self.public(db.execute('SELECT * FROM task_jobs WHERE id=? AND owner_id=?',(task_id,owner)).fetchone())

    def get_any(self, task_id):
        with self.db() as db: return self.public(db.execute('SELECT * FROM task_jobs WHERE id=?',(task_id,)).fetchone())

    def list(self, owner, task_type=None, limit=50):
        sql, args = 'SELECT * FROM task_jobs WHERE owner_id=?', [owner]
        if task_type: sql, args = sql+' AND type=?', [owner,task_type]
        with self.db() as db:
            rows=db.execute(sql+' ORDER BY created_at DESC LIMIT ?',(*args,limit)).fetchall()
        return [self.public(row) for row in rows]

    def events(self, owner, task_id, after=0):
        if not self.get(owner, task_id): return None
        with self.db() as db:
            rows=db.execute('SELECT * FROM task_events WHERE task_id=? AND id>? ORDER BY id LIMIT 500',(task_id,after)).fetchall()
        return [{'id':r['id'],'sequence':r['sequence'],'type':r['event_type'],'created_at':r['created_at'],
                 **json.loads(r['payload'])} for r in rows]

    def append_event(self, task_id, kind, payload):
        with self.lock, self.db() as db: self._event(db,task_id,kind,payload)

    def claim(self, worker, lease_seconds, accepted=None):
        deadline=iso(now()+timedelta(seconds=lease_seconds)); stamp=iso()
        with self.lock, self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            args=[stamp]; where="status IN ('queued','retry_wait') AND run_at<=?"
            if accepted:
                where += ' AND type IN ('+','.join('?'*len(accepted))+')'; args += list(accepted)
            row=db.execute(f'SELECT * FROM task_jobs WHERE {where} ORDER BY priority DESC,created_at LIMIT 1',args).fetchone()
            if not row:return None
            db.execute("UPDATE task_jobs SET status='running',lease_owner=?,lease_expires_at=?,attempt=attempt+1,started_at=coalesce(started_at,?),updated_at=? WHERE id=?",
                       (worker,deadline,stamp,stamp,row['id']))
            self._event(db,row['id'],'status',{'status':'running','attempt':row['attempt']+1})
            return self.public(db.execute('SELECT * FROM task_jobs WHERE id=?',(row['id'],)).fetchone())

    def heartbeat(self, task_id, worker, lease_seconds):
        with self.db() as db:
            changed=db.execute("UPDATE task_jobs SET lease_expires_at=?,updated_at=? WHERE id=? AND status='running' AND lease_owner=?",
              (iso(now()+timedelta(seconds=lease_seconds)),iso(),task_id,worker)).rowcount
        return changed == 1

    def cancel(self, owner, task_id):
        with self.lock,self.db() as db:
            row=db.execute('SELECT * FROM task_jobs WHERE id=? AND owner_id=?',(task_id,owner)).fetchone()
            if not row:return None
            status=row['status']
            if status in ('queued','retry_wait'):
                status='cancelled'; db.execute("UPDATE task_jobs SET status='cancelled',cancel_requested_at=?,finished_at=?,updated_at=? WHERE id=?",(iso(),iso(),iso(),task_id))
                self._event(db,task_id,'status',{'status':'cancelled'})
            elif status=='running': db.execute('UPDATE task_jobs SET cancel_requested_at=?,updated_at=? WHERE id=?',(iso(),iso(),task_id))
            return self.public(db.execute('SELECT * FROM task_jobs WHERE id=?',(task_id,)).fetchone())

    def cancelled(self, task_id):
        row=self.get_any(task_id); return not row or row['cancel_requested_at'] is not None

    def finish(self, task_id, worker, result):
        return self._terminal(task_id,worker,'succeeded',result,None)

    def fail(self, task_id, worker, error):
        with self.lock,self.db() as db:
            row=db.execute("SELECT * FROM task_jobs WHERE id=? AND status='running' AND lease_owner=?",(task_id,worker)).fetchone()
            if not row:return False
            if row['cancel_requested_at']: status,run_at='cancelled',row['run_at']
            elif row['attempt'] < row['max_attempts']: status,run_at='retry_wait',iso(now()+timedelta(seconds=min(300,2**row['attempt'])))
            else: status,run_at='failed',row['run_at']
            db.execute('UPDATE task_jobs SET status=?,error=?,run_at=?,lease_owner=NULL,lease_expires_at=NULL,finished_at=?,updated_at=? WHERE id=?',
              (status,json.dumps({'message':str(error)[:2000]}),run_at,iso() if status in TERMINAL else None,iso(),task_id))
            self._event(db,task_id,'status',{'status':status,'error':str(error)[:2000]})
            return True

    def _terminal(self,task_id,worker,status,result,error):
        with self.lock,self.db() as db:
            row=db.execute("SELECT cancel_requested_at FROM task_jobs WHERE id=? AND status='running' AND lease_owner=?",(task_id,worker)).fetchone()
            if not row:return False
            if row['cancel_requested_at']: status,result='cancelled',None
            db.execute('UPDATE task_jobs SET status=?,result=?,error=?,lease_owner=NULL,lease_expires_at=NULL,finished_at=?,updated_at=? WHERE id=?',
              (status,json.dumps(result,ensure_ascii=False) if result is not None else None,error,iso(),iso(),task_id))
            self._event(db,task_id,'status',{'status':status})
            return True

    def retry(self, owner, task_id, idempotency_key=None):
        old=self.get(owner,task_id)
        if not old:return None
        if old['status'] not in ('failed','cancelled'): raise ValueError('Only failed or cancelled tasks can be retried')
        return self.submit(old['type'],owner,old['payload'],idempotency_key,max_attempts=old['max_attempts'],parent_task_id=task_id)[0]

    def recover_expired(self):
        with self.lock,self.db() as db:
            rows=db.execute("SELECT id FROM task_jobs WHERE status='running' AND lease_expires_at<?",(iso(),)).fetchall()
            for row in rows:
                db.execute("UPDATE task_jobs SET status='retry_wait',lease_owner=NULL,lease_expires_at=NULL,run_at=?,updated_at=? WHERE id=?",(iso(),iso(),row['id']))
                self._event(db,row['id'],'lease_expired',{'status':'retry_wait'})
        return len(rows)

    def counts(self):
        with self.db() as db: rows=db.execute('SELECT type,status,count(*) n FROM task_jobs GROUP BY type,status').fetchall()
        return {(r['type'],r['status']):r['n'] for r in rows}

    def oldest_queued_seconds(self,task_type):
        with self.db() as db:row=db.execute("SELECT created_at FROM task_jobs WHERE type=? AND status IN ('queued','retry_wait') ORDER BY created_at LIMIT 1",(task_type,)).fetchone()
        return max(0.0,(now()-datetime.fromisoformat(row['created_at'])).total_seconds()) if row else 0.0


class PostgresTaskStore:
    def __init__(self, dsn):
        try: import psycopg
        except ImportError as exc: raise RuntimeError('Install psycopg[binary] for PostgreSQL tasks') from exc
        self.psycopg,self.dsn=psycopg,dsn

    @contextmanager
    def db(self):
        from psycopg.rows import dict_row
        with self.psycopg.connect(self.dsn,row_factory=dict_row) as db:
            with db.transaction(): yield db

    def submit(self,task_type,owner,payload,idempotency_key=None,run_at=None,max_attempts=3,parent_task_id=None):
        task_id=str(uuid4())
        with self.db() as db:
            if idempotency_key:
                old=db.execute('SELECT * FROM task_jobs WHERE owner_id=%s AND type=%s AND idempotency_key=%s',(owner,task_type,idempotency_key)).fetchone()
                if old:return old,False
            row=db.execute('''INSERT INTO task_jobs(id,type,owner_id,status,payload,idempotency_key,run_at,max_attempts,parent_task_id)
              VALUES(%s,%s,%s,'queued',%s,%s,coalesce(%s,now()),%s,%s) RETURNING *''',
              (task_id,task_type,owner,json.dumps(payload),idempotency_key,run_at,max_attempts,parent_task_id)).fetchone()
            db.execute("INSERT INTO task_events(task_id,sequence,event_type,payload) VALUES(%s,1,'submitted',%s)",(task_id,json.dumps({'status':'queued'})))
            return row,True

    def get(self,owner,task_id):
        with self.db() as db:return db.execute('SELECT * FROM task_jobs WHERE id=%s AND owner_id=%s',(task_id,owner)).fetchone()
    def get_any(self,task_id):
        with self.db() as db:return db.execute('SELECT * FROM task_jobs WHERE id=%s',(task_id,)).fetchone()
    def list(self,owner,task_type=None,limit=50):
        query='SELECT * FROM task_jobs WHERE owner_id=%s'
        params=[owner]
        if task_type is not None:
            query+=' AND type=%s'
            params.append(task_type)
        query+=' ORDER BY created_at DESC LIMIT %s'
        params.append(limit)
        with self.db() as db:return db.execute(query,params).fetchall()
    def events(self,owner,task_id,after=0):
        if not self.get(owner,task_id):return None
        with self.db() as db: rows=db.execute('SELECT * FROM task_events WHERE task_id=%s AND id>%s ORDER BY id LIMIT 500',(task_id,after)).fetchall()
        return [{'id':r['id'],'sequence':r['sequence'],'type':r['event_type'],'created_at':r['created_at'].isoformat(),**r['payload']} for r in rows]
    def append_event(self,task_id,kind,payload):
        with self.db() as db:
            db.execute('SELECT 1 FROM task_jobs WHERE id=%s FOR UPDATE',(task_id,))
            db.execute('''INSERT INTO task_events(task_id,sequence,event_type,payload)
              SELECT %s,coalesce(max(sequence),0)+1,%s,%s FROM task_events WHERE task_id=%s''',(task_id,kind,json.dumps(payload),task_id))
    def claim(self,worker,lease_seconds,accepted=None):
        with self.db() as db:
            row=db.execute('''SELECT * FROM task_jobs WHERE status IN ('queued','retry_wait') AND run_at<=now()
              AND (%s::text[] IS NULL OR type=ANY(%s)) ORDER BY priority DESC,created_at FOR UPDATE SKIP LOCKED LIMIT 1''',(accepted,accepted)).fetchone()
            if not row:return None
            row=db.execute("UPDATE task_jobs SET status='running',lease_owner=%s,lease_expires_at=now()+(%s||' seconds')::interval,attempt=attempt+1,started_at=coalesce(started_at,now()),updated_at=now() WHERE id=%s RETURNING *",(worker,lease_seconds,row['id'])).fetchone()
            db.execute('''INSERT INTO task_events(task_id,sequence,event_type,payload)
              SELECT %s,coalesce(max(sequence),0)+1,'status',%s FROM task_events WHERE task_id=%s''',
              (row['id'],json.dumps({'status':'running','attempt':row['attempt']}),row['id']))
            return row
    def heartbeat(self,task_id,worker,lease_seconds):
        with self.db() as db:return db.execute("UPDATE task_jobs SET lease_expires_at=now()+(%s||' seconds')::interval,updated_at=now() WHERE id=%s AND status='running' AND lease_owner=%s",(lease_seconds,task_id,worker)).rowcount==1
    def cancel(self,owner,task_id):
        with self.db() as db:
            row=db.execute('SELECT * FROM task_jobs WHERE id=%s AND owner_id=%s FOR UPDATE',(task_id,owner)).fetchone()
            if not row:return None
            status='cancelled' if row['status'] in ('queued','retry_wait') else row['status']
            updated=db.execute('UPDATE task_jobs SET status=%s,cancel_requested_at=now(),finished_at=CASE WHEN %s=%s THEN now() ELSE finished_at END,updated_at=now() WHERE id=%s RETURNING *',(status,status,'cancelled',task_id)).fetchone()
            db.execute('''INSERT INTO task_events(task_id,sequence,event_type,payload)
              SELECT %s,coalesce(max(sequence),0)+1,'cancel_requested',%s FROM task_events WHERE task_id=%s''',
              (task_id,json.dumps({'status':status}),task_id))
            return updated
    def cancelled(self,task_id):
        row=self.get_any(task_id);return not row or row['cancel_requested_at'] is not None
    def finish(self,task_id,worker,result):
        with self.db() as db:
            row=db.execute("UPDATE task_jobs SET status=CASE WHEN cancel_requested_at IS NULL THEN 'succeeded' ELSE 'cancelled' END,result=%s,lease_owner=NULL,lease_expires_at=NULL,finished_at=now(),updated_at=now() WHERE id=%s AND status='running' AND lease_owner=%s RETURNING status",(json.dumps(result),task_id,worker)).fetchone()
            if row:db.execute('''INSERT INTO task_events(task_id,sequence,event_type,payload) SELECT %s,coalesce(max(sequence),0)+1,'status',%s FROM task_events WHERE task_id=%s''',(task_id,json.dumps({'status':row['status']}),task_id))
            return bool(row)
    def fail(self,task_id,worker,error):
        with self.db() as db:
            row=db.execute("UPDATE task_jobs SET status=CASE WHEN cancel_requested_at IS NOT NULL THEN 'cancelled' WHEN attempt<max_attempts THEN 'retry_wait' ELSE 'failed' END,error=%s,run_at=CASE WHEN attempt<max_attempts THEN now()+make_interval(secs=>least(300,power(2,attempt)::int)) ELSE run_at END,lease_owner=NULL,lease_expires_at=NULL,updated_at=now() WHERE id=%s AND status='running' AND lease_owner=%s RETURNING status",(json.dumps({'message':str(error)[:2000]}),task_id,worker)).fetchone()
            if row:db.execute('''INSERT INTO task_events(task_id,sequence,event_type,payload) SELECT %s,coalesce(max(sequence),0)+1,'status',%s FROM task_events WHERE task_id=%s''',(task_id,json.dumps({'status':row['status'],'error':str(error)[:2000]}),task_id))
            return bool(row)
    def retry(self,owner,task_id,idempotency_key=None):
        old=self.get(owner,task_id)
        if not old:return None
        if old['status'] not in ('failed','cancelled'):raise ValueError('Only failed or cancelled tasks can be retried')
        return self.submit(old['type'],owner,old['payload'],idempotency_key,max_attempts=old['max_attempts'],parent_task_id=task_id)[0]
    def recover_expired(self):
        with self.db() as db:return db.execute("UPDATE task_jobs SET status='retry_wait',lease_owner=NULL,lease_expires_at=NULL,run_at=now(),updated_at=now() WHERE status='running' AND lease_expires_at<now()").rowcount
    def counts(self):
        with self.db() as db:rows=db.execute('SELECT type,status,count(*) n FROM task_jobs GROUP BY type,status').fetchall()
        return {(r['type'],r['status']):r['n'] for r in rows}
    def oldest_queued_seconds(self,task_type):
        with self.db() as db:row=db.execute("SELECT extract(epoch FROM now()-min(created_at)) age FROM task_jobs WHERE type=%s AND status IN ('queued','retry_wait')",(task_type,)).fetchone()
        return float(row['age'] or 0)


class RedisNotifier:
    def __init__(self,url,stream='rag:tasks'):
        try: import redis
        except ImportError as exc: raise RuntimeError('Install redis for distributed tasks') from exc
        self.client,self.stream=redis.Redis.from_url(url,decode_responses=True),stream
        self.group=os.getenv('RAG_REDIS_GROUP','rag-workers')
        self.consumer=os.getenv('RAG_WORKER_ID',f'worker-{os.getpid()}-{uuid4().hex[:8]}')
        try:self.client.xgroup_create(self.stream,self.group,id='0',mkstream=True)
        except redis.ResponseError as exc:
            if 'BUSYGROUP' not in str(exc):raise
    def notify(self,task_id,task_type): self.client.xadd(self.stream,{'task_id':task_id,'type':task_type},maxlen=100000,approximate=True)
    def wait(self,timeout_ms=1000):
        rows=self.client.xreadgroup(self.group,self.consumer,{self.stream:'>'},count=10,block=timeout_ms)
        return [(message_id,fields) for _,messages in rows for message_id,fields in messages]
    def ack(self,message_id):self.client.xack(self.stream,self.group,message_id)


class TaskService:
    def __init__(self,config):
        backend=os.getenv('RAG_TASK_BACKEND','sqlite')
        self.distributed=backend=='postgres'
        if self.distributed:
            self.store=PostgresTaskStore(os.environ['RAG_DATABASE_URL'])
            self.notifier=RedisNotifier(os.environ['RAG_REDIS_URL'],os.getenv('RAG_REDIS_STREAM','rag:tasks'))
        else:
            self.store=SqliteTaskStore(Path(config['data_dir'])/'tasks.sqlite3');self.notifier=None
        self.handlers={};self.closed=threading.Event();self.worker=None

    def submit(self,task_type,owner,payload,idempotency_key=None,**kwargs):
        task,created=self.store.submit(task_type,owner,payload,idempotency_key,**kwargs)
        if created and self.notifier:self.notifier.notify(str(task['id']),task_type)
        return task,created

    def retry(self,owner,task_id,idempotency_key=None):
        task=self.store.retry(owner,task_id,idempotency_key)
        if task and self.notifier:self.notifier.notify(str(task['id']),task['type'])
        return task

    def register(self,task_type,handler):
        self.handlers[task_type]=handler

    def start(self):
        if self.worker or not self.handlers:return
        self.worker=threading.Thread(target=self._work,name='unified-tasks',daemon=True);self.worker.start()

    def close(self):
        self.closed.set()
        if self.worker:self.worker.join(timeout=5)

    def _work(self):
        worker=os.getenv('RAG_WORKER_ID',f'worker-{os.getpid()}-{uuid4().hex[:8]}')
        lease=int(os.getenv('RAG_TASK_LEASE_SECONDS','60'))
        while not self.closed.is_set():
            messages=[]
            if self.notifier:
                try:messages=self.notifier.wait(1000)
                except Exception:time.sleep(1)
            else:self.closed.wait(.25)
            try:self.store.recover_expired()
            except Exception:continue
            while not self.closed.is_set():
                task=self.store.claim(worker,lease,list(self.handlers))
                if not task:break
                stop=threading.Event()
                heartbeat=threading.Thread(target=self._heartbeat,args=(task['id'],worker,lease,stop),daemon=True)
                heartbeat.start()
                try:
                    emit=lambda kind,payload:self.store.append_event(task['id'],kind,payload)
                    result=self.handlers[task['type']](task['payload'],emit,lambda:self.store.cancelled(task['id']))
                    self.store.finish(task['id'],worker,result)
                except Exception as exc:
                    self.store.fail(task['id'],worker,exc)
                finally:
                    stop.set();heartbeat.join(timeout=1)
            for message_id,_ in messages:
                try:self.notifier.ack(message_id)
                except Exception:pass

    def _heartbeat(self,task_id,worker,lease,stop):
        while not stop.wait(max(1,lease/3)):
            if not self.store.heartbeat(task_id,worker,lease):return
