"""Persistent, owner-scoped queue for the local TradingAgents subprocess."""

from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import re
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from uuid import uuid4


_DEFAULT_RUNNER = Path(__file__).resolve().parents[4] / 'TradingAgents-CN/semiconductor_runner.py'
RUNNER = Path(os.getenv('FINANCE_AGENT_RUNNER', str(_DEFAULT_RUNNER)))
PYTHON = Path(os.getenv('FINANCE_AGENT_PYTHON', str(RUNNER.parent / '.venv-agent/bin/python')))
MAX_PENDING = 4


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class AgentJobs:
    def __init__(self, path: Path, start_worker: bool = True):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.closed = threading.Event()
        self._ready_until = 0.0
        self._ready_value = False
        self._date_cache = {}
        self._catalog = None
        self._catalog_at = 0.0
        self._catalog_lock = threading.Lock()
        self._process = None
        self._process_job = None
        with self._db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, ticker TEXT NOT NULL,
                trade_date TEXT NOT NULL, analysts TEXT NOT NULL,
                status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                result TEXT, error TEXT)''')
            db.execute("UPDATE jobs SET status='queued', updated_at=? WHERE status='running'", (now(),))
            db.execute('''CREATE TABLE IF NOT EXISTS job_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                payload TEXT NOT NULL, created_at TEXT NOT NULL)''')
            db.execute('CREATE INDEX IF NOT EXISTS job_events_job_id ON job_events(job_id,id)')
        self.worker = threading.Thread(target=self._work, name='finance-agent', daemon=True) if start_worker else None
        if self.worker:
            self.worker.start()

    def _db(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _public(row):
        item = dict(row)
        item.pop('owner')
        item['analysts'] = json.loads(item['analysts'])
        item['result'] = json.loads(item['result']) if item['result'] else None
        return item

    def submit(self, owner: str, ticker: str, trade_date: str, analysts: list[str],
               idempotency_key: str | None = None) -> dict:
        if not self.ready():
            raise RuntimeError('金融 Agent 运行依赖尚未就绪')
        with self.lock, self._db() as db:
            pending = db.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
            if pending >= MAX_PENDING:
                raise OverflowError('分析任务已排满，请稍后重试')
            job_id = uuid4().hex
            stamp = now()
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?)',
                       (job_id, owner, ticker, trade_date, json.dumps(analysts),
                        'queued', stamp, stamp, None, None))
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        self.wake.set()
        return self._public(row)

    def stocks(self, query: str) -> list[dict]:
        query = query.strip().upper()
        if not query or len(query) > 40:
            return []
        with self._catalog_lock:
            if self._catalog is None or time.monotonic() > self._catalog_at:
                cache = self.path.with_name('agent_stock_catalog.json')
                if cache.exists() and time.time() - cache.stat().st_mtime < 86400:
                    self._catalog = json.loads(cache.read_text(encoding='utf-8'))
                else:
                    with tempfile.TemporaryDirectory(prefix='finance-stocks-') as temporary:
                        output = Path(temporary) / 'stocks.json'
                        try:
                            done = subprocess.run([str(PYTHON), str(RUNNER), '--stock-catalog',
                                                   '--output', str(output)], cwd=RUNNER.parent,
                                                  capture_output=True, text=True, timeout=90)
                        except (OSError, subprocess.TimeoutExpired) as exc:
                            raise RuntimeError('股票目录获取超时，请稍后重试') from exc
                        if done.returncode or not output.exists():
                            raise RuntimeError('股票目录暂不可用，请稍后重试')
                        self._catalog = json.loads(output.read_text(encoding='utf-8'))
                        cache.write_text(json.dumps(self._catalog, ensure_ascii=False), encoding='utf-8')
                self._catalog_at = time.monotonic() + 3600
        code_query = re.sub(r'^(SH|SZ)[.\s]?', '', query)
        matches = [stock for stock in self._catalog
                   if code_query in stock['ticker'] or query in stock['name'].upper()
                   or (len(query) >= 4 and len(stock['name']) >= 3
                       and stock['name'].upper() in query)]
        matches.sort(key=lambda stock: (stock['ticker'] != code_query and stock['name'].upper() != query,
                                        not stock['ticker'].startswith(code_query), stock['ticker']))
        return matches[:20]

    def resolve_stock(self, raw: str) -> dict:
        raw = raw.strip().upper()
        code = re.sub(r'^(SH|SZ)[.]?', '', raw)
        matches = self.stocks(raw)
        exact = [stock for stock in matches if stock['ticker'] == code or stock['name'].upper() == raw
                 or (len(raw) >= 4 and len(stock['name']) >= 3 and stock['name'].upper() in raw)]
        if len(exact) == 1:
            stock = exact[0]
            prefix = re.match(r'^(SH|SZ)', raw)
            if prefix and prefix.group(1) != stock['exchange']:
                raise ValueError('股票代码前缀与交易所不一致')
            return stock
        if not matches:
            raise ValueError('未找到该 A 股标的，请检查代码或名称')
        raise ValueError('匹配到多个标的，请从候选列表选择并确认')

    def add_event(self, job_id: str, payload: dict) -> None:
        with self.lock, self._db() as db:
            db.execute('INSERT INTO job_events(job_id,payload,created_at) VALUES (?,?,?)',
                       (job_id, json.dumps(payload, ensure_ascii=False), now()))

    def events_since(self, owner: str, job_id: str, after: int = 0) -> list[dict] | None:
        with self.lock, self._db() as db:
            if not db.execute('SELECT 1 FROM jobs WHERE owner=? AND id=?', (owner, job_id)).fetchone():
                return None
            rows = db.execute('SELECT id,payload,created_at FROM job_events WHERE job_id=? AND id>? ORDER BY id LIMIT 200',
                              (job_id, after)).fetchall()
        return [{'id': row['id'], 'created_at': row['created_at'],
                 **json.loads(row['payload'])} for row in rows]

    def cancel(self, owner: str, job_id: str) -> dict | None:
        changed = False
        with self.lock, self._db() as db:
            row = db.execute('SELECT * FROM jobs WHERE owner=? AND id=?', (owner, job_id)).fetchone()
            if not row:
                return None
            if row['status'] in ('queued', 'running'):
                changed = True
                db.execute('UPDATE jobs SET status=?,updated_at=? WHERE id=?', ('cancelled', now(), job_id))
                if self._process_job == job_id and self._process and self._process.poll() is None:
                    os.killpg(self._process.pid, signal.SIGTERM)
            updated = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if changed:
            self.add_event(job_id, {'type': 'status', 'status': 'cancelled'})
        return self._public(updated)

    def ready(self) -> bool:
        with self.lock:
            if time.monotonic() < self._ready_until:
                return self._ready_value
            self._ready_value = False
            if RUNNER.is_file() and PYTHON.is_file():
                try:
                    probe = subprocess.run([str(PYTHON), '-c',
                        'import langchain_openai, langgraph, akshare, tradingagents.graph.trading_graph'],
                        cwd=RUNNER.parent, capture_output=True, timeout=20)
                    self._ready_value = probe.returncode == 0
                except (OSError, subprocess.TimeoutExpired):
                    pass
            self._ready_until = time.monotonic() + 30
            return self._ready_value

    def get(self, owner: str, job_id: str) -> dict | None:
        with self.lock, self._db() as db:
            row = db.execute('SELECT * FROM jobs WHERE owner=? AND id=?', (owner, job_id)).fetchone()
        return self._public(row) if row else None

    def latest_date(self, ticker: str) -> dict:
        with self.lock:
            cached = self._date_cache.get(ticker)
            if cached and time.monotonic() < cached[0]:
                return cached[1]
        with tempfile.TemporaryDirectory(prefix='finance-date-') as temporary:
            output = Path(temporary) / 'date.json'
            try:
                done = subprocess.run([str(PYTHON), str(RUNNER), '--ticker', ticker,
                                       '--latest-date', '--output', str(output)],
                                      cwd=RUNNER.parent, capture_output=True, text=True, timeout=20)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise RuntimeError('交易日数据源超时') from exc
            if done.returncode or not output.exists():
                raise RuntimeError('最近交易日暂不可用')
            result = json.loads(output.read_text(encoding='utf-8'))
        with self.lock:
            self._date_cache[ticker] = (time.monotonic() + 300, result)
        return result

    def list(self, owner: str) -> list[dict]:
        with self.lock, self._db() as db:
            rows = db.execute('SELECT * FROM jobs WHERE owner=? ORDER BY created_at DESC LIMIT 30',
                              (owner,)).fetchall()
        return [self._public(row) for row in rows]

    def status_counts(self) -> dict[str, int]:
        with self.lock, self._db() as db:
            rows = db.execute('SELECT status,count(*) AS n FROM jobs GROUP BY status').fetchall()
        return {row['status']: row['n'] for row in rows}

    def oldest_queued_seconds(self) -> float:
        with self.lock, self._db() as db:
            row = db.execute("SELECT created_at FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
        if not row:
            return 0.0
        return max(0.0, (datetime.now(timezone.utc) - datetime.fromisoformat(row['created_at'])).total_seconds())

    def _work(self):
        while not self.closed.is_set():
            with self.lock, self._db() as db:
                row = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
                if row:
                    db.execute("UPDATE jobs SET status='running',updated_at=? WHERE id=?", (now(), row['id']))
            if not row:
                self.wake.wait(2)
                self.wake.clear()
                continue
            with tempfile.TemporaryDirectory(prefix='finance-agent-') as temporary:
                with self.lock, self._db() as db:
                    active = db.execute('SELECT status FROM jobs WHERE id=?', (row['id'],)).fetchone()
                if active['status'] == 'cancelled':
                    continue
                output = Path(temporary) / 'result.json'
                command = [str(PYTHON), str(RUNNER), '--ticker', row['ticker'],
                           '--date', row['trade_date'], '--analysts', ','.join(json.loads(row['analysts'])),
                           '--output', str(output)]
                env = os.environ.copy()
                bypass = [entry for entry in env.get('NO_PROXY', env.get('no_proxy', '')).split(',') if entry]
                for host in ('127.0.0.1', 'localhost', '::1'):
                    if host not in bypass:
                        bypass.append(host)
                env['NO_PROXY'] = env['no_proxy'] = ','.join(bypass)
                process = None
                try:
                    self.add_event(row['id'], {'type': 'status', 'status': 'running'})
                    with open(Path(temporary) / 'stderr.log', 'w+', encoding='utf-8') as stderr:
                        process = subprocess.Popen([*command, '--event-stream'], cwd=RUNNER.parent,
                                                   env=env, stdout=subprocess.PIPE, stderr=stderr,
                                                   text=True, bufsize=1, start_new_session=True)
                        with self.lock:
                            self._process, self._process_job = process, row['id']
                            with self._db() as db:
                                cancelled = db.execute('SELECT status FROM jobs WHERE id=?', (row['id'],)).fetchone()['status'] == 'cancelled'
                            if cancelled:
                                os.killpg(process.pid, signal.SIGTERM)
                        started = time.monotonic()
                        lines = queue.Queue()
                        reader = threading.Thread(target=lambda: [lines.put(line) for line in process.stdout],
                                                  daemon=True)
                        reader.start()
                        while reader.is_alive() or not lines.empty():
                            if time.monotonic() - started > int(os.getenv('FINANCE_AGENT_TASK_TIMEOUT', '1200')):
                                os.killpg(process.pid, signal.SIGTERM)
                                raise RuntimeError('分析超时，已保留已生成内容')
                            try:
                                line = lines.get(timeout=0.5)
                            except queue.Empty:
                                continue
                            try:
                                event = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            if isinstance(event, dict) and event.get('type') in ('plan', 'step', 'text'):
                                self.add_event(row['id'], event)
                        process.wait(timeout=10)
                        stderr.seek(0)
                        detail = stderr.read()[-1500:]
                    if process.returncode or not output.exists():
                        raise RuntimeError(detail or '分析进程未生成结果')
                    result, error, status = output.read_text(encoding='utf-8'), None, 'completed'
                except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
                    if process and process.poll() is None:
                        os.killpg(process.pid, signal.SIGTERM)
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                    result, error, status = None, str(exc)[-1500:], 'failed'
                finally:
                    with self.lock:
                        self._process, self._process_job = None, None
                with self.lock, self._db() as db:
                    current = db.execute('SELECT status FROM jobs WHERE id=?', (row['id'],)).fetchone()
                    if current['status'] != 'cancelled':
                        db.execute('UPDATE jobs SET status=?,updated_at=?,result=?,error=? WHERE id=?',
                                   (status, now(), result, error, row['id']))
                        finished = True
                    else:
                        finished = False
                if finished:
                    self.add_event(row['id'], {'type': 'status', 'status': status,
                                               'error': error if status == 'failed' else None})

    def close(self):
        self.closed.set()
        self.wake.set()
        if self.worker:
            self.worker.join(timeout=2)
