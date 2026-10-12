from __future__ import annotations

import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from semi_rag.answer import Answerer, date_markers, evidence_text, exact_excerpt, normalize_coverage, numbers, repair_citations, validate_answer
from semi_rag.api import create_app
from semi_rag.ingest import page_chunks, restored_pages, split_text
from semi_rag.retrieve import matches, rrf, temporal_markers, validate_filters


def source():
    return {'source_id': 'S1', 'title': '中芯国际报告', 'publish_date': '2025-12-05',
            'text': '2025年第三季度，中芯国际产能利用率为95.8%，环比上升3.3个百分点。'}


def payload(text='中芯国际产能利用率为95.8%。', quote=None, sid='S1'):
    return {'answerable': True, 'claims': [{'text': text, 'citations': [{
        'source_id': sid, 'quote': quote or source()['text']}]}]}


def test_citations_and_numerical_grounding():
    assert validate_answer(payload(), [source()])[1] == []
    assert validate_answer(payload(sid='S9'), [source()])[1]
    assert validate_answer(payload(quote='并不存在的连续原文内容'), [source()])[1]
    assert validate_answer(payload(text='产能利用率为99.9%。'), [source()])[1]
    assert validate_answer(payload(text='增长3.3个百分点。', quote='中芯国际产能利用率为95.8%'), [source()])[1] == []
    assert validate_answer({'answerable': 'true'}, [source()])[1]
    assert validate_answer({'answerable': False, 'claims': []}, [source()]) == ([], [])
    assert numbers('营收1,234.50亿元，95.8%') == {'1234.50', '95.8'}
    assert numbers('本周(2026.7.13-7.17)调整') == {'2026', '7', '13', '17'}
    assert numbers('汽车上涨7. 06%') == {'7.06'}
    assert exact_excerpt(source()['text'], '2025 年第三季度, 中芯国际产能利用率为95.8%') == '2025年第三季度，中芯国际产能利用率为95.8%'
    adjacent = source() | {'context_before': '公司于2025年3月18日披露。'}
    adjacent_payload = payload(text='公司于2025年3月18日披露。', quote='公司于2025年3月18日披露。')
    assert validate_answer(adjacent_payload, [adjacent])[1] == []


def test_partial_coverage_when_cited_evidence_omits_question_date():
    claim = payload()['claims']
    coverage, limitations = normalize_coverage('公司在2026年3月18日披露什么？', [source()], claim, 'full', [])
    assert coverage == 'partial' and limitations
    dated = source() | {'text': source()['text'] + ' 公司于2026年3月18日披露。'}
    assert normalize_coverage('公司在2026年3月18日披露什么？', [dated], claim, 'full', []) == ('full', [])
    july = source() | {'text': source()['text'] + ' 统计期为2026.7.13-7.17。'}
    assert normalize_coverage('分析2026年7月行业表现', [july], claim, 'full', []) == ('full', [])


def test_best_effort_repairs_missing_citation_only_from_exact_evidence():
    raw = {'answerable': True, 'claims': [{'text': '产能利用率为95.8%。', 'citations': []}]}
    repaired = repair_citations(raw, [source()], '产能利用率是多少？')
    assert validate_answer(repaired, [source()])[1] == []
    unsupported = {'answerable': True, 'claims': [{'text': '产能利用率为99.9%。', 'citations': []}]}
    assert repair_citations(unsupported, [source()], '产能利用率是多少？') == unsupported


def test_date_filter_prevents_future_leakage():
    filters = validate_filters({'as_of': '2026-01-01', 'date_to': '2027-01-01', 'org': '开源证券'})
    assert filters['date_to'] == '2026-01-01'
    chunk = {'publish_date': '2026-01-02', 'org': '开源证券', 'doc_id': 'abc', 'kind': 'text'}
    assert not matches(chunk, filters)
    chunk['publish_date'] = '2025-12-01'
    assert matches(chunk, filters)
    assert not matches(chunk, validate_filters({'org': '东海证券'}))
    assert not matches(chunk, validate_filters({'doc_ids': []}))
    with pytest.raises(ValueError):
        validate_filters({'date_from': '2026-02-01', 'as_of': '2026-01-01'})
    with pytest.raises(ValueError):
        validate_filters({'date_from': 'not-a-date'})
    with pytest.raises(ValueError):
        validate_filters({'organization': 'ignored-filter'})


def test_chunking_preserves_text_and_table_headers():
    text = '甲公司营收持续增长。乙公司毛利率上升。' * 80
    chunks = split_text(text, 100, 10)
    assert max(map(len, chunks)) <= 100
    assert text.endswith(chunks[-1])
    assert all(piece in text for piece in chunks)
    rows = ['|公司|营收|', '|---|---|'] + [f'|公司{i}|{i}亿元|' for i in range(30)]
    tables = page_chunks(rows, 100, 10)
    assert len(tables) > 1
    assert all(kind == 'table' and block.startswith('|公司|营收|\n|---|---|') for kind, block in tables)
    assert all(any(row in block for _, block in tables) for row in rows)


