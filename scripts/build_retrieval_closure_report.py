"""Build a reproducible JSON and Markdown closure report from completed RAG runs."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EVAL = ROOT / 'data/rag/eval'


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def retrieval(report: dict, mode='rerank') -> dict:
    values = report['retrieval']['modes'][mode]
    return {
        'precision_at_5': values['precision@5']['mean'],
        'recall_at_5': values['evidence_recall@5']['mean'],
        'hit_rate_at_5': values['evidence_hit@5']['mean'],
        'mrr_at_5': values['mrr@5']['mean'],
        'ndcg_at_5': values['ndcg@5']['mean'],
        'retrieval_p95_ms': values['success_latency_ms']['p95'],
        'retrieval_p99_ms': values['success_latency_ms']['p99'],
        'retrieval_error_rate': values['failed_requests'] / values['requests'],
    }


def load_stage(report: dict, kind: str, value: float) -> dict:
    row = next(row for row in report[kind] if row['value'] == value)
    return {
        'load': value,
        'qps': row['successful_throughput_rps'],
        'p95_ms': row['success_latency_ms']['p95'],
        'p99_ms': row['success_latency_ms']['p99'],
        'error_rate': row['error_rate'],
        'rejection_rate': row['rejection_rate'],
        'successful': row['successful'],
        'sent': row['sent'],
    }


def metric_line(name: str, values: dict) -> str:
    return ('| {name} | {precision_at_5:.4f} | {recall_at_5:.4f} | {hit_rate_at_5:.4f} | '
            '{mrr_at_5:.4f} | {ndcg_at_5:.4f} | {retrieval_p95_ms:.1f} | {retrieval_p99_ms:.1f} | '
            '{retrieval_error_rate:.2%} |'.format(name=name, **values))


def main():
    baseline_path = EVAL / 'benchmarks/20260909-baseline-legacy/report.json'
    optimized_path = EVAL / 'benchmarks/20260909-balanced/report.json'
    dev_path = EVAL / 'retrieval-upgrade/20260909-dev-ablation.json'
    dev_final_path = EVAL / 'retrieval-upgrade/20260909-dev-final.json'
    test_path = EVAL / 'retrieval-upgrade/20260909-test-comparison.json'
    baseline, optimized = load(baseline_path), load(optimized_path)
    dev, dev_final, test = load(dev_path), load(dev_final_path), load(test_path)
    stages = {name: {
        'candidate_recall': row['candidate_recall'], 'precision_at_5': row['precision@5'],
        'recall_at_5': row['evidence_recall@5'], 'hit_rate_at_5': row['evidence_hit@5'],
        'mrr_at_5': row['mrr@5'], 'ndcg_at_5': row['ndcg@5'], 'p50_ms': row['p50_ms'],
    } for name, row in dev['summary'].items()}
    stages.update({f'dev_final_{name}': {
        'candidate_recall': row['candidate_recall'], 'precision_at_5': row['precision@5'],
        'recall_at_5': row['evidence_recall@5'], 'hit_rate_at_5': row['evidence_hit@5'],
        'mrr_at_5': row['mrr@5'], 'ndcg_at_5': row['ndcg@5'], 'p50_ms': row['p50_ms'],
    } for name, row in dev_final['summary'].items()})
    heldout = {name: {
        'candidate_recall': row['candidate_recall'], 'candidate_hit_rate': row['candidate_hit'],
        'precision_at_5': row['precision@5'], 'recall_at_5': row['evidence_recall@5'],
        'hit_rate_at_5': row['evidence_hit@5'], 'mrr_at_5': row['mrr@5'],
        'ndcg_at_5': row['ndcg@5'], 'p50_ms': row['p50_ms'],
    } for name, row in test['summary'].items()}
    result = {
        'schema_version': 1,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'index_generation': baseline['before']['stats']['generation'],
        'evaluation': {'as_of': '2026-09-09', 'heldout_questions': 60, 'development_questions': 60,
                       'label_warning': 'SFT source-overlap weak labels; not an independent human relevance set.'},
        'baseline_online': retrieval(baseline),
        'optimized_online': retrieval(optimized),
        'online_quality': {'baseline': baseline['quality'], 'optimized': optimized['quality']},
        'concurrency_closed_loop': {
            'baseline': [load_stage(baseline, 'concurrency', value) for value in (1, 2, 4, 8)],
            'optimized': [load_stage(optimized, 'concurrency', value) for value in (1, 2, 4, 8)],
        },
        'stress_open_loop': {
            'baseline': [load_stage(baseline, 'stress', value) for value in (1., 2., 4., 8.)],
            'optimized': [load_stage(optimized, 'stress', value) for value in (1., 2., 4., 8.)],
        },
        'development_ablations': stages,
        'heldout_comparison': heldout,
        'retained': ['BM25 + BGE-M3 dense RRF', 'body BM25 field', 'deterministic query expansion',
                     'BGE cross-encoder reranking', 'retrieval/rerank rank fusion', 'per-document quota=5',
                     'explicit source routing', 'adjacent evidence context', 'metadata filters'],
        'reverted_or_non_default': ['dynamic candidate quotas: development P@5/R@5/nDCG regressed vs static balanced',
                                    'rerank body-only: development P@5/MRR regressed',
                                    'wide candidates/body BM25/query expansion as isolated stages: no P@5 gain vs legacy'],
    }
    out_dir = EVAL / 'closure-20260909'
    out_dir.mkdir(exist_ok=True)
    (out_dir / 'comparison.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    base, opt = result['baseline_online'], result['optimized_online']
    lines = ['# RAG Retrieval Closure Report', '',
             '## Scope', '',
             '- Index: `{}`; 395 reports, 13,205 evidence chunks; BGE-M3 (1024-dim) + BGE reranker v2-m3 + FAISS IndexFlatIP + jieba BM25.'.format(result['index_generation']),
             '- Held-out and development sets each contain 60 questions. Labels are SFT source-overlap weak labels, so the metrics measure retrieval-label agreement, not independent human factual accuracy.',
             '- The online reports use the same index generation and fixed cutoff date `2026-09-09`.', '',
             '## Online Baseline To Final', '',
             '| Configuration | P@5 | R@5 | Hit@5 | MRR@5 | nDCG@5 | P95 ms | P99 ms | Error rate |',
             '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |',
             metric_line('legacy rerank', base), metric_line('balanced rerank', opt), '',
             '- Answer regression: baseline {}/{}; final {}/{}. Continuous excerpt citation validity was {:.0%} in both runs.'.format(
                 baseline['quality']['passed'], baseline['quality']['questions'], optimized['quality']['passed'], optimized['quality']['questions'], optimized['quality']['citation_excerpt_validity']),
             '- Quality trade-off: final P@5/R@5/nDCG@5 improved by {:.2f}/{:.2f}/{:.2f} percentage points; MRR@5 changed by {:.4f}; P95 increased {:.1f}x.'.format(
                 100 * (opt['precision_at_5'] - base['precision_at_5']), 100 * (opt['recall_at_5'] - base['recall_at_5']),
                 100 * (opt['ndcg_at_5'] - base['ndcg_at_5']), opt['mrr_at_5'] - base['mrr_at_5'], opt['retrieval_p95_ms'] / base['retrieval_p95_ms']), '',
             '## Development Ablation', '',
             '| Stage | Candidate R | P@5 | R@5 | Hit@5 | MRR@5 | nDCG@5 | P50 ms |',
             '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
    labels = [('legacy', 'legacy'), ('wide', 'wider candidate pool'), ('body', 'body BM25'),
              ('expanded', 'query expansion'), ('rank_fusion', 'rank fusion'), ('focused', 'quota=5'),
              ('dynamic', 'dynamic candidate quotas'), ('balanced', 'balanced + dynamic quotas')]
    for key, label in labels:
        row = stages[key]
        lines.append('| {} | {:.4f} | {:.4f} | {:.4f} | {:.4f} | {:.4f} | {:.4f} | {:.1f} |'.format(
            label, row['candidate_recall'], row['precision_at_5'], row['recall_at_5'], row['hit_rate_at_5'],
            row['mrr_at_5'], row['ndcg_at_5'], row['p50_ms']))
    for key, label in (('dev_final_balanced', 'balanced (static quota validation)'),
                       ('dev_final_balanced_dynamic', 'balanced + dynamic quota validation')):
        row = stages[key]
        lines.append('| {} | {:.4f} | {:.4f} | {:.4f} | {:.4f} | {:.4f} | {:.4f} | {:.1f} |'.format(
            label, row['candidate_recall'], row['precision_at_5'], row['recall_at_5'], row['hit_rate_at_5'],
            row['mrr_at_5'], row['ndcg_at_5'], row['p50_ms']))
    lines.extend(['', 'The development choice was static `balanced`: the direct static-vs-dynamic check gave P@5/R@5/nDCG@5 `{:.4f}/{:.4f}/{:.4f}` vs `{:.4f}/{:.4f}/{:.4f}`. Dynamic quotas are not default.'.format(
        stages['dev_final_balanced']['precision_at_5'], stages['dev_final_balanced']['recall_at_5'], stages['dev_final_balanced']['ndcg_at_5'],
        stages['dev_final_balanced_dynamic']['precision_at_5'], stages['dev_final_balanced_dynamic']['recall_at_5'], stages['dev_final_balanced_dynamic']['ndcg_at_5']), '',
        '## Held-Out Confirmation', '',
        '| Configuration | Candidate R | P@5 | R@5 | Hit@5 | MRR@5 | nDCG@5 | P50 ms |',
        '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |'])
    for name in ('legacy', 'balanced', 'balanced_dynamic'):
        row = heldout[name]
        lines.append('| {} | {:.4f} | {:.4f} | {:.4f} | {:.4f} | {:.4f} | {:.4f} | {:.1f} |'.format(
            name, row['candidate_recall'], row['precision_at_5'], row['recall_at_5'], row['hit_rate_at_5'], row['mrr_at_5'], row['ndcg_at_5'], row['p50_ms']))
    lines.extend(['', '## Capacity', '',
        'At open-loop 1 RPS, legacy delivered {:.2f} effective QPS at {:.0%} rejection; balanced delivered {:.2f} at {:.0%}. At open-loop 8 RPS, legacy delivered {:.2f} at {:.0%}; balanced delivered {:.2f} at {:.0%}. HTTP error rate was 0% in all completed load stages; capacity loss appears as explicit 503 busy rejections.'.format(
            result['stress_open_loop']['baseline'][0]['qps'], result['stress_open_loop']['baseline'][0]['rejection_rate'],
            result['stress_open_loop']['optimized'][0]['qps'], result['stress_open_loop']['optimized'][0]['rejection_rate'],
            result['stress_open_loop']['baseline'][-1]['qps'], result['stress_open_loop']['baseline'][-1]['rejection_rate'],
            result['stress_open_loop']['optimized'][-1]['qps'], result['stress_open_loop']['optimized'][-1]['rejection_rate']), '',
        '## Decision', '',
        '- Default: static balanced retrieval for evidence quality, explicit metadata filtering, parent/page metadata, title path, adjacent evidence context, and original PDF/page/excerpt citations.',
        '- Not default: dynamic candidate quotas and body-only reranking, because the frozen development comparison was negative; the held-out dynamic variant also reduced MRR/nDCG versus static balanced.',
        '- The final configuration is quality-first. It is not suitable for a 1 RPS SLO under the current one-request admission gate; a separate serving-concurrency change must be benchmarked before claiming capacity improvement.', ''])
    (out_dir / 'comparison.md').write_text('\n'.join(lines))
    print(json.dumps({'json': str(out_dir / 'comparison.json'), 'markdown': str(out_dir / 'comparison.md')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
