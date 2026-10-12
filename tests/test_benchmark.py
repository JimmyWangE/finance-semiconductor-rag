import asyncio
import io
import json
import math
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from semi_rag.benchmark import (ReportWriter, answer_checks, check_gates, filters_for, load_jsonl,
                                 validate_retrieval_dataset)
from semi_rag.benchmark_load import classify_response, run_phase, send_request
from semi_rag.benchmark_resources import ResourceSampler
from semi_rag.eval_metrics import aggregate_retrieval, percentiles, ranking_metrics, summarize_load


def hit(cid, doc='d1'):
    return {'chunk_id': cid, 'doc_id': doc}


def test_precision_recall_and_ranking_hand_computed():
    row = {'gold_chunk_ids': ['a', 'b', 'c'], 'gold_doc_ids': ['d1']}
    result = ranking_metrics([hit('a'), hit('x'), hit('b')], row, 5)
    assert result['precision@5'] == 2 / 5
    assert result['evidence_recall@5'] == 2 / 3
    assert result['evidence_hit@5'] == 1
    assert result['ap@5'] == pytest.approx((1 + 2 / 3) / 3)
    assert result['mrr@5'] == 1
    assert result['ndcg@5'] == pytest.approx(1.5 / (1 + 1 / math.log2(3) + .5))
    assert result['returned@5'] == 3


def test_duplicates_and_missing_slots_do_not_inflate_precision():
    row = {'gold_chunk_ids': ['a', 'b'], 'gold_doc_ids': ['d1']}
    result = ranking_metrics([hit('a'), hit('a'), hit('a')], row, 3)
    assert result['precision@3'] == 1 / 3
    assert result['evidence_recall@3'] == .5
    assert ranking_metrics([hit('a')], row, 5)['precision@5'] == .2
    assert ranking_metrics([], row, 5)['evidence_recall@5'] == 0
    perfect = ranking_metrics([hit('a'), hit('b')], row, 2)
    assert perfect['f1@2'] == perfect['ndcg@2'] == perfect['ap@2'] == 1
    negative = ranking_metrics([], {'gold_chunk_ids': [], 'gold_doc_ids': []}, 5)
    assert negative['evidence_recall@5'] is None


def test_macro_micro_and_failures_have_explicit_denominators():
    row = {'gold_chunk_ids': ['a'], 'gold_doc_ids': ['d1']}
    records = [{'ok': True, 'gold_count': 1, 'latency_ms': 10, 'metrics': ranking_metrics([hit('a')], row, 1)},
               {'ok': False, 'gold_count': 1, 'latency_ms': 1, 'metrics': ranking_metrics([], row, 1)}]
    result = aggregate_retrieval(records, [1])
    assert result['precision@1']['mean'] == .5
    assert result['micro_recall@1'] == .5
    assert result['failed_requests'] == 1
    assert result['success_latency_ms']['mean'] == 10


def test_percentiles_empty_and_interpolated():
    assert percentiles([])['p95'] is None
    assert percentiles([10])['p99'] == 10
    assert percentiles([0, 100])['p95'] == 95


def test_load_rejections_never_count_as_goodput():
    records = [{'outcome': outcome, 'status_code': code, 'latency_ms': latency, 'schedule_lag_ms': 0,
                'answer_status': None} for outcome, code, latency in
               [('success', 200, 1000), ('rejected', 503, 2), ('rejected', 503, 2), ('timeout', None, 2000), ('client_dropped', None, 0)]]
    result = summarize_load(records, 4, scheduled_seconds=3, peak_inflight=3)
    assert result['successful_throughput_rps'] == .25
    assert result['attempt_throughput_rps'] == 1
    assert result['rejection_rate'] == .5 and result['error_rate'] == .25
    assert result['success_latency_ms']['p95'] == 1000
    assert result['client_dropped'] == 1 and result['delivery_rate'] == .8


