import json
import sqlite3
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from semi_rag.api import create_app
from semi_rag.conversations import ConversationStore, ConversationConflict
from semi_rag.general import answer_general, stream_general
from semi_rag.understanding import answer_turn, understand
from test_rag import FakeAnswerer, FakeRetriever


def test_history_persistence_isolation_revision_and_replay(tmp_path):
    config = {'data_dir': str(tmp_path)}
    store = ConversationStore(config)
    session = store.create('browser-a')
    cid = session['id']
    request = {'query': '中芯国际营收', 'filters': {'org': '东海证券'}}
    result = store.append('browser-a', cid, 0, 'request-1', request, {'answer': '有来源的回答', 'sources': []}, request['filters'])
    assert result['turn_number'] == 1 and result['conversation_revision'] == 1
    reloaded = ConversationStore(config)
    assert reloaded.history('browser-a', cid)['turns'][0]['response'] == result
    assert reloaded.replay('browser-a', cid, 'request-1', request) == result
    with pytest.raises(ConversationConflict):
        reloaded.replay('browser-a', cid, 'request-1', {'query': 'different'})
    with pytest.raises(ConversationConflict):
        reloaded.append('browser-a', cid, 0, 'request-2', request, {}, {})
    assert reloaded.list('browser-b')['total'] == 0
    with pytest.raises(KeyError):
        reloaded.history('browser-b', cid)
    renamed = store.change('browser-a', cid, 1, title='晶圆代工')
    assert renamed['revision'] == 2
    store.change('browser-a', cid, 2, delete=True)
    with pytest.raises(KeyError):
        store.get('browser-a', cid)
    assert store.list('browser-a')['total'] == 0


def test_conversation_modes_are_isolated_and_old_database_is_migrated(tmp_path):
    root = tmp_path / 'rag/conversations'
    root.mkdir(parents=True)
    database = root / 'history.sqlite3'
    with sqlite3.connect(database) as conn:
        conn.execute('''CREATE TABLE conversations (
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, title TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 0, filters TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0
        )''')
        conn.execute('INSERT INTO conversations VALUES (?,?,?,?,?,?,?,?)',
                     ('legacy', 'owner', '旧研究会话', 0, '{}', 'now', 'now', 0))
    store = ConversationStore({'data_dir': str(tmp_path)})
    assert store.get('owner', 'legacy')['mode'] == 'rag'
    general = store.create('owner', mode='general')
    assert store.list('owner')['total'] == 1
    assert store.list('owner', mode='general')['conversations'][0]['id'] == general['id']
    with pytest.raises(KeyError):
        store.get('owner', general['id'], mode='rag')
    with pytest.raises(KeyError):
        store.get('owner', 'legacy', mode='general')


def test_general_answer_uses_bounded_history_without_retrieval():
    requests = []

    class GeneralAnswerer:
        config = {'llm_model': 'test-sft', 'generation_max_tokens': 500,
                  'general_history_turns': 2, 'general_context_tokens': 5000}

        def __init__(self):
            def handle(request):
                requests.append(json.loads(request.content))
                return httpx.Response(200, json={'choices': [{'finish_reason': 'stop',
                    'message': {'content': '这是通用回答。'}}]})
            self.client = httpx.Client(base_url='http://local/v1/', transport=httpx.MockTransport(handle))

        def token_count(self, messages):
            return len(json.dumps(messages, ensure_ascii=False))

    answerer = GeneralAnswerer()
    history = [{'query': f'问题{i}', 'response': {'answer': f'回答{i}',
                'sources': [{'text': '不应进入通用提示'}]}} for i in range(3)]
    result = answer_general(answerer, '继续说明', history)
    messages = requests[0]['messages']
    assert result['mode'] == 'general' and result['history_turns_used'] == 2
    assert result['answer'] == '这是通用回答。' and result['sources'] == []
    assert '问题0' not in str(messages) and '问题1' in str(messages)
    assert '不应进入通用提示' not in str(messages)
    assert requests[0]['chat_template_kwargs'] == {'enable_thinking': False}
    answerer.client.close()


