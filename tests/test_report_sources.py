import hashlib

from fastapi.testclient import TestClient

from semi_rag import report_sources
from semi_rag.api import create_app
from semi_rag.uploads import UploadManager
from test_rag import FakeAnswerer, FakeRetriever


def example(external_id='AP1', org='东吴证券', title='半导体材料专题'):
    return {'external_id': external_id, 'title': title, 'org': org,
            'publish_date': '2026-09-22', 'industry': '半导体', 'pages': 3,
            'size_kb': 12, 'info_url': 'https://data.eastmoney.com/report/info/AP1.html'}


def test_broker_channels_filter_publisher_and_validate_id(monkeypatch):
    seen = []
    def discover(code, limit):
        seen.append((code, limit))
        return [example(), example('AP2', '国金证券')]
    monkeypatch.setattr(report_sources.eastmoney, 'discover', discover)
    assert report_sources.discover('dongwu', 5) == [example()]
    assert seen == [('1036', 100)]
    assert len(report_sources.discover('eastmoney', 5)) == 2
    try:
        report_sources.discover('invalid')
        assert False
    except ValueError:
        pass


def test_cross_channel_import_dedupes_metadata_and_pdf(tmp_path, monkeypatch):
    monkeypatch.setattr(UploadManager, 'start', lambda self: None)
    monkeypatch.setattr(UploadManager, 'close', lambda self: None)
    monkeypatch.setattr(report_sources.eastmoney, 'discover', lambda *_: [example()])
    pdf = b'%PDF-1.4\n' + b'x' * 12_000
    downloads = []
    def download(report):
        downloads.append(report['external_id'])
        return pdf, 'https://pdf.dfcfw.com/pdf/H3_AP1_1.pdf'
    monkeypatch.setattr(report_sources.eastmoney, 'download', download)
    cfg = {'data_dir': str(tmp_path), 'pdf_dir': str(tmp_path), 'chunk_chars': 300,
           'chunk_overlap': 30, 'upload_max_pages': 200}
    with TestClient(create_app(cfg, FakeRetriever(), FakeAnswerer())) as client:
        assert len(client.get('/report-sources').json()['sources']) == 5
        assert client.get('/report-sources/unknown/reports').status_code == 404
        url = '/report-sources/dongwu/import'
        body = {'external_ids': ['AP1']}
        assert client.get('/report-sources/dongwu/reports').json()['new_count'] == 1
        first = client.post(url, json=body).json()
        assert len(first['imported']) == 1
        assert first['imported'][0]['job']['id'] == hashlib.sha256(pdf).hexdigest()
        assert client.get('/report-sources/dongwu/reports').json()['new_count'] == 0
        assert client.post(url, json=body).json()['skipped'] == ['AP1']
        # Another identifier and title for the same PDF must not create a new job.
        monkeypatch.setattr(report_sources.eastmoney, 'discover', lambda *_: [example('AP3', title='半导体材料报告')])
        assert client.post(url, json={'external_ids': ['AP3']}).json()['skipped'] == ['AP3']
        assert len(client.get('/uploads').json()['uploads']) == 1
        assert downloads == ['AP1', 'AP3']
