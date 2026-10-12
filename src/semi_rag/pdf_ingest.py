"""Bounded PDF extraction in a subprocess, retaining physical pages and geometry."""
from __future__ import annotations

import argparse
import json
import re
import threading
from collections import Counter
from pathlib import Path

import pymupdf

import clean
from parse_pymupdf import order_blocks, table_to_md
from utils import merge_wrapped_lines, norm_space

PDF_LOCK = threading.Lock()


def clean_block(text: str, boiler: set[str], box: list, height: float) -> str:
    result = []
    for line in text.splitlines():
        line = norm_space(line).strip()
        if not line:
            continue
        # Repeated content is only boilerplate at page margins, never in the body.
        if (box[3] < height * .12 or box[1] > height * .88) and clean.norm_key(line) in boiler:
            continue
        for pattern, replacement in clean.INLINE_SUB:
            line = pattern.sub(replacement, line)
        if any(pattern.match(line) for pattern in clean.LINE_DROP) or clean.COPYRIGHT_HINT.search(line):
            continue
        result.append(line)
    return '\n'.join(merge_wrapped_lines(result)).strip()


def render_page(pdf: Path, page_number: int, boxes: list | None = None) -> bytes:
    with PDF_LOCK, pymupdf.open(pdf) as doc:
        if not 1 <= page_number <= len(doc):
            raise ValueError('Invalid PDF page')
        page = doc[page_number - 1]
        for box in boxes or []:
            rect = pymupdf.Rect(box)
            page.draw_rect(rect, color=(.1, .55, .3), fill=(.6, 1, .7), fill_opacity=.22, overlay=True)
        scale = min(2, 1800 / max(page.rect.width, page.rect.height))
        return page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).tobytes('png')


def parse_pdf(pdf: Path, destination: Path, max_pages: int = 200) -> dict:
    destination.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(pdf) as doc:
        if not doc.is_pdf or doc.needs_pass:
            raise ValueError('PDF is encrypted or unreadable; upload an unlocked PDF')
        if not 1 <= len(doc) <= max_pages:
            raise ValueError(f'PDF must contain 1 to {max_pages} pages')
        frequency = Counter()
        for page in doc:
            keys = set()
            for block in page.get_text('blocks'):
                if block[6] == 0 and (block[3] < page.cropbox.height * .12 or block[1] > page.cropbox.height * .88):
                    keys.update(clean.norm_key(line) for line in block[4].splitlines())
            frequency.update(keys)
        boiler = {key for key, count in frequency.items() if count >= max(3, len(doc) * .4)}
        pages, warnings, tail = [], [], False
        for number, page in enumerate(doc, 1):
            (destination / 'progress.json').write_text(json.dumps({'page': number, 'pages': len(doc)}))
            width, height = page.cropbox.width, page.cropbox.height
            blocks = [block for block in page.get_text('blocks') if block[6] == 0]
            native_chars = sum(len(re.sub(r'\s+', '', block[4])) for block in blocks)
            replacement_chars = sum(block[4].count('\ufffd') for block in blocks)
            scanned = native_chars < 60 or replacement_chars > max(5, native_chars * .1)
            nodes, table_boxes, page_warnings = [], [], []
            if not scanned:
                try:
                    for table in page.find_tables().tables:
                        text = table_to_md(table)
                        if text:
                            table_boxes.append(pymupdf.Rect(table.bbox))
                            nodes.append({'kind': 'table', 'text': text, 'bbox': list(table.bbox),
                                          'method': 'pymupdf_table', 'included': not tail})
                except Exception:
                    page_warnings.append('表格结构提取失败，保留文本层并核对原页')
                for block in order_blocks(blocks, width):
                    box = pymupdf.Rect(block[:4])
                    if any(box.get_area() and (box & table).get_area() / box.get_area() > .6 for table in table_boxes):
                        continue
                    if number > len(doc) * .5 and any(clean.TAIL_CUT.match(line.strip()) for line in block[4].splitlines()):
                        tail = True
                    text = clean_block(block[4], boiler, list(box), height)
                    if text:
                        nodes.append({'kind': 'text', 'text': text, 'bbox': list(box),
                                      'method': 'pymupdf_text', 'included': not tail})
            # Render the page, including vector graphics which are absent from get_images().
            has_visual = bool(page.get_images()) or len(page.get_drawings()) >= 12
            if scanned or has_visual:
                scale = min(2, 1800 / max(width, height))
                page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).save(destination / f'page-{number}.png')
            if scanned:
                page_warnings.append('此页文本层稀少或损坏，需要视觉识别并校对')
            positioned = [(*node['bbox'], node['text'], index, 0) for index, node in enumerate(nodes)]
            nodes = [nodes[block[5]] for block in order_blocks(positioned, width)]
            for index, node in enumerate(nodes):
                node.update(block_id=f'p{number}-b{index}', original_text=node['text'])
            record = {'page': number, 'width': width, 'height': height, 'native_chars': native_chars,
                      'needs_ocr': scanned, 'has_visual': has_visual, 'nodes': nodes,
                      'warnings': page_warnings, 'excluded': False, 'visual_status': 'pending' if scanned or has_visual else 'not_needed'}
            pages.append(record)
            warnings.extend(f'第{number}页：{item}' for item in page_warnings)
        first_text = '\n'.join(node['text'] for page in pages[:3] for node in page['nodes'])
        date_match = re.search(r'(20\d{2})[年./-]\s*(\d{1,2})[月./-]\s*(\d{1,2})', first_text)
        candidate_date = f'{date_match[1]}-{int(date_match[2]):02d}-{int(date_match[3]):02d}' if date_match else ''
        result = {'parser': 'pymupdf', 'parser_version': pymupdf.VersionBind,
                  'pages': pages, 'warnings': warnings, 'suggested_date': candidate_date,
                  'pdf_title': doc.metadata.get('title', '')}
        (destination / 'parsed.json').write_text(json.dumps(result, ensure_ascii=False))
        return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('pdf', type=Path)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--max-pages', type=int, default=200)
    args = parser.parse_args()
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (8 * 1024 ** 3, 8 * 1024 ** 3))
    parse_pdf(args.pdf, args.destination, args.max_pages)