def test_general_stream_forwards_deltas_and_returns_complete_answer():
    class StreamingAnswerer:
        config = {'llm_model': 'test-sft', 'generation_max_tokens': 500,
                  'general_history_turns': 2, 'general_context_tokens': 5000}

        def __init__(self):
            chunks = [
                {'choices': [{'delta': {'content': '逐字'}, 'finish_reason': None}]},
                {'choices': [{'delta': {'content': '输出'}, 'finish_reason': 'stop'}]},
            ]
            body = ''.join('data: ' + json.dumps(chunk, ensure_ascii=False) + '\n\n' for chunk in chunks)
            body += 'data: [DONE]\n\n'
            self.client = httpx.Client(base_url='http://local/v1/', transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text=body, headers={'content-type': 'text/event-stream'})))

        def token_count(self, messages):
            return len(json.dumps(messages, ensure_ascii=False))

    answerer = StreamingAnswerer()
    completion = stream_general(answerer, '测试流式输出', [])
    deltas = []
    while True:
        try:
            deltas.append(next(completion))
        except StopIteration as stopped:
            result = stopped.value
            break
    answerer.client.close()
    assert deltas == ['逐字', '输出']
    assert result['answer'] == '逐字输出' and result['status'] == 'answered'


class UnderstandingAnswerer:
    config = {'llm_model': 'test', 'conversation_history_turns': 2, 'conversation_history_tokens': 5000}
    retriever = SimpleNamespace(manifest={'generation': 'test'})

    def __init__(self, payload):
        self.payload = payload
        self.seen = []
        self.searches = []
        def handle(request):
            self.seen.append(json.loads(request.content))
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(self.payload)}}]})
        self.client = httpx.Client(base_url='http://local/v1/', transport=httpx.MockTransport(handle))

    def token_count(self, messages):
        return len(json.dumps(messages, ensure_ascii=False))

    def ask(self, query, filters, top_k):
        self.searches.append((query, filters, top_k))
        return {'answer': '新检索的回答', 'sources': [], 'answerable': True, 'filters': filters}


def rewrite(**changes):
    return {'standalone_query': '中芯国际2025年第三季度营收是多少？', 'intent': 'fact',
            'needs_clarification': False, 'clarification_question': '', 'focus_doc_ids': [], **changes}


def turn(query='中芯国际2025年第三季度产能利用率是多少？'):
    return {'query': query, 'response': {'answer': '历史数值不得当作新证据', 'status': 'answered',
            'resolved_query': query, 'sources': [{'doc_id': 'doc-1', 'title': '研报'}]}}


def test_understanding_bounded_history_retrieval_and_filters():
    answerer = UnderstandingAnswerer(rewrite(focus_doc_ids=['doc-1']))
    result = answer_turn(answerer, '这份报告里的营收呢？', [turn()] * 5,
                         {'as_of': '2025-12-31', 'org': '东海证券', 'doc_ids': ['doc-2']}, 6)
    assert answerer.searches == [('中芯国际2025年第三季度营业收入是多少？',
                                 {'as_of': '2025-12-31', 'org': '东海证券', 'doc_ids': []}, 6)]
    assert result['query'] == '这份报告里的营收呢？'
    assert result['understanding']['history_turns_used'] == 2
    sent = json.loads(answerer.seen[0]['messages'][1]['content'])
    assert len(sent['history']) == 2
    assert '历史数值' not in answerer.searches[0][0]
    answerer.client.close()


def test_clarification_never_searches_and_unknown_source_is_rejected():
    answerer = UnderstandingAnswerer(rewrite(needs_clarification=True, clarification_question='您指哪家公司？'))
    result = answer_turn(answerer, '它呢？', [], {}, 6)
    assert result['status'] == 'needs_clarification' and not result['answerable']
    assert not answerer.searches and not result['sources']
    answerer.payload = rewrite(focus_doc_ids=['invented-id'])
    with pytest.raises(ValueError):
        understand(answerer, '报告呢', [turn()], {})
    answerer.client.close()


