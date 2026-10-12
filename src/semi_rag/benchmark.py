"""HTTP evaluation of retrieval quality, grounded answers, and service capacity."""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import logging
import os
import platform
import random
import re
import statistics
import sys
import time
import unicodedata
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

import httpx

from semi_rag.benchmark_load import classify_response, run_phase, send_request
from semi_rag.eval_metrics import aggregate_retrieval, percentiles, ranking_metrics
from semi_rag.benchmark_resources import ResourceSampler

log = logging.getLogger(__name__)


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding='utf-8') as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id']:
                raise ValueError(f'{path}:{number}: missing string id')
            if not isinstance(row.get('query'), str) or not 1 <= len(row['query'].strip()) <= 1000:
                raise ValueError(f'{path}:{number}: query must have 1..1000 characters')
            if row.get('filters') is not None and not isinstance(row['filters'], dict):
                raise ValueError(f'{path}:{number}: filters must be an object')
            rows.append(row)
    if not rows or len({row['id'] for row in rows}) != len(rows):
        raise ValueError(f'{path}: empty dataset or duplicate IDs')
    return rows


def filters_for(row: dict, as_of: str) -> dict:
    filters = {key: value for key, value in (row.get('filters') or {}).items() if value is not None}
    unknown = set(filters) - {'date_from', 'date_to', 'as_of', 'org', 'doc_ids', 'kind',
                              'company', 'industry', 'page_from', 'page_to'}
    if unknown:
        raise ValueError(f'Unknown filters: {unknown}')
    filters.setdefault('as_of', as_of)
    for field in ('date_from', 'date_to', 'as_of'):
        if field in filters:
            filters[field] = date.fromisoformat(filters[field]).isoformat()
    if filters.get('date_from', '') > min(filters['as_of'], filters.get('date_to', filters['as_of'])):
        raise ValueError(f'{row["id"]}: empty date range')
    if 'doc_ids' in filters and (not isinstance(filters['doc_ids'], list) or not all(isinstance(v, str) for v in filters['doc_ids'])):
        raise ValueError('doc_ids must be a list of strings')
    if 'org' in filters and not isinstance(filters['org'], str):
        raise ValueError('org must be a string')
    for field in ('company', 'industry'):
        if field in filters and (not isinstance(filters[field], str) or not filters[field].strip()):
            raise ValueError(f'{field} must be a non-empty string')
    for field in ('page_from', 'page_to'):
        if field in filters and (type(filters[field]) is not int or filters[field] < 1):
            raise ValueError(f'{field} must be a positive integer')
    if filters.get('page_from', 1) > filters.get('page_to', float('inf')):
        raise ValueError('page_from is after page_to')
    if filters.get('kind', 'text') not in ('text', 'table', 'chart'):
        raise ValueError('Invalid evidence kind')
    return filters


def inside_filters(chunk: dict, filters: dict) -> bool:
    end = min(filters['as_of'], filters.get('date_to', filters['as_of']))
    return (bool(chunk.get('publish_date')) and filters.get('date_from', '') <= chunk['publish_date'] <= end
        and ('org' not in filters or chunk.get('org') == filters['org'])
        and ('kind' not in filters or chunk.get('kind') == filters['kind'])
        and ('doc_ids' not in filters or chunk.get('doc_id') in filters['doc_ids'])
        and ('industry' not in filters or chunk.get('industry') == filters['industry'])
        and ('company' not in filters or filters['company'] in chunk.get('stock_name', '')
             or filters['company'] in chunk.get('title', ''))
        and ('page_from' not in filters or chunk.get('page') is not None and chunk['page'] >= filters['page_from'])
        and ('page_to' not in filters or chunk.get('page') is not None and chunk['page'] <= filters['page_to']))


