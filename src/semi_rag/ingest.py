from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

import clean
from utils import merge_wrapped_lines, norm_space, read_jsonl


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def restored_pages(raw: str, global_boiler: set[str]) -> list[tuple[int | None, list[str]]]:
    """Replay the existing cleaner while retaining its original zero-based page IDs."""
    pages, current, number = [], [], None
    for line in raw.splitlines():
        match = clean.PAGE_RE.match(line)
        if match:
            if current:
                pages.append((number, current))
            current, number = [], int(match[1])
        elif not clean.DOC_RE.match(line):
            current.append(line)
    if current:
        pages.append((number, current))
    boiler = clean.doc_boilerplate([lines for _, lines in pages])
    output, stop = [], False
    for index, (number, lines) in enumerate(pages):
        kept = []
        for line in lines:
            line = norm_space(line).strip()
            if not line:
                continue
            if index >= int(len(pages) * clean.TAIL_CUT_MIN_FRAC) and clean.TAIL_CUT.match(line):
                stop = True
                break
            table = line.startswith('|')
            if not table and (clean.norm_key(line) in boiler or clean.norm_key(line) in global_boiler):
                continue
            if not table:
                for pattern, replacement in clean.INLINE_SUB:
                    line = pattern.sub(replacement, line)
                line = norm_space(line).strip()
                if any(pattern.match(line) for pattern in clean.LINE_DROP) or clean.COPYRIGHT_HINT.search(line):
                    continue
                line = line.strip(' ，,、')
            if len(line) >= 2:
                kept.append(line)
        output.append((number, merge_wrapped_lines(kept)))
        if stop:
            break
    return output


def split_text(text: str, limit: int, overlap: int) -> list[str]:
    pieces, start = [], 0
    while start < len(text):
        end = min(start + limit, len(text))
        if end < len(text):
            boundaries = [m.end() for m in re.finditer(r'[。！？；\n]', text[start:end])]
            if boundaries and boundaries[-1] >= limit // 2:
                end = start + boundaries[-1]
        pieces.append(text[start:end].strip())
        if end == len(text):
            break
        start = max(start + 1, end - overlap)
    return [piece for piece in pieces if piece]


def page_chunks(lines: list[str], limit: int, overlap: int):
    """Keep tables separate; repeat their header when a large table is split."""
    pending, table = [], []
    def flush_text():
        if pending:
            text = '\n'.join(pending)
            pending.clear()
            return [('text', part) for part in split_text(text, limit, overlap)]
        return []
    def flush_table():
        if not table:
            return []
        rows = table[:]
        table.clear()
        header = rows[:1]
        if len(rows) > 1 and re.fullmatch(r'[|:\-\s]+', rows[1]):
            header.append(rows[1])
        prefix = '\n'.join(header)
        result, batch = [], []
        for row in rows[len(header):]:
            if batch and len(prefix) + sum(len(r) + 1 for r in batch) + len(row) > limit:
                result.append(('table', prefix + '\n' + '\n'.join(batch)))
                batch = []
            batch.append(row)
        result.append(('table', prefix + ('\n' + '\n'.join(batch) if batch else '')))
        return result
    result = []
    for line in lines:
        if line.startswith('|'):
            result.extend(flush_text())
            table.append(line)
        else:
            result.extend(flush_table())
            pending.append(line)
    result.extend(flush_text())
    result.extend(flush_table())
    return result


