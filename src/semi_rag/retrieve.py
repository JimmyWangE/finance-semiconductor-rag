from __future__ import annotations

import json
import logging
import math
import re
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

import faiss
import jieba
import numpy as np
from rank_bm25 import BM25Okapi

from semi_rag.index import read_index
from semi_rag.models import Encoder, Reranker
from semi_rag.query_variants import query_variants, source_organization

jieba.setLogLevel(logging.WARNING)
STOPWORDS = set('的 了 是 在 和 与 及 对 为 有 哪些 什么 如何 请 根据 分析 一下 研报 报告 认为'.split())
DOMAIN_WORDS = '中芯国际 华虹半导体 华虹公司 北方华创 中微公司 兆易创新 澜起科技 长鑫存储 长江存储 寒武纪 海光信息 先进封装 存储芯片 晶圆代工 半导体设备 产能利用率 国产替代 光刻胶 光刻机 芯原股份 HBM DRAM NAND DDR5 AI GPU CPU CoWoS'.split()
for word in DOMAIN_WORDS:
    jieba.add_word(word, freq=100000)


def tokenize(text: str) -> list[str]:
    return [token.lower() for token in jieba.cut_for_search(text)
            if token.strip() and token not in STOPWORDS and re.search(r'[\w\u4e00-\u9fff]', token)]


def temporal_markers(text: str) -> set[str]:
    text = re.sub(r'\s+', '', unicodedata.normalize('NFKC', text))
    markers = set()
    for match in re.finditer(r'(?<!\d)(20\d{2})[年./-](\d{1,2})[月./-](\d{1,2})日?', text):
        year, month, day = match.groups()
        markers.update((f'ym:{year}-{int(month):02d}', f'ymd:{year}-{int(month):02d}-{int(day):02d}',
                        f'md:{int(month):02d}-{int(day):02d}'))
    return markers


def validate_filters(filters: dict | None) -> dict:
    filters = {key: value for key, value in (filters or {}).items() if value is not None}
    unknown = set(filters) - {'date_from', 'date_to', 'as_of', 'org', 'doc_ids', 'kind',
                              'company', 'industry', 'page_from', 'page_to'}
    if unknown:
        raise ValueError(f'Unknown filters: {sorted(unknown)}')
    for key in ('date_from', 'date_to', 'as_of'):
        if key in filters:
            parsed = date.fromisoformat(filters[key])
            filters[key] = parsed.isoformat()
    filters.setdefault('as_of', date.today().isoformat())
    end = min(filters.get('date_to', filters['as_of']), filters['as_of'])
    if filters.get('date_from', '') > end:
        raise ValueError('date_from is after the effective end date')
    filters['date_to'] = end
    if 'kind' in filters and filters['kind'] not in ('text', 'table', 'chart'):
        raise ValueError('kind must be text, table, or chart')
    if 'doc_ids' in filters and not isinstance(filters['doc_ids'], list):
        raise ValueError('doc_ids must be a list')
    for key in ('company', 'industry'):
        if key in filters and (not isinstance(filters[key], str) or not filters[key].strip()):
            raise ValueError(f'{key} must be a non-empty string')
    for key in ('page_from', 'page_to'):
        if key in filters and (type(filters[key]) is not int or filters[key] < 1):
            raise ValueError(f'{key} must be a positive integer')
    if filters.get('page_from', 1) > filters.get('page_to', float('inf')):
        raise ValueError('page_from is after page_to')
    return filters


def matches(chunk: dict, filters: dict) -> bool:
    published = chunk['publish_date']
    if not published or not filters.get('date_from', '') <= published <= filters['date_to']:
        return False
    return (('org' not in filters or chunk['org'] == filters['org'])
        and ('doc_ids' not in filters or chunk['doc_id'] in filters['doc_ids'])
        and ('kind' not in filters or chunk['kind'] == filters['kind'])
        and ('industry' not in filters or chunk.get('industry') == filters['industry'])
        and ('company' not in filters or filters['company'] in chunk.get('stock_name', '')
             or filters['company'] in chunk.get('title', ''))
        and ('page_from' not in filters or chunk.get('page') is not None and chunk['page'] >= filters['page_from'])
        and ('page_to' not in filters or chunk.get('page') is not None and chunk['page'] <= filters['page_to']))


