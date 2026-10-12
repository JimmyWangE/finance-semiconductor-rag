from __future__ import annotations

import hashlib
import html
import os
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup


SOURCES = (
    ('semi-insights', '半导体行业观察', '全球产业、技术趋势与深度观察'),
    ('icsmart', '芯智讯', '芯片产业动态与公司资讯'),
    ('semiconductor-digest', 'Semiconductor Digest', '制造、设备、材料与封装'),
    ('semiconductor-today', 'Semiconductor Today', '光电、化合物半导体、设备与市场'),
    ('ithome', 'IT之家', '国内芯片、存储器与制造动态'),
    ('qudong', '驱动中国', '国内芯片企业与产业动态'),
    ('newtimespace', '新时空科技', '半导体产业与资本市场动态'),
)
OFFICIAL_FEEDS = {
    'semiconductor-digest': ('https://www.semiconductor-digest.com/feed/', 'www.semiconductor-digest.com'),
    'semiconductor-today': ('https://www.semiconductor-today.com/rss/news.xml', 'www.semiconductor-today.com'),
    'ithome': ('https://www.ithome.com/rss/', 'www.ithome.com'),
    'qudong': ('https://news.qudong.com/rss.xml', 'www.qudong.com'),
    'newtimespace': ('https://www.newtimespace.com/feed/rss_template.xml?id=100003&site=rss&lang=zh-cn',
                     'www.newtimespace.com'),
}
DOMESTIC_SOURCES = {'ithome', 'qudong', 'newtimespace'}
SEMICONDUCTOR_TITLE = re.compile(
    r'半导体|集成电路|芯片|晶圆|光刻|制程|封测|先进封装|存储器|闪存芯片|'
    r'碳化硅|氮化镓|功率器件|硅光|光模块|光芯片|光电子|'
    r'\b(?:HBM|DRAM|NAND|LPDDR|ASIC|EDA|ASML|Foundry)\b|'
    r'台积电|中芯国际|华虹半导体', re.IGNORECASE)
ALLOWED_ARTICLE_HOSTS = {'mp.weixin.qq.com'}
ALLOWED_FEED_HOSTS = {'127.0.0.1', 'localhost', '::1'}
FEED_MIN_INTERVAL = timedelta(minutes=15)
BROWSER_USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                      'AppleWebKit/537.36 Chrome/148.0.0.0 Safari/537.36')


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def validate_article_url(url: str) -> str:
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.hostname not in ALLOWED_ARTICLE_HOSTS:
        raise ValueError('仅支持 https://mp.weixin.qq.com 的公众号文章链接')
    if not (parsed.path == '/s' or parsed.path.startswith('/s/')):
        raise ValueError('链接不是有效的微信公众号文章地址')
    return url


def validate_feed_url(url: str) -> str:
    url = url.strip()
    if not url:
        return ''
    parsed = urlparse(url)
    if url not in {feed for feed, _ in OFFICIAL_FEEDS.values()} and (
        parsed.scheme not in {'http', 'https'} or parsed.hostname not in ALLOWED_FEED_HOSTS
    ):
        raise ValueError('订阅源必须是本机 RSS 服务或已审核的官方 RSS 地址')
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError('订阅源地址格式无效')
    return url


def official_article_url(url: str, source_id: str) -> str:
    host = OFFICIAL_FEEDS[source_id][1]
    parsed = urlparse(url.strip())
    allowed_hosts = ({host, 'semiconductor-today.com'} if source_id == 'semiconductor-today'
                     else {host})
    try:
        valid = (parsed.scheme == 'https' and parsed.hostname in allowed_hosts and
                 parsed.path.startswith('/') and not parsed.path.startswith('//') and
                 not parsed.username and not parsed.password and not parsed.port)
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('订阅条目不是该来源的 HTTPS 原文链接')
    query = urlencode([(key, value) for key, value in parse_qsl(parsed.query)
                       if not key.lower().startswith('utm_') and key.lower() not in {'fbclid', 'gclid'}])
    return urlunparse(parsed._replace(netloc=host, query=query, fragment='',
                                     path=parsed.path.rstrip('/') or '/'))


