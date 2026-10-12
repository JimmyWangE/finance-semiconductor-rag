from __future__ import annotations

import asyncio
import hmac
import hashlib
import io
import json
import logging
import os
import threading
import secrets
import re
import time
from uuid import UUID
from urllib.parse import urlencode
from collections import Counter
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.responses import StreamingResponse
from fastapi.encoders import jsonable_encoder
from starlette.background import BackgroundTask
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from semi_rag.answer import Answerer, public_source
from semi_rag.config import load_config
from semi_rag.retrieve import Retriever
from semi_rag.uploads import UploadManager, DistributedUploadManager
from semi_rag.upload_store import upload_dir
from semi_rag.pdf_ingest import render_page
from semi_rag.media import MediaStore
from semi_rag.conversations import ConversationStore, ConversationConflict
from semi_rag.general import answer_general, stream_general
from semi_rag.understanding import answer_turn
from semi_rag import eastmoney, report_sources
from semi_rag.articles import ArticleStore
from semi_rag.agent_jobs import AgentJobs
from semi_rag.distributed_agent import DistributedAgentJobs
from semi_rag.auth import (AuthManager, AuthenticationError, SESSION_COOKIE,
                           STATE_COOKIE, admin_required)
from semi_rag.observability import Observability, configure_logging, request_id_var
from semi_rag.tasks import TaskService

log = logging.getLogger(__name__)

# The workbench is deployed behind internal proxies. Revalidate the small entry
# documents on each visit so a frontend hotfix cannot remain hidden in a cache.
UI_CACHE_HEADERS = {'Cache-Control': 'no-cache, must-revalidate'}
_DEFAULT_DIGITAL_HUMAN_DIR = Path(__file__).resolve().parents[4] / 'chatagent/apps/stage-web/dist'
DIGITAL_HUMAN_DIR = Path(os.environ.get('DIGITAL_HUMAN_DIST', str(_DEFAULT_DIGITAL_HUMAN_DIR)))
RSS_SERVICE_URL = 'http://127.0.0.1:192'


class Filters(BaseModel):
    model_config = ConfigDict(extra='forbid')
    date_from: date | None = None
    date_to: date | None = None
    as_of: date | None = None
    org: str | None = Field(default=None, max_length=100)
    doc_ids: list[str] | None = Field(default=None, max_length=100)
    kind: Literal['text', 'table', 'chart'] | None = None
    company: str | None = Field(default=None, min_length=1, max_length=100)
    industry: str | None = Field(default=None, min_length=1, max_length=100)
    page_from: int | None = Field(default=None, ge=1)
    page_to: int | None = Field(default=None, ge=1)


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    query: str = Field(min_length=1, max_length=1000)
    filters: Filters = Field(default_factory=Filters)
    top_k: int = Field(default=6, ge=1, le=32)
    mode: Literal['bm25', 'dense', 'hybrid', 'rerank'] = 'rerank'


class MediaRef(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(pattern=r'^[0-9a-f]{64}$')


class AskRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    query: str = Field(default='', max_length=1000)
    attachments: list[MediaRef] = Field(default_factory=list, max_length=4)
    filters: Filters | None = None
    top_k: int = Field(default=6, ge=1, le=12)
    answer_policy: Literal['strict', 'best_effort'] | None = None
    conversation_id: UUID | None = None
    request_id: UUID | None = None
    expected_revision: int | None = Field(default=None, ge=0)


class GeneralAskRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    query: str = Field(default='', max_length=4000)
    attachments: list[MediaRef] = Field(default_factory=list, max_length=4)
    conversation_id: UUID | None = None
    request_id: UUID | None = None
    expected_revision: int | None = Field(default=None, ge=0)


class ConversationCreate(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    title: str = Field(default='新会话', min_length=1, max_length=100)


class ConversationChange(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=0)


class ReportMetadata(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=200)
    org: str = Field(min_length=1, max_length=100)
    publish_date: date
    rating: str = Field(default='', max_length=50)
    stock_name: str = Field(default='', max_length=100)
    stock_code: str = Field(default='', max_length=30)
    industry: str = Field(default='', max_length=100)
    external_id: str = Field(default='', max_length=80)
    source_url: str = Field(default='', max_length=500)
    source_pdf_url: str = Field(default='', max_length=500)


class BlockEdit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    block_id: str = Field(max_length=40)
    text: str = Field(max_length=30000)
    included: bool = True


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=0)
    metadata: ReportMetadata
    excluded_pages: list[int] = Field(default_factory=list, max_length=200)
    edits: list[BlockEdit] = Field(default_factory=list, max_length=2000)


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=0)


class EastmoneyImportRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    external_ids: list[str] = Field(min_length=1, max_length=50)
    industry_code: str = Field(default='1036', pattern=r'^\d{1,10}$')
    limit: int = Field(default=50, ge=1, le=100)
    visual: bool = False


class ReportSourceImportRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    external_ids: list[str] = Field(min_length=1, max_length=50)
    limit: int = Field(default=50, ge=1, le=100)
    visual: bool = False


class ArticleImportRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    source_id: str = Field(min_length=1, max_length=60, pattern=r'^[a-z0-9-]+$')
    url: str = Field(min_length=20, max_length=1000)


class ArticleFeedRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    feed_url: str = Field(default='', max_length=1000)


class ArticleSyncRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    source_id: str = Field(default='', max_length=60, pattern=r'^(|[a-z0-9-]+)$')


class AgentAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    ticker: str = Field(min_length=2, max_length=40)
    trade_date: date
    analysts: list[Literal['market', 'news', 'fundamentals', 'social']] = Field(
        default_factory=lambda: ['market', 'news', 'fundamentals'], min_length=1, max_length=4)


class TaskCreateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    type: Literal['article.sync']
    payload: dict = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)
    run_at: datetime | None = None


def _report_key(record: dict) -> tuple[str, str, str]:
    clean = lambda value: re.sub(r'\s+', '', str(value or '')).casefold()
    return clean(record.get('title')), clean(record.get('org')), str(record.get('publish_date') or '')[:10]


def _known_reports(app) -> tuple[set[str], set[tuple[str, str, str]]]:
    records = [job.get('metadata', {}) for job in app.state.uploads.store.list()]
    records.extend(app.state.retriever.chunks)
    external_ids = {str(record.get('external_id') or '') for record in records} - {''}
    keys = {_report_key(record) for record in records if record.get('title') and record.get('publish_date')}
    return external_ids, keys


def _known_pdf_hashes(app) -> set[str]:
    uploads = {job['id'] for job in app.state.uploads.store.list()}
    indexed = {record.get('pdf_sha256') for record in app.state.retriever.chunks}
    return uploads | (indexed - {None, ''})