def collect_chunks(config: dict) -> tuple[list[dict], dict]:
    data = Path(config['data_dir'])
    metadata = {r['doc_id']: r for r in read_jsonl(data / 'report/metadata.jsonl')}
    raw_files = sorted((data / 'raw_md').glob('*.md'))
    global_boiler = clean.collect_global_boilerplate(raw_files, max(5, int(len(raw_files) * .05)))
    captions = {}
    for row in read_jsonl(data / 'captions/captions.jsonl'):
        # A later failed retry must not erase a previous successful caption.
        if row.get('ok') and not row.get('invalid') and len(row.get('vlm_text', '')) >= 40:
            captions[(row['doc_id'], row['file'])] = row
    by_doc = defaultdict(list)
    for row in captions.values():
        by_doc[row['doc_id']].append(row)
    chunks, report = [], Counter()
    documents = []
    for source in sorted((data / 'clean').glob('*.md')):
        did = source.stem
        if did not in metadata:
            raise ValueError(f'Missing metadata: {did}')
        meta = metadata[did]
        original = source.read_text()
        allowed = set(line for line in original.splitlines() if line.strip())
        pages = restored_pages((data / 'raw_md' / source.name).read_text(), global_boiler)
        recovered = {line for _, lines in pages for line in lines if line.strip()}
        missing = allowed - recovered
        report['unmapped_paragraphs'] += len(missing)
        doc_chunks = []
        def add(text: str, kind: str, page: int | None, **extra):
            if len(text.strip()) < 25:
                return
            record = {
                'doc_id': did, 'title': meta['title'], 'org': meta.get('org', ''),
                'publish_date': meta.get('publish_date', ''), 'rating': meta.get('rating', ''),
                'industry': meta.get('industry', ''), 'stock_name': meta.get('stock_name', ''), 'stock_code': meta.get('stock_code', ''),
                'file': meta['file'], 'kind': kind, 'page': page + 1 if page is not None else None,
                'text': text, 'content_hash': digest(re.sub(r'\s+', '', text)),
                'provenance': 'vlm_caption' if kind == 'chart' else 'clean_pdf_text',
                **extra,
            }
            record['chunk_id'] = digest(json.dumps([did, record['page'], kind, text, extra], ensure_ascii=False))[:24]
            record['parent_id'] = f"{did}:page:{record['page']}" if record['page'] else did
            record['title_path'] = [record['title'], f"第{record['page']}页" if record['page'] else '未定位页码']
            record['search_text'] = f"{record['title']}\n{record['org']} {record['publish_date']}\n{text}"
            doc_chunks.append(record)
        for page, lines in pages:
            lines = [line for line in lines if line in allowed and not line.startswith('# ')]
            for kind, text in page_chunks(lines, config['chunk_chars'], config['chunk_overlap']):
                add(text, kind, page)
        for text in sorted(missing):
            for part in split_text(text, config['chunk_chars'], config['chunk_overlap']):
                add(part, 'text', None)
        for caption in sorted(by_doc[did], key=lambda row: (row['page'], row['file'])):
            figure = Path(caption['file'])
            root = (data / 'raw_md/figures').resolve()
            if not figure.resolve().is_relative_to(root) or not figure.is_file():
                report['missing_figures'] += 1
                continue
            text = (caption.get('pdf_caption', '') + '\n' + caption['vlm_text']).strip()
            for part in split_text(text, config['chunk_chars'], config['chunk_overlap']):
                add(part, 'chart', caption['page'], figure=str(figure.relative_to(root)),
                    caption_warning='模型图表转述，数值须核对原图')
        unique = {record['chunk_id']: record for record in doc_chunks}
        for index, record in enumerate(unique.values()):
            record['position'] = index
            report[record['kind']] += 1
            chunks.append(record)
        documents.append(meta)
    from semi_rag.upload_store import approved_chunks
    added = approved_chunks(config)
    chunks.extend(added)
    if not chunks:
        raise ValueError('No chunks; check the input data paths')
    report['documents'] = len({record['doc_id'] for record in chunks})
    report['chunks'] = len(chunks)
    report['duplicate_content'] = len(chunks) - len({record['content_hash'] for record in chunks})
    dates = sorted(record['publish_date'] for record in chunks if record.get('publish_date'))
    return chunks, {**report, 'date_min': dates[0] if dates else None,
                    'date_max': dates[-1] if dates else None}
