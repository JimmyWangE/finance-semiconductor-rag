from __future__ import annotations

import pytest

from semi_rag import eastmoney


CHALLENGE = '''<script>function a(a){function n(){for(var a={wQzOV:_0x649a("0x4"),iTyzs:function(a,n){return a+n}},n=a[_0x649a("0x5")][_0x649a("0x6")]("|"),e=0;;){switch(n[e++]){case"0":t+="EO_Bot_Ssid=";continue;case"1":return t;case"2":t+="";continue;case"3":t=a[_0x649a("0x7")](t,756613120);continue;case"4":var t="";continue}break}}var e={WTKkN:1498137497,bOYDu:11823220,dtzqS:function(a,n){return a+n},wyeCN:551194188,pCQRM:function(a){return a()}},t=0;</script>'''


class Response:
    def __init__(self, *, data=None, text='', content=b'', url='https://example.test'):
        self._data, self.text, self.content, self.url = data, text, content, url

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


class FakeClient:
    responses = []
    calls = []

    def __init__(self, **kwargs):
        self.cookies = {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def close(self):
        return None

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def test_challenge_cookie_parser():
    assert eastmoney._challenge_cookies(CHALLENGE) == {
        '__tst_status': '2061154905#',
        'EO_Bot_Ssid': '756613120',
    }
    with pytest.raises(ValueError, match='校验格式已变化'):
        eastmoney._challenge_cookies('<html>blocked</html>')


def test_discover_uses_filtered_report_api(monkeypatch):
    FakeClient.calls = []
    FakeClient.responses = [Response(data={'data': [{
        'infoCode': 'AP1', 'title': '报告', 'publishDate': '2026-09-18 00:00:00.000',
        'orgSName': '机构', 'industryCode': '1036', 'industryName': '半导体',
        'attachPages': 8, 'attachSize': 123,
    }]})]
    monkeypatch.setattr(eastmoney.httpx, 'Client', FakeClient)

    reports = eastmoney.discover('1036', 1)

    assert reports[0]['external_id'] == 'AP1'
    assert reports[0]['industry_code'] == '1036'
    assert FakeClient.calls[0][0] == eastmoney.API_URL
    assert FakeClient.calls[0][1]['params']['industryCode'] == '1036'


def test_download_completes_cookie_challenge(monkeypatch):
    detail = ('var zwinfo={"attach_url":'
              '"https://pdf.dfcfw.com/pdf/H3_AP1_1.pdf?1.pdf"}')
    pdf = b'%PDF-1.4\n' + b'x' * 10_000
    FakeClient.calls = []
    FakeClient.responses = [
        Response(text=detail, url='https://data.eastmoney.com/report/info/AP1.html'),
        Response(text=CHALLENGE, content=CHALLENGE.encode()),
        Response(content=pdf),
    ]
    monkeypatch.setattr(eastmoney.httpx, 'Client', FakeClient)

    data, url = eastmoney.download({
        'external_id': 'AP1', 'info_url': 'https://data.eastmoney.com/report/info/AP1.html',
        'pdf_url': 'https://pdf.dfcfw.com/pdf/H3_AP1_1.pdf', 'size_kb': 0,
    })

    assert data == pdf
    assert url.endswith('H3_AP1_1.pdf?1.pdf')
    assert len(FakeClient.calls) == 3


def test_download_rejects_incomplete_pdf(monkeypatch):
    FakeClient.calls = []
    FakeClient.responses = [
        Response(text='no attachment'),
        Response(content=b'%PDF-1.4 tiny'),
    ]
    monkeypatch.setattr(eastmoney.httpx, 'Client', FakeClient)

    with pytest.raises(ValueError, match='不完整'):
        eastmoney.download({
            'external_id': 'AP1', 'info_url': 'https://example.test/info',
            'pdf_url': 'https://example.test/report.pdf', 'size_kb': 100,
        })
