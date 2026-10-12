"""Metric definitions shared by offline and HTTP evaluations."""
from __future__ import annotations

import math
import random
import statistics
from collections import Counter


def ranking_metrics(hits: list[dict], row: dict, k: int) -> dict:
    if k < 1:
        raise ValueError('k must be positive')
    gold = set(row['gold_chunk_ids'])
    gold_docs = set(row.get('gold_doc_ids', []))
    seen, relevant = set(), []
    for hit in hits[:k]:
        cid = hit['chunk_id']
        relevant.append(int(cid in gold and cid not in seen))
        seen.add(cid)
    tp = sum(relevant)
    precision = tp / k  # Missing returned slots count as nonrelevant, not a better precision.
    recall = tp / len(gold) if gold else None
    ideal = sum(1 / math.log2(i + 2) for i in range(min(len(gold), k)))
    dcg = sum(value / math.log2(i + 2) for i, value in enumerate(relevant))
    running, ap = 0, 0.
    for i, value in enumerate(relevant, 1):
        running += value
        ap += value * running / i
    return {f'precision@{k}': precision, f'evidence_recall@{k}': recall,
        f'evidence_hit@{k}': float(tp > 0) if gold else None,
        f'f1@{k}': 2 * precision * recall / (precision + recall) if recall is not None and precision + recall else (0. if gold else None),
        f'mrr@{k}': next((1 / (i + 1) for i, value in enumerate(relevant) if value), 0.) if gold else None,
        f'ndcg@{k}': dcg / ideal if ideal else None,
        f'ap@{k}': ap / min(len(gold), k) if gold else None,
        f'doc_hit@{k}': float(any(hit['doc_id'] in gold_docs for hit in hits[:k])) if gold_docs else None,
        f'tp@{k}': tp, f'returned@{k}': len(hits[:k])}


def percentiles(values: list[float]) -> dict:
    values = sorted(values)
    def quantile(p):
        position = (len(values) - 1) * p
        low = int(position)
        return values[low] + (values[min(low + 1, len(values) - 1)] - values[low]) * (position - low)
    if not values:
        return {'n': 0, 'mean': None, 'p50': None, 'p90': None, 'p95': None, 'p99': None, 'max': None}
    return {'n': len(values), 'mean': statistics.mean(values), 'p50': quantile(.5), 'p90': quantile(.9),
            'p95': quantile(.95), 'p99': quantile(.99), 'max': values[-1]}


def bootstrap_mean(values: list[float], seed=42, samples=1000) -> dict:
    if not values:
        return {'mean': None, 'ci95': None, 'n': 0}
    rng = random.Random(seed)
    means = sorted(statistics.mean(rng.choices(values, k=len(values))) for _ in range(samples))
    return {'mean': statistics.mean(values), 'ci95': [means[int(.025 * samples)], means[min(samples - 1, int(.975 * samples))]], 'n': len(values)}


def aggregate_retrieval(records: list[dict], ks: list[int], seed=42) -> dict:
    summary = {'requests': len(records), 'failed_requests': sum(not row['ok'] for row in records)}
    for k in ks:
        for metric in ('precision', 'evidence_recall', 'evidence_hit', 'f1', 'mrr', 'ndcg', 'ap', 'doc_hit'):
            name = f'{metric}@{k}'
            summary[name] = bootstrap_mean([row['metrics'][name] for row in records if row['metrics'][name] is not None], seed)
        tp = sum(row['metrics'][f'tp@{k}'] for row in records)
        gold = sum(row['gold_count'] for row in records)
        summary[f'micro_precision@{k}'] = tp / (len(records) * k) if records else None
        summary[f'micro_recall@{k}'] = tp / gold if gold else None
    summary['success_latency_ms'] = percentiles([row['latency_ms'] for row in records if row['ok']])
    summary['filter_violations'] = sum(row.get('filter_violations', 0) for row in records)
    return summary


def summarize_load(records: list[dict], wall_seconds: float, *, scheduled_seconds: float, peak_inflight: int) -> dict:
    sent = [row for row in records if row['outcome'] != 'client_dropped']
    successful = [row for row in sent if row['outcome'] == 'success']
    rejected = [row for row in sent if row['outcome'] == 'rejected']
    errors = [row for row in sent if row['outcome'] not in ('success', 'rejected')]
    duration = max(wall_seconds, 1e-9)
    count = len(sent)
    return {'scheduled': len(records), 'sent': count, 'successful': len(successful),
        'rejected': len(rejected), 'errors': len(errors), 'client_dropped': len(records) - count,
        'success_rate': len(successful) / count if count else None,
        'rejection_rate': len(rejected) / count if count else None,
        'error_rate': len(errors) / count if count else None,
        'delivery_rate': count / len(records) if records else None,
        'attempt_throughput_rps': count / duration, 'successful_throughput_rps': len(successful) / duration,
        'answered_throughput_rps': sum(row.get('answer_status') == 'answered' for row in successful) / duration,
        'wall_seconds_including_drain': wall_seconds, 'scheduled_seconds': scheduled_seconds,
        'peak_inflight': peak_inflight, 'http_status_counts': dict(Counter(str(row['status_code']) for row in sent)),
        'outcomes': dict(Counter(row['outcome'] for row in records)),
        'answer_status_counts': dict(Counter(row['answer_status'] for row in successful if row.get('answer_status'))),
        'success_latency_ms': percentiles([row['latency_ms'] for row in successful]),
        'rejection_latency_ms': percentiles([row['latency_ms'] for row in rejected]),
        'error_latency_ms': percentiles([row['latency_ms'] for row in errors]),
        'all_sent_latency_ms': percentiles([row['latency_ms'] for row in sent]),
        'schedule_lag_ms': percentiles([row['schedule_lag_ms'] for row in records]),
        'scheduled_to_completion_ms': percentiles([row['latency_ms'] + row['schedule_lag_ms'] for row in sent])}