def test_page_numbers_do_not_shift_on_empty_pages():
    raw = '<!--doc:abc-->\n# 标题\n<!--page:0-->\n\n<!--page:1-->\n营收同比增长。\n<!--page:2-->\n净利润下降。'
    pages = restored_pages(raw, set())
    assert (1, ['营收同比增长。']) in pages
    assert (2, ['净利润下降。']) in pages


def test_rrf_rewards_agreement_and_ties_are_stable():
    ranks = rrf([[1, 2, 3], [3, 2, 4]])
    assert {index for index, _ in ranks[:3]} == {1, 2, 3}
    assert ranks[0][0] == 3
    assert rrf([[2], [1]])[0][0] == 1
    assert temporal_markers('统计期为2026.07.05-2026.07.12') >= {
        'ym:2026-07', 'ymd:2026-07-05', 'ymd:2026-07-12'}


def test_token_budget_counts_token_ids_not_batch_keys():
    class Tokenizer:
        def apply_chat_template(self, messages, **kwargs):
            assert kwargs['return_dict'] is False
            return list(range(200))
    answerer = Answerer.__new__(Answerer)
    answerer.tokenizer = Tokenizer()
    assert answerer.token_count([]) == 200


class FakeRetriever:
    manifest = {'generation': 'test'}
    chunks = []
    by_id = {}

    def search(self, query, filters, top_k, mode='rerank'):
        validate_filters(filters)
        if not query.strip():
            raise ValueError('empty query')
        return {'hits': [], 'filters': filters}


class FakeAnswerer:
    def llm_health(self):
        return {'model': 'test'}

    def close(self):
        pass

    def ask(self, *args):
        return {'answerable': False}


def test_api_filters_auth_and_source_path(tmp_path, monkeypatch):
    config = {'data_dir': str(tmp_path), 'pdf_dir': str(tmp_path)}
    app = create_app(config, FakeRetriever(), FakeAnswerer())
    with TestClient(app) as client:
        assert client.get('/health').status_code == 200
        assert client.post('/search', json={'query': '中芯国际', 'filters': {'typo': 'bad'}}).status_code == 422
        assert client.post('/search', json={'query': '  '}).status_code == 422
        assert client.post('/search', json={'query': '中芯国际', 'top_k': 99}).status_code == 422
        assert client.get('/sources/unknown/pdf').status_code == 404
        app.state.retriever.by_id['escape'] = {'file': '../secret.pdf'}
        assert client.get('/sources/escape/pdf').status_code == 404
        app.state.gate.acquire()
        assert client.post('/ask', json={'query': '中芯国际'}).status_code == 503
        app.state.gate.release()
        monkeypatch.setenv('RAG_API_KEY', 'testing-key')
        assert client.post('/ask', json={'query': '中芯国际'}).status_code == 401
        assert client.post('/ask', json={'query': '中芯国际'}, headers={'Authorization': 'Bearer testing-key'}).status_code == 200


def test_index_cache_removal_and_failed_build_preserve_snapshot(tmp_path, monkeypatch):
    import numpy as np
    from semi_rag import index as module
    model = tmp_path / 'model'
    model.mkdir()
    (model / 'config.json').write_text('{}')
    config = {'index_dir': str(tmp_path / 'index'), 'embedding_model': str(model),
              'embedding_max_tokens': 100, 'chunk_chars': 80, 'chunk_overlap': 8, 'batch_size': 2}
    data = [{'chunk_id': str(i), 'search_text': text, 'text': text, 'kind': 'text',
             'content_hash': text} for i, text in enumerate(['first', 'second'])]
    calls = []

    class TinyEncoder:
        def __init__(self, config):
            self.config = config
            self.tokenizer = self

        def encode(self, values):
            if isinstance(values, str):
                return list(range(len(values)))
            calls.extend(values)
            return np.array([[1., 0.] for _ in values], dtype='float32')

    monkeypatch.setattr(module, 'Encoder', TinyEncoder)
    monkeypatch.setattr(module, 'collect_chunks', lambda _: (list(data), {}))
    first = module.build_index(config)
    assert len(calls) == 2
    second = module.build_index(config)
    assert second['cache_hits'] == 2 and len(calls) == 2
    data.pop()
    third = module.build_index(config)
    assert third['report']['chunks'] == 1
    assert module.read_index(config)[3].ntotal == 1
    with pytest.raises(ValueError, match='token budget'):
        module.read_index({**config, 'embedding_max_tokens': 101})
    current = (tmp_path / 'index/current').resolve()
    data.append({'chunk_id': 'new', 'search_text': 'new', 'text': 'new', 'kind': 'text', 'content_hash': 'new'})

    class BrokenEncoder(TinyEncoder):
        def encode(self, values):
            if isinstance(values, str):
                return super().encode(values)
            raise RuntimeError('device failure')

    monkeypatch.setattr(module, 'Encoder', BrokenEncoder)
    with pytest.raises(RuntimeError, match='device failure'):
        module.build_index(config)
    assert (tmp_path / 'index/current').resolve() == current
    assert module.read_index(config)[3].ntotal == 1