def validate_retrieval_dataset(rows: list[dict], chunks_path: Path | None, as_of: str):
    chunks = None
    if chunks_path:
        with chunks_path.open() as stream:
            chunks = {row['chunk_id']: row for row in map(json.loads, stream)}
    for row in rows:
        for field in ('gold_chunk_ids', 'gold_doc_ids'):
            if not isinstance(row.get(field), list) or not all(isinstance(v, str) and v for v in row[field]):
                raise ValueError(f'{row["id"]}: {field} must be a string list')
            if len(set(row[field])) != len(row[field]):
                raise ValueError(f'{row["id"]}: duplicate labels in {field}')
        if not row['gold_chunk_ids']:
            raise ValueError('Retrieval benchmark requires positive labels; put unanswerable questions in quality dataset')
        filters = filters_for(row, as_of)
        if chunks is not None:
            missing = set(row['gold_chunk_ids']) - chunks.keys()
            if missing:
                raise ValueError(f'{row["id"]}: gold IDs missing from index; rebuild labels')
            for cid in row['gold_chunk_ids']:
                if not inside_filters(chunks[cid], filters):
                    raise ValueError(f'{row["id"]}: gold evidence {cid} is excluded by filters')
                if chunks[cid]['doc_id'] not in row['gold_doc_ids']:
                    raise ValueError(f'{row["id"]}: chunk/document labels disagree')


def normalized(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', text))


def answer_checks(data: dict, row: dict, filters: dict) -> dict:
    sources = data.get('sources', [])
    lookup = {source.get('source_id'): source for source in sources if isinstance(source, dict)}
    references, valid, total_claims, cited_claims = 0, 0, 0, 0
    for claim in data.get('claims', []):
        total_claims += 1
        supported = False
        for citation in claim.get('citations', []):
            references += 1
            source = lookup.get(citation.get('source_id'))
            quote = citation.get('quote')
            full_evidence = '\n'.join(value for value in (
                source.get('context_before', '') if source else '',
                source.get('text', '') if source else '',
                source.get('context_after', '') if source else '') if value)
            if (source and isinstance(quote, str) and len(normalized(quote)) >= 8
                and normalized(quote) in normalized(full_evidence)):
                valid += 1
                supported = True
        cited_claims += supported
    expected = row.get('expected_answerable')
    answerable = data.get('answerable')
    status = data.get('status')
    all_terms = row.get('expected_all', [])
    any_terms = row.get('expected_any', [])
    forbidden = row.get('forbidden', [])
    text = normalized(data.get('answer', ''))
    anchor_pass = (all(normalized(term) in text for term in all_terms)
                   and (not any_terms or any(normalized(term) in text for term in any_terms))
                   and not any(normalized(term) in text for term in forbidden))
    status_correct = (answerable is expected and status == ('answered' if expected else 'insufficient_evidence')) if expected is not None else None
    filter_errors = sum(not inside_filters(source, filters) for source in sources)
    structure_ok = (valid == references and (not answerable or (references > 0 and total_claims > 0 and cited_claims == total_claims))
                    and len(lookup) == len(sources) and filter_errors == 0)
    return {'answerable': answerable, 'status': status, 'expected_answerable': expected,
        'status_correct': status_correct, 'anchor_pass': anchor_pass if all_terms or any_terms or forbidden else None,
        'citations': references, 'valid_excerpts': valid, 'claims': total_claims, 'cited_claims': cited_claims,
        'filter_violations': filter_errors, 'structure_ok': structure_ok,
        'regression_pass': bool(status_correct and anchor_pass and structure_ok),
        'source_pages_present': all(isinstance(s.get('page'), int) and s['page'] >= 1 for s in sources)}


class ReportWriter:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=False)
        self.directory = directory
        self.raw = (directory / 'requests.jsonl').open('w', encoding='utf-8')

    def append(self, row):
        self.raw.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')
        self.raw.flush()

    def save(self, report):
        temporary = self.directory / 'report.json.tmp'
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
        temporary.replace(self.directory / 'report.json')
        (self.directory / 'report.md').write_text(markdown_report(report))

    def close(self):
        self.raw.close()
        with (self.directory / 'requests.jsonl').open() as source, (self.directory / 'requests.csv').open('w') as target:
            fields = ['section', 'phase', 'number', 'query_id', 'mode', 'ok', 'outcome', 'status_code',
                      'latency_ms', 'schedule_lag_ms', 'answer_status', 'generation', 'error']
            writer = csv.DictWriter(target, fieldnames=fields, extrasaction='ignore')
            writer.writeheader()
            for line in source:
                writer.writerow(json.loads(line))


