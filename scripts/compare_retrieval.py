"""Development-set ablations and unchanged held-out retrieval comparison."""
import argparse
import hashlib
import json
import logging
import random
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from semi_rag.config import load_config
from semi_rag.evaluate import grams
from semi_rag.eval_metrics import ranking_metrics
from semi_rag.retrieve import Retriever

BASELINE = {'retrieval_profile': 'legacy', 'candidate_k': 48, 'rerank_k': 32,
            'query_expansion': False, 'rerank_body_only': False, 'rerank_rank_fusion': False,
            'source_routing': False, 'max_per_doc': 3}
PROFILES = {
    'legacy': {'retrieval_profile': 'legacy', 'candidate_k': 48, 'rerank_k': 32},
    'wide': {'retrieval_profile': 'legacy', 'candidate_k': 128, 'rerank_k': 96},
    'body': {'retrieval_profile': 'enhanced', 'candidate_k': 128, 'rerank_k': 96, 'query_expansion': False},
    'expanded': {'retrieval_profile': 'enhanced', 'candidate_k': 128, 'rerank_k': 96, 'query_expansion': True},
    'dynamic': {'retrieval_profile': 'enhanced', 'candidate_k': 128, 'rerank_k': 96, 'query_expansion': True,
                'dynamic_candidate_quota': True},
    'body_rerank': {'retrieval_profile': 'enhanced', 'candidate_k': 128, 'rerank_k': 96, 'query_expansion': True, 'rerank_body_only': True},
    'rank_fusion': {'retrieval_profile': 'enhanced', 'candidate_k': 128, 'rerank_k': 96, 'query_expansion': True, 'rerank_rank_fusion': True},
    'focused': {'retrieval_profile': 'enhanced', 'candidate_k': 128, 'rerank_k': 96, 'query_expansion': True, 'max_per_doc': 5},
    'source_route': {'retrieval_profile': 'legacy', 'candidate_k': 48, 'rerank_k': 32, 'source_routing': True},
    'routed_expanded': {'retrieval_profile': 'enhanced', 'candidate_k': 128, 'rerank_k': 96, 'query_expansion': True, 'source_routing': True},
    'balanced': {'retrieval_profile': 'enhanced', 'candidate_k': 128, 'rerank_k': 96, 'query_expansion': True,
                 'max_per_doc': 5, 'rerank_rank_fusion': True, 'source_routing': True},
    'balanced_dynamic': {'retrieval_profile': 'enhanced', 'candidate_k': 128, 'rerank_k': 96, 'query_expansion': True,
                         'max_per_doc': 5, 'rerank_rank_fusion': True, 'source_routing': True,
                         'dynamic_candidate_quota': True},
}


def prepare_dev(config, destination, limit):
    data = Path(config['data_dir'])
    val = json.loads((data / 'sft/val.json').read_text())
    keys = {(r['instruction'], r['input']) for r in val}
    test = [json.loads(s) for s in (data / 'rag/eval/questions.jsonl').read_text().splitlines()]
    test_docs = {did for row in test for did in row['gold_doc_ids']}
    candidates = [json.loads(line) for line in (data / 'sft/verified.jsonl').read_text().splitlines()]
    candidates = [r for r in candidates if (r['instruction'], r['input']) in keys and r['doc_id'] not in test_docs
                  and r['task'] in ('qa_num', 'qa_logic', 'chart_qa', 'risk')]
    chunks = [json.loads(s) for s in (Path(config['index_dir']) / 'current/chunks.jsonl').read_text().splitlines()]
    lookup = defaultdict(list)
    for chunk in chunks:
        lookup[chunk['doc_id']].append((chunk['chunk_id'], grams(chunk['text'])))
    random.Random(314159).shuffle(candidates)
    records, counts = [], defaultdict(int)
    for row in candidates:
        if counts[row['doc_id']] >= 3:
            continue
        source = grams(row['input'])
        gold = [cid for cid, tokens in lookup[row['doc_id']] if len(tokens & source) >= max(24, .35 * len(tokens))]
        if not gold:
            continue
        records.append({'id': hashlib.sha256(row['instruction'].encode()).hexdigest()[:20], 'query': row['instruction'],
            'gold_chunk_ids': gold, 'gold_doc_ids': [row['doc_id']], 'task': row['task'], 'filters': {},
            'label_origin': 'SFT validation source-overlap weak labels; document-disjoint from August test set'})
        counts[row['doc_id']] += 1
        if len(records) >= limit:
            break
    if not records:
        raise ValueError('No development questions')
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(''.join(json.dumps(r, ensure_ascii=False)+'\n' for r in records))
    print(json.dumps({'questions': len(records), 'documents': len(counts)}, ensure_ascii=False))


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--prepare', type=Path)
    p.add_argument('--dataset', type=Path)
    p.add_argument('--output', type=Path)
    p.add_argument('--limit', type=int, default=60)
    p.add_argument('--profiles', nargs='+', choices=PROFILES, default=list(PROFILES))
    args = p.parse_args()
    if args.limit <= 0 or not args.prepare and (not args.dataset or not args.output):
        p.error('Provide positive limit and either --prepare or --dataset plus --output')
    config = load_config()
    if args.prepare:
        prepare_dev(config, args.prepare, args.limit)
        return
    rows = [json.loads(s) for s in args.dataset.read_text().splitlines()][:args.limit]
    if not rows or len({row['id'] for row in rows}) != len(rows):
        raise ValueError('Empty dataset or duplicate question IDs')
    config['retrieval_debug'] = True
    retriever = Retriever(config)
    if any(not row['gold_chunk_ids'] or not set(row['gold_chunk_ids']) <= retriever.by_id.keys() for row in rows):
        raise ValueError('Gold labels do not match the current index')
    reports, details = {}, []
    for profile in args.profiles:
        retriever.config = {**config, **BASELINE, **PROFILES[profile]}
        records = []
        for i, row in enumerate(rows):
            result = retriever.search(row['query'], row['filters'], top_k=10)
            record = {'profile': profile, 'id': row['id'], 'task': row['task'], 'latency_ms': result['latency_ms'],
                      'hits': [h['chunk_id'] for h in result['hits']]}
            gold = set(row['gold_chunk_ids'])
            record['candidate_recall'] = len(gold & set(result['candidate_ids'])) / len(gold)
            record['candidate_hit'] = float(bool(gold & set(result['candidate_ids'])))
            for k in (1, 5, 10):
                record.update(ranking_metrics(result['hits'], row, k))
            records.append(record)
            if (i+1) % 10 == 0:
                print(profile, i+1, len(rows), flush=True)
        details.extend(records)
        reports[profile] = {key: float(np.mean([r[key] for r in records])) for key in records[0]
                            if '@' in key or key.startswith('candidate_')}
        reports[profile]['p50_ms'] = float(np.median([r['latency_ms'] for r in records]))
        print(json.dumps({profile: reports[profile]}, ensure_ascii=False), flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({'dataset': str(args.dataset), 'dataset_sha256': hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
        'generation': retriever.manifest['generation'], 'profiles': {p: {**BASELINE, **PROFILES[p]} for p in args.profiles},
        'questions': len(rows), 'summary': reports}, ensure_ascii=False, indent=2))
    args.output.with_suffix('.details.jsonl').write_text(''.join(json.dumps(r, ensure_ascii=False)+'\n' for r in details))


if __name__ == '__main__':
    main()
