"""Run only against the isolated upload sandbox, never the production index."""
import argparse
import json
import time
from pathlib import Path

import httpx


def wait_for(client, upload_id, wanted, probe=False):
    deadline = time.monotonic() + 240
    previous = None
    while time.monotonic() < deadline:
        job = client.get(f'/uploads/{upload_id}').json()
        if job['status'] != previous:
            print(job['status'], job.get('error'), flush=True)
            previous = job['status']
        if job['status'] in wanted:
            return job
        if job['status'] in ('failed', 'index_failed'):
            raise AssertionError(job)
        if probe:
            response = client.post('/search', json={'query': '中芯国际产能利用率', 'mode': 'dense', 'top_k': 1})
            assert response.status_code in (200, 503), (response.status_code, response.text)
            if response.status_code == 200:
                assert response.json()['hits']
        time.sleep(1)
    raise AssertionError('Task timed out')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('sandbox', type=Path)
    parser.add_argument('--url', default='http://127.0.0.1:8091')
    args = parser.parse_args()
    assert args.url != 'http://127.0.0.1:8090'
    with httpx.Client(base_url=args.url, trust_env=False, timeout=180) as client:
        before = client.get('/stats').json()
        form = {'title': '芯测科技上传验收报告', 'org': '测试证券', 'publish_date': '2026-09-07', 'visual': 'true'}
        pdf = (args.sandbox / 'upload-fixture.pdf').read_bytes()
        response = client.post('/uploads', data=form, files={'file': ('upload-fixture.pdf', pdf, 'application/pdf')})
        response.raise_for_status()
        job = wait_for(client, response.json()['id'], {'review'})
        upload_id = job['id']
        duplicate = client.post('/uploads', data=form, files={'file': ('renamed.pdf', pdf, 'application/pdf')}).json()
        assert duplicate['duplicate'] and duplicate['id'] == upload_id
        pages = [client.get(f'/uploads/{upload_id}/pages/{number}').json()['page'] for number in range(1, 4)]
        assert any(node['kind'] == 'table' and '12.34' in node['text'] for node in pages[1]['nodes'])
        assert pages[2]['visual_status'] == 'done', pages[2]
        assert any('4.8' in node['text'] for node in pages[2]['nodes']), pages[2]
        assert all(page['visual_status'] != 'failed' for page in pages), pages
        result = client.post('/search', json={'query': '芯测科技营业收入', 'filters': {'doc_ids': [job['doc_id']]}}).json()
        assert not result['hits'], 'Unapproved document leaked into retrieval'
        review = {'revision': job['revision'], 'metadata': job['metadata'], 'excluded_pages': [], 'edits': []}
        response = client.put(f'/uploads/{upload_id}/review', json=review)
        response.raise_for_status()
        job = response.json()
        response = client.post(f'/uploads/{upload_id}/publish', json={'revision': job['revision']})
        response.raise_for_status()
        job = wait_for(client, upload_id, {'indexed'}, probe=True)
        after = client.get('/stats').json()
        assert after['report']['documents'] == before['report']['documents'] + 1
        assert after['generation'] == job['generation']
        result = client.post('/ask', json={'query': '芯测科技2026年第二季度营业收入是多少亿元？',
            'filters': {'doc_ids': [job['doc_id']]}})
        result.raise_for_status()
        answer = result.json()
        assert answer['answerable'] and '12.34' in answer['answer'], answer
        for source in answer['sources']:
            assert source['page'] in (1, 2)
            assert source['source_blocks']
            assert client.get(source['pdf_url']).content == pdf
            assert client.get(source['page_image_url']).content.startswith(b'\x89PNG')
        print(answer['answer'], flush=True)
        result = client.post('/search', json={'query': '测试产线月产能4.8万片', 'filters': {'doc_ids': [job['doc_id']]}}).json()
        assert any(hit['page'] == 3 and '4.8' in hit['text'] for hit in result['hits'])
        report = {'job': job, 'answer': answer, 'page_methods': [[node['method'] for node in page['nodes']] for page in pages],
                  'before_generation': before['generation'], 'after_generation': after['generation'],
                  'cache_hits': after['cache_hits'], 'checks': ['upload', 'dedup', 'table', 'visual_ocr',
                  'review_gate', 'hot_reload', 'answer', 'source_pdf', 'source_geometry', 'scanned_retrieval']}
        (args.sandbox / 'acceptance.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps({'checks': report['checks'], 'job': job['id'], 'indexed_chunks': job['indexed_chunks']}, indent=2))


if __name__ == '__main__':
    main()
