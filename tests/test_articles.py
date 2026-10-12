from __future__ import annotations

import httpx
import pytest

from semi_rag.articles import (ArticleStore, official_article_url, parse_feed, parse_wechat_article,
                               validate_article_url, validate_feed_url)


PAGE = '''<!doctype html><html><head>
<meta property="og:description" content="用于测试的文章摘要">
</head><body><h1 id="activity-name">先进封装观察</h1>
<span id="js_name">半导体行业观察</span><span id="publish_time">2026-09-20</span>
<div id="js_content"><p onclick="bad()">正文内容</p>
<img data-src="https://mmbiz.qpic.cn/test/image.jpg" onerror="bad()">
<script>bad()</script><a href="javascript:bad()">无效链接</a></div></body></html>'''

PAGE_WITH_RELATED = '''<!doctype html><html><head>
<meta property="og:title" content="最新文章"></head><body>
<span id="js_name">芯智讯</span><div id="js_content"><p>正文</p>
<a href="https://mp.weixin.qq.com/s?__biz=TEST%3D%3D&amp;mid=2&amp;idx=1&amp;sn=two#wechat_redirect">上一篇文章</a>
<a href="https://mp.weixin.qq.com/s?__biz=OTHER&amp;mid=3">其他公众号</a>
</div><script>var biz = "TEST=="; var ct = "1789954169";</script></body></html>'''


def test_wechat_url_validation_and_article_sanitizing():
    url = 'https://mp.weixin.qq.com/s/example'
    assert validate_article_url(url) == url
    for invalid in ('http://mp.weixin.qq.com/s/example', 'https://example.com/s/example',
                    'https://mp.weixin.qq.com/profile'):
        with pytest.raises(ValueError):
            validate_article_url(invalid)

    article = parse_wechat_article(PAGE, url)
    assert article['title'] == '先进封装观察'
    assert article['source_name'] == '半导体行业观察'
    assert article['published_at'] == '2026-09-20'
    assert '<script' not in article['content_html']
    assert 'onclick=' not in article['content_html'] and 'onerror=' not in article['content_html']
    assert 'src="https://mmbiz.qpic.cn/test/image.jpg"' in article['content_html']
    assert 'javascript:' not in article['content_html']


def test_article_store_import_filter_read_and_deduplicate(tmp_path):
    store = ArticleStore({'data_dir': str(tmp_path)})
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=PAGE, request=request))
    with httpx.Client(transport=transport) as client:
        article = store.import_url('semi-insights', 'https://mp.weixin.qq.com/s/example', client)
        duplicate = store.import_url('semi-insights', 'https://mp.weixin.qq.com/s/example', client)

    assert article['created'] is True and duplicate['created'] is False
    assert store.list(query='先进封装')['total'] == 1
    assert store.list(query='先进封装')['articles'][0]['has_content'] == 1
    assert store.list(source_id='icsmart')['total'] == 0
    assert store.list(unread=True)['total'] == 1
    document = store.content_document(article['id'])
    assert '先进封装观察' in document and '正文内容' in document
    assert store.list(unread=True)['total'] == 0
    source = next(item for item in store.sources() if item['id'] == 'semi-insights')
    assert source['article_count'] == 1 and source['unread_count'] == 0


def test_article_import_includes_related_articles_from_same_account(tmp_path):
    store = ArticleStore({'data_dir': str(tmp_path)})
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text=PAGE_WITH_RELATED, request=request))
    with httpx.Client(transport=transport) as client:
        article = store.import_url(
            'icsmart', 'https://mp.weixin.qq.com/s/latest', client)

    assert article['related_created'] == 1
    items = store.list(source_id='icsmart')['articles']
    assert {item['title'] for item in items} == {'最新文章', '上一篇文章'}
    assert sorted(item['has_content'] for item in items) == [0, 1]


def test_manual_feed_sync_is_incremental_and_loopback_only(tmp_path):
    rss = '''<rss version="2.0"><channel><title>订阅</title><item>
        <title>先进封装观察</title><link>https://mp.weixin.qq.com/s/example</link>
        <guid>wechat-example</guid><pubDate>Sun, 20 Sep 2026 08:00:00 GMT</pubDate>
        </item></channel></rss>'''
    assert parse_feed(rss)[0]['url'] == 'https://mp.weixin.qq.com/s/example'
    assert validate_feed_url('http://127.0.0.1:192/feeds/test.rss').endswith('test.rss')
    with pytest.raises(ValueError, match='本机'):
        validate_feed_url('https://feeds.example.com/test.rss')

    store = ArticleStore({'data_dir': str(tmp_path)})
    configured = store.configure_feed('semi-insights', 'http://127.0.0.1:192/feeds/test.rss')
    assert configured['feed_status'] == 'ready'

    def handle(request):
        if request.url.host == '127.0.0.1' and request.url.path == '/feeds':
            return httpx.Response(200, json=[], request=request)
        if request.url.host == '127.0.0.1':
            return httpx.Response(200, text=rss, request=request)
        return httpx.Response(200, text=PAGE, request=request)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        first = store.sync(client=client)
        second = store.sync(client=client)
    assert first['checked'] == 1 and first['created'] == 1 and first['skipped'] == 0
    assert second['checked'] == 1 and second['created'] == 0 and second['skipped'] == 1
    source = next(item for item in store.sources() if item['id'] == 'semi-insights')
    assert source['last_synced_at'] and source['last_error'] == ''


