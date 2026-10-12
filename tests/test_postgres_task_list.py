"""Exercise PostgreSQL's parameter typing and owner scoping against a real database."""

import os
from uuid import uuid4

import pytest

from semi_rag.tasks import PostgresTaskStore


@pytest.mark.skipif(not os.getenv('RAG_DATABASE_URL'), reason='requires local PostgreSQL')
def test_postgres_task_list_with_and_without_type():
    store = PostgresTaskStore(os.environ['RAG_DATABASE_URL'])
    owner = 'test-agent-list-' + uuid4().hex
    task, _ = store.submit('agent.analysis', owner, {
        'ticker': '600519', 'trade_date': '2026-09-28', 'analysts': ['market'],
    }, run_at='2099-01-01T00:00:00Z')
    try:
        assert [row['id'] for row in store.list(owner)] == [task['id']]
        assert [row['id'] for row in store.list(owner, 'agent.analysis')] == [task['id']]
        assert store.list(owner, 'article.sync') == []
        assert store.list('another-user', 'agent.analysis') == []
    finally:
        with store.db() as db:
            db.execute('DELETE FROM task_jobs WHERE id=%s', (task['id'],))

