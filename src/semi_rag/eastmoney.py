from __future__ import annotations

import re
from datetime import date, timedelta
from urllib.parse import urljoin

import httpx

API_URL = 'https://reportapi.eastmoney.com/report/list'
INFO_URL = 'https://data.eastmoney.com/report/info/{info_code}.html'
PDF_BASE = 'https://pdf.dfcfw.com/pdf/H3_{info_code}_1.pdf'
USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
              'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36')


def _parse_date(value: str) -> str:
    return date.fromisoformat(value[:10]).isoformat()


def _normalize(item: dict) -> dict | None:
    info_code = str(item.get('infoCode') or '').strip()
    title = str(item.get('title') or '').strip()
    if not info_code or not title or not item.get('publishDate'):
        return None
    return {
        'external_id': info_code,
        'title': title,
        'org': str(item.get('orgSName') or item.get('orgName') or '').strip(),
        'publish_date': _parse_date(str(item['publishDate'])),
        'industry': str(item.get('industryName') or '').strip(),
        'industry_code': str(item.get('industryCode') or '').strip(),
        'pages': int(item.get('attachPages') or 0),
        'size_kb': int(item.get('attachSize') or 0),
        'info_url': INFO_URL.format(info_code=info_code),
        'pdf_url': PDF_BASE.format(info_code=info_code),
    }


def discover(industry_code: str = '1036', limit: int = 50) -> list[dict]:
    """Return the latest reports from Eastmoney's industry-filtered API."""
    if not re.fullmatch(r'\d{1,10}', industry_code):
        raise ValueError('行业代码格式无效')
    limit = max(1, min(int(limit), 100))
    today = date.today()
    begin = today - timedelta(days=366 * 2)
    records: list[dict] = []
    with httpx.Client(headers={'User-Agent': USER_AGENT}, follow_redirects=True,
                      timeout=30, trust_env=False) as client:
        for page in range(1, 1 + (limit + 49) // 50):
            response = client.get(API_URL, params={
                'industryCode': industry_code, 'pageSize': min(50, limit - len(records)),
                'industry': '*', 'rating': '*', 'ratingChange': '*',
                'beginTime': begin.isoformat(), 'endTime': today.isoformat(),
                'pageNo': page, 'fields': '', 'qType': 1,
            }, headers={'Referer': f'https://data.eastmoney.com/report/industry.jshtml?hyid={industry_code}'})
            response.raise_for_status()
            payload = response.json()
            items = payload.get('data') or []
            for item in items:
                report = _normalize(item)
                if report and report['industry_code'] == industry_code:
                    records.append(report)
                    if len(records) >= limit:
                        return records
            if not items:
                break
    return records


def enrich_pdf_url(report: dict, client: httpx.Client | None = None) -> dict:
    """Resolve the canonical PDF URL from the report detail page when possible."""
    owned = client is None
    client = client or httpx.Client(headers={'User-Agent': USER_AGENT}, follow_redirects=True,
                                    timeout=30, trust_env=False)
    try:
        response = client.get(report['info_url'])
        response.raise_for_status()
        match = re.search(r'"attach_url"\s*:\s*"([^"]+\.pdf[^"\\]*)"', response.text)
        if match:
            report = {**report, 'pdf_url': urljoin(str(response.url), match.group(1).replace('\\/', '/'))}
            return report
    except httpx.HTTPError:
        pass
    finally:
        if owned:
            client.close()
    return report


def _challenge_cookies(script: str) -> dict[str, str]:
    """Parse EdgeOne's arithmetic cookie challenge without executing remote JavaScript."""
    status = re.search(r'WTKkN:(\d+),bOYDu:(\d+).*?wyeCN:(\d+)', script, re.DOTALL)
    session = re.search(r't,\s*(\d+)\);continue', script)
    if not status or not session or 'EO_Bot_Ssid=' not in script:
        raise ValueError('东方财富 PDF 下载校验格式已变化')
    return {'__tst_status': f'{sum(map(int, status.groups()))}#', 'EO_Bot_Ssid': session.group(1)}


def download(report: dict) -> tuple[bytes, str]:
    """Download a report after the provider challenge and reject placeholders."""
    headers = {'User-Agent': USER_AGENT, 'Referer': report['info_url']}
    with httpx.Client(headers=headers, follow_redirects=True, timeout=60, trust_env=False) as client:
        report = enrich_pdf_url(report, client)
        response = client.get(report['pdf_url'])
        response.raise_for_status()
        data = response.content
        if not data.startswith(b'%PDF-'):
            client.cookies.update(_challenge_cookies(response.text))
            response = client.get(report['pdf_url'])
            response.raise_for_status()
            data = response.content
    if not data.startswith(b'%PDF-'):
        raise ValueError('东方财富返回的内容不是 PDF')
    expected = int(report.get('size_kb') or 0) * 1024
    minimum = max(10_000, expected // 2) if expected else 10_000
    if len(data) < minimum:
        raise ValueError(f'PDF 文件不完整（收到 {len(data)} 字节）')
    return data, report['pdf_url']
