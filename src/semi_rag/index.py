from __future__ import annotations

import fcntl
import json
import logging
import os
import sqlite3
import time
import uuid
from collections import Counter
from contextlib import nullcontext
from pathlib import Path

import faiss
import numpy as np

from semi_rag.ingest import collect_chunks, digest
from semi_rag.models import Encoder, model_fingerprint

log = logging.getLogger(__name__)
SCHEMA_VERSION = 1


def fit_chunks(chunks: list[dict], encoder: Encoder) -> list[dict]:
    result = []
    for chunk in chunks:
        if len(encoder.tokenizer.encode(chunk['search_text'])) <= encoder.config['embedding_max_tokens']:
            result.append(chunk)
            continue
        prefix = chunk['search_text'][:-len(chunk['text'])]
        header = chunk.get('table_header', '')
        body = chunk['text']
        if header and body.startswith(header):
            body = body[len(header):].lstrip('\n')
        budget = encoder.config['embedding_max_tokens'] - len(encoder.tokenizer.encode(prefix + header)) - 8
        if budget < 64:
            raise ValueError('Report title exceeds embedding token budget')
        encoded = encoder.tokenizer(body, add_special_tokens=False, return_offsets_mapping=True)
        offsets = encoded['offset_mapping']
        for start in range(0, len(offsets), budget - 32):
            end = min(start + budget, len(offsets))
            text = (header + '\n' if header else '') + body[offsets[start][0]:offsets[end - 1][1]]
            part = {**chunk, 'text': text, 'search_text': prefix + text,
                    'chunk_id': digest(chunk['chunk_id'] + f':{start}:{end}')[:24],
                    'content_hash': digest(''.join(text.split())), 'token_split': True}
            result.append(part)
            if end == len(offsets):
                break
    return result


def build_index(config: dict, *, encoder=None, additional_chunks=None, on_ready=None) -> dict:
    root = Path(config['index_dir'])
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'build.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _build(config, root, encoder, additional_chunks, on_ready)


