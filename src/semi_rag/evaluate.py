from __future__ import annotations

import json
import logging
import math
import random
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from semi_rag.eval_metrics import ranking_metrics

from semi_rag.ingest import digest
from utils import read_jsonl

log = logging.getLogger(__name__)


def grams(text: str, size: int = 12) -> set[str]:
    text = re.sub(r'\s+', '', text)
    return {text[i:i + size] for i in range(max(0, len(text) - size + 1))}


def prepare_dataset(config: dict, output: Path, limit: int) -> dict:
    """Use held-out SFT questions, never their answers, as retrieval queries."""
    data = Path(config['data_dir'])
    test = json.loads((data / 'sft/test.json').read_text())
    test_keys = {(row['instruction'], row['input']) for row in test}
    candidates = [row for row in read_jsonl(data / 'sft/verified.jsonl')
                  if (row['instruction'], row['input']) in test_keys
                  and row['task'] in ('qa_num', 'qa_logic', 'chart_qa', 'risk')]
    chunks = list(read_jsonl(Path(config['index_dir']) / 'current/chunks.jsonl'))
    by_doc = defaultdict(list)
    for chunk in chunks:
        by_doc[chunk['doc_id']].append((chunk, grams(chunk['text'])))
    random.Random(42).shuffle(candidates)
    selected, counts, skipped = [], Counter(), 0
    for row in candidates:
        if counts[row['doc_id']] >= 3:
            continue
        gold_grams = grams(row['input'])
        positives = [chunk['chunk_id'] for chunk, chunk_grams in by_doc[row['doc_id']]
                     if len(gold_grams & chunk_grams) >= max(24, .35 * len(chunk_grams))]
        if not positives:
            skipped += 1
            continue
        selected.append({'id': digest(row['instruction'] + row['doc_id'])[:20],
            'query': row['instruction'], 'task': row['task'], 'gold_doc_ids': [row['doc_id']],
            'gold_chunk_ids': positives, 'source_date': row['date'],
            'label_origin': 'SFT test question; source-passage overlap weak labels',
            'filters': {}})
        counts[row['doc_id']] += 1
        if len(selected) == limit:
            break
    if not selected:
        raise ValueError('No held-out questions could be mapped to indexed evidence')
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('w') as stream:
        for row in selected:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
    return {'questions': len(selected), 'documents': len(counts), 'unmapped_skipped': skipped,
            'tasks': dict(Counter(row['task'] for row in selected)), 'output': str(output)}


def metrics(hits: list[dict], row: dict, k: int) -> dict:
    return ranking_metrics(hits, row, k)


def evaluate(config: dict, dataset: Path, output: Path, limit: int = 0) -> dict:
    from semi_rag.retrieve import Retriever
    retriever = Retriever(config)
    questions = list(read_jsonl(dataset))
    if limit:
        questions = questions[:limit]
    if not questions:
        raise ValueError('Empty evaluation dataset')
    for row in questions:
        if not row.get('gold_chunk_ids') or not set(row['gold_chunk_ids']) <= retriever.by_id.keys():
            raise ValueError('Evaluation labels do not match this index; prepare the dataset again')
    records, summary = [], {}
    for mode in ('bm25', 'dense', 'hybrid', 'rerank'):
        mode_records = []
        for index, row in enumerate(questions):
            result = retriever.search(row['query'], row.get('filters'), top_k=10, mode=mode, diversify=False)
            record = {'id': row['id'], 'query': row['query'], 'mode': mode,
                'latency_ms': result['latency_ms'], 'hits': [hit['chunk_id'] for hit in result['hits']]}
            for k in (1, 5, 10):
                record.update(metrics(result['hits'], row, k))
            records.append(record)
            mode_records.append(record)
            if (index + 1) % 10 == 0:
                log.info('%s evaluation %d/%d', mode, index + 1, len(questions))
        summary[mode] = {key: round(float(np.mean([r[key] for r in mode_records])), 4)
                         for key in mode_records[0] if '@' in key}
        summary[mode]['latency_p50_ms'] = round(float(np.median([r['latency_ms'] for r in mode_records])), 1)
        summary[mode]['latency_p95_ms'] = round(float(np.percentile([r['latency_ms'] for r in mode_records], 95)), 1)
    report = {'generation': retriever.manifest['generation'], 'questions': len(questions),
        'created_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'label_origin': 'Weak source-passage labels from held-out SFT questions; not an independent human benchmark',
        'evaluation_policy': 'All modes use identical filters; no per-document diversity cap; no answer generation',
        'summary': summary}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    with output.with_suffix('.details.jsonl').open('w') as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + '\n')
    return report