def markdown_report(report):
    def fmt(value):
        return 'N/A' if value is None else f'{value:.4f}' if isinstance(value, float) else str(value)
    lines = ['# RAG 测评报告', '', f'- 运行状态：{report.get("status", "running")}',
             f'- 开始时间：{report["started_at"]}', f'- 固定截至日期：{report["settings"]["as_of"]}',
             f'- 索引：{report.get("before", {}).get("stats", {}).get("generation", "unknown")}',
             '', '## 检索质量', '', '标注外的片段按不相关处理；弱标注结果不等于人工准确率。API 使用线上去重及每报告数量限制。', '',
             '| 模式 | 题数 | 失败 | P@5 | R@5 | Hit@5 | MRR@5 | nDCG@5 | 成功延迟 P95 ms |',
             '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
    for mode, data in report.get('retrieval', {}).get('modes', {}).items():
        fields = [mode, data['requests'], data['failed_requests']]
        fields += [data.get(name+'@5', {}).get('mean') for name in ('precision', 'evidence_recall', 'evidence_hit', 'mrr', 'ndcg')]
        fields.append(data['success_latency_ms']['p95'])
        lines.append('| ' + ' | '.join(fmt(value) for value in fields) + ' |')
    if 'quality' in report:
        data = report['quality']
        lines.extend(['', '## 问答回归', '', f'- 用例通过：{data["passed"]}/{data["questions"]}',
            f'- 结构引用有效率：{fmt(data["citation_excerpt_validity"])}',
            '- 此处验证显式预期、拒答与摘录匹配，不声称完成语义真实性评估。'])
    lines.extend(['', '## 并发与压力', '', '| 阶段 | 端点 | 负载 | 成功/发送 | 拒绝率 | 错误率 | 有效 RPS | 成功 P95 ms | 客户端丢弃 |',
                  '| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |'])
    for section in ('concurrency', 'stress'):
        for phase in report.get(section, []):
            lines.append('| ' + ' | '.join(fmt(v) for v in [section, phase['endpoint'], f'{phase["kind"]}:{phase["value"]}',
                f'{phase["successful"]}/{phase["sent"]}', phase['rejection_rate'], phase['error_rate'],
                phase['successful_throughput_rps'], phase['success_latency_ms']['p95'], phase['client_dropped']]) + ' |')
    lines.extend(['', '## 结论与限制', ''])
    lines.extend('- ' + message for message in report.get('warnings', []))
    lines.extend('- 未通过阈值：' + message for message in report.get('gate_failures', []))
    if report.get('error'):
        lines.append('- 执行错误：' + report['error'])
    lines.extend(['', '完整分位数、置信区间、分任务指标、逐秒窗口和恢复结果见 report.json；逐请求记录见 requests.jsonl / requests.csv。'])
    return '\n'.join(lines) + '\n'


async def snapshot(client):
    result = {}
    for name, path in (('health', '/health'), ('stats', '/stats')):
        response = await client.get(path, timeout=10)
        response.raise_for_status()
        result[name] = response.json()
    return result


async def retrieval_evaluation(client, rows, args, writer, generation):
    modes, tasks = {}, {}
    for mode in args.modes:
        records = []
        for index, row in enumerate(rows):
            filters = filters_for(row, args.as_of)
            payload = {'query': row['query'], 'filters': filters, 'top_k': max(args.ks), 'mode': mode}
            started = time.monotonic()
            response = None
            try:
                response = await client.post('/search', json=payload)
                outcome, data = classify_response(response, '/search')
            except httpx.HTTPError as exc:
                outcome, data = type(exc).__name__, {}
            hits = data.get('hits', []) if outcome == 'success' else []
            if outcome == 'success' and data['generation'] != generation:
                raise RuntimeError('Index changed during retrieval evaluation; results cannot be combined')
            record = {'section': 'retrieval', 'query_id': row['id'], 'mode': mode,
                'ok': outcome == 'success', 'outcome': outcome, 'status_code': response.status_code if response is not None else None,
                'latency_ms': (time.monotonic() - started) * 1000, 'task': row.get('task', 'unspecified'),
                'generation': data.get('generation'),
                'gold_count': len(row['gold_chunk_ids']), 'hits': [h['chunk_id'] for h in hits],
                'filter_violations': sum(not inside_filters(h, filters) for h in hits), 'metrics': {}}
            for k in args.ks:
                record['metrics'].update(ranking_metrics(hits, row, k))
            writer.append(record)
            records.append(record)
            if (index + 1) % 10 == 0 or index + 1 == len(rows):
                log.info('retrieval %s %d/%d', mode, index + 1, len(rows))
        modes[mode] = aggregate_retrieval(records, args.ks, args.seed)
        tasks[mode] = {task: aggregate_retrieval([r for r in records if r['task'] == task], args.ks, args.seed)
                       for task in sorted({r['task'] for r in records})}
    return {'modes': modes, 'by_task': tasks, 'label_origins': dict(Counter(row.get('label_origin', 'unspecified') for row in rows)),
            'bootstrap_unit': 'question, 1000 resamples; correlated questions from the same report can narrow intervals'}


async def quality_evaluation(client, rows, args, writer):
    records = []
    for row in rows:
        filters = filters_for(row, args.as_of)
        started = time.monotonic()
        checks, error = {}, None
        try:
            response = await client.post('/ask', json={'query': row['query'], 'filters': filters, 'top_k': args.top_k})
            outcome, data = classify_response(response, '/ask')
            if outcome == 'success':
                checks = answer_checks(data, row, filters)
            else:
                error = outcome
        except httpx.HTTPError as exc:
            data, error = {}, type(exc).__name__
        record = {'section': 'quality', 'query_id': row['id'], 'ok': error is None,
                  'latency_ms': (time.monotonic() - started) * 1000, 'checks': checks,
                  'error': error, 'response': data}
        records.append(record)
        writer.append(record)
        log.info('quality %s: %s', row['id'], checks.get('regression_pass', False))
    citations = sum(r['checks'].get('citations', 0) for r in records)
    answerable_rows = [r for r, q in zip(records, rows) if q['expected_answerable']]
    negative_rows = [r for r, q in zip(records, rows) if not q['expected_answerable']]
    return {'questions': len(records), 'passed': sum(r['checks'].get('regression_pass', False) for r in records),
        'http_failures': sum(not r['ok'] for r in records),
        'answerable_case_pass_rate': sum(r['checks'].get('regression_pass', False) for r in answerable_rows) / len(answerable_rows) if answerable_rows else None,
        'unanswerable_case_pass_rate': sum(r['checks'].get('regression_pass', False) for r in negative_rows) / len(negative_rows) if negative_rows else None,
        'citation_excerpt_validity': sum(r['checks'].get('valid_excerpts', 0) for r in records) / citations if citations else None,
        'latency_ms': percentiles([r['latency_ms'] for r in records if r['ok']])}


async def recovery(client, workload, endpoint, args):
    started = time.monotonic()
    attempts = []
    deadline = started + args.recovery_timeout
    while time.monotonic() < deadline:
        try:
            health = await client.get('/health', timeout=max(.01, min(5, deadline - time.monotonic())))
            entry = workload[0]
            record = await asyncio.wait_for(send_request(client, endpoint, entry['payload'], entry['id'],
                time.monotonic(), started, len(attempts)), timeout=max(.01, deadline - time.monotonic()))
            attempts.append(record)
            if health.status_code == 200 and record['outcome'] == 'success':
                return {'recovered': True, 'seconds': time.monotonic() - started, 'attempts': attempts}
        except (httpx.HTTPError, TimeoutError):
            pass
        await asyncio.sleep(min(1, max(0, deadline - time.monotonic())))
    return {'recovered': False, 'seconds': time.monotonic() - started, 'attempts': attempts}


async def benchmark(args, writer):
    rows = load_jsonl(args.dataset)
    if args.limit:
        rows = rows[:args.limit]
    for row in rows:
        filters_for(row, args.as_of)
    headers = {'Authorization': 'Bearer ' + os.environ[args.api_key_env]} if os.environ.get(args.api_key_env) else {}
    report = {'schema_version': 1, 'status': 'running', 'started_at': datetime.now(timezone.utc).isoformat(),
        'settings': {key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()},
        'dataset_sha256': hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
        'environment': {'python': sys.version.split()[0], 'platform': platform.platform(), 'httpx': httpx.__version__},
            'warnings': ['未标注的片段按不相关处理，弱标注精确率/召回率不能作为最终人工质量结论。',
                     'HTTP 200 的拒答单独统计为业务状态，不等同于有用回答；503 busy 不计入有效吞吐。',
                     '负载为非流式请求，延迟为完整响应时间，不是首 token 延迟。'], 'gate_failures': []}
    if 'retrieval' in args.sections and args.chunks is None:
        report['warnings'].append('未提供 --chunks，仅验证标签格式，未验证标注片段是否属于线上索引或被过滤条件排除。')
    limits = httpx.Limits(max_connections=args.max_inflight + 4, max_keepalive_connections=args.max_inflight + 4)
    sampler = ResourceSampler(args.server_pid, args.sample_interval)
    telemetry = asyncio.create_task(sampler.run())
    async with httpx.AsyncClient(base_url=args.url.rstrip('/'), headers=headers, trust_env=False,
                                timeout=httpx.Timeout(args.timeout, connect=5), limits=limits) as client:
        try:
            report['before'] = await snapshot(client)
            generation = report['before']['stats']['generation']
            if args.chunks and 'retrieval' in args.sections:
                expected = report['before']['stats'].get('files', {}).get('chunks.jsonl')
                if expected and hashlib.sha256(args.chunks.read_bytes()).hexdigest() != expected:
                    raise ValueError('Local label corpus does not match the running server index')
            if 'retrieval' in args.sections:
                validate_retrieval_dataset(rows, args.chunks, args.as_of)
            quality_rows = load_jsonl(args.quality_dataset) if 'quality' in args.sections else []
            for row in quality_rows:
                if type(row.get('expected_answerable')) is not bool:
                    raise ValueError('quality rows require expected_answerable boolean')
                for field in ('expected_all', 'expected_any', 'forbidden'):
                    if not isinstance(row.get(field, []), list) or not all(isinstance(v, str) and v for v in row.get(field, [])):
                        raise ValueError(f'Invalid quality anchors: {field}')
                filters_for(row, args.as_of)
            for index in range(args.warmup):
                response = await client.post('/search', json={'query': rows[index % len(rows)]['query'], 'mode': 'rerank',
                    'top_k': args.top_k, 'filters': filters_for(rows[index % len(rows)], args.as_of)})
                if classify_response(response, '/search')[0] != 'success':
                    raise RuntimeError('Warmup failed; service must be healthy and available')
            writer.save(report)
            if 'retrieval' in args.sections:
                report['retrieval'] = await retrieval_evaluation(client, rows, args, writer, generation)
                writer.save(report)
            if 'quality' in args.sections:
                report['quality'] = await quality_evaluation(client, quality_rows, args, writer)
                report['quality_dataset_sha256'] = hashlib.sha256(args.quality_dataset.read_bytes()).hexdigest()
                writer.save(report)
            shuffled = list(rows)
            random.Random(args.seed).shuffle(shuffled)
            for section in ('concurrency', 'stress'):
                if section not in args.sections:
                    continue
                report[section] = []
                for endpoint_name in args.endpoints:
                    endpoint = '/' + endpoint_name
                    workload = [{'id': row['id'], 'payload': {'query': row['query'], 'filters': filters_for(row, args.as_of),
                        'top_k': args.top_k, **({'mode': args.load_mode} if endpoint == '/search' else {})}} for row in shuffled]
                    warm = await client.post(endpoint, json=workload[0]['payload'])
                    if classify_response(warm, endpoint)[0] != 'success':
                        raise RuntimeError(f'{endpoint} warmup failed')
                    values = args.concurrency if section == 'concurrency' else args.rates
                    for value in values:
                        log.info('%s %s load=%s', section, endpoint, value)
                        phase_name = f'{section}-{endpoint_name}-{value}'
                        summary, records = await run_phase(client, workload, endpoint=endpoint,
                            kind='closed' if section == 'concurrency' else 'open', value=value,
                            duration=args.concurrency_duration if section == 'concurrency' else args.stage_seconds,
                            requests=(args.requests or None) if section == 'concurrency' else None,
                            max_inflight=args.max_inflight, max_requests=args.max_requests,
                            min_interval=args.min_interval, error_stop=args.error_stop, min_error_samples=args.min_error_samples,
                            sink=lambda row: writer.append({'section': section, 'phase': phase_name, **row}))
                        generations = {row['generation'] for row in records if row.get('generation')}
                        summary['generations'] = sorted(generations)
                        if summary['successful'] < 20:
                            report['warnings'].append(f'{phase_name}: 只有 {summary["successful"]} 个成功响应，尾延迟与吞吐样本不足以估计稳定容量。')
                        summary['recovery'] = await recovery(client, workload, endpoint, args)
                        report[section].append(summary)
                        log.info('%s: success=%d reject=%d error=%d goodput=%.2f/s recovered=%s', phase_name,
                            summary['successful'], summary['rejected'], summary['errors'], summary['successful_throughput_rps'], summary['recovery']['recovered'])
                        writer.save(report)
                        if summary['abort_reason'] or not summary['recovery']['recovered']:
                            raise RuntimeError('Stopped after hard-error threshold or failed recovery')
                        await asyncio.sleep(args.cooldown)
            report['after'] = await snapshot(client)
            if report['after']['stats']['generation'] != generation:
                report['warnings'].append('测评期间索引发生变化，负载结果包含多个版本；不得直接比较前后质量。')
            report['gate_failures'] = check_gates(report, args)
            report['status'] = 'failed_gates' if report['gate_failures'] else 'complete'
        except asyncio.CancelledError:
            report.update(status='interrupted', error='User interrupted the benchmark; partial results only')
            raise
        except Exception as exc:
            report.update(status='failed', error=f'{type(exc).__name__}: {exc}')
            log.exception('Benchmark stopped')
        finally:
            sampler.stop.set()
            await telemetry
            report['resources'] = sampler.summary()
            with (writer.directory / 'resources.jsonl').open('w') as stream:
                for row in sampler.samples:
                    stream.write(json.dumps(row) + '\n')
            report['finished_at'] = datetime.now(timezone.utc).isoformat()
            writer.save(report)
    return report


def check_gates(report, args):
    failures = []
    if 'retrieval' in report:
        for mode, data in report['retrieval']['modes'].items():
            if data['failed_requests'] or data['filter_violations']:
                failures.append(f'{mode}: retrieval errors or filter violations')
        data = report['retrieval']['modes'].get('rerank', next(iter(report['retrieval']['modes'].values())))
        for metric, minimum in (('precision', args.min_precision), ('evidence_recall', args.min_recall)):
            if minimum is not None and data[f'{metric}@{args.gate_k}']['mean'] < minimum:
                failures.append(f'{metric}@{args.gate_k} < {minimum}')
    if 'quality' in report and report['quality']['passed'] < report['quality']['questions']:
        failures.append('Some answer regression cases failed; inspect raw responses')
    for section in ('concurrency', 'stress'):
        for phase in report.get(section, []):
            name = f'{section} {phase["endpoint"]} {phase["value"]}'
            if args.max_p95_ms is not None and (phase['success_latency_ms']['p95'] is None or phase['success_latency_ms']['p95'] > args.max_p95_ms):
                failures.append(name + ': success p95 outside SLO or no successful sample')
            if args.max_rejection_rate is not None and (phase['rejection_rate'] is None or phase['rejection_rate'] > args.max_rejection_rate):
                failures.append(name + ': rejection rate outside SLO')
            if phase['errors'] or not phase['successful'] or not phase['requested_count_completed'] or phase['client_dropped'] or phase['arrival_schedule_truncated']:
                failures.append(name + ': incomplete load delivery, hard errors, or no successful responses')
    return failures


def csv_numbers(value, cast):
    try:
        values = [cast(item) for item in value.split(',')]
    except ValueError as exc:
        raise argparse.ArgumentTypeError('Expected comma-separated numbers') from exc
    if not values or any(v <= 0 or not float(v) < float('inf') for v in values):
        raise argparse.ArgumentTypeError('Values must be positive and finite')
    return sorted(set(values))


def main():
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8090')
    parser.add_argument('--dataset', type=Path, default=root / 'data/rag/eval/questions.jsonl')
    parser.add_argument('--quality-dataset', type=Path, default=root / 'data/rag/eval/quality_cases.jsonl')
    parser.add_argument('--chunks', type=Path, help='Optional local chunks.jsonl for label/filter and index-hash validation')
    parser.add_argument('--output', type=Path, help='New output directory; existing directories are never overwritten')
    parser.add_argument('--sections', nargs='+', choices=['retrieval', 'quality', 'concurrency', 'stress'], default=['retrieval', 'quality', 'concurrency', 'stress'])
    parser.add_argument('--modes', nargs='+', choices=['bm25', 'dense', 'hybrid', 'rerank'], default=['bm25', 'dense', 'hybrid', 'rerank'])
    parser.add_argument('--ks', type=lambda x: csv_numbers(x, int), default=[1, 5, 10])
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--as-of', default=date.today().isoformat())
    parser.add_argument('--warmup', type=int, default=2)
    parser.add_argument('--endpoints', nargs='+', choices=['search', 'ask'], default=['search'])
    parser.add_argument('--load-mode', choices=['bm25', 'dense', 'hybrid', 'rerank'], default='rerank')
    parser.add_argument('--top-k', type=int, default=6)
    parser.add_argument('--concurrency', type=lambda x: csv_numbers(x, int), default=[1, 2, 4, 8])
    parser.add_argument('--requests', type=int, default=40, help='Total requests per closed-loop stage, not per worker; 0 runs until the stage duration')
    parser.add_argument('--concurrency-duration', type=float, default=60, help='Per-stage time ceiling; in-flight requests drain afterward')
    parser.add_argument('--min-interval', type=float, default=.1, help='Minimum per-user request-start interval')
    parser.add_argument('--rates', type=lambda x: csv_numbers(x, float), default=[1., 2., 4., 8.])
    parser.add_argument('--stage-seconds', type=float, default=15)
    parser.add_argument('--max-inflight', type=int, default=32)
    parser.add_argument('--max-requests', type=int, default=10000)
    parser.add_argument('--timeout', type=float, default=120)
    parser.add_argument('--cooldown', type=float, default=2)
    parser.add_argument('--recovery-timeout', type=float, default=30)
    parser.add_argument('--error-stop', type=float, default=.3, help='Hard-error fraction in the latest minimum sample window; busy rejections excluded')
    parser.add_argument('--min-error-samples', type=int, default=20)
    parser.add_argument('--api-key-env', default='RAG_API_KEY')
    parser.add_argument('--gate-k', type=int, default=5)
    parser.add_argument('--min-precision', type=float)
    parser.add_argument('--min-recall', type=float)
    parser.add_argument('--max-p95-ms', type=float)
    parser.add_argument('--max-rejection-rate', type=float)
    parser.add_argument('--server-pid', type=int, help='Optional local RAG API PID for CPU/RSS sampling (not remote-server metrics)')
    parser.add_argument('--sample-interval', type=float, default=2)
    args = parser.parse_args()
    if not 1 <= args.top_k <= (12 if 'ask' in args.endpoints or 'quality' in args.sections else 32) or max(args.ks) > 32:
        parser.error('top-k or ks exceeds API bounds')
    if not 1 <= args.max_inflight <= 256 or max(args.concurrency) > args.max_inflight:
        parser.error('Concurrency must not exceed max-inflight (1..256)')
    if not 0 <= args.requests <= args.max_requests <= 100000 or args.max_requests < 1 or args.limit < 0 or args.warmup < 0:
        parser.error('Invalid request/limit counts')
    for key in ('stage_seconds', 'concurrency_duration', 'timeout', 'recovery_timeout', 'min_interval'):
        value = getattr(args, key)
        if not 0 < value <= 86400:
            parser.error(f'{key} must be positive and <=86400')
    if not 0 <= args.cooldown <= 86400 or not 0 < args.error_stop <= 1 or args.min_error_samples < 1:
        parser.error('Invalid cooldown or error stop settings')
    if not .1 <= args.sample_interval <= 60 or args.server_pid is not None and args.server_pid <= 0:
        parser.error('Invalid process telemetry settings')
    if args.max_p95_ms is not None and not 0 < args.max_p95_ms < float('inf'):
        parser.error('max-p95-ms must be positive and finite')
    if args.gate_k not in args.ks:
        parser.error('gate-k must be in ks')
    for key in ('min_precision', 'min_recall', 'max_rejection_rate'):
        value = getattr(args, key)
        if value is not None and not 0 <= value <= 1:
            parser.error(f'{key} must be in [0,1]')
    try:
        args.as_of = date.fromisoformat(args.as_of).isoformat()
        url = httpx.URL(args.url)
    except (ValueError, httpx.InvalidURL) as exc:
        parser.error(str(exc))
    if url.scheme not in ('http', 'https') or not url.host or url.username or url.password or url.query or url.fragment:
        parser.error('url must be an HTTP(S) base URL without credentials, query, or fragment')
    args.output = args.output or root / 'data/rag/eval/benchmarks' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + os.urandom(3).hex())
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    writer = ReportWriter(args.output)
    try:
        report = asyncio.run(benchmark(args, writer))
    except KeyboardInterrupt:
        log.warning('Interrupted; raw request records are preserved')
        raise SystemExit(130)
    except Exception as exc:
        report = {'status': 'failed', 'started_at': datetime.now(timezone.utc).isoformat(),
                  'settings': {'as_of': args.as_of}, 'error': f'{type(exc).__name__}: {exc}',
                  'warnings': ['Setup failed before the benchmark could run'], 'gate_failures': []}
        writer.save(report)
    finally:
        writer.close()
    print(json.dumps({'status': report['status'], 'output': str(args.output), 'gate_failures': report['gate_failures']}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report['status'] == 'complete' else 1)


if __name__ == '__main__':
    main()
