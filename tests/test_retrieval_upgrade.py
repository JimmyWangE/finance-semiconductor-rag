import numpy as np
import pytest

from semi_rag.query_variants import query_variants, source_organization
from semi_rag.retrieve import Retriever, matches, rrf, validate_filters


def test_query_variants_preserve_original_and_financial_numbers():
    query = '中芯国际2025年第三季度营业收入23.82亿美元对应什么增速？'
    variants = query_variants(query)
    assert variants[0] == query
    assert len(variants) <= 2
    assert '2025Q3' in variants[1] and '23.82' in variants[1] and '美元' in variants[1]
    assert '营业收入 营收' in variants[1]
    assert query_variants('纯技术问题') == ['纯技术问题']


def test_query_variants_add_market_ranking_terms_for_risk_analysis():
    query = '分析2026年7月5日至7月12日表现落后的子行业及个股风险'
    variants = query_variants(query)
    assert variants[0] == query
    assert '涨跌幅靠后' in variants[1]
    assert '跌幅前五' in variants[1]
    assert '2026年7月5日至7月12日' in variants[1]


class FakeEncoder:
    def encode(self, queries):
        return np.array([[1., 0.] for _ in queries], dtype='float32')


class FakeRanker:
    def __init__(self):
        self.texts = []

    def score(self, query, texts):
        self.texts = texts
        return [float('23.82' in text) for text in texts]


def test_enhanced_candidates_never_escape_filters(monkeypatch):
    from rank_bm25 import BM25Okapi
    from semi_rag.retrieve import tokenize
    retriever = Retriever.__new__(Retriever)
    retriever.config = {'retrieval_profile': 'enhanced', 'candidate_k': 128, 'rerank_k': 96,
                        'max_per_doc': 3, 'top_k': 5, 'retrieval_debug': True}
    retriever.chunks = []
    for index, (date, org) in enumerate([('2025-12-01', '甲券商'), ('2026-02-01', '甲券商'), ('2025-12-01', '乙券商')]):
        text = '中芯国际营业收入23.82亿美元' if index == 0 else '中芯国际营业收入问题资料'
        retriever.chunks.append({'chunk_id': str(index), 'doc_id': str(index), 'publish_date': date,
            'org': org, 'kind': 'text', 'text': text, 'search_text': '研报\n'+text, 'content_hash': str(index)})
    retriever.vectors = np.array([[1., 0.]] * 3, dtype='float32')
    retriever.manifest = {'generation': 'test'}
    retriever.encoder, retriever.reranker = FakeEncoder(), FakeRanker()
    retriever.bm25 = BM25Okapi([tokenize(c['search_text']) for c in retriever.chunks])
    retriever.body_bm25 = BM25Okapi([tokenize(c['text']) for c in retriever.chunks])
    result = retriever.search('中芯国际营业收入', {'as_of': '2025-12-31', 'org': '甲券商'})
    assert result['candidate_ids'] == ['0']
    assert [r['chunk_id'] for r in result['hits']] == ['0']
    assert result['hits'][0]['text'] == retriever.chunks[0]['text']
    assert not retriever.search('中芯国际', {'doc_ids': []})['hits']


def test_weighted_rrf_reduces_duplicate_field_influence():
    result = rrf([[1, 2], [2, 1]], weights=[1., 2.])
    assert result[0][0] == 2
    with pytest.raises(ValueError):
        rrf([[1], [2]], weights=[1])


def test_source_routing_requires_an_explicit_single_publisher():
    orgs = ['东海证券', '开源证券']
    assert source_organization('根据东海证券的研报，行业风险有哪些？', orgs) == '东海证券'
    assert source_organization('开源证券指出哪些产业趋势？', orgs) == '开源证券'
    assert source_organization('比较开源证券与东海证券的观点', orgs) is None
    assert source_organization('开源证券和其他机构有什么不同？', orgs) is None
    assert source_organization('开源证券的股价是多少？', orgs) is None
    assert source_organization('2025年三季度中芯国际的营业收入', orgs) is None


def test_metadata_filters_and_dynamic_quotas_stay_bounded():
    chunk = {'publish_date': '2026-01-01', 'org': '甲券商', 'doc_id': 'd', 'kind': 'text',
             'industry': '半导体', 'stock_name': '中芯国际', 'title': '中芯国际深度报告', 'page': 3}
    filters = validate_filters({'as_of': '2026-02-01', 'company': '中芯国际', 'industry': '半导体',
                                'page_from': 2, 'page_to': 4})
    assert matches(chunk, filters)
    assert not matches(chunk, {**filters, 'page_to': 2})
    with pytest.raises(ValueError):
        validate_filters({'page_from': 4, 'page_to': 3})
    retriever = Retriever.__new__(Retriever)
    retriever.config = {'retrieval_profile': 'enhanced', 'candidate_k': 100, 'dynamic_candidate_quota': True}
    numeric = retriever.candidate_quotas('中芯国际2026Q1营业收入是多少？', 80)
    semantic = retriever.candidate_quotas('分析先进封装产业链竞争格局与传导逻辑', 80)
    assert all(16 <= value <= 80 for value in numeric.values())
    assert numeric['title_bm25'] > semantic['title_bm25']


def test_adjacent_context_is_scoped_to_same_document_and_kind():
    retriever = Retriever.__new__(Retriever)
    retriever.config = {'context_expansion': True, 'adjacent_chunk_radius': 1}
    retriever.chunks = [
        {'chunk_id': 'a', 'doc_id': 'd', 'kind': 'text', 'position': 0, 'text': '前文'},
        {'chunk_id': 'b', 'doc_id': 'd', 'kind': 'text', 'position': 1, 'text': '命中'},
        {'chunk_id': 'c', 'doc_id': 'd', 'kind': 'text', 'position': 2, 'text': '后文'},
        {'chunk_id': 'x', 'doc_id': 'other', 'kind': 'text', 'position': 1, 'text': '其他报告'},
        {'chunk_id': 'y', 'doc_id': 'd', 'kind': 'chart', 'position': 2, 'text': '图表'},
    ]
    assert [row['chunk_id'] for row in retriever.adjacent_context(retriever.chunks[1])] == ['a', 'c']
