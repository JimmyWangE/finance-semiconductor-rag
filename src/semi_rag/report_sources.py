"""Publisher-specific report channels from the public Eastmoney distribution feed."""

from semi_rag import eastmoney


SOURCES = {
    'eastmoney': {'name': '全部半导体研报', 'org': '', 'url': 'https://data.eastmoney.com/report/industry.jshtml?hyid=1036'},
    'donghai': {'name': '东海证券', 'org': '东海证券', 'url': 'https://data.eastmoney.com/report/industry.jshtml?hyid=1036'},
    'guojin': {'name': '国金证券', 'org': '国金证券', 'url': 'https://data.eastmoney.com/report/industry.jshtml?hyid=1036'},
    'dongwu': {'name': '东吴证券', 'org': '东吴证券', 'url': 'https://data.eastmoney.com/report/industry.jshtml?hyid=1036'},
    'kaiyuan': {'name': '开源证券', 'org': '开源证券', 'url': 'https://data.eastmoney.com/report/industry.jshtml?hyid=1036'},
}


def discover(source: str, limit: int = 50) -> list[dict]:
    if source not in SOURCES:
        raise ValueError('未知研报来源')
    publisher = SOURCES[source]['org']
    # The public API does not provide a verified publisher filter. Scan its
    # bounded recent window and only return records attributed to this broker.
    reports = eastmoney.discover('1036', 100 if publisher else limit)
    return [report for report in reports if not publisher or report['org'] == publisher][:limit]