def feed_date(value: str) -> str:
    if not value:
        return ''
    try:
        date = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            date = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return ''
    return date.replace(tzinfo=date.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat(timespec='seconds')


def parse_feed(payload: str) -> list[dict]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ValueError('订阅源没有返回有效的 RSS/Atom 文档') from exc
    entries = []
    for item in root.findall('.//item'):
        link = (item.findtext('link') or '').strip()
        guid = (item.findtext('guid') or '').strip()
        content = item.findtext('{http://purl.org/rss/1.0/modules/content/}encoded') or ''
        entries.append({'title': (item.findtext('title') or '').strip(),
                        'url': link or guid, 'published_at': (item.findtext('pubDate') or '').strip(),
                        'description': (item.findtext('description') or '').strip(),
                        'content_html': content.strip()})
    if entries:
        return entries
    namespace = {'atom': 'http://www.w3.org/2005/Atom'}
    for item in root.findall('.//atom:entry', namespace):
        links = item.findall('atom:link', namespace)
        link = next((node.get('href', '') for node in links
                     if node.get('rel', 'alternate') == 'alternate'), '')
        entries.append({'title': (item.findtext('atom:title', '', namespace) or '').strip(),
                        'url': link.strip(),
                        'published_at': (item.findtext('atom:published', '', namespace)
                                         or item.findtext('atom:updated', '', namespace) or '').strip(),
                        'description': (item.findtext('atom:summary', '', namespace) or '').strip(),
                        'content_html': (item.findtext('atom:content', '', namespace) or '').strip()})
    return entries


def _text(node) -> str:
    return re.sub(r'\s+', ' ', node.get_text(' ', strip=True)).strip() if node else ''


def _sanitize_article_body(body) -> str:
    for tag in body.select('script, style, iframe, object, embed, form, input, button'):
        tag.decompose()
    for tag in body.find_all(True):
        for attribute in list(tag.attrs):
            if attribute.lower().startswith('on') or attribute.lower() in {'contenteditable', 'tabindex'}:
                del tag.attrs[attribute]
        if tag.name == 'img':
            source = tag.get('data-src') or tag.get('src')
            if source and source.startswith('https://'):
                tag['src'] = source
                tag['referrerpolicy'] = 'no-referrer'
                tag['loading'] = 'lazy'
            else:
                tag.decompose()
        elif tag.name == 'a':
            href = tag.get('href', '')
            if not href.startswith(('https://', 'http://')):
                tag.attrs.pop('href', None)
            else:
                tag['target'] = '_blank'
                tag['rel'] = 'noopener noreferrer'
    return str(body)


def parse_wechat_article(page: str, url: str) -> dict:
    soup = BeautifulSoup(page, 'html.parser')
    title = _text(soup.select_one('#activity-name, .rich_media_title'))
    if not title:
        meta = soup.select_one('meta[property="og:title"]')
        title = (meta.get('content') or '').strip() if meta else ''
    source_name = _text(soup.select_one('#js_name, .rich_media_meta_nickname'))
    author = _text(soup.select_one('#js_author_name, .rich_media_meta_text'))
    published = _text(soup.select_one('#publish_time'))
    if not published:
        match = re.search(r'var\s+createTime\s*=\s*["\']([^"\']+)', page)
        published = match.group(1).strip() if match else ''
    if not published:
        match = re.search(r'var\s+ct\s*=\s*["\'](\d+)["\']', page)
        if match:
            published = datetime.fromtimestamp(
                int(match.group(1)), timezone.utc).isoformat(timespec='seconds')
    body = soup.select_one('#js_content')
    if not title or body is None:
        raise ValueError('无法识别文章标题或正文，链接可能已失效或需要微信验证')

    encoded_biz_match = re.search(
        r'(?:var\s+biz|bizuin)\s*=\s*["\']([A-Za-z0-9+/=]+)["\']', page)
    encoded_biz = encoded_biz_match.group(1) if encoded_biz_match else ''
    related_articles = []
    seen_urls = {urlunparse(urlparse(url)._replace(fragment=''))}
    for link in body.select('a[href]'):
        related_url = urlunparse(urlparse(link.get('href', ''))._replace(fragment=''))
        parsed = urlparse(related_url)
        if (parsed.scheme != 'https' or parsed.hostname != 'mp.weixin.qq.com' or parsed.path != '/s'):
            continue
        if encoded_biz and parse_qs(parsed.query).get('__biz', [''])[0] != encoded_biz:
            continue
        related_title = _text(link)
        if not related_title or related_url in seen_urls:
            continue
        seen_urls.add(related_url)
        related_articles.append({'title': related_title, 'url': related_url})
        if len(related_articles) >= 9:
            break

    content_html = _sanitize_article_body(body)
    description_meta = soup.select_one('meta[property="og:description"], meta[name="description"]')
    description = ((description_meta.get('content') or '').strip() if description_meta else '')
    if not description:
        description = _text(body)[:180]
    return {
        'title': title,
        'source_name': source_name,
        'author': author,
        'published_at': published,
        'description': description,
        'content_html': content_html,
        'url': url,
        'related_articles': related_articles,
    }


class ArticleStore:
    def __init__(self, config: dict):
        directory = Path(config['data_dir']) / 'rag/articles'
        directory.mkdir(parents=True, exist_ok=True)
        self.database = directory / 'articles.sqlite3'
        self.feed_service_url = validate_feed_url(
            os.environ.get('ARTICLE_FEED_SERVICE_URL', 'http://127.0.0.1:192')).rstrip('/')
        self._initialize()

    def connect(self):
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self.connect() as connection:
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS sources (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    description TEXT NOT NULL DEFAULT '',
                    feed_status TEXT NOT NULL DEFAULT 'manual',
                    last_synced_at TEXT,
                    feed_url TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    etag TEXT NOT NULL DEFAULT '',
                    last_modified TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS articles (
                    id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL REFERENCES sources(id),
                    title TEXT NOT NULL,
                    author TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    published_at TEXT NOT NULL DEFAULT '',
                    imported_at TEXT NOT NULL,
                    url TEXT NOT NULL UNIQUE,
                    content_html TEXT NOT NULL DEFAULT '',
                    read INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS article_order ON articles(published_at DESC, imported_at DESC);
                CREATE INDEX IF NOT EXISTS article_source ON articles(source_id);
            ''')
            columns = {row[1] for row in connection.execute('PRAGMA table_info(sources)')}
            if 'feed_url' not in columns:
                connection.execute("ALTER TABLE sources ADD COLUMN feed_url TEXT NOT NULL DEFAULT ''")
            if 'last_error' not in columns:
                connection.execute("ALTER TABLE sources ADD COLUMN last_error TEXT NOT NULL DEFAULT ''")
            if 'etag' not in columns:
                connection.execute("ALTER TABLE sources ADD COLUMN etag TEXT NOT NULL DEFAULT ''")
            if 'last_modified' not in columns:
                connection.execute("ALTER TABLE sources ADD COLUMN last_modified TEXT NOT NULL DEFAULT ''")
            connection.executemany(
                'INSERT OR IGNORE INTO sources(id,name,description) VALUES(?,?,?)',
                (item for item in SOURCES if item[0] not in OFFICIAL_FEEDS))
            for source_id, (feed, _) in OFFICIAL_FEEDS.items():
                name, description = next((name, description) for key, name, description in SOURCES
                                         if key == source_id)
                connection.execute('''INSERT OR IGNORE INTO sources
                    (id,name,description,feed_url,feed_status) VALUES(?,?,?,?,'ready')''',
                    (source_id, name, description, feed))

    def sources(self) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute('''
                SELECT s.*, COUNT(a.id) AS article_count,
                       COALESCE(SUM(CASE WHEN a.read=0 THEN 1 ELSE 0 END),0) AS unread_count,
                       MAX(NULLIF(a.published_at,'')) AS latest_at
                FROM sources s LEFT JOIN articles a ON a.source_id=s.id
                GROUP BY s.id ORDER BY s.name
            ''').fetchall()
        return [dict(row) for row in rows]

    def configure_feed(self, source_id: str, feed_url: str) -> dict:
        feed_url = validate_feed_url(feed_url)
        if source_id in OFFICIAL_FEEDS and feed_url and feed_url != OFFICIAL_FEEDS[source_id][0]:
            raise ValueError('该媒体仅支持已审核的官方 RSS 地址')
        if source_id not in OFFICIAL_FEEDS and feed_url in {item[0] for item in OFFICIAL_FEEDS.values()}:
            raise ValueError('官方 RSS 地址与所选来源不匹配')
        with self.connect() as connection:
            changed = connection.execute(
                "UPDATE sources SET feed_url=?,feed_status=?,last_error='',etag='',last_modified='' WHERE id=?",
                (feed_url, 'ready' if feed_url else 'manual', source_id)).rowcount
        if not changed:
            raise KeyError(source_id)
        return next(source for source in self.sources() if source['id'] == source_id)

    def _exists(self, url: str) -> bool:
        with self.connect() as connection:
            return connection.execute('SELECT 1 FROM articles WHERE url=?', (url,)).fetchone() is not None

    def discover_feeds(self, client) -> int:
        response = client.get(self.feed_service_url + '/feeds')
        response.raise_for_status()
        try:
            feeds = response.json()
        except ValueError as exc:
            raise ValueError('公众号订阅服务返回了无效的源列表') from exc
        if not isinstance(feeds, list):
            raise ValueError('公众号订阅服务返回了无效的源列表')
        available = {re.sub(r'\s+', '', str(item.get('name', ''))).casefold(): item
                     for item in feeds if isinstance(item, dict) and item.get('id')}
        configured = 0
        for source in self.sources():
            if source['id'] in OFFICIAL_FEEDS:
                continue
            matched = available.get(re.sub(r'\s+', '', source['name']).casefold())
            if matched:
                feed_url = f"{self.feed_service_url}/feeds/{matched['id']}.rss?limit=100"
                self.configure_feed(source['id'], feed_url)
                configured += 1
        return configured

    def list(self, *, source_id='', query='', unread=False, limit=100) -> dict:
        clauses, values = ['1=1'], []
        if source_id:
            clauses.append('a.source_id=?')
            values.append(source_id)
        if query.strip():
            clauses.append('(a.title LIKE ? OR a.description LIKE ? OR a.author LIKE ?)')
            term = f'%{query.strip()}%'
            values.extend([term, term, term])
        if unread:
            clauses.append('a.read=0')
        sql = f'''SELECT a.id,a.source_id,s.name AS source_name,a.title,a.author,a.description,
                         a.published_at,a.imported_at,a.url,a.read,
                         CASE WHEN length(a.content_html)>0 THEN 1 ELSE 0 END AS has_content
                  FROM articles a JOIN sources s ON s.id=a.source_id
                  WHERE {' AND '.join(clauses)}
                  ORDER BY CASE WHEN a.published_at='' THEN 1 ELSE 0 END,
                           a.published_at DESC,a.imported_at DESC LIMIT ?'''
        with self.connect() as connection:
            rows = connection.execute(sql, (*values, min(max(limit, 1), 500))).fetchall()
            total = connection.execute(
                f'''SELECT COUNT(*) FROM articles a WHERE {' AND '.join(clauses)}''', values).fetchone()[0]
        return {'articles': [dict(row) for row in rows], 'total': total}

    def get(self, article_id: str, mark_read=False) -> dict:
        with self.connect() as connection:
            row = connection.execute('''SELECT a.*,s.name AS source_name FROM articles a
                JOIN sources s ON s.id=a.source_id WHERE a.id=?''', (article_id,)).fetchone()
            if row is None:
                raise KeyError(article_id)
            if mark_read and not row['read']:
                connection.execute('UPDATE articles SET read=1 WHERE id=?', (article_id,))
            result = dict(row)
            result['read'] = 1 if mark_read else result['read']
            result['has_content'] = bool(result.get('content_html'))
            return result

    def import_url(self, source_id: str, url: str, client=None) -> dict:
        url = validate_article_url(url)
        with self.connect() as connection:
            if connection.execute('SELECT 1 FROM sources WHERE id=?', (source_id,)).fetchone() is None:
                raise ValueError('未知公众号')
            existing = connection.execute('SELECT id FROM articles WHERE url=?', (url,)).fetchone()
        if existing:
            return {**self.get(existing['id']), 'created': False}
        owns_client = client is None
        client = client or httpx.Client(follow_redirects=True, trust_env=False, timeout=30,
            headers={'User-Agent': BROWSER_USER_AGENT, 'Accept-Language': 'zh-CN,zh;q=0.9'})
        try:
            response = client.get(url)
            response.raise_for_status()
            article = parse_wechat_article(response.text, str(response.url))
        finally:
            if owns_client:
                client.close()
        article_id = hashlib.sha256(article['url'].encode()).hexdigest()[:24]
        imported_at = utc_now()
        with self.connect() as connection:
            connection.execute('''INSERT OR IGNORE INTO articles
                (id,source_id,title,author,description,published_at,imported_at,url,content_html)
                VALUES(?,?,?,?,?,?,?,?,?)''', (article_id, source_id, article['title'], article['author'],
                    article['description'], article['published_at'], imported_at, article['url'], article['content_html']))
            related_created = 0
            imported = datetime.now(timezone.utc)
            for index, related in enumerate(article['related_articles']):
                related_id = hashlib.sha256(related['url'].encode()).hexdigest()[:24]
                related_imported_at = (imported - timedelta(seconds=index + 1)).isoformat(timespec='seconds')
                created = connection.execute('''INSERT OR IGNORE INTO articles
                    (id,source_id,title,author,description,published_at,imported_at,url,content_html)
                    VALUES(?,?,?,?,?,?,?,?,?)''', (related_id, source_id, related['title'], '', '', '',
                                                  related_imported_at, related['url'], ''))
                related_created += created.rowcount
        return {**self.get(article_id), 'created': True, 'related_created': related_created}

    def import_feed_entry(self, source_id: str, entry: dict) -> bool:
        official = source_id in OFFICIAL_FEEDS
        url = (official_article_url(entry.get('url', ''), source_id) if official else
               validate_article_url(entry.get('url', '')))
        title = str(entry.get('title', '')).strip()
        if not title:
            raise ValueError('订阅条目缺少文章标题')
        content = str(entry.get('content_html', '')).strip() if not official else ''
        content_html = ''
        if content:
            soup = BeautifulSoup(content, 'html.parser')
            body = soup.select_one('#js_content') or soup
            content_html = _sanitize_article_body(body)
        article_id = hashlib.sha256(url.encode()).hexdigest()[:24]
        description = ''
        published = str(entry.get('published_at', '')).strip()
        if official:
            if source_id not in DOMESTIC_SOURCES and source_id != 'semiconductor-today':
                description = _text(BeautifulSoup(str(entry.get('description', '')), 'html.parser'))[:180]
            published = feed_date(published)
        with self.connect() as connection:
            if official and published and len(title) >= 16:
                duplicate = connection.execute('''SELECT 1 FROM articles
                    WHERE title=? AND substr(published_at,1,10)=? LIMIT 1''',
                    (title, published[:10])).fetchone()
                if duplicate:
                    return False
            result = connection.execute('''INSERT OR IGNORE INTO articles
                (id,source_id,title,author,description,published_at,imported_at,url,content_html)
                VALUES(?,?,?,?,?,?,?,?,?)''', (article_id, source_id, title, '', description,
                    published, utc_now(), url, content_html))
        return result.rowcount > 0

    def content_document(self, article_id: str) -> str:
        article = self.get(article_id, mark_read=True)
        title = html.escape(article['title'])
        source = html.escape(article['source_name'])
        published = html.escape(article['published_at'])
        missing_body = ('本来源仅保存文章索引与摘要，完整内容请在原站阅读。' if article['source_id'] in OFFICIAL_FEEDS
                        else '微信当前要求验证，服务器暂时无法抓取这篇文章正文。')
        return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer"><link rel="stylesheet" href="/ui/tokens.css">
<style>:root{{color-scheme:light}}*{{box-sizing:border-box}}body{{margin:0 auto;padding:42px 30px 72px;max-width:760px;color:var(--sr-ink);background:var(--sr-surface);font:16px/1.85 var(--sr-font-body);overflow-wrap:anywhere}}header{{padding-bottom:24px;margin-bottom:30px;border-bottom:1px solid var(--sr-rule)}}h1{{font-size:28px;line-height:1.45;margin:0 0 12px}}.meta{{color:var(--sr-muted);font-size:13px}}#js_content{{visibility:visible!important}}img{{display:block;max-width:100%!important;height:auto!important;margin:22px auto}}p{{margin:0 0 1.25em}}a{{color:var(--sr-accent)}}.original-only{{padding:26px 0;border-bottom:1px solid var(--sr-rule)}}.original-only h2{{margin:0 0 8px;font-size:18px}}.original-only p{{color:var(--sr-muted)}}.original-link{{display:inline-block;padding:9px 14px;border:1px solid var(--sr-accent);border-radius:4px;text-decoration:none;font-weight:650}}@media(max-width:600px){{body{{padding:26px 18px 56px}}h1{{font-size:23px}}}}</style></head>
<body><header><h1>{title}</h1><div class="meta">{source} · {published}</div></header>{article['content_html'] or f'''<section class="original-only"><h2>阅读原文</h2><p>{missing_body}</p><p><a class="original-link" href="{html.escape(article['url'])}" target="_blank" rel="noopener noreferrer">前往原站阅读</a></p></section>'''}</body></html>'''

    def sync(self, source_id: str = '', client=None) -> dict:
        if source_id and not any(source['id'] == source_id for source in self.sources()):
            raise KeyError(source_id)
        owns_client = client is None
        client = client or httpx.Client(follow_redirects=True, trust_env=False, timeout=45,
            headers={'User-Agent': 'SemiResearch-ArticleSync/1.0'})
        result = {'sources': [], 'checked': 0, 'created': 0, 'skipped': 0, 'filtered': 0, 'failed': 0}
        try:
            if not source_id or source_id not in OFFICIAL_FEEDS:
                try:
                    self.discover_feeds(client)
                except (ValueError, httpx.HTTPError):
                    pass
            sources = [source for source in self.sources()
                       if source['feed_url'] and (not source_id or source['id'] == source_id)]
            if not sources:
                raise ValueError('尚未配置对应订阅源')
            for source in sources:
                detail = {'source_id': source['id'], 'source_name': source['name'],
                          'checked': 0, 'created': 0, 'skipped': 0, 'filtered': 0, 'errors': []}
                try:
                    official = source['id'] in OFFICIAL_FEEDS
                    if official and source['feed_url'] != OFFICIAL_FEEDS[source['id']][0]:
                        raise ValueError('官方来源订阅地址与已审核地址不一致')
                    last = feed_date(source['last_synced_at'] or '')
                    if official and last and datetime.now(timezone.utc) - datetime.fromisoformat(last) < FEED_MIN_INTERVAL:
                        detail['throttled'] = True
                        result['sources'].append(detail)
                        continue
                    if official:
                        headers = {}
                        if source['etag']:
                            headers['If-None-Match'] = source['etag']
                        if source['last_modified']:
                            headers['If-Modified-Since'] = source['last_modified']
                        for attempt in range(3):
                            response = client.get(source['feed_url'], headers=headers)
                            if response.status_code not in {429, 500, 502, 503, 504} or attempt == 2:
                                break
                            retry_after = response.headers.get('Retry-After', '')
                            try:
                                delay = max(float(retry_after), 0)
                            except ValueError:
                                try:
                                    until = parsedate_to_datetime(retry_after)
                                    delay = max((until - datetime.now(timezone.utc)).total_seconds(), 0)
                                except (TypeError, ValueError):
                                    delay = 0
                            if delay > 4:
                                response.raise_for_status()
                            time.sleep(max(delay, 0.5 * (2 ** attempt)))
                    else:
                        separator = '&' if '?' in source['feed_url'] else '?'
                        trigger = client.get(source['feed_url'] + separator + 'update=true')
                        trigger.raise_for_status()
                        response = client.get(source['feed_url'])
                    if response.status_code != 304:
                        response.raise_for_status()
                    limit = 4_000_000 if source['id'] == 'semiconductor-today' else 2_000_000
                    if official and len(response.content) > limit:
                        raise ValueError('订阅源响应超过大小限额')
                    entries = [] if response.status_code == 304 else parse_feed(response.text)[:100]
                    for entry in entries:
                        if source['id'] in DOMESTIC_SOURCES and not SEMICONDUCTOR_TITLE.search(entry['title']):
                            detail['filtered'] += 1
                            continue
                        detail['checked'] += 1
                        try:
                            url = (official_article_url(entry['url'], source['id']) if official else
                                   validate_article_url(entry['url']))
                            if self._exists(url):
                                detail['skipped'] += 1
                            else:
                                if self.import_feed_entry(source['id'], entry):
                                    detail['created'] += 1
                                else:
                                    detail['skipped'] += 1
                        except (ValueError, httpx.HTTPError) as exc:
                            detail['errors'].append(f"{entry.get('title') or entry.get('url')}: {exc}")
                    synced_at = utc_now()
                    last_error = '; '.join(detail['errors'][:3])
                    with self.connect() as connection:
                        connection.execute('''UPDATE sources SET last_synced_at=?,feed_status=?,last_error=?,
                            etag=?,last_modified=? WHERE id=?''',
                            (synced_at, 'warning' if last_error else 'ready', last_error,
                             response.headers.get('ETag', source['etag']) if official else source['etag'],
                             response.headers.get('Last-Modified', source['last_modified']) if official else source['last_modified'],
                             source['id']))
                except (ValueError, httpx.HTTPError) as exc:
                    detail['errors'].append(str(exc))
                    with self.connect() as connection:
                        connection.execute("UPDATE sources SET feed_status='error',last_error=? WHERE id=?",
                                           (str(exc), source['id']))
                result['checked'] += detail['checked']
                result['created'] += detail['created']
                result['skipped'] += detail['skipped']
                result['filtered'] += detail['filtered']
                result['failed'] += len(detail['errors'])
                result['sources'].append(detail)
        finally:
            if owns_client:
                client.close()
        return result