def test_history_budget_removes_oldest_and_limits_turns(tmp_path):
    answerer = UnderstandingAnswerer(rewrite())
    answerer.config = {**answerer.config, 'conversation_history_turns': 8, 'conversation_history_tokens': 2400}
    result = understand(answerer, '中芯国际营收', [turn('较早问题' + '长' * 1000), turn()], {})
    assert result['history_turns_used'] < 2
    answerer.client.close()
    store = ConversationStore({'data_dir': str(tmp_path), 'conversation_max_turns': 1})
    cid = store.create('owner')['id']
    store.append('owner', cid, 0, '1', {'query': '问题'}, {}, {})
    with pytest.raises(ConversationConflict, match='轮数上限'):
        store.append('owner', cid, 1, '2', {'query': '追问'}, {}, {})


def test_dollar_query_retries_wrong_currency_answer():
    from semi_rag.answer import Answerer
    answerer = Answerer.__new__(Answerer)
    answerer.config = {'min_rerank_score': .1, 'context_tokens': 9000,
                       'generation_max_tokens': 1000, 'llm_model': 'test'}
    source = {'title': '2025年第三季度报告', 'publish_date': '2025-12-05',
        'doc_id': 'doc', 'chunk_id': 'chunk', 'page': 1, 'kind': 'text', 'org': '券商',
        'text': '公司营业收入为171.62亿元，美元口径营收为23.82亿美元。', 'rerank_score': .9}
    answerer.retriever = SimpleNamespace(search=lambda *args: {'hits': [source],
        'filters': {'as_of': '2026-09-07'}, 'generation': 'test', 'latency_ms': 1},
        adjacent_context=lambda hit: [])
    answerer.token_count = lambda _: 100
    outputs = iter(['营收为171.62亿元。', '营收为23.82亿美元。'])
    calls = []
    def handle(request):
        calls.append(json.loads(request.content))
        payload = {'answerable': True, 'claims': [{'text': next(outputs), 'citations': [
            {'source_id': 'S1', 'quote': source['text']}]}]}
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(payload)}}]})
    answerer.client = httpx.Client(base_url='http://local/', transport=httpx.MockTransport(handle))
    result = answerer.ask('营收是多少亿美元？')
    assert len(calls) == 2 and '23.82亿美元' in result['answer']
    answerer.close()


def test_conversation_api_inherits_filters_replays_and_isolates(tmp_path, monkeypatch):
    calls = []
    def reply(answerer, query, history, filters, top_k):
        calls.append((query, len(history), filters))
        if query == 'trigger timeout':
            raise httpx.ReadTimeout('timeout')
        return {'query': query, 'resolved_query': '中芯国际的营收', 'answer': '回答', 'sources': [], 'filters': filters}
    monkeypatch.setattr('semi_rag.api.answer_turn', reply)
    app = create_app({'data_dir': str(tmp_path), 'pdf_dir': str(tmp_path)}, FakeRetriever(), FakeAnswerer())
    with TestClient(app) as client:
        session = client.post('/conversations', json={}).json()
        cid = session['id']
        body = {'query': '中芯国际', 'conversation_id': cid, 'expected_revision': 0,
                'request_id': str(uuid.uuid4()), 'filters': {'as_of': '2025-12-31', 'org': '东海证券'}}
        response = client.post('/ask', json=body)
        assert response.status_code == 200
        assert client.post('/ask', json=body).json() == response.json()
        assert len(calls) == 1
        body = {**body, 'query': '营收呢', 'expected_revision': 1, 'request_id': str(uuid.uuid4())}
        body.pop('filters')
        assert client.post('/ask', json=body).status_code == 200
        assert calls[-1][1:] == (1, {'as_of': '2025-12-31', 'org': '东海证券'})
        stale = {**body, 'request_id': str(uuid.uuid4())}
        assert client.post('/ask', json=stale).status_code == 409
        failed = {**body, 'query': 'trigger timeout', 'expected_revision': 2, 'request_id': str(uuid.uuid4())}
        assert client.post('/ask', json=failed).status_code == 504
        assert client.get('/conversations/'+cid).json()['revision'] == 2
        cleared = {**body, 'expected_revision': 2, 'request_id': str(uuid.uuid4()), 'filters': {}}
        assert client.post('/ask', json=cleared).status_code == 200 and calls[-1][2] == {}
        owner_cookie = client.cookies.get('rag_client')
        client.cookies.clear()
        assert client.get('/conversations/'+cid).status_code == 404
        assert client.get('/conversations').json()['total'] == 0
        client.cookies.set('rag_client', owner_cookie)
        assert len(client.get('/conversations/'+cid).json()['turns']) == 3
        assert client.delete('/conversations/'+cid+'?expected_revision=3').status_code == 200
        assert client.get('/conversations/'+cid).status_code == 404


