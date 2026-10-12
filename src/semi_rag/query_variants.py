"""Conservative multi-query normalization without adding inferred financial facts."""
import re
import unicodedata

ALIASES = (
    ('营业收入', '营收'), ('产能利用率', '稼动率'), ('资本开支', '资本支出'),
    ('晶圆代工', '晶圆制造'),
    ('高带宽存储', 'HBM'), ('动态随机存取存储器', 'DRAM'),
    ('NAND闪存', 'NAND'), ('化学机械抛光', 'CMP'),
)
QUARTERS = {'一': '1', '二': '2', '三': '3', '四': '4'}


def query_variants(query: str) -> list[str]:
    normalized = unicodedata.normalize('NFKC', query).strip()
    compact = re.sub(r'(20\d{2})\s*年?\s*第?([一二三四1-4])季度',
                     lambda m: m[1] + 'Q' + QUARTERS.get(m[2], m[2]), normalized)
    compact = re.sub(r'(20\d{2})\s*年\s*([1-4])\s*季度', r'\1Q\2', compact)
    expanded = compact
    for full, short in ALIASES:
        if full in expanded and short not in expanded.replace(full, ''):
            expanded = expanded.replace(full, full + ' ' + short)
        elif short in expanded and full not in expanded:
            expanded = expanded.replace(short, short + ' ' + full)
    # Analytical market questions often use abstract wording while report
    # evidence uses concrete ranking labels. Add retrieval vocabulary only;
    # never introduce a company, date, value, or conclusion.
    if ('个股' in expanded and re.search(r'板块|行业', expanded)
            and re.search(r'表现落后|落后|下跌|跌幅|风险', expanded)):
        expanded += ' 涨跌幅靠后 跌幅前五 周涨跌幅'
    return list(dict.fromkeys([query, expanded]))[:2]


def source_organization(query: str, organizations: list[str]) -> str | None:
    """Resolve only an explicitly named report publisher; never infer financial dates."""
    found = [org for org in organizations if org and org in query]
    if len(found) != 1 or re.search(r'比较|对比|不同券商|各券商|多家|其他机构', query):
        return None
    org = re.escape(found[0])
    if re.search(r'(?:根据|依据|参考|结合|按照|基于).{0,12}' + org, query) or re.search(
            org + r'.{0,12}(?:研报|报告|认为|指出|预测|预计|观点|提示|判断|建议|分析)', query):
        return found[0]
    return None
