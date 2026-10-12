"""Step 0 — 分诊：扫描全部 PDF，统计页数/文字密度/图片数，标记哪些需要 VLM 补图表。

产出 data/report/triage.jsonl，后续解析与抽样都基于它。
"""
from __future__ import annotations

import argparse
import statistics
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pymupdf

from utils import DATA, PDF_DIR, doc_id, get_logger, write_jsonl

log = get_logger("triage")

# 文字密度低于该阈值(字符/页)说明内容主要在图里，正文抽取会漏信息
LOW_TEXT_CHARS_PER_PAGE = 600
# 图片密度高于该阈值(张/页)同样说明图表承载了核心信息
HIGH_IMG_PER_PAGE = 3.0


def probe(pdf: Path) -> dict:
    rec: dict = {"doc_id": doc_id(pdf), "file": pdf.name, "path": str(pdf)}
    try:
        with pymupdf.open(pdf) as d:
            n = d.page_count
            chars = imgs = 0
            for pg in d:
                chars += len(pg.get_text())
                imgs += len(pg.get_images())
            rec.update(
                pages=n,
                chars=chars,
                images=imgs,
                chars_per_page=round(chars / max(1, n), 1),
                imgs_per_page=round(imgs / max(1, n), 2),
                encrypted=d.is_encrypted,
                ok=True,
            )
            rec["needs_ocr"] = rec["chars_per_page"] < 50
            rec["needs_vlm"] = (
                rec["chars_per_page"] < LOW_TEXT_CHARS_PER_PAGE
                or rec["imgs_per_page"] > HIGH_IMG_PER_PAGE
            )
    except Exception as e:  # 损坏/加密 PDF 不应中断全量扫描
        rec.update(ok=False, error=f"{type(e).__name__}: {e}", pages=0)
    return rec


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf-dir", type=Path, default=PDF_DIR)
    ap.add_argument("--out", type=Path, default=DATA / "report" / "triage.jsonl")
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()

    pdfs = sorted(a.pdf_dir.glob("*.pdf"))
    log.info("扫描 %d 份 PDF …", len(pdfs))
    with ProcessPoolExecutor(a.workers) as ex:
        rows = list(ex.map(probe, pdfs, chunksize=4))

    write_jsonl(a.out, rows)

    ok = [r for r in rows if r["ok"]]
    bad = [r for r in rows if not r["ok"]]
    pgs = [r["pages"] for r in ok]
    log.info("成功 %d / 失败 %d", len(ok), len(bad))
    for r in bad:
        log.warning("  解析失败: %s -> %s", r["file"], r["error"])
    log.info("总页数 %d，中位 %.0f，最大 %d", sum(pgs), statistics.median(pgs), max(pgs))
    log.info("总字符 %.2fM (≈%.2fM tokens)", sum(r["chars"] for r in ok) / 1e6,
             sum(r["chars"] for r in ok) / 1e6 * 0.75)
    log.info("需要 OCR: %d", sum(r["needs_ocr"] for r in ok))
    log.info("需要 VLM 补图表: %d", sum(r["needs_vlm"] for r in ok))
    log.info("写出 -> %s", a.out)


if __name__ == "__main__":
    main()