@pytest.mark.parametrize('code,body,headers,expected', [
    (200, {'hits': [], 'generation': 'v1'}, {}, 'success'),
    (200, {'bad': 'response'}, {}, 'invalid_response'),
    (200, ['not-an-object'], {}, 'invalid_response'),
    (503, {'detail': 'Model is busy; retry shortly'}, {'Retry-After': '3'}, 'rejected'),
    (503, {'detail': 'Engine crashed'}, {}, 'http_error'),
    (429, {}, {}, 'rejected'),
    (500, {}, {}, 'http_error'),
])
def test_http_outcomes(code, body, headers, expected):
    assert classify_response(httpx.Response(code, json=body, headers=headers), '/search')[0] == expected


def phase(client, **changes):
    options = dict(endpoint='/search', kind='closed', value=3, duration=2., requests=12,
                   max_inflight=3, max_requests=100, min_interval=.001,
                   error_stop=.5, min_error_samples=3)
    options.update(changes)
    return run_phase(client, [{'id': 'q', 'payload': {'query': 'test'}}], **options)


def test_closed_loop_exact_request_count_and_real_concurrency():
    async def run():
        active, peak = 0, 0
        async def handler(request):
            nonlocal active, peak
            active += 1
            peak = max(active, peak)
            await asyncio.sleep(.01)
            active -= 1
            return httpx.Response(200, json={'hits': [], 'generation': 'v1'})
        async with httpx.AsyncClient(base_url='http://test', transport=httpx.MockTransport(handler)) as client:
            summary, records = await phase(client)
        assert len(records) == summary['successful'] == 12
        assert peak == summary['peak_inflight'] == 3
        assert len({record['number'] for record in records}) == 12
    asyncio.run(run())


def test_open_loop_drops_instead_of_hiding_client_backlog():
    async def run():
        async def handler(request):
            await asyncio.sleep(.06)
            return httpx.Response(200, json={'hits': [], 'generation': 'v1'})
        async with httpx.AsyncClient(base_url='http://test', transport=httpx.MockTransport(handler)) as client:
            summary, records = await phase(client, kind='open', value=100, duration=.12, requests=None, max_inflight=2)
        assert summary['scheduled'] == 12
        assert summary['client_dropped'] > 0
        assert summary['peak_inflight'] <= 2
        assert summary['successful'] + summary['client_dropped'] == 12
        assert summary['wall_seconds_including_drain'] >= .12
    asyncio.run(run())


def test_hard_errors_abort_but_busy_rejections_do_not():
    async def run(code, headers, body):
        async with httpx.AsyncClient(base_url='http://test', transport=httpx.MockTransport(
            lambda _: httpx.Response(code, json=body, headers=headers))) as client:
            return await phase(client, value=1)
    summary, records = asyncio.run(run(500, {}, {}))
    assert summary['abort_reason'] == 'hard_error_threshold' and len(records) == 3
    summary, records = asyncio.run(run(503, {'Retry-After': '1'}, {'detail': 'Model busy'}))
    assert summary['abort_reason'] is None and len(records) == 12
    assert summary['successful'] == 0 and summary['rejected'] == 12


def test_duration_mode_and_open_request_cap_are_reported():
    async def run():
        async with httpx.AsyncClient(base_url='http://test', transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={'hits': [], 'generation': 'v1'}))) as client:
            duration, records = await phase(client, value=1, duration=.05, requests=None, min_interval=.01)
            assert 1 <= len(records) < 12
            assert duration['configured_requests'] is None and duration['wall_seconds_including_drain'] >= .05
            capped, records = await phase(client, kind='open', value=100, duration=.05, requests=None, max_requests=2)
            assert capped['intended_arrivals'] == 5 and len(records) == 2
            assert capped['arrival_schedule_truncated']
    asyncio.run(run())


def test_dataset_validation_checks_stale_and_excluded_labels(tmp_path):
    chunks = tmp_path / 'chunks.jsonl'
    chunks.write_text(json.dumps({'chunk_id': 'a', 'doc_id': 'd', 'publish_date': '2026-01-01', 'kind': 'text', 'org': '机构'}) + '\n')
    row = {'id': 'q', 'query': '测试', 'gold_chunk_ids': ['a'], 'gold_doc_ids': ['d'], 'filters': {}}
    validate_retrieval_dataset([row], chunks, '2026-09-08')
    with pytest.raises(ValueError, match='excluded'):
        validate_retrieval_dataset([{**row, 'filters': {'as_of': '2025-01-01'}}], chunks, '2026-09-08')
    with pytest.raises(ValueError, match='missing'):
        validate_retrieval_dataset([{**row, 'gold_chunk_ids': ['stale']}], chunks, '2026-09-08')
    with pytest.raises(ValueError):
        filters_for({**row, 'filters': {'typo': 'not-ignored'}}, '2026-09-08')


