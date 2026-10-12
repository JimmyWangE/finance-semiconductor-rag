"""Low-cardinality metrics and request-correlated structured logging."""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, Info, generate_latest


request_id_var = contextvars.ContextVar('request_id', default='')
REQUEST_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$')


class JsonFormatter(logging.Formatter):
    def format(self, record):
        value = {'timestamp': datetime.now(timezone.utc).isoformat(), 'level': record.levelname,
                 'logger': record.name, 'message': record.getMessage(),
                 'request_id': request_id_var.get() or None}
        for key in ('trace_id', 'task_id', 'task_type', 'duration_ms', 'outcome'):
            if hasattr(record, key):
                value[key] = getattr(record, key)
        if record.exc_info:
            value['exception'] = self.formatException(record.exc_info)
        return json.dumps(value, ensure_ascii=False)


def configure_logging():
    if os.getenv('RAG_LOG_FORMAT', 'text').lower() != 'json':
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(os.getenv('RAG_LOG_LEVEL', 'INFO').upper())


class Observability:
    def __init__(self):
        self.registry = CollectorRegistry()
        self.http_requests = Counter('rag_http_requests_total', 'HTTP requests',
            ('method', 'route', 'status'), registry=self.registry)
        self.http_duration = Histogram('rag_http_request_duration_seconds', 'HTTP request duration',
            ('method', 'route'), buckets=(.01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10, 30, 60, 180), registry=self.registry)
        self.http_inflight = Gauge('rag_http_inflight', 'In-flight HTTP requests', registry=self.registry)
        self.model_calls = Counter('rag_model_calls_total', 'Model operations',
            ('operation', 'outcome'), registry=self.registry)
        self.model_duration = Histogram('rag_model_call_duration_seconds', 'Model operation duration',
            ('operation', 'outcome'), buckets=(.1, .25, .5, 1, 2.5, 5, 10, 30, 60, 180, 600), registry=self.registry)
        self.tasks = Gauge('rag_tasks', 'Tasks by type and state', ('type', 'status'), registry=self.registry)
        self.oldest_task = Gauge('rag_task_oldest_queued_seconds', 'Age of oldest queued task',
            ('type',), registry=self.registry)
        self.auth_events = Counter('rag_auth_events_total', 'Authentication events',
            ('event', 'outcome'), registry=self.registry)
        self.build = Info('rag_build', 'RAG service build', registry=self.registry)
        self.build.info({'version': os.getenv('RAG_BUILD_VERSION', 'development')})

    def request_id(self, supplied: str) -> str:
        return supplied if REQUEST_ID.fullmatch(supplied or '') else uuid4().hex

    @contextmanager
    def model(self, operation: str):
        started, outcome = time.monotonic(), 'success'
        try:
            yield
        except Exception:
            outcome = 'error'
            raise
        finally:
            self.model_calls.labels(operation, outcome).inc()
            self.model_duration.labels(operation, outcome).observe(time.monotonic() - started)

    def render(self) -> bytes:
        return generate_latest(self.registry)