def test_general_api_replays_and_cannot_cross_conversation_modes(tmp_path, monkeypatch):
    calls = []

    def reply(answerer, query, history):
        calls.append((query, len(history)))
        return {'query': query, 'answer': '通用回答', 'answerable': True,
                'status': 'answered', 'mode': 'general', 'sources': [], 'claims': [], 'latency_ms': 12.0}

    monkeypatch.setattr('semi_rag.api.answer_general', reply)
    app = create_app({'data_dir': str(tmp_path), 'pdf_dir': str(tmp_path)}, FakeRetriever(), FakeAnswerer())
    with TestClient(app) as client:
        assert client.get('/chat').status_code == 200
        assert client.get('/general.js').status_code == 200
        rag = client.post('/conversations', json={}).json()
        general = client.post('/general/conversations', json={}).json()
        assert rag['mode'] == 'rag' and general['mode'] == 'general'
        assert client.get('/conversations').json()['total'] == 1
        assert client.get('/general/conversations').json()['total'] == 1
        assert client.get('/conversations/' + general['id']).status_code == 404
        assert client.get('/general/conversations/' + rag['id']).status_code == 404

        body = {'query': '解释注意力机制', 'conversation_id': general['id'],
                'expected_revision': 0, 'request_id': str(uuid.uuid4())}
        response = client.post('/general/ask', json=body)
        assert response.status_code == 200 and response.json()['answer'] == '通用回答'
        assert client.post('/general/ask', json=body).json() == response.json()
        assert calls == [('解释注意力机制', 0)]
        assert client.post('/ask', json={**body, 'top_k': 6}).status_code == 404
        assert client.post('/general/ask', json={**body, 'conversation_id': rag['id'],
            'request_id': str(uuid.uuid4())}).status_code == 404


def test_general_stream_api_commits_only_complete_response_and_replays(tmp_path, monkeypatch):
    calls = []

    def reply(answerer, query, history):
        calls.append((query, len(history)))
        yield '流式'
        yield '回答'
        return {'query': query, 'answer': '流式回答', 'answerable': True,
                'status': 'answered', 'mode': 'general', 'sources': [], 'claims': [], 'latency_ms': 8.0}

    monkeypatch.setattr('semi_rag.api.stream_general', reply)
    app = create_app({'data_dir': str(tmp_path), 'pdf_dir': str(tmp_path)}, FakeRetriever(), FakeAnswerer())
    with TestClient(app) as client:
        conversation = client.post('/general/conversations', json={}).json()
        body = {'query': '测试', 'conversation_id': conversation['id'], 'expected_revision': 0,
                'request_id': str(uuid.uuid4())}
        events = [json.loads(line) for line in client.post('/general/ask/stream', json=body).iter_lines()]
        assert [event['type'] for event in events] == ['delta', 'delta', 'done']
        assert ''.join(event['text'] for event in events[:-1]) == '流式回答'
        assert events[-1]['response']['conversation_revision'] == 1
        assert client.get('/general/conversations/' + conversation['id']).json()['turns'][0][
            'response']['answer'] == '流式回答'
        replay = [json.loads(line) for line in client.post('/general/ask/stream', json=body).iter_lines()]
        assert [event['type'] for event in replay] == ['done']
        assert calls == [('测试', 0)]