def test_benchmark_metadata_filters_match_online_contract():
    chunk = {'publish_date': '2026-01-01', 'org': '机构', 'doc_id': 'd', 'kind': 'text',
             'industry': '半导体', 'stock_name': '中芯国际', 'title': '中芯国际报告', 'page': 2}
    filters = filters_for({'filters': {'company': '中芯国际', 'industry': '半导体', 'page_from': 2, 'page_to': 2}}, '2026-09-08')
    from semi_rag.benchmark import inside_filters
    assert inside_filters(chunk, filters)
    with pytest.raises(ValueError):
        filters_for({'filters': {'page_from': 3, 'page_to': 2}}, '2026-09-08')


def test_quality_refusal_and_citations_not_self_reported():
    filters = {'as_of': '2026-09-08'}
    source = {'source_id': 'S1', 'text': '产能利用率达到95.8%，接近满产。', 'publish_date': '2025-12-05', 'page': 2}
    data = {'answerable': True, 'status': 'answered', 'answer': '产能利用率为95.8%。', 'sources': [source],
            'claims': [{'text': '产能利用率为95.8%。', 'citations': [{'source_id': 'S1', 'quote': source['text']}]}]}
    row = {'expected_answerable': True, 'expected_all': ['95.8', '%']}
    assert answer_checks(data, row, filters)['regression_pass']
    source['context_after'] = '相邻段落同样可以作为连续引用证据。'
    data['claims'][0]['citations'][0]['quote'] = source['context_after']
    assert answer_checks(data, row, filters)['regression_pass']
    data['claims'][0]['citations'][0]['quote'] = '不存在的来源摘录内容'
    assert not answer_checks(data, row, filters)['regression_pass']
    refused = {'answerable': False, 'status': 'validation_failed', 'answer': '无法回答', 'sources': [], 'claims': []}
    assert not answer_checks(refused, {'expected_answerable': False}, filters)['regression_pass']
    refused['status'] = 'insufficient_evidence'
    assert answer_checks(refused, {'expected_answerable': False}, filters)['regression_pass']


def test_resource_sampler_and_output_not_overwritten(tmp_path):
    sampler = ResourceSampler(os.getpid(), 1)
    sampler.sample()
    assert sampler.summary()['server_rss_mb']['mean'] > 0
    directory = tmp_path / 'report'
    writer = ReportWriter(directory)
    writer.append({'section': 'test', 'latency_ms': 1})
    writer.close()
    assert 'latency_ms' in (directory / 'requests.csv').read_text()
    with pytest.raises(FileExistsError):
        ReportWriter(directory)


def test_gates_distinguish_saturation_from_hard_failure():
    args = SimpleNamespace(min_precision=None, min_recall=None, gate_k=5,
                           max_p95_ms=None, max_rejection_rate=None)
    phase = {'endpoint': '/search', 'value': 8, 'success_latency_ms': {'p95': 400},
             'rejection_rate': .9, 'errors': 0, 'successful': 5,
             'requested_count_completed': True, 'client_dropped': 0, 'arrival_schedule_truncated': False}
    report = {'stress': [phase]}
    assert check_gates(report, args) == []
    args.max_rejection_rate = .01
    assert check_gates(report, args)
    args.max_rejection_rate = None
    phase['successful'] = 0
    assert check_gates(report, args)


def test_malformed_success_is_not_valid_generation():
    response = httpx.Response(200, json={'answerable': True, 'status': 'answered', 'answer': 'fabricated',
                              'generation': 'v1', 'sources': [], 'claims': []})
    assert classify_response(response, '/ask')[0] == 'invalid_response'
