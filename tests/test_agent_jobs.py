import json
import time
from types import SimpleNamespace

from semi_rag import agent_jobs


def test_agent_jobs_owner_scope_and_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_jobs.AgentJobs, 'ready', staticmethod(lambda: True))

    class FakeProcess:
        def __init__(self, command, **kwargs):
            self.stdout = iter([json.dumps({'type': 'plan', 'steps': [{'id': 'market', 'title': '市场分析'}]}) + '\n',
                                json.dumps({'type': 'text', 'step': 'market', 'field': 'market_report', 'chunk': 'ok'}) + '\n'])
            self.returncode = 0
            self.pid = 100000
            self.output = command[command.index('--output') + 1]

        def poll(self):
            return self.returncode

        def wait(self, **kwargs):
            from pathlib import Path
            Path(self.output).write_text(json.dumps({'market_report': 'ok'}))
            return self.returncode

    def fake_popen(command, **kwargs):
        output = command[command.index('--output') + 1]
        assert output
        return FakeProcess(command, **kwargs)

    monkeypatch.setattr(agent_jobs.subprocess, 'Popen', fake_popen)
    jobs = agent_jobs.AgentJobs(tmp_path / 'jobs.sqlite3')
    try:
        created = jobs.submit('owner-a', '688981', '2026-09-26', ['market'])
        assert jobs.get('owner-b', created['id']) is None
        assert jobs.list('owner-b') == []
        for _ in range(100):
            found = jobs.get('owner-a', created['id'])
            if found['status'] == 'completed':
                break
            time.sleep(0.02)
        assert found['status'] == 'completed'
        assert found['result']['market_report'] == 'ok'
        events = jobs.events_since('owner-a', created['id'])
        assert any(event.get('chunk') == 'ok' for event in events)
        assert jobs.events_since('owner-b', created['id']) is None
        assert jobs.events_since('owner-a', created['id'], events[-1]['id']) == []
    finally:
        jobs.close()


def test_agent_jobs_reject_unready_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_jobs.AgentJobs, 'ready', staticmethod(lambda: False))
    jobs = agent_jobs.AgentJobs(tmp_path / 'jobs.sqlite3')
    try:
        import pytest
        with pytest.raises(RuntimeError, match='运行依赖'):
            jobs.submit('owner', '688981', '2026-09-26', ['market'])
    finally:
        jobs.close()


def test_latest_date_is_cached(tmp_path, monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        from pathlib import Path
        output = Path(command[command.index('--output') + 1])
        output.write_text(json.dumps({'trade_date': '2026-09-24', 'source': 'BaoStock'}))
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    monkeypatch.setattr(agent_jobs.subprocess, 'run', fake_run)
    jobs = agent_jobs.AgentJobs(tmp_path / 'jobs.sqlite3')
    try:
        assert jobs.latest_date('688981')['trade_date'] == '2026-09-24'
        assert jobs.latest_date('688981')['trade_date'] == '2026-09-24'
        assert len(calls) == 1
    finally:
        jobs.close()


def test_stock_search_and_unique_resolution(tmp_path, monkeypatch):
    def fake_run(command, **kwargs):
        from pathlib import Path
        output = Path(command[command.index('--output') + 1])
        output.write_text(json.dumps([
            {'ticker': '600519', 'name': '贵州茅台', 'exchange': 'SH'},
            {'ticker': '000001', 'name': '平安银行', 'exchange': 'SZ'},
            {'ticker': '000002', 'name': '平安发展', 'exchange': 'SZ'},
        ]))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(agent_jobs.subprocess, 'run', fake_run)
    jobs = agent_jobs.AgentJobs(tmp_path / 'jobs.sqlite3')
    try:
        assert jobs.resolve_stock('SH600519')['name'] == '贵州茅台'
        assert jobs.resolve_stock('贵州茅台')['ticker'] == '600519'
        assert jobs.resolve_stock('贵州茅台酒股份有限公司')['ticker'] == '600519'
        assert len(jobs.stocks('平安')) == 2
        import pytest
        with pytest.raises(ValueError, match='多个标的'):
            jobs.resolve_stock('平安')
        with pytest.raises(ValueError, match='前缀'):
            jobs.resolve_stock('SZ600519')
        with pytest.raises(ValueError, match='未找到'):
            jobs.resolve_stock('不存在')
    finally:
        jobs.close()


def test_cancel_queued_job_preserves_owner_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_jobs.AgentJobs, 'ready', staticmethod(lambda: True))
    jobs = agent_jobs.AgentJobs(tmp_path / 'jobs.sqlite3')
    try:
        jobs.closed.set()
        jobs.wake.set()
        jobs.worker.join(timeout=2)
        created = jobs.submit('owner-a', '688981', '2026-09-24', ['market'])
        assert jobs.cancel('owner-b', created['id']) is None
        cancelled = jobs.cancel('owner-a', created['id'])
        assert cancelled['status'] == 'cancelled'
        assert jobs.events_since('owner-a', created['id'])[-1]['status'] == 'cancelled'
    finally:
        jobs.close()
