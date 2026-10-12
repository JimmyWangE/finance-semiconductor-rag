import threading
import time

from semi_rag.tasks import SqliteTaskStore, TaskService


def test_task_idempotency_owner_events_and_retry(tmp_path):
    store=SqliteTaskStore(tmp_path/'tasks.sqlite3')
    first,created=store.submit('article.sync','u1',{'source_id':'x'},'same',max_attempts=1)
    second,created_again=store.submit('article.sync','u1',{'source_id':'changed'},'same',max_attempts=1)
    assert created and not created_again and second['id']==first['id']
    assert store.get('u2',first['id']) is None
    claimed=store.claim('w1',30,['article.sync'])
    assert claimed['attempt']==1 and store.claim('w2',30,['article.sync']) is None
    store.append_event(first['id'],'progress',{'value':1})
    assert [e['sequence'] for e in store.events('u1',first['id'])]==[1,2,3]
    assert store.fail(first['id'],'w1','broken')
    assert store.get('u1',first['id'])['status']=='failed'
    retried=store.retry('u1',first['id'],'retry-1')
    assert retried['parent_task_id']==first['id']


def test_cancel_and_expired_lease(tmp_path):
    store=SqliteTaskStore(tmp_path/'tasks.sqlite3')
    queued,_=store.submit('agent.analysis','u1',{})
    assert store.cancel('u1',queued['id'])['status']=='cancelled'
    running,_=store.submit('agent.analysis','u1',{})
    store.claim('worker',-1,['agent.analysis'])
    assert store.recover_expired()==1
    assert store.get('u1',running['id'])['status']=='retry_wait'


def test_task_service_worker(tmp_path,monkeypatch):
    monkeypatch.setenv('RAG_TASK_BACKEND','sqlite')
    service=TaskService({'data_dir':str(tmp_path)})
    calls=[]
    def handler(payload,emit,cancelled):
        calls.append(payload['value']);emit('progress',{'value':payload['value']});return {'ok':True}
    service.register('test.run',handler);service.start()
    task,_=service.submit('test.run','u1',{'value':7},'key')
    for _ in range(100):
        current=service.store.get('u1',task['id'])
        if current['status']=='succeeded':break
        time.sleep(.02)
    service.close()
    assert current['result']=={'ok':True} and calls==[7]


def test_two_workers_claim_only_once(tmp_path):
    store=SqliteTaskStore(tmp_path/'tasks.sqlite3');store.submit('x','u',{})
    barrier=threading.Barrier(3);results=[]
    def claim(name):barrier.wait();results.append(store.claim(name,30,['x']))
    threads=[threading.Thread(target=claim,args=(str(i),)) for i in range(2)]
    [x.start() for x in threads];barrier.wait();[x.join() for x in threads]
    assert sum(row is not None for row in results)==1