def create_app(config=None, retriever=None, answerer=None):
    config = config or load_config()
    auth = AuthManager()
    configure_logging()
    telemetry = Observability()
    tasks = TaskService(config)

    @asynccontextmanager
    async def lifespan(app):
        app.state.retriever = retriever or Retriever(config)
        app.state.answerer = answerer or Answerer(config, app.state.retriever)
        app.state.gate = threading.BoundedSemaphore(1)
        app.state.conversations = ConversationStore(config)
        app.state.media = MediaStore(config)
        app.state.articles = ArticleStore(config)
        agent_path = Path(config['data_dir']) / 'finance_agent_jobs.sqlite3'
        app.state.agent_jobs = (DistributedAgentJobs(agent_path, tasks) if tasks.distributed else AgentJobs(agent_path))
        app.state.tasks = tasks
        app.state.uploads = (DistributedUploadManager(config, app, tasks) if tasks.distributed
                             else UploadManager(config, app))
        app.state.uploads.start()
        tasks.start()
        log.info('RAG ready: %s', app.state.retriever.manifest['generation'])
        yield
        import anyio
        await anyio.to_thread.run_sync(app.state.uploads.close)
        tasks.close()
        app.state.agent_jobs.close()
        app.state.answerer.close()

    app = FastAPI(title='半导体研报 RAG', version='1.0.0', lifespan=lifespan)
    app.state.auth = auth
    app.state.telemetry = telemetry
    app.mount('/ui', StaticFiles(directory=Path(__file__).with_name('ui')), name='ui')
    if DIGITAL_HUMAN_DIR.exists():
        app.mount('/digital-human', StaticFiles(directory=DIGITAL_HUMAN_DIR, html=True), name='digital-human')

    @app.get('/', include_in_schema=False)
    def workbench():
        return FileResponse(Path(__file__).with_name('workbench.html'), media_type='text/html', headers=UI_CACHE_HEADERS)

    @app.get('/workbench.js', include_in_schema=False)
    def workbench_script():
        return FileResponse(Path(__file__).with_name('workbench.js'), media_type='application/javascript', headers=UI_CACHE_HEADERS)

    @app.get('/chat', include_in_schema=False)
    def general_workbench():
        return FileResponse(Path(__file__).with_name('general.html'), media_type='text/html', headers=UI_CACHE_HEADERS)

    @app.get('/general.js', include_in_schema=False)
    def general_script():
        return FileResponse(Path(__file__).with_name('general.js'), media_type='application/javascript', headers=UI_CACHE_HEADERS)

    @app.get('/library', include_in_schema=False)
    def library():
        return FileResponse(Path(__file__).with_name('library.html'), media_type='text/html', headers=UI_CACHE_HEADERS)

    @app.get('/library.js', include_in_schema=False)
    def library_script():
        return FileResponse(Path(__file__).with_name('library.js'), media_type='application/javascript', headers=UI_CACHE_HEADERS)

    @app.get('/articles', include_in_schema=False)
    def article_library():
        return FileResponse(Path(__file__).with_name('articles.html'), media_type='text/html', headers=UI_CACHE_HEADERS)

    @app.get('/articles.js', include_in_schema=False)
    def article_library_script():
        return FileResponse(Path(__file__).with_name('articles.js'), media_type='application/javascript', headers=UI_CACHE_HEADERS)

    @app.get('/agent', include_in_schema=False)
    def finance_agent_page():
        return FileResponse(Path(__file__).with_name('agent.html'), media_type='text/html', headers=UI_CACHE_HEADERS)

    @app.get('/agent.js', include_in_schema=False)
    def finance_agent_script():
        return FileResponse(Path(__file__).with_name('agent.js'), media_type='application/javascript', headers=UI_CACHE_HEADERS)

    @app.get('/agent.css', include_in_schema=False)
    def finance_agent_style():
        return FileResponse(Path(__file__).with_name('agent.css'), media_type='text/css', headers=UI_CACHE_HEADERS)

    @app.get('/auth/login', include_in_schema=False)
    async def auth_login(return_to: str = '/'):
        if auth.mode != 'oidc':
            raise HTTPException(404, 'SSO is not enabled')
        try:
            location, state_token = await auth.login(return_to)
        except (httpx.HTTPError, AuthenticationError) as exc:
            log.warning('OIDC discovery failed: %s', type(exc).__name__)
            raise HTTPException(503, '统一身份认证暂不可用') from exc
        response = Response(status_code=302, headers={'Location': location})
        cookie_secure = os.getenv('RAG_COOKIE_SECURE', 'true').lower() == 'true'
        response.set_cookie(STATE_COOKIE, state_token, max_age=600, httponly=True,
                            secure=cookie_secure, samesite='lax')
        return response

    @app.get('/auth/callback', include_in_schema=False)
    async def auth_callback(request: Request, code: str = '', state: str = ''):
        try:
            user, return_to = await auth.callback(code, state, request.cookies.get(STATE_COOKIE, ''))
        except (httpx.HTTPError, AuthenticationError) as exc:
            log.warning('OIDC callback rejected: %s', type(exc).__name__)
            raise HTTPException(401, '统一身份认证失败，请重新登录') from exc
        response = Response(status_code=302, headers={'Location': return_to})
        cookie_secure = os.getenv('RAG_COOKIE_SECURE', 'true').lower() == 'true'
        response.set_cookie(SESSION_COOKIE, auth.session_token(user), max_age=auth.session_ttl,
                            httponly=True, secure=cookie_secure, samesite='lax')
        response.delete_cookie(STATE_COOKIE)
        return response

    @app.post('/auth/logout', status_code=204)
    def auth_logout():
        response = Response(status_code=204)
        response.delete_cookie(SESSION_COOKIE)
        response.delete_cookie(STATE_COOKIE)
        return response

    @app.get('/auth/me')
    def auth_me(request: Request):
        user = request.state.user
        if user is None:
            raise HTTPException(404, 'SSO is not enabled')
        return {'subject': user.subject, 'display_name': user.display_name,
                'role': user.role, 'expires_at': user.expires_at}

    @app.post('/tasks', status_code=202)
    def create_task(body: TaskCreateRequest, request: Request):
        if request.state.user and request.state.user.role != 'admin':
            raise HTTPException(403, 'Administrator permission required')
        task, created = tasks.submit(body.type, request.state.conversation_owner, body.payload,
                                     body.idempotency_key,
                                     run_at=body.run_at.isoformat() if body.run_at else None)
        return JSONResponse(jsonable_encoder(task), status_code=202 if created else 200)

    @app.get('/tasks')
    def list_tasks(request: Request, type: str | None = None, limit: int = 50):
        if not 1 <= limit <= 100:
            raise HTTPException(422, 'Invalid limit')
        return {'tasks': tasks.store.list(request.state.conversation_owner, type, limit)}

    @app.get('/tasks/{task_id}')
    def get_task(task_id: str, request: Request):
        task = tasks.store.get(request.state.conversation_owner, task_id)
        if not task:
            raise HTTPException(404, '任务不存在')
        return task

    @app.get('/tasks/{task_id}/events')
    def get_task_events(task_id: str, request: Request, after: int = 0):
        events = tasks.store.events(request.state.conversation_owner, task_id, max(0, after))
        if events is None:
            raise HTTPException(404, '任务不存在')
        return {'events': events}

    @app.post('/tasks/{task_id}/cancel')
    def cancel_task(task_id: str, request: Request):
        task = tasks.store.cancel(request.state.conversation_owner, task_id)
        if not task:
            raise HTTPException(404, '任务不存在')
        return task

    @app.post('/tasks/{task_id}/retry', status_code=202)
    def retry_task(task_id: str, request: Request):
        try:
            task = tasks.retry(request.state.conversation_owner, task_id,
                               request.headers.get('idempotency-key'))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        if not task:
            raise HTTPException(404, '任务不存在')
        return task

    @app.get('/metrics', include_in_schema=False)
    def metrics(request: Request):
        token = os.getenv('RAG_METRICS_TOKEN', '')
        if token and not hmac.compare_digest(request.headers.get('authorization', ''), 'Bearer ' + token):
            raise HTTPException(401, 'Metrics token required')
        try:
            pending = app.state.agent_jobs.status_counts()
            for status, count in pending.items():
                telemetry.tasks.labels('agent.analysis', status).set(count)
            telemetry.oldest_task.labels('agent.analysis').set(app.state.agent_jobs.oldest_queued_seconds())
            for (task_type, status), count in tasks.store.counts().items():
                telemetry.tasks.labels(task_type, status).set(count)
                telemetry.oldest_task.labels(task_type).set(tasks.store.oldest_queued_seconds(task_type))
            uploads_by_status = Counter(job['status'] for job in app.state.uploads.store.list())
            for status, count in uploads_by_status.items():
                telemetry.tasks.labels('document.upload', status).set(count)
        except Exception:
            log.exception('Failed to refresh task metrics')
        return Response(telemetry.render(), media_type='text/plain; version=0.0.4; charset=utf-8')

    @app.post('/agent/jobs', status_code=202)
    def submit_agent_job(payload: AgentAnalysisRequest, request: Request):
        if payload.trade_date > date.today():
            raise HTTPException(422, '分析日期不能晚于今天')
        analysts = list(dict.fromkeys(payload.analysts))
        try:
            stock = app.state.agent_jobs.resolve_stock(payload.ticker)
            return app.state.agent_jobs.submit(request.state.conversation_owner,
                stock['ticker'], payload.trade_date.isoformat(), analysts,
                request.headers.get('idempotency-key'))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except OverflowError as exc:
            raise HTTPException(429, str(exc), headers={'Retry-After': '30'}) from exc
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.get('/agent/health')
    def agent_health():
        return {'ready': app.state.agent_jobs.ready()}

    @app.get('/agent/stocks')
    def agent_stocks(q: str = ''):
        try:
            return {'matches': app.state.agent_jobs.stocks(q)}
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.get('/agent/latest-date/{ticker}')
    def agent_latest_date(ticker: str):
        if not re.fullmatch(r'\d{6}', ticker):
            raise HTTPException(422, '请输入六位 A 股代码')
        try:
            return app.state.agent_jobs.latest_date(ticker)
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.get('/agent/jobs')
    def list_agent_jobs(request: Request):
        return app.state.agent_jobs.list(request.state.conversation_owner)

    @app.get('/agent/jobs/{job_id}')
    def get_agent_job(job_id: str, request: Request):
        if not re.fullmatch(r'[0-9a-f-]{32,36}', job_id):
            raise HTTPException(404, '任务不存在')
        job = app.state.agent_jobs.get(request.state.conversation_owner, job_id)
        if not job:
            raise HTTPException(404, '任务不存在')
        return job

    @app.get('/agent/jobs/{job_id}/events')
    async def stream_agent_events(job_id: str, request: Request, after: int = 0):
        if not re.fullmatch(r'[0-9a-f-]{32,36}', job_id):
            raise HTTPException(404, '任务不存在')
        owner = request.state.conversation_owner
        if not app.state.agent_jobs.get(owner, job_id):
            raise HTTPException(404, '任务不存在')
        try:
            cursor = max(after, int(request.headers.get('last-event-id', '0')))
        except ValueError:
            cursor = max(after, 0)

        async def event_stream():
            nonlocal cursor
            idle = 0
            while not await request.is_disconnected():
                events = app.state.agent_jobs.events_since(owner, job_id, cursor)
                for event in events or []:
                    cursor = event['id']
                    yield f'id: {cursor}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n'
                job = app.state.agent_jobs.get(owner, job_id)
                if job['status'] in ('completed', 'failed', 'cancelled') and not events:
                    return
                if not events:
                    idle += 1
                    if idle >= 20:
                        yield ': keepalive\n\n'
                        idle = 0
                else:
                    idle = 0
                await asyncio.sleep(0.5)
        return StreamingResponse(event_stream(), media_type='text/event-stream',
                                 headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

    @app.post('/agent/jobs/{job_id}/cancel')
    def cancel_agent_job(job_id: str, request: Request):
        job = app.state.agent_jobs.cancel(request.state.conversation_owner, job_id)
        if not job:
            raise HTTPException(404, '任务不存在')
        return job

    @app.post('/agent/jobs/{job_id}/retry', status_code=202)
    def retry_agent_job(job_id: str, request: Request):
        job = app.state.agent_jobs.get(request.state.conversation_owner, job_id)
        if not job:
            raise HTTPException(404, '任务不存在')
        if job['status'] not in ('failed', 'cancelled'):
            raise HTTPException(409, '只有失败或已中断的任务可以重试')
        try:
            if tasks.distributed:
                return app.state.agent_jobs.retry(request.state.conversation_owner, job_id,
                                                  request.headers.get('idempotency-key'))
            return app.state.agent_jobs.submit(request.state.conversation_owner,
                                               job['ticker'], job['trade_date'], job['analysts'])
        except OverflowError as exc:
            raise HTTPException(429, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc

    async def rss_proxy(request: Request, upstream_path: str):
        query = ('?' + request.url.query) if request.url.query else ''
        headers = {key: value for key, value in request.headers.items()
                   if key.lower() in {'authorization', 'content-type', 'accept'}}
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=60) as client:
                upstream = await client.request(request.method,
                    f'{RSS_SERVICE_URL}/{upstream_path}{query}', headers=headers,
                    content=await request.body())
        except httpx.HTTPError as exc:
            raise HTTPException(502, '公众号订阅管理服务不可用') from exc
        response_headers = {key: value for key, value in upstream.headers.items()
                            if key.lower() in {'content-type', 'cache-control'}}
        return Response(upstream.content, status_code=upstream.status_code, headers=response_headers)

    @app.get('/rss-admin', include_in_schema=False)
    def rss_admin_redirect():
        return Response(status_code=307, headers={'Location': '/dash'})

    @app.api_route('/dash', methods=['GET'], include_in_schema=False)
    async def rss_dashboard(request: Request):
        return await rss_proxy(request, 'dash')

    @app.api_route('/dash/{path:path}', methods=['GET'], include_in_schema=False)
    async def rss_dashboard_assets(path: str, request: Request):
        return await rss_proxy(request, f'dash/{path}')

    @app.api_route('/trpc/{path:path}', methods=['GET', 'POST'], include_in_schema=False)
    async def rss_trpc(path: str, request: Request):
        return await rss_proxy(request, f'trpc/{path}')

    @app.api_route('/feeds', methods=['GET'], include_in_schema=False)
    async def rss_feeds(request: Request):
        return await rss_proxy(request, 'feeds')

    @app.api_route('/feeds/{path:path}', methods=['GET'], include_in_schema=False)
    async def rss_feed(path: str, request: Request):
        return await rss_proxy(request, f'feeds/{path}')

    @app.get('/article-api/sources')
    def article_sources():
        return {'sources': app.state.articles.sources()}

    @app.get('/article-api/items')
    def article_items(source_id: str = '', query: str = '', unread: bool = False, limit: int = 100):
        return app.state.articles.list(source_id=source_id, query=query, unread=unread, limit=limit)

    @app.put('/article-api/sources/{source_id}/feed')
    def article_source_feed(source_id: str, body: ArticleFeedRequest):
        try:
            return app.state.articles.configure_feed(source_id, body.feed_url)
        except KeyError as exc:
            raise HTTPException(404, '公众号不存在') from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post('/article-api/sync')
    def article_sync(body: ArticleSyncRequest, request: Request):
        if tasks.distributed:
            task, created = tasks.submit('article.sync', request.state.conversation_owner,
                body.model_dump(), request.headers.get('idempotency-key'))
            return JSONResponse(jsonable_encoder(task), status_code=202 if created else 200)
        try:
            return app.state.articles.sync(body.source_id)
        except KeyError as exc:
            raise HTTPException(404, '公众号不存在') from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except httpx.TimeoutException as exc:
            raise HTTPException(504, '公众号订阅服务响应超时') from exc

    def sync_article_task(payload, emit, cancelled):
        if cancelled():
            return {'cancelled': True}
        emit('progress', {'stage': 'fetch'})
        return app.state.articles.sync(str(payload.get('source_id', '')))

    tasks.register('article.sync', sync_article_task)

    @app.get('/article-api/items/{article_id}')
    def article_item(article_id: str):
        try:
            article = app.state.articles.get(article_id, mark_read=True)
        except KeyError as exc:
            raise HTTPException(404, '文章不存在') from exc
        article.pop('content_html', None)
        return article

    @app.get('/article-api/items/{article_id}/content', include_in_schema=False)
    def article_content(article_id: str):
        try:
            document = app.state.articles.content_document(article_id)
        except KeyError as exc:
            raise HTTPException(404, '文章不存在') from exc
        return Response(document, media_type='text/html', headers={
            'Content-Security-Policy': "default-src 'none'; img-src https: data:; style-src 'self' 'unsafe-inline'; base-uri 'none'; form-action 'none'",
            'Cache-Control': 'private, max-age=300',
            'Referrer-Policy': 'no-referrer',
        })

    @app.post('/article-api/import', status_code=201)
    def article_import(body: ArticleImportRequest):
        try:
            article = app.state.articles.import_url(body.source_id, body.url)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except httpx.TimeoutException as exc:
            raise HTTPException(504, '微信文章读取超时，请稍后重试') from exc
        except httpx.HTTPError as exc:
            log.warning('Wechat article import failed: %s', type(exc).__name__)
            raise HTTPException(502, '无法读取微信文章，请确认链接可以公开访问') from exc
        article.pop('content_html', None)
        return article

    @app.get('/airi/v1/models', include_in_schema=False)
    async def airi_models(request: Request):
        """Expose the private vLLM model catalog to the same-origin AIRI app."""
        key_file = Path(config['llm_api_key_file'])
        key = os.environ.get('VLLM_API_KEY') or (key_file.read_text().strip() if key_file.exists() else 'EMPTY')
        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
            upstream = await client.get(config['llm_base_url'].rstrip('/') + '/models',
                                        headers={'Authorization': 'Bearer ' + key})
        return Response(content=upstream.content, status_code=upstream.status_code,
                        media_type='application/json')

    @app.post('/airi/v1/chat/completions', include_in_schema=False)
    async def airi_chat_completions(request: Request):
        """Proxy AIRI's OpenAI-compatible chat stream without exposing vLLM."""
        key_file = Path(config['llm_api_key_file'])
        key = os.environ.get('VLLM_API_KEY') or (key_file.read_text().strip() if key_file.exists() else 'EMPTY')
        try:
            payload = await request.json()
        except (json.JSONDecodeError, TypeError):
            return JSONResponse({'detail': 'Invalid JSON body'}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({'detail': 'JSON body must be an object'}, status_code=400)

        # AIRI sends the assistant token stream directly into its speech queue.
        # Keep Qwen's private reasoning out of both the visible reply and TTS.
        template_kwargs = payload.setdefault('chat_template_kwargs', {})
        if not isinstance(template_kwargs, dict):
            return JSONResponse({'detail': 'chat_template_kwargs must be an object'}, status_code=400)
        template_kwargs.setdefault('enable_thinking', False)
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        is_streaming = bool(payload.get('stream'))
        headers = {'Authorization': 'Bearer ' + key, 'Content-Type': request.headers.get('content-type', 'application/json')}
        upstream_url = config['llm_base_url'].rstrip('/') + '/chat/completions'

        async def relay():
            timeout = httpx.Timeout(config.get('llm_timeout', 180), connect=10)
            async with httpx.AsyncClient(trust_env=False, timeout=timeout) as client:
                async with client.stream('POST', upstream_url, content=body, headers=headers) as upstream:
                    async for chunk in upstream.aiter_raw():
                        yield chunk

        return StreamingResponse(relay(), media_type='text/event-stream' if is_streaming else 'application/json')

    @app.post('/airi/tts/v1/audio/speech', include_in_schema=False)
    async def airi_tts_speech(request: Request):
        """Proxy AIRI speech synthesis to the private Mandarin Kokoro service."""
        try:
            payload = await request.json()
        except (json.JSONDecodeError, TypeError):
            return JSONResponse({'detail': 'Invalid JSON body'}, status_code=400)
        # WAV keeps the private service independent of optional ffmpeg/PyAV
        # codecs and is decoded natively by AIRI's AudioContext.
        payload['response_format'] = 'wav'
        async with httpx.AsyncClient(trust_env=False, timeout=180) as client:
            upstream = await client.post(
                'http://127.0.0.1:191/v1/audio/speech',
                json=payload,
            )
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get('content-type', 'audio/wav'),
        )

    @app.middleware('http')
    async def authorization(request: Request, call_next):
        request_id = telemetry.request_id(request.headers.get('x-request-id', ''))
        token = request_id_var.set(request_id)
        started = time.monotonic()
        telemetry.http_inflight.inc()
        response = None
        async def finish(value):
            route = request.scope.get('route')
            route_name = getattr(route, 'path', None) or 'unmatched'
            telemetry.http_requests.labels(request.method, route_name, str(value.status_code)).inc()
            telemetry.http_duration.labels(request.method, route_name).observe(time.monotonic() - started)
            value.headers['X-Request-ID'] = request_id
            return value
        try:
            response = await _authorize(request, call_next)
            return await finish(response)
        finally:
            telemetry.http_inflight.dec()
            request_id_var.reset(token)

    async def _authorize(request: Request, call_next):
        metrics_token = os.getenv('RAG_METRICS_TOKEN', '')
        metrics_authorized = (request.url.path == '/metrics' and metrics_token and
                              hmac.compare_digest(request.headers.get('authorization', ''),
                                                  'Bearer ' + metrics_token))
        public = request.url.path in {'/health', '/auth/login', '/auth/callback'} or metrics_authorized
        user = None
        if auth.mode != 'legacy':
            try:
                user = auth.authenticate(request)
            except AuthenticationError:
                user = None
            if not public and user is None:
                telemetry.auth_events.labels('authenticate', 'failure').inc()
                accepts_html = 'text/html' in request.headers.get('accept', '') and request.method == 'GET'
                if accepts_html:
                    target = request.url.path + (('?' + request.url.query) if request.url.query else '')
                    return Response(status_code=302, headers={'Location': '/auth/login?' + urlencode({'return_to': target})})
                response = JSONResponse({'detail': 'Authentication required'}, status_code=401)
                if request.cookies.get(SESSION_COOKIE):
                    response.delete_cookie(SESSION_COOKIE)
                return response
            if user and admin_required(request.method, request.url.path) and user.role != 'admin':
                telemetry.auth_events.labels('authorize', 'denied').inc()
                return JSONResponse({'detail': 'Administrator permission required'}, status_code=403)
            if user and request.method in {'POST', 'PUT', 'PATCH', 'DELETE'} and auth.mode == 'oidc':
                origin = request.headers.get('origin', '')
                public_origin = os.getenv('RAG_PUBLIC_ORIGIN', str(request.base_url)).rstrip('/')
                if origin and origin.rstrip('/') != public_origin:
                    return JSONResponse({'detail': 'Cross-origin write rejected'}, status_code=403)
            request.state.user = user
            request.state.conversation_owner = user.subject if user else ''
            if user:
                telemetry.auth_events.labels('authenticate', 'success').inc()
        else:
            request.state.user = None
        expected = os.environ.get('RAG_API_KEY')
        if auth.mode == 'legacy' and expected and request.url.path != '/health':
            supplied = request.headers.get('authorization', '')
            if not hmac.compare_digest(supplied, 'Bearer ' + expected):
                return JSONResponse({'detail': 'Unauthorized'}, status_code=401)
        if request.method in ('POST', 'PUT') and request.url.path.startswith(('/uploads', '/media')):
            length = request.headers.get('content-length')
            if length is None:
                return JSONResponse({'detail': 'Content-Length required'}, status_code=411)
            try:
                size = int(length)
            except ValueError:
                return JSONResponse({'detail': 'Invalid Content-Length'}, status_code=400)
            maximum = (config.get('upload_max_mb', 50) + 1) * 1024 * 1024
            if size < 0 or size > maximum:
                return JSONResponse({'detail': 'Upload exceeds size limit'}, status_code=413)
        new_owner = False
        if auth.mode == 'legacy':
            owner = request.cookies.get('rag_client', '')
            new_owner = not re.fullmatch(r'[0-9a-f]{64}', owner)
            if new_owner:
                owner = secrets.token_hex(32)
            request.state.conversation_owner = owner
        response = await call_next(request)
        if request.url.path in ('/digital-human', '/digital-human/') or request.url.path.endswith('.html'):
            response.headers['Cache-Control'] = 'no-cache, must-revalidate'
        if auth.mode == 'legacy' and new_owner:
            response.set_cookie('rag_client', owner, max_age=365 * 86400, httponly=True,
                                samesite='strict', secure=request.url.scheme == 'https')
        return response

    @app.exception_handler(ConversationConflict)
    async def conversation_conflict(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=409)

    @contextmanager
    def conversation_access():
        try:
            yield
        except KeyError as exc:
            raise HTTPException(404, '会话不存在') from exc

    @contextmanager
    def admitted():
        if not app.state.gate.acquire(blocking=False):
            raise HTTPException(503, 'Model is busy; retry shortly', headers={'Retry-After': '3'})
        try:
            with telemetry.model('generation'):
                yield
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except httpx.TimeoutException as exc:
            raise HTTPException(504, 'Generation service timed out') from exc
        except httpx.HTTPError as exc:
            log.warning('Generation service failure: %s', type(exc).__name__)
            raise HTTPException(502, 'Generation service unavailable') from exc
        finally:
            app.state.gate.release()

    @app.get('/health')
    def health():
        try:
            llm = app.state.answerer.llm_health()
            return {'status': 'ok', 'index': app.state.retriever.manifest['generation'], 'llm': llm}
        except (httpx.HTTPError, RuntimeError, KeyError):
            return JSONResponse({'status': 'degraded', 'retrieval': 'ready', 'llm': 'unavailable'}, status_code=503)

    @app.get('/stats')
    def stats():
        return {**app.state.retriever.manifest, 'retrieval_parameters': {
            key: config.get(key) for key in ('retrieval_profile', 'candidate_k', 'rerank_k',
                'query_expansion', 'rerank_body_only', 'rerank_rank_fusion', 'source_routing', 'max_per_doc')}}

    @app.post('/media', status_code=201)
    def upload_media(request: Request, file: UploadFile = File(...)):
        try:
            return app.state.media.save(request.state.conversation_owner, file.file,
                                        file.filename or 'media', file.content_type or '')
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            file.file.close()

    @app.get('/media/{media_id}')
    def get_media(media_id: str, request: Request):
        try:
            metadata, path = app.state.media.get(request.state.conversation_owner, media_id)
        except KeyError as exc:
            raise HTTPException(404, '媒体文件不存在') from exc
        return FileResponse(path, media_type=metadata['mime'], filename=metadata['name'],
                            content_disposition_type='inline')

    @app.get('/documents')
    def documents(offset: int = 0, limit: int = 50):
        if offset < 0 or not 1 <= limit <= 400:
            raise HTTPException(422, 'Invalid pagination')
        rows = {}
        for chunk in app.state.retriever.chunks:
            rows.setdefault(chunk['doc_id'], {key: chunk[key] for key in
                ('doc_id', 'title', 'org', 'publish_date', 'file')})
        ordered = sorted(rows.values(), key=lambda row: (row['publish_date'], row['doc_id']), reverse=True)
        return {'total': len(ordered), 'documents': ordered[offset:offset + limit],
                'organizations': dict(Counter(row['org'] for row in ordered))}

    @app.post('/search')
    def search(body: SearchRequest):
        with admitted():
            result = app.state.retriever.search(body.query,
                body.filters.model_dump(mode='json', exclude_none=True), body.top_k, body.mode)
            result['hits'] = [public_source(hit) for hit in result['hits']]
            return result

    @app.post('/ask')
    def ask(body: AskRequest, request: Request):
        if not body.query.strip() and not body.attachments:
            raise HTTPException(422, '问题不能为空')
        query = body.query.strip() or '请结合知识库分析附件内容。'
        try:
            attachments = app.state.media.resolve(request.state.conversation_owner,
                                                  [item.model_dump() for item in body.attachments])
        except (ValueError, KeyError) as exc:
            raise HTTPException(422, str(exc) or '媒体文件不存在') from exc
        public_attachments = [app.state.media.public(item) for item in attachments]
        if body.conversation_id is None:
            if body.request_id is not None or body.expected_revision is not None:
                raise HTTPException(422, 'request_id 和 expected_revision 需要 conversation_id')
            with admitted():
                args = (query, body.filters.model_dump(mode='json', exclude_none=True) if body.filters else {}, body.top_k)
                if attachments:
                    return (app.state.answerer.ask(*args, body.answer_policy, attachments)
                            if body.answer_policy is not None else app.state.answerer.ask(*args, attachments=attachments))
                return (app.state.answerer.ask(*args, body.answer_policy) if body.answer_policy is not None
                        else app.state.answerer.ask(*args))
        if body.request_id is None or body.expected_revision is None:
            raise HTTPException(422, '会话请求需要 request_id 和 expected_revision')
        owner, cid = request.state.conversation_owner, body.conversation_id.hex
        store = app.state.conversations
        original = {'query': query, 'attachments': public_attachments,
            'filters': body.filters.model_dump(mode='json', exclude_none=True) if body.filters is not None else None,
            'top_k': body.top_k, 'answer_policy': body.answer_policy}
        with conversation_access():
            replay = store.replay(owner, cid, body.request_id.hex, original, mode='rag')
            if replay is not None:
                return replay
            with admitted():
                history = store.history(owner, cid, mode='rag')
                if history['revision'] != body.expected_revision:
                    raise ConversationConflict('会话已在其他页面更新，请重新加载后再发送')
                if len(history['turns']) >= store.max_turns:
                    raise ConversationConflict('本会话已达到轮数上限，请新建会话')
                filters = history['filters'] if original['filters'] is None else original['filters']
                from semi_rag.retrieve import validate_filters
                validate_filters(filters)
                if attachments:
                    result = (answer_turn(app.state.answerer, original['query'], history['turns'], filters,
                        body.top_k, body.answer_policy, attachments) if body.answer_policy is not None else
                        answer_turn(app.state.answerer, original['query'], history['turns'], filters, body.top_k,
                                    attachments=attachments))
                else:
                    result = (answer_turn(app.state.answerer, original['query'], history['turns'], filters,
                        body.top_k, body.answer_policy) if body.answer_policy is not None else
                        answer_turn(app.state.answerer, original['query'], history['turns'], filters, body.top_k))
                return store.append(owner, cid, history['revision'], body.request_id.hex, original, result,
                                    filters, mode='rag')

    @app.post('/conversations', status_code=201)
    def new_conversation(body: ConversationCreate, request: Request):
        return app.state.conversations.create(request.state.conversation_owner, body.title, mode='rag')

    @app.get('/conversations')
    def conversations(request: Request, offset: int = 0, limit: int = 50):
        if offset < 0 or not 1 <= limit <= 100:
            raise HTTPException(422, 'Invalid pagination')
        return app.state.conversations.list(request.state.conversation_owner, offset, limit, mode='rag')

    @app.get('/conversations/{conversation_id}')
    def conversation(conversation_id: UUID, request: Request):
        with conversation_access():
            return app.state.conversations.history(request.state.conversation_owner, conversation_id.hex, mode='rag')

    @app.patch('/conversations/{conversation_id}')
    def rename_conversation(conversation_id: UUID, body: ConversationChange, request: Request):
        with conversation_access():
            return app.state.conversations.change(request.state.conversation_owner, conversation_id.hex,
                                                  body.expected_revision, title=body.title, mode='rag')

    @app.delete('/conversations/{conversation_id}')
    def delete_conversation(conversation_id: UUID, request: Request, expected_revision: int):
        with conversation_access():
            return app.state.conversations.change(request.state.conversation_owner, conversation_id.hex,
                                                  expected_revision, delete=True, mode='rag')

    @app.post('/general/ask')
    def general_ask(body: GeneralAskRequest, request: Request):
        query = body.query.strip()
        if not query and not body.attachments:
            raise HTTPException(422, '问题不能为空')
        query = query or '请分析附件内容。'
        try:
            attachments = app.state.media.resolve(request.state.conversation_owner,
                                                  [item.model_dump() for item in body.attachments])
        except (ValueError, KeyError) as exc:
            raise HTTPException(422, str(exc) or '媒体文件不存在') from exc
        public_attachments = [app.state.media.public(item) for item in attachments]
        if body.conversation_id is None:
            if body.request_id is not None or body.expected_revision is not None:
                raise HTTPException(422, 'request_id 和 expected_revision 需要 conversation_id')
            with admitted():
                return (answer_general(app.state.answerer, query, [], attachments) if attachments
                        else answer_general(app.state.answerer, query, []))
        if body.request_id is None or body.expected_revision is None:
            raise HTTPException(422, '会话请求需要 request_id 和 expected_revision')
        owner, cid = request.state.conversation_owner, body.conversation_id.hex
        store = app.state.conversations
        original = {'query': query, 'attachments': public_attachments}
        with conversation_access():
            replay = store.replay(owner, cid, body.request_id.hex, original, mode='general')
            if replay is not None:
                return replay
            with admitted():
                history = store.history(owner, cid, mode='general')
                if history['revision'] != body.expected_revision:
                    raise ConversationConflict('会话已在其他页面更新，请重新加载后再发送')
                if len(history['turns']) >= store.max_turns:
                    raise ConversationConflict('本会话已达到轮数上限，请新建会话')
                result = (answer_general(app.state.answerer, query, history['turns'], attachments) if attachments
                          else answer_general(app.state.answerer, query, history['turns']))
                return store.append(owner, cid, history['revision'], body.request_id.hex,
                                    original, result, {}, mode='general')

    @app.post('/general/conversations', status_code=201)
    def new_general_conversation(body: ConversationCreate, request: Request):
        return app.state.conversations.create(request.state.conversation_owner, body.title, mode='general')

    @app.get('/general/conversations')
    def general_conversations(request: Request, offset: int = 0, limit: int = 50):
        if offset < 0 or not 1 <= limit <= 100:
            raise HTTPException(422, 'Invalid pagination')
        return app.state.conversations.list(request.state.conversation_owner, offset, limit, mode='general')

    @app.get('/general/conversations/{conversation_id}')
    def general_conversation(conversation_id: UUID, request: Request):
        with conversation_access():
            return app.state.conversations.history(request.state.conversation_owner,
                                                   conversation_id.hex, mode='general')

    @app.patch('/general/conversations/{conversation_id}')
    def rename_general_conversation(conversation_id: UUID, body: ConversationChange, request: Request):
        with conversation_access():
            return app.state.conversations.change(request.state.conversation_owner, conversation_id.hex,
                                                  body.expected_revision, title=body.title, mode='general')

    @app.delete('/general/conversations/{conversation_id}')
    def delete_general_conversation(conversation_id: UUID, request: Request, expected_revision: int):
        with conversation_access():
            return app.state.conversations.change(request.state.conversation_owner, conversation_id.hex,
                expected_revision, delete=True, mode='general')

    @app.post('/general/ask/stream')
    def general_ask_stream(body: GeneralAskRequest, request: Request):
        query = body.query.strip()
        if not query and not body.attachments:
            raise HTTPException(422, '问题不能为空')
        query = query or '请分析附件内容。'
        owner = request.state.conversation_owner
        try:
            attachments = app.state.media.resolve(owner, [item.model_dump() for item in body.attachments])
        except (ValueError, KeyError) as exc:
            raise HTTPException(422, str(exc) or '媒体文件不存在') from exc
        public_attachments = [app.state.media.public(item) for item in attachments]
        cid = body.conversation_id.hex if body.conversation_id is not None else None
        store = app.state.conversations
        original = {'query': query, 'attachments': public_attachments}
        history = []
        if cid is None:
            if body.request_id is not None or body.expected_revision is not None:
                raise HTTPException(422, 'request_id 和 expected_revision 需要 conversation_id')
        else:
            if body.request_id is None or body.expected_revision is None:
                raise HTTPException(422, '会话请求需要 request_id 和 expected_revision')
            with conversation_access():
                replay = store.replay(owner, cid, body.request_id.hex, original, mode='general')
                if replay is not None:
                    line = json.dumps({'type': 'done', 'response': replay}, ensure_ascii=False) + '\n'
                    return Response(line, media_type='application/x-ndjson')
                history = store.history(owner, cid, mode='general')
            if history['revision'] != body.expected_revision:
                raise ConversationConflict('会话已在其他页面更新，请重新加载后再发送')
            if len(history['turns']) >= store.max_turns:
                raise ConversationConflict('本会话已达到轮数上限，请新建会话')
        if not app.state.gate.acquire(blocking=False):
            raise HTTPException(503, 'Model is busy; retry shortly', headers={'Retry-After': '3'})

        def event_stream():
            try:
                completion = (stream_general(app.state.answerer, query,
                                             history['turns'] if cid is not None else [], attachments)
                              if attachments else stream_general(app.state.answerer, query,
                                                                 history['turns'] if cid is not None else []))
                while True:
                    try:
                        delta = next(completion)
                    except StopIteration as stopped:
                        result = stopped.value
                        break
                    yield json.dumps({'type': 'delta', 'text': delta}, ensure_ascii=False) + '\n'
                if cid is not None:
                    result = store.append(owner, cid, history['revision'], body.request_id.hex,
                                          original, result, {}, mode='general')
                yield json.dumps({'type': 'done', 'response': result}, ensure_ascii=False) + '\n'
            except ValueError as exc:
                yield json.dumps({'type': 'error', 'detail': str(exc)}, ensure_ascii=False) + '\n'
            except httpx.TimeoutException:
                yield json.dumps({'type': 'error', 'detail': 'Generation service timed out'}, ensure_ascii=False) + '\n'
            except httpx.HTTPError as exc:
                log.warning('Streaming generation service failure: %s', type(exc).__name__)
                yield json.dumps({'type': 'error', 'detail': 'Generation service unavailable'}, ensure_ascii=False) + '\n'
            except ConversationConflict as exc:
                yield json.dumps({'type': 'error', 'detail': str(exc), 'status': 409}, ensure_ascii=False) + '\n'

        return StreamingResponse(event_stream(), media_type='application/x-ndjson',
            headers={'Cache-Control': 'no-cache, no-transform', 'X-Accel-Buffering': 'no'},
            background=BackgroundTask(app.state.gate.release))

    def source_file(chunk_id: str, figure: bool):
        chunk = app.state.retriever.by_id.get(chunk_id)
        if not chunk:
            raise HTTPException(404, 'Unknown source')
        if chunk.get('upload_id'):
            root = upload_dir(config, chunk['upload_id'])
            name = chunk.get('figure') if figure else 'source.pdf'
        else:
            root = Path(config['data_dir']) / 'raw_md/figures' if figure else Path(config['pdf_dir'])
            name = chunk.get('figure') if figure else chunk['file']
        if not name:
            raise HTTPException(404, 'No image for this source')
        path = (root / name).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            raise HTTPException(404, 'Source file not found')
        return FileResponse(path, media_type='image/png' if figure else 'application/pdf',
            filename=path.name, content_disposition_type='inline')

    @app.get('/sources/{chunk_id}/pdf')
    def source_pdf(chunk_id: str):
        return source_file(chunk_id, False)

    @app.get('/sources/{chunk_id}/figure')
    def source_figure(chunk_id: str):
        return source_file(chunk_id, True)

    @app.get('/sources/{chunk_id}/page')
    def source_page(chunk_id: str):
        chunk = app.state.retriever.by_id.get(chunk_id)
        if not chunk or not chunk.get('upload_id') or not chunk.get('page'):
            raise HTTPException(404, 'Source coordinates unavailable')
        pdf = upload_dir(config, chunk['upload_id']) / 'source.pdf'
        return Response(render_page(pdf, chunk['page'], [node['bbox'] for node in chunk['source_blocks']]),
                        media_type='image/png')

    @app.get('/uploads')
    def uploads():
        return {'uploads': app.state.uploads.store.list(), 'max_mb': config.get('upload_max_mb', 50),
                'max_pages': config.get('upload_max_pages', 200)}

    def mark_imported(reports):
        known, known_keys = _known_reports(app)
        for report in reports:
            report['imported'] = report['external_id'] in known or _report_key(report) in known_keys
        return reports

    @app.get('/report-sources')
    def report_source_list():
        return {'sources': [{'id': key, **source} for key, source in report_sources.SOURCES.items()],
                'distribution': '东方财富公开研报平台'}

    @app.get('/report-sources/{source_id}/reports')
    def report_source_reports(source_id: str, limit: int = 50):
        if source_id not in report_sources.SOURCES:
            raise HTTPException(404, '研报频道不存在')
        try:
            reports = mark_imported(report_sources.discover(source_id, min(max(limit, 1), 100)))
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(502, f'研报列表获取失败：{exc}') from exc
        return {'source': source_id, 'reports': reports,
                'new_count': sum(not report['imported'] for report in reports)}

    @app.get('/eastmoney/reports')
    def eastmoney_reports(industry_code: str = '1036', limit: int = 50):
        """Retain the original industry endpoint for existing clients."""
        try:
            reports = mark_imported(eastmoney.discover(industry_code, min(limit, 100)))
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(502, f'东方财富研报列表获取失败：{exc}') from exc
        return {'source': 'eastmoney', 'industry_code': industry_code, 'reports': reports,
                'new_count': sum(not report['imported'] for report in reports)}

    def import_reports(reports, selected, visual, owner='system'):
        selected = set(selected)
        by_id = {report['external_id']: report for report in reports}
        known, known_keys = _known_reports(app)
        known_hashes = _known_pdf_hashes(app)
        imported, skipped = [], []
        failed = [{'external_id': value, 'error': '所选研报不在当前最新列表中'}
                  for value in sorted(selected - by_id.keys())]
        max_pages = config.get('upload_max_pages', 200)
        max_bytes = config.get('upload_max_mb', 50) * 1024 * 1024
        for report in reports:
            if report['external_id'] not in selected:
                continue
            if report['external_id'] in known or _report_key(report) in known_keys:
                skipped.append(report['external_id'])
                continue
            try:
                if report['pages'] > max_pages:
                    raise ValueError(f'报告共 {report["pages"]} 页，超过 {max_pages} 页限制')
                if report['size_kb'] * 1024 > max_bytes:
                    raise ValueError(f'报告大小超过 {config.get("upload_max_mb", 50)} MB 限制')
                data, pdf_url = eastmoney.download(report)
                pdf_hash = hashlib.sha256(data).hexdigest()
                if pdf_hash in known_hashes:
                    skipped.append(report['external_id'])
                    continue
                metadata = ReportMetadata(
                    title=report['title'], org=report['org'] or '东方财富研报',
                    publish_date=date.fromisoformat(report['publish_date']),
                    industry=report.get('industry', ''), external_id=report['external_id'],
                    source_url=report['info_url'], source_pdf_url=pdf_url,
                )
                job = app.state.uploads.submit(io.BytesIO(data), f"{report['external_id']}.pdf",
                                                metadata.model_dump(mode='json'), visual, auto_publish=True,
                                                owner=owner)
                if job.get('duplicate'):
                    skipped.append(report['external_id'])
                    continue
                imported.append({'external_id': report['external_id'], 'job': job})
                known.add(report['external_id'])
                known_keys.add(_report_key(report))
                known_hashes.add(pdf_hash)
            except (httpx.HTTPError, ValueError) as exc:
                failed.append({'external_id': report['external_id'], 'error': str(exc)})
        return {'imported': imported, 'skipped': skipped, 'failed': failed}

    @app.post('/report-sources/{source_id}/import', status_code=202)
    def report_source_import(source_id: str, body: ReportSourceImportRequest, request: Request):
        if source_id not in report_sources.SOURCES:
            raise HTTPException(404, '研报频道不存在')
        if tasks.distributed:
            task, created = tasks.submit('report.sync', request.state.conversation_owner,
                {'source_id': source_id, '_owner': request.state.conversation_owner, **body.model_dump()},
                request.headers.get('idempotency-key'))
            return JSONResponse(jsonable_encoder(task), status_code=202 if created else 200)
        try:
            reports = report_sources.discover(source_id, body.limit)
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(502, f'研报列表获取失败：{exc}') from exc
        return import_reports(reports, body.external_ids, body.visual)

    @app.post('/eastmoney/import', status_code=202)
    def eastmoney_import(body: EastmoneyImportRequest, request: Request):
        if tasks.distributed:
            task, created = tasks.submit('report.sync', request.state.conversation_owner,
                {'source_id': 'eastmoney', '_owner': request.state.conversation_owner, **body.model_dump()},
                request.headers.get('idempotency-key'))
            return JSONResponse(jsonable_encoder(task), status_code=202 if created else 200)
        try:
            reports = eastmoney.discover(body.industry_code, body.limit)
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(502, f'东方财富研报列表获取失败：{exc}') from exc
        return import_reports(reports, body.external_ids, body.visual)

    def sync_reports_task(payload, emit, cancelled):
        emit('progress', {'stage': 'discover', 'source_id': payload['source_id']})
        if payload['source_id'] == 'eastmoney':
            reports = eastmoney.discover(payload.get('industry_code', '1036'), payload.get('limit', 50))
        else:
            reports = report_sources.discover(payload['source_id'], payload.get('limit', 50))
        if cancelled():
            return {'cancelled': True}
        emit('progress', {'stage': 'import', 'count': len(payload['external_ids'])})
        return import_reports(reports, payload['external_ids'], payload.get('visual', False),
                              owner=payload.get('_owner', 'system'))

    tasks.register('report.sync', sync_reports_task)

    def get_job(upload_id):
        job = app.state.uploads.store.get(upload_id)
        if not job:
            raise HTTPException(404, 'Upload not found')
        return job

    @app.post('/uploads', status_code=202)
    def upload(request: Request, file: UploadFile = File(...), title: str = Form(..., min_length=1, max_length=200),
               org: str = Form(..., min_length=1, max_length=100), publish_date: date = Form(...),
               visual: bool = Form(True)):
        try:
            metadata = ReportMetadata(title=title, org=org, publish_date=publish_date)
            return app.state.uploads.submit(file.file, file.filename or 'report.pdf',
                metadata.model_dump(mode='json'), visual, owner=request.state.conversation_owner)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            file.file.close()

    @app.get('/uploads/{upload_id}')
    def upload_status(upload_id: str):
        return get_job(upload_id)

    @app.get('/uploads/{upload_id}/pages/{page_number}')
    def upload_page(upload_id: str, page_number: int):
        import json
        job = get_job(upload_id)
        if job['status'] not in ('review', 'queued_index', 'indexing', 'index_failed', 'indexed'):
            raise HTTPException(409, 'Parsing has not finished')
        doc = json.loads((upload_dir(config, upload_id) / 'document.json').read_text())
        if not 1 <= page_number <= len(doc['pages']):
            raise HTTPException(404, 'Page not found')
        return {'job': job, 'page': doc['pages'][page_number - 1],
                'excluded_pages': [p['page'] for p in doc['pages'] if p['excluded']]}

    @app.get('/uploads/{upload_id}/pdf')
    def upload_pdf(upload_id: str):
        job = get_job(upload_id)
        return FileResponse(upload_dir(config, upload_id) / 'source.pdf', media_type='application/pdf',
                            filename=job['filename'], content_disposition_type='inline')

    @app.get('/uploads/{upload_id}/pages/{page_number}/image')
    def upload_image(upload_id: str, page_number: int, block_id: str | None = None):
        result = upload_page(upload_id, page_number)
        nodes = result['page']['nodes']
        if block_id and not any(node['block_id'] == block_id for node in nodes):
            raise HTTPException(404, 'Block not found')
        boxes = [node['bbox'] for node in nodes if node['block_id'] == block_id]
        return Response(render_page(upload_dir(config, upload_id) / 'source.pdf', page_number, boxes), media_type='image/png')

    @app.put('/uploads/{upload_id}/review')
    def review(upload_id: str, body: ReviewRequest):
        get_job(upload_id)
        try:
            return app.state.uploads.review(upload_id, body.model_dump(mode='json'))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/uploads/{upload_id}/publish', status_code=202)
    def publish(upload_id: str, body: ApprovalRequest):
        get_job(upload_id)
        try:
            return app.state.uploads.approve(upload_id, body.revision)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/uploads/{upload_id}/retry', status_code=202)
    def retry(upload_id: str):
        get_job(upload_id)
        try:
            return app.state.uploads.retry(upload_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    return app