def _build(config: dict, root: Path, encoder=None, additional_chunks=None, on_ready=None) -> dict:
    started = time.monotonic()
    existing_vectors = {}
    encoder = encoder or Encoder(config)
    if additional_chunks is None:
        chunks, report = collect_chunks(config)
        with getattr(encoder, 'lock', nullcontext()):
            chunks = fit_chunks(chunks, encoder)
    else:
        _, current, matrix, _ = read_index(config)
        existing_vectors = {chunk['search_text']: matrix[index] for index, chunk in enumerate(current)}
        with getattr(encoder, 'lock', nullcontext()):
            additional_chunks = fit_chunks(additional_chunks, encoder)
        replaced = {chunk['doc_id'] for chunk in additional_chunks}
        chunks = [chunk for chunk in current if chunk['doc_id'] not in replaced] + additional_chunks
        dates = sorted(chunk['publish_date'] for chunk in chunks if chunk.get('publish_date'))
        report = {'documents': len({c['doc_id'] for c in chunks}),
                  'date_min': dates[0] if dates else None, 'date_max': dates[-1] if dates else None,
                  'unmapped_paragraphs': sum(c.get('page') is None for c in chunks)}
    signature = digest(json.dumps([model_fingerprint(config['embedding_model']),
        config['embedding_max_tokens'], 'normalized-cls-v1']))
    cache = sqlite3.connect(root / 'embedding_cache.sqlite3')
    cache.execute('CREATE TABLE IF NOT EXISTS embeddings (key TEXT PRIMARY KEY, vector BLOB NOT NULL)')
    vectors, missing, hits = [None] * len(chunks), [], 0
    try:
        for index, chunk in enumerate(chunks):
            key = digest(signature + chunk['search_text'])
            row = cache.execute('SELECT vector FROM embeddings WHERE key=?', (key,)).fetchone()
            if row:
                vectors[index] = np.frombuffer(row[0], dtype='float32').copy()
                hits += 1
            elif chunk['search_text'] in existing_vectors:
                vectors[index] = np.asarray(existing_vectors[chunk['search_text']], dtype='float32').copy()
                cache.execute('INSERT INTO embeddings VALUES (?,?)', (key, vectors[index].tobytes()))
                hits += 1
            else:
                missing.append((index, key))
        cache.commit()
        log.info('Embedding chunks=%d cached=%d pending=%d', len(chunks), hits, len(missing))
        batch = config['batch_size']
        for start in range(0, len(missing), batch):
            records = missing[start:start + batch]
            encoded = encoder.encode([chunks[index]['search_text'] for index, _ in records])
            for (index, key), vector in zip(records, encoded):
                vectors[index] = vector
                cache.execute('INSERT OR REPLACE INTO embeddings VALUES (?,?)', (key, vector.tobytes()))
            cache.commit()
            if start % (batch * 25) == 0:
                log.info('Encoded %d/%d; %.1fs', start + len(records), len(missing), time.monotonic() - started)
    finally:
        cache.close()
    matrix = np.stack(vectors).astype('float32')
    if not np.isfinite(matrix).all() or not np.allclose(np.linalg.norm(matrix, axis=1), 1, atol=.01):
        raise ValueError('Invalid embeddings; current index has not been changed')
    generation = time.strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8]
    destination = root / 'generations' / generation
    destination.mkdir(parents=True)
    index = faiss.IndexFlatIP(matrix.shape[1])
    index.add(matrix)
    faiss.write_index(index, str(destination / 'dense.faiss'))
    np.save(destination / 'vectors.npy', matrix, allow_pickle=False)
    with (destination / 'chunks.jsonl').open('w') as output:
        for chunk in chunks:
            output.write(json.dumps(chunk, ensure_ascii=False) + '\n')
    report.update(Counter(c['kind'] for c in chunks))
    report.update(chunks=len(chunks), token_split_chunks=sum(c.get('token_split', False) for c in chunks),
                  duplicate_content=len(chunks) - len({c['content_hash'] for c in chunks}))
    manifest = {
        'schema_version': SCHEMA_VERSION, 'generation': generation, 'embedding_signature': signature,
        'embedding_model_fingerprint': model_fingerprint(config['embedding_model']),
        'embedding_max_tokens': config['embedding_max_tokens'], 'dimension': matrix.shape[1],
        'chunk_chars': config['chunk_chars'], 'chunk_overlap': config['chunk_overlap'],
        'cache_hits': hits, 'build_seconds': round(time.monotonic() - started, 2),
        'corpus_hash': digest(''.join(c['chunk_id'] + c['search_text'] for c in chunks)),
        'report': report,
    }
    manifest['files'] = {name: digest_bytes(destination / name)
                         for name in ('dense.faiss', 'vectors.npy', 'chunks.jsonl')}
    (destination / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    if on_ready:
        on_ready(manifest, destination)
    else:
        activate_index(config, generation)
    return manifest


def activate_index(config: dict, generation: str):
    root = Path(config['index_dir'])
    temporary = root / f'.current-{generation}'
    temporary.symlink_to(Path('generations') / generation)
    os.replace(temporary, root / 'current')


def digest_bytes(path: Path) -> str:
    import hashlib
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read_index(config: dict, directory=None):
    directory = Path(directory or Path(config['index_dir']) / 'current').resolve(strict=True)
    manifest = json.loads((directory / 'manifest.json').read_text())
    if manifest['schema_version'] != SCHEMA_VERSION:
        raise ValueError('Unsupported index schema; rebuild required')
    if manifest['embedding_model_fingerprint'] != model_fingerprint(config['embedding_model']):
        raise ValueError('Embedding model differs from index; rebuild required')
    if manifest['embedding_max_tokens'] != config['embedding_max_tokens']:
        raise ValueError('Embedding token budget differs from index; rebuild required')
    for filename, expected in manifest['files'].items():
        if digest_bytes(directory / filename) != expected:
            raise ValueError(f'Index checksum mismatch: {filename}')
    with (directory / 'chunks.jsonl').open() as stream:
        chunks = [json.loads(line) for line in stream]
    matrix = np.load(directory / 'vectors.npy', mmap_mode='r', allow_pickle=False)
    index = faiss.read_index(str(directory / 'dense.faiss'))
    if index.ntotal != len(chunks) or matrix.shape != (len(chunks), index.d):
        raise ValueError('Index count/dimension mismatch')
    return manifest, chunks, matrix, index
