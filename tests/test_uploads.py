import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from semi_rag.api import create_app
from semi_rag.pdf_ingest import parse_pdf, render_page
from semi_rag.uploads import UploadManager, blocking_visual_failures, make_upload_chunks
from semi_rag.upload_store import atomic_json, upload_dir
from prepare_upload_sandbox import create_fixture
from test_rag import FakeAnswerer, FakeRetriever


@pytest.fixture
def fixture_pdf(tmp_path):
    pdf = tmp_path / 'fixture.pdf'
    create_fixture(pdf)
    return pdf


def config(tmp_path):
    return {'data_dir': str(tmp_path / 'data'), 'pdf_dir': str(tmp_path),
            'chunk_chars': 300, 'chunk_overlap': 30, 'upload_max_pages': 200}


def test_parse_tables_scans_and_source_coordinates(tmp_path, fixture_pdf):
    result = parse_pdf(fixture_pdf, tmp_path / 'parsed')
    assert len(result['pages']) == 3
    page = result['pages'][0]
    assert '12.34' in '\n'.join(node['text'] for node in page['nodes'])
    assert not page['needs_ocr']
    tables = [node for node in result['pages'][1]['nodes'] if node['kind'] == 'table']
    assert tables and '12.34' in tables[0]['text'] and '2.56' in tables[0]['text']
    assert result['pages'][2]['needs_ocr']
    for page in result['pages']:
        for node in page['nodes']:
            x0, y0, x1, y1 = node['bbox']
            assert 0 <= x0 < x1 <= page['width'] and 0 <= y0 < y1 <= page['height']
    image = render_page(fixture_pdf, 1, [result['pages'][0]['nodes'][0]['bbox']])
    assert image.startswith(b'\x89PNG')
    with pytest.raises(ValueError):
        render_page(fixture_pdf, 99)
    with pytest.raises(ValueError):
        parse_pdf(fixture_pdf, tmp_path / 'over-limit', max_pages=2)


def test_review_dedup_exclusion_and_publication_gate(tmp_path, fixture_pdf):
    cfg = config(tmp_path)
    manager = UploadManager(cfg, None)
    metadata = {'title': '验收报告', 'org': '测试证券', 'publish_date': '2026-09-07'}
    job = manager.submit(io.BytesIO(fixture_pdf.read_bytes()), '../../test.pdf', metadata, True)
    assert job['filename'] == 'test.pdf'
    duplicate = manager.submit(io.BytesIO(fixture_pdf.read_bytes()), 'renamed.pdf', metadata, False)
    assert duplicate['duplicate'] and duplicate['id'] == job['id']
    assert len(manager.store.list()) == 1
    directory = upload_dir(cfg, job['id'])
    document = parse_pdf(fixture_pdf, directory)
    document['pages'][2]['visual_status'] = 'failed'
    document['pages'][0]['visual_status'] = 'failed'
    atomic_json(directory / 'document.json', document)
    manager.store.update(job['id'], status='review')
    assert blocking_visual_failures(document) == [3]
    with pytest.raises(ValueError, match='第3页.*全文视觉识别失败'):
        manager.approve(job['id'], 0)
    node = next(node for node in document['pages'][0]['nodes'] if '12.34' in node['text'])
    edited = node['text'].replace('12.34', '12.35')
    job = manager.review(job['id'], {'revision': 0, 'metadata': metadata, 'excluded_pages': [3],
        'edits': [{'block_id': node['block_id'], 'text': edited, 'included': True}]})
    assert job['revision'] == 1
    with pytest.raises(ValueError, match='changed'):
        manager.review(job['id'], {'revision': 0})
    document = json.loads((directory / 'document.json').read_text())
    assert blocking_visual_failures(document) == []
    chunks = make_upload_chunks(cfg, job, document)
    assert chunks and all(chunk['page'] in (1, 2) for chunk in chunks)
    assert any(block['edited'] for chunk in chunks for block in chunk['source_blocks'])
    saved_node = next(n for n in document['pages'][0]['nodes'] if n['block_id'] == node['block_id'])
    assert saved_node['original_text'] == node['text']
    assert manager.approve(job['id'], 1)['status'] == 'queued_index'
    assert (directory / 'approved.json').is_file()
    with pytest.raises(ValueError):
        manager.approve(job['id'], 1)


def test_upload_limits_and_recovery(tmp_path, fixture_pdf):
    cfg = config(tmp_path)
    manager = UploadManager({**cfg, 'upload_max_mb': 0}, None)
    with pytest.raises(ValueError, match='exceeds'):
        manager.submit(io.BytesIO(fixture_pdf.read_bytes()), 'large.pdf', {}, True)
    manager = UploadManager(cfg, None)
    with pytest.raises(ValueError, match='header'):
        manager.submit(io.BytesIO(b'this is not a pdf'), 'bad.pdf', {}, True)
    assert not list((tmp_path / 'data/rag/uploads').glob('*.upload'))
    metadata = {'title': '测试', 'org': '测试', 'publish_date': '2026-09-07'}
    job = manager.submit(io.BytesIO(fixture_pdf.read_bytes()), 'valid.pdf', metadata, True)
    manager.store.update(job['id'], status='recognizing')
    recovered = UploadManager(cfg, None)
    recovered.worker = SimpleNamespace(start=lambda: None)
    recovered.start()
    assert recovered.store.get(job['id'])['status'] == 'queued'
    assert recovered.tasks.get_nowait() == ('parse', job['id'])


def test_upload_api_validation_and_source_security(tmp_path, fixture_pdf, monkeypatch):
    monkeypatch.setattr(UploadManager, 'start', lambda self: None)
    monkeypatch.setattr(UploadManager, 'close', lambda self: None)
    app = create_app(config(tmp_path), FakeRetriever(), FakeAnswerer())
    form = {'title': '测试报告', 'org': '测试证券', 'publish_date': '2026-09-07', 'visual': 'false'}
    with TestClient(app) as client:
        assert client.get('/library').status_code == 200
        assert client.get('/library.js').status_code == 200
        assert client.post('/uploads', data=form, files={'file': ('bad.pdf', b'not a pdf')}).status_code == 422
        bad = {**form, 'publish_date': 'unknown'}
        assert client.post('/uploads', data=bad, files={'file': ('report.pdf', fixture_pdf.read_bytes())}).status_code == 422
        response = client.post('/uploads', data=form, files={'file': ('report.pdf', fixture_pdf.read_bytes())})
        assert response.status_code == 202
        job = response.json()
        assert client.get(f'/uploads/{job["id"]}/pdf').content == fixture_pdf.read_bytes()
        assert client.get(f'/uploads/{job["id"]}/pages/1').status_code == 409
        assert client.get('/uploads/unknown').status_code == 404
        assert client.post('/uploads', content=b'', headers={'Content-Length': '999999999'}).status_code == 413
        monkeypatch.setenv('RAG_API_KEY', 'test-key')
        assert client.get('/uploads').status_code == 401
        assert client.post('/uploads', data=form, files={'file': ('report.pdf', fixture_pdf.read_bytes())}).status_code == 401
