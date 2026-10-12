import json
import logging

from fastapi.testclient import TestClient
from semi_rag.api import create_app
from semi_rag.observability import JsonFormatter, Observability, request_id_var


class Retriever:
    manifest = {'generation': 'test'}
    chunks = []
    by_id = {}
    def search(self, query, filters, top_k, mode='rerank'):
        return {'hits': [], 'filters': filters}


class Answerer:
    def llm_health(self): return {'model': 'test'}
    def close(self): pass


def test_metrics_and_request_id(tmp_path, monkeypatch):
    monkeypatch.setenv('RAG_AUTH_MODE', 'dev')
    app = create_app({'data_dir': str(tmp_path), 'pdf_dir': str(tmp_path)}, Retriever(), Answerer())
    headers = {'x-rag-dev-user': 'admin', 'x-rag-dev-role': 'admin', 'x-request-id': 'edge-123'}
    with TestClient(app) as client:
        response = client.get('/stats', headers=headers)
        assert response.headers['x-request-id'] == 'edge-123'
        metrics = client.get('/metrics', headers=headers).text
        assert 'rag_http_requests_total' in metrics
        assert 'rag_tasks' in metrics


def test_metrics_token_and_json_log(tmp_path, monkeypatch):
    monkeypatch.setenv('RAG_AUTH_MODE', 'dev')
    monkeypatch.setenv('RAG_METRICS_TOKEN', 'monitor-secret')
    app = create_app({'data_dir': str(tmp_path), 'pdf_dir': str(tmp_path)}, Retriever(), Answerer())
    with TestClient(app) as client:
        assert client.get('/metrics').status_code == 401
        assert client.get('/metrics', headers={'authorization': 'Bearer monitor-secret'}).status_code == 200
    request_id_var.set('request-7')
    record = logging.LogRecord('test', logging.INFO, '', 0, 'hello %s', ('world',), None)
    assert json.loads(JsonFormatter().format(record))['request_id'] == 'request-7'


def test_model_error_metric():
    telemetry = Observability()
    try:
        with telemetry.model('generation'):
            raise RuntimeError('boom')
    except RuntimeError:
        pass
    text = telemetry.render().decode()
    assert 'rag_model_calls_total{operation="generation",outcome="error"} 1.0' in text
