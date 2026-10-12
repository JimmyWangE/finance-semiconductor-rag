import argparse
import json
import uuid
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:8091')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    results = []
    with httpx.Client(base_url=args.url, trust_env=False, timeout=180) as client:
        response = client.post('/conversations', json={})
        response.raise_for_status()
        session = response.json()
        def ask(query, filters=None):
            body = {'query': query, 'conversation_id': session['id'], 'expected_revision': session['revision'],
                    'request_id': str(uuid.uuid4())}
            if filters is not None:
                body['filters'] = filters
            response = client.post('/ask', json=body)
            response.raise_for_status()
            result = response.json()
            session['revision'] = result['conversation_revision']
            results.append(result)
            print(json.dumps({key: result.get(key) for key in ['query', 'resolved_query', 'status', 'answer', 'latency_ms']}, ensure_ascii=False), flush=True)
            assert client.post('/ask', json=body).json() == result
            return result
        first = ask('中芯国际2025年第三季度产能利用率是多少？')
        assert first['answerable'] and '95.8' in first['answer']
        follow = ask('它同期的营业收入是多少亿美元？')
        assert '中芯国际' in follow['resolved_query'] and '2025' in follow['resolved_query']
        assert follow['answerable'] and '23.82' in follow['answer']
        switched = ask('那华虹呢？')
        assert '华虹' in switched['resolved_query'] and '2025' in switched['resolved_query']
        assert switched['answerable'] and '6.35' in switched['answer']
        changed = ask('解释光刻胶在芯片制造中的作用。')
        assert '光刻胶' in changed['resolved_query'] and '华虹' not in changed['resolved_query']
        assert '2025' not in changed['resolved_query']
        saved = client.get('/conversations/'+session['id']).json()
        assert len(saved['turns']) == 4
        with httpx.Client(base_url=args.url, trust_env=False, timeout=30) as stranger:
            assert stranger.get('/conversations/'+session['id']).status_code == 404
        session = client.post('/conversations', json={}).json()
        ambiguous = ask('它的营收是多少？')
        assert ambiguous['status'] == 'needs_clarification' and not ambiguous['sources']
        clarified = ask('我指中芯国际，2025年第三季度，以亿美元为单位。')
        assert clarified['answerable'] and '23.82' in clarified['answer']
        limited = ask('那华虹呢？', {'as_of': '2020-01-01'})
        assert not limited['answerable'] and not limited['sources']
        assert limited['filters']['as_of'] == '2020-01-01'
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