def test_official_feeds_restrict_urls_and_preserve_wechat_sources(tmp_path):
    store = ArticleStore({'data_dir': str(tmp_path)})
    sources = {item['id']: item for item in store.sources()}
    assert sources['semiconductor-digest']['feed_url'] == 'https://www.semiconductor-digest.com/feed/'
    assert sources['semiconductor-today']['feed_url'] == 'https://www.semiconductor-today.com/rss/news.xml'
    assert sources['icsmart']['feed_url'] == ''
    assert official_article_url('https://www.semiconductor-today.com/chips/?utm_source=rss#top',
                                'semiconductor-today') == 'https://www.semiconductor-today.com/chips'
    assert official_article_url('https://semiconductor-today.com/chips/', 'semiconductor-today') == 'https://www.semiconductor-today.com/chips'
    for invalid in ('https://attacker.example/chips', 'http://www.semiconductor-today.com/chips',
                    'https://www.semiconductor-today.com.evil.example/chips'):
        with pytest.raises(ValueError):
            official_article_url(invalid, 'semiconductor-today')
    with pytest.raises(ValueError):
        store.configure_feed('icsmart', 'https://www.semiconductor-today.com/rss/news.xml')
    with pytest.raises(ValueError):
        store.configure_feed('semiconductor-today', 'http://127.0.0.1:192/feeds/evil.rss')
    store.configure_feed('semiconductor-today', '')
    assert next(item for item in ArticleStore({'data_dir': str(tmp_path)}).sources()
                if item['id'] == 'semiconductor-today')['feed_url'] == ''


def test_official_sync_incremental_304_throttle_and_cross_source_dedup(tmp_path):
    store = ArticleStore({'data_dir': str(tmp_path)})
    article_title = 'A Shared Chip Production Announcement'
    rss = f'''<rss><channel><item><title>{article_title}</title>
      <link>https://www.semiconductor-today.com/chip-news/?utm_source=rss</link>
      <pubDate>Mon, 28 Sep 2026 17:44:51 +0000</pubDate>
      <description>&lt;p&gt;Chip supply update for engineers.&lt;/p&gt;</description>
      <content:encoded xmlns:content="http://purl.org/rss/1.0/modules/content/">Full copyright text</content:encoded>
      </item><item><title>Malicious</title><link>https://other.example/article</link></item>
      </channel></rss>'''
    requests = []

    def handle(request):
        requests.append(request)
        if request.headers.get('If-None-Match'):
            return httpx.Response(304, request=request)
        return httpx.Response(200, text=rss, headers={'ETag': '"first"'}, request=request)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        first = store.sync('semiconductor-today', client=client)
        second = store.sync('semiconductor-today', client=client)
        with store.connect() as connection:
            connection.execute("UPDATE sources SET last_synced_at='2020-01-01T00:00:00+00:00' WHERE id='semiconductor-today'")
        third = store.sync('semiconductor-today', client=client)
    assert first['created'] == 1 and first['failed'] == 1
    assert second['sources'][0]['throttled'] is True
    assert third['checked'] == 0 and third['failed'] == 0
    assert len(requests) == 2 and requests[1].headers['If-None-Match'] == '"first"'
    article = store.list(source_id='semiconductor-today')['articles'][0]
    assert article['url'] == 'https://www.semiconductor-today.com/chip-news'
    assert article['published_at'].startswith('2026-09-28T17:44:51')
    assert article['description'] == ''
    assert article['has_content'] == 0
    assert 'Full copyright text' not in store.content_document(article['id'])
    assert store.import_feed_entry('semiconductor-today', {
        'url': 'https://www.semiconductor-today.com/chip-news/?utm_campaign=test',
        'title': article_title, 'published_at': 'Mon, 28 Sep 2026 17:44:51 +0000'}) is False


def test_official_sync_respects_retry_after_without_repeat_request(tmp_path):
    store = ArticleStore({'data_dir': str(tmp_path)})
    calls = []

    def limited(request):
        calls.append(request)
        return httpx.Response(429, headers={'Retry-After': '120'}, request=request)

    with httpx.Client(transport=httpx.MockTransport(limited)) as client:
        result = store.sync('semiconductor-today', client=client)
    assert len(calls) == 1 and result['failed'] == 1
    assert next(row for row in store.sources() if row['id'] == 'semiconductor-today')['feed_status'] == 'error'


@pytest.mark.parametrize('source_id,feed_url,host', [
    ('ithome', 'https://www.ithome.com/rss/', 'www.ithome.com'),
    ('qudong', 'https://news.qudong.com/rss.xml', 'www.qudong.com'),
    ('newtimespace', 'https://www.newtimespace.com/feed/rss_template.xml?id=100003&site=rss&lang=zh-cn',
     'www.newtimespace.com'),
])
def test_domestic_feeds_keep_only_semiconductor_headlines(tmp_path, source_id, feed_url, host):
    store = ArticleStore({'data_dir': str(tmp_path)})
    assert next(row for row in store.sources() if row['id'] == source_id)['feed_url'] == feed_url
    rss = f'''<rss><channel>
        <item><title>国产半导体芯片完成流片</title><link>https://{host}/article/1.htm</link>
        <pubDate>Tue, 29 Sep 2026 07:20:38 GMT</pubDate>
        <description>该内容不应被本地转载</description></item>
        <item><title>电视机开售</title><link>https://{host}/article/2.htm</link></item>
        <item><title>USB 闪存盘开售</title><link>https://{host}/article/4.htm</link></item>
        <item><title>晶圆厂新闻</title><link>https://elsewhere.example/article</link></item>
        </channel></rss>'''
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, text=rss, request=request)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = store.sync(source_id, client=client)
    assert len(requests) == 1
    assert result['created'] == 1 and result['filtered'] == 2 and result['failed'] == 1
    article = store.list(source_id=source_id)['articles'][0]
    assert article['title'] == '国产半导体芯片完成流片'
    assert article['description'] == '' and article['has_content'] == 0
    assert article['url'] == f'https://{host}/article/1.htm'
