"""Compatibility facade from /agent/jobs to the unified task service."""
from __future__ import annotations

import json, os, queue, signal, subprocess, tempfile, threading, time
from pathlib import Path

from semi_rag.agent_jobs import AgentJobs, PYTHON, RUNNER


STATUS={'succeeded':'completed','retry_wait':'queued'}


class DistributedAgentJobs:
    def __init__(self,path,service):
        self.service=service
        self.catalog=AgentJobs(path,start_worker=False)
        self.process=None;self.process_task=None;self.lock=threading.RLock()
        service.register('agent.analysis',self._execute)

    def ready(self):return self.catalog.ready()
    def stocks(self,q):return self.catalog.stocks(q)
    def resolve_stock(self,q):return self.catalog.resolve_stock(q)
    def latest_date(self,ticker):return self.catalog.latest_date(ticker)

    @staticmethod
    def public(task):
        if not task:return None
        payload=task['payload'];return {'id':str(task['id']),'ticker':payload['ticker'],
          'trade_date':payload['trade_date'],'analysts':payload['analysts'],
          'status':STATUS.get(task['status'],task['status']),'created_at':str(task['created_at']),
          'updated_at':str(task['updated_at']),'result':task.get('result'),
          'error':(task.get('error') or {}).get('message') if isinstance(task.get('error'),dict) else task.get('error')}

    def submit(self,owner,ticker,trade_date,analysts,idempotency_key=None):
        if not self.ready():raise RuntimeError('金融 Agent 运行依赖尚未就绪')
        counts=self.service.store.counts()
        pending=sum(count for (kind,status),count in counts.items() if kind=='agent.analysis' and status in ('queued','running','retry_wait'))
        if pending>=int(os.getenv('FINANCE_AGENT_MAX_PENDING','20')):raise OverflowError('分析任务已排满，请稍后重试')
        task,_=self.service.submit('agent.analysis',owner,{'ticker':ticker,'trade_date':trade_date,'analysts':analysts},idempotency_key)
        return self.public(task)

    def get(self,owner,job_id):return self.public(self.service.store.get(owner,job_id))
    def list(self,owner):return [self.public(x) for x in self.service.store.list(owner,'agent.analysis',30)]
    def cancel(self,owner,job_id):
        task=self.service.store.cancel(owner,job_id)
        with self.lock:
            if task and self.process_task==job_id and self.process and self.process.poll() is None:
                os.killpg(self.process.pid,signal.SIGTERM)
        return self.public(task)
    def events_since(self,owner,job_id,after=0):return self.service.store.events(owner,job_id,after)
    def retry(self,owner,job_id,idempotency_key=None):return self.public(self.service.retry(owner,job_id,idempotency_key))
    def status_counts(self):
        result={}
        for (kind,status),count in self.service.store.counts().items():
            if kind=='agent.analysis':result[STATUS.get(status,status)]=count
        return result
    def oldest_queued_seconds(self):return self.service.store.oldest_queued_seconds('agent.analysis')
    def close(self):self.catalog.close()

    def _execute(self,payload,emit,cancelled):
        with tempfile.TemporaryDirectory(prefix='finance-agent-') as temporary:
            output=Path(temporary)/'result.json'
            command=[str(PYTHON),str(RUNNER),'--ticker',payload['ticker'],'--date',payload['trade_date'],
                     '--analysts',','.join(payload['analysts']),'--output',str(output),'--event-stream']
            env=os.environ.copy();bypass=[x for x in env.get('NO_PROXY',env.get('no_proxy','')).split(',') if x]
            for host in ('127.0.0.1','localhost','::1'):
                if host not in bypass:bypass.append(host)
            env['NO_PROXY']=env['no_proxy']=','.join(bypass)
            with open(Path(temporary)/'stderr.log','w+',encoding='utf-8') as stderr:
                process=subprocess.Popen(command,cwd=RUNNER.parent,env=env,stdout=subprocess.PIPE,
                    stderr=stderr,text=True,bufsize=1,start_new_session=True)
                with self.lock:self.process,self.process_task=process,'active'
                started=time.monotonic();lines=queue.Queue()
                reader=threading.Thread(target=lambda:[lines.put(line) for line in process.stdout],daemon=True);reader.start()
                try:
                    while reader.is_alive() or not lines.empty():
                        if cancelled():
                            os.killpg(process.pid,signal.SIGTERM);raise RuntimeError('任务已取消')
                        if time.monotonic()-started>int(os.getenv('FINANCE_AGENT_TASK_TIMEOUT','1200')):
                            os.killpg(process.pid,signal.SIGTERM);raise RuntimeError('分析超时，已保留已生成内容')
                        try:line=lines.get(timeout=.5)
                        except queue.Empty:continue
                        try:event=json.loads(line)
                        except json.JSONDecodeError:continue
                        if isinstance(event,dict) and event.get('type') in ('plan','step','text'):emit(event['type'],event)
                    process.wait(timeout=10);stderr.seek(0);detail=stderr.read()[-1500:]
                    if process.returncode or not output.exists():raise RuntimeError(detail or '分析进程未生成结果')
                    return json.loads(output.read_text(encoding='utf-8'))
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid,signal.SIGTERM)
                    with self.lock:self.process,self.process_task=None,None
