"""Exercise the running RAG against real indexed evidence and model output."""
import argparse
import json
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:8090')
    parser.add_argument('--output', type=Path, default=Path('data/rag/eval/smoke_report.json'))
    args = parser.parse_args()
    results = []
    with httpx.Client(base_url=args.url, trust_env=False, timeout=200) as client:
        client.get('/health').raise_for_status()
        cases = [
            ('numeric', {'query': '中芯国际2025年第三季度产能利用率是多少？'}, True),
            ('unknown_future', {'query': '2035年中芯国际实际净利润是多少？'}, False),
            ('empty_history', {'query': '中芯国际产能利用率是多少？', 'filters': {'as_of': '2020-01-01'}}, False),
            ('risk', {'query': '东海证券对半导体行业提示了哪些风险？', 'filters': {'org': '东海证券'}}, True),
        ]
        for name, body, expected in cases:
            response = client.post('/ask', json=body)
            response.raise_for_status()
            result = response.json()
            assert result['answerable'] is expected, (name, result)
            assert not result['validation_errors'], (name, result['validation_errors'])
            if name == 'numeric':
                assert '95.8' in result['answer']
                assert 100 < result['input_tokens'] <= 9000
            for source in result['sources']:
                assert source['publish_date'] <= result['filters']['as_of']
                pdf = client.get(source['pdf_url'])
                pdf.raise_for_status()
                assert pdf.content.startswith(b'%PDF')
            results.append({'case': name, **result})
            print(name, result['status'], result['latency_ms'], flush=True)
        response = client.post('/search', json={'query': 'DRAM价格变化', 'filters': {'kind': 'chart'}, 'top_k': 2})
        response.raise_for_status()
        charts = response.json()
        assert len(charts['hits']) == 2
        for hit in charts['hits']:
            image = client.get(hit['figure_url'])
            image.raise_for_status()
            assert image.content.startswith(b'\x89PNG')
            assert hit['page'] >= 1 and hit['kind'] == 'chart'
        results.append({'case': 'chart_sources', **charts})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