def rrf(rankings: list[list[int]], constant: int = 60, weights=None) -> list[tuple[int, float]]:
    if constant < 0 or weights is not None and (len(weights) != len(rankings) or any(not math.isfinite(w) or w < 0 for w in weights)):
        raise ValueError('Invalid RRF weights or rank constant')
    scores = defaultdict(float)
    for ranking, weight in zip(rankings, weights if weights is not None else [1.] * len(rankings)):
        for rank, index in enumerate(ranking, 1):
            scores[index] += weight / (constant + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


class Retriever:
    def __init__(self, config: dict, encoder=None, reranker=None, directory=None):
        self.config = config
        self.manifest, self.chunks, self.vectors, self.index = read_index(config, directory)
        self._enrich_metadata()
        faiss.omp_set_num_threads(8)
        self.encoder = encoder or Encoder(config)
        self.reranker = reranker or Reranker(config)
        self.bm25 = BM25Okapi([tokenize(chunk['search_text']) for chunk in self.chunks])
        self.by_id = {chunk['chunk_id']: chunk for chunk in self.chunks}
        self.organizations = sorted({c['org'] for c in self.chunks if c.get('org')})
        self.body_bm25 = None
        if config.get('retrieval_profile', 'legacy') != 'legacy':
            self.body_bm25 = BM25Okapi([tokenize(chunk['text']) for chunk in self.chunks])

    def _enrich_metadata(self):
        """Add non-vector metadata without changing immutable indexed evidence IDs."""
        metadata_path = Path(self.config['data_dir']) / 'report/metadata.jsonl'
        if metadata_path.is_file():
            metadata = {row['doc_id']: row for row in
                        (json.loads(line) for line in metadata_path.read_text().splitlines() if line.strip())}
            for chunk in self.chunks:
                meta = metadata.get(chunk['doc_id'], {})
                for key in ('industry', 'stock_name', 'stock_code'):
                    chunk.setdefault(key, meta.get(key, ''))
        for chunk in self.chunks:
            page = chunk.get('page')
            chunk.setdefault('parent_id', f"{chunk['doc_id']}:page:{page}" if page else chunk['doc_id'])
            chunk.setdefault('title_path', [chunk.get('title', ''), f'第{page}页' if page else '未定位页码'])

    def candidate_quotas(self, query: str, eligible_count: int) -> dict[str, int]:
        """Allocate a bounded candidate budget by query specificity, never outside filters."""
        ceiling = min(self.config['candidate_k'], eligible_count)
        if self.config.get('retrieval_profile', 'legacy') == 'legacy' or not self.config.get('dynamic_candidate_quota'):
            return {'title_bm25': ceiling, 'body_bm25': ceiling, 'dense': ceiling}
        terms = tokenize(query)
        numeric_or_exact = bool(re.search(r'\d|[A-Za-z]{2,}', query))
        lexical = min(eligible_count, max(16, int(ceiling * (1.25 if numeric_or_exact else .75))))
        dense = min(eligible_count, max(16, int(ceiling * (1.15 if len(terms) <= 5 else .85))))
        body = min(eligible_count, max(16, int(ceiling * (1.10 if len(terms) >= 5 else .80))))
        return {'title_bm25': lexical, 'body_bm25': body, 'dense': dense}

    def adjacent_context(self, hit: dict) -> list[dict]:
        if not self.config.get('context_expansion', False):
            return []
        radius = self.config.get('adjacent_chunk_radius', 1)
        if radius < 1:
            return []
        position = hit.get('position')
        if position is None:
            return []
        neighbors = [candidate for candidate in self.chunks
                     if candidate['doc_id'] == hit['doc_id']
                     and candidate.get('kind') == hit.get('kind')
                     and abs(candidate.get('position', -10**9) - position) <= radius
                     and candidate['chunk_id'] != hit['chunk_id']]
        return sorted(neighbors, key=lambda row: row['position'])

    def search(self, query: str, filters: dict | None = None, top_k: int | None = None,
               mode: str = 'rerank', diversify: bool = True) -> dict:
        started = time.monotonic()
        query = query.strip()
        if not query or len(query) > 1000:
            raise ValueError('query must contain 1 to 1000 characters')
        if mode not in ('bm25', 'dense', 'hybrid', 'rerank'):
            raise ValueError('Unsupported retrieval mode')
        top_k = self.config['top_k'] if top_k is None else top_k
        if not 1 <= top_k <= self.config['rerank_k']:
            raise ValueError(f'top_k must be between 1 and {self.config["rerank_k"]}')
        filters = validate_filters(filters)
        routing = None
        if self.config.get('source_routing') and 'org' not in filters:
            routing = source_organization(query, self.organizations)
            if routing:
                filters['org'] = routing
        eligible = np.array([i for i, c in enumerate(self.chunks) if matches(c, filters)], dtype=np.int64)
        base = {'query': query, 'filters': filters, 'mode': mode, 'generation': self.manifest['generation'],
                'source_routing': routing}
        if not len(eligible):
            return {**base, 'hits': [], 'eligible_chunks': 0, 'latency_ms': 0}
        quotas = self.candidate_quotas(query, len(eligible))
        lexical_scores, dense_scores = {}, {}
        rankings = []
        enhanced = self.config.get('retrieval_profile', 'legacy') != 'legacy' and mode in ('hybrid', 'rerank')
        variants = query_variants(query) if enhanced and self.config.get('query_expansion', True) else [query]
        if mode != 'dense':
            lexical = self.bm25.get_scores(tokenize(query))
            order = sorted(eligible, key=lambda index: (-lexical[index], index))
            lexical_scores = {int(index): float(lexical[index]) for index in order[:quotas['title_bm25']] if lexical[index] > 0}
            rankings.append(list(lexical_scores))
        if enhanced:
            if self.body_bm25 is None:
                self.body_bm25 = BM25Okapi([tokenize(chunk['text']) for chunk in self.chunks])
            for variant in variants:
                body_scores = self.body_bm25.get_scores(tokenize(variant))
                order = sorted(eligible, key=lambda i: (-body_scores[i], i))[:quotas['body_bm25']]
                rankings.append([int(i) for i in order if body_scores[i] > 0])
        if mode != 'bm25':
            vector = self.encoder.encode([query])
            if len(eligible) == len(self.chunks):
                scores, indices = self.index.search(vector, quotas['dense'])
                dense_scores = dict(zip(map(int, indices[0]), map(float, scores[0])))
            else:
                # Score only eligible vectors so a restrictive filter cannot lose its candidates.
                scores = self.vectors[eligible] @ vector[0]
                positions = np.argsort(-scores, kind='stable')[:quotas['dense']]
                dense_scores = {int(eligible[p]): float(scores[p]) for p in positions}
            rankings.append(list(dense_scores))
        fused = rrf(rankings)
        candidates = fused[:self.config['rerank_k']] if mode == 'rerank' else fused
        ranked = []
        if mode == 'rerank' and candidates:
            rerank_texts = [self.chunks[index]['search_text'] for index, _ in candidates]
            if enhanced and self.config.get('rerank_body_only', False):
                rerank_texts = [self.chunks[index]['text'] for index, _ in candidates]
            scores = self.reranker.score(query, rerank_texts)
        else:
            scores = [None] * len(candidates)
        for (index, fused_score), score in zip(candidates, scores):
            ranked.append({**self.chunks[index], 'rrf_score': fused_score,
                'bm25_score': lexical_scores.get(index), 'dense_score': dense_scores.get(index),
                'rerank_score': score})
        if mode == 'rerank':
            ranked.sort(key=lambda row: (-row['rerank_score'], -row['rrf_score'], row['chunk_id']))
            if enhanced and self.config.get('rerank_rank_fusion', False):
                original_rank = {self.chunks[index]['chunk_id']: rank for rank, (index, _) in enumerate(candidates, 1)}
                query_times = temporal_markers(query)
                for rank, row in enumerate(ranked, 1):
                    row['final_rank_score'] = 2 / (60 + rank) + 1 / (60 + original_rank[row['chunk_id']])
                    row['temporal_overlap'] = (len(query_times & temporal_markers(row['text'])) / len(query_times)
                                               if query_times else 0.)
                    row['final_rank_score'] += self.config.get('temporal_rank_weight', 0.) * row['temporal_overlap']
                ranked.sort(key=lambda row: (-row['final_rank_score'], -row['rerank_score'], row['chunk_id']))
        selected, seen, counts = [], set(), Counter()
        for row in ranked:
            if diversify and (row['content_hash'] in seen or counts[row['doc_id']] >= self.config['max_per_doc']):
                continue
            selected.append(row)
            seen.add(row['content_hash'])
            counts[row['doc_id']] += 1
            if len(selected) == top_k:
                break
        diagnostics = {'candidate_ids': [self.chunks[i]['chunk_id'] for i, _ in candidates]} if self.config.get('retrieval_debug') else {}
        return {**base, **diagnostics, 'hits': selected, 'eligible_chunks': len(eligible),
                'retrieval_profile': self.config.get('retrieval_profile', 'legacy'),
                'candidate_quotas': quotas,
                'query_variants': variants, 'reranked_candidates': len(candidates) if mode == 'rerank' else 0,
                'latency_ms': round((time.monotonic() - started) * 1000, 1)}
