"""Step 1 — 解析后端 A：PyMuPDF。

全部 PDF 都带文本层（见 triage），所以不需要 OCR。这里做的是：
  1. 按阅读顺序抽正文（自动识别双栏排版）
  2. 用 PyMuPDF 内置 find_tables 抽表格 -> Markdown
  3. 导出正文图表为 PNG + 记录题注，供 Step 2 的 VLM 补描述

产出 data/raw_md/<doc_id>.md（带 page 标记）和 data/raw_md/<doc_id>.json（结构化页级数据）。
"""
from __future__ import annotations

import argparse
import re
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import pymupdf

from utils import DATA, doc_id, dump_json, get_logger, norm_space, read_jsonl, write_jsonl

log = get_logger("parse")

MIN_IMG_PX = 120        # 小于此边长的图基本是 logo / 图标
MAX_ASPECT = 12.0       # 极端长条 = 分割线
CAPTION_RE = re.compile(r"^\s*(图表?\s*\d+|表\s*\d+|Figure\s*\d+|Exhibit\s*\d+|数据来源[:：])")


# ------------------------------------------------------------------ 版面
def detect_columns(blocks: list, page_width: float) -> int:
    """检测是否双栏：看页面中带 (45%~55% 宽) 是否被文本块横跨。"""
    mid_lo, mid_hi = page_width * 0.45, page_width * 0.55
    body = [b for b in blocks if (b[3] - b[1]) > 5 and (b[2] - b[0]) > 20]
    if len(body) < 4:
        return 1
    crossing = sum(1 for b in body if b[0] < mid_lo and b[2] > mid_hi)
    return 1 if crossing / len(body) > 0.15 else 2


def order_blocks(blocks: list, page_width: float) -> list:
    """按阅读顺序排序文本块。单栏 y→x；双栏先左栏后右栏。"""
    if detect_columns(blocks, page_width) == 1:
        return sorted(blocks, key=lambda b: (round(b[1], 1), b[0]))
    mid = page_width / 2
    left = sorted([b for b in blocks if (b[0] + b[2]) / 2 < mid], key=lambda b: (round(b[1], 1), b[0]))
    right = sorted([b for b in blocks if (b[0] + b[2]) / 2 >= mid], key=lambda b: (round(b[1], 1), b[0]))
    return left + right


# ------------------------------------------------------------------ 表格
def table_to_md(tbl) -> str:
    try:
        rows = tbl.extract()
    except Exception:
        return ""
    rows = [[norm_space(str(c or "")).replace("\n", " ").replace("|", "/") for c in r] for r in rows]
    rows = [r for r in rows if any(c.strip() for c in r)]
    if len(rows) < 2 or len(rows[0]) < 2:
        return ""
    ncol = max(len(r) for r in rows)
    rows = [r + [""] * (ncol - len(r)) for r in rows]
    head, body = rows[0], rows[1:]
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * ncol]
    out += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(out)


# ------------------------------------------------------------------ 图片
def export_images(page, doc, out_dir: Path, pageno: int) -> list[dict]:
    figs = []
    for i, info in enumerate(page.get_images(full=True)):
        xref = info[0]
        try:
            rects = page.get_image_rects(xref)
            if not rects:
                continue
            r = rects[0]
            w, h = r.width, r.height
            if w < MIN_IMG_PX or h < MIN_IMG_PX:
                continue
            if max(w, h) / max(1.0, min(w, h)) > MAX_ASPECT:
                continue
            pix = pymupdf.Pixmap(doc, xref)
            if pix.colorspace and pix.colorspace.n > 3:      # CMYK -> RGB
                pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
            out_dir.mkdir(parents=True, exist_ok=True)
            fn = out_dir / f"p{pageno:03d}_{i}.png"
            pix.save(fn)
            pix = None
            figs.append({"page": pageno, "file": str(fn), "bbox": [r.x0, r.y0, r.x1, r.y1],
                         "caption": nearest_caption(page, r)})
        except Exception:
            continue
    return figs


def nearest_caption(page, rect) -> str:
    """取图上方/下方最近的一行『图N …』或『数据来源：…』作为题注。"""
    best, best_d = "", 1e9
    for b in page.get_text("blocks"):
        txt = norm_space(b[4]).strip()
        if not CAPTION_RE.match(txt):
            continue
        d = min(abs(b[1] - rect.y1), abs(rect.y0 - b[3]))
        if d < best_d and d < 80:
            best, best_d = txt.split("\n")[0][:120], d
    return best


# ------------------------------------------------------------------ 主流程
def parse_one(task: tuple[str, str, Path, bool]) -> dict:
    did, path, out_root, want_images = task
    pdf = Path(path)
    pages_out, all_figs = [], []
    try:
        with pymupdf.open(pdf) as doc:
            for pno, page in enumerate(doc):
                pw = page.rect.width

                # 表格先抽，并记下 bbox 以便把表格区域的文字从正文里剔除
                md_tables, tbl_boxes = [], []
                try:
                    for t in page.find_tables().tables:
                        md = table_to_md(t)
                        if md:
                            md_tables.append(md)
                            tbl_boxes.append(pymupdf.Rect(t.bbox))
                except Exception:
                    pass

                blocks = [b for b in page.get_text("blocks") if b[6] == 0]
                blocks = [b for b in blocks
                          if not any(pymupdf.Rect(b[:4]).intersects(tb)
                                     and pymupdf.Rect(b[:4]).get_area()
                                     and (pymupdf.Rect(b[:4]) & tb).get_area() / pymupdf.Rect(b[:4]).get_area() > 0.6
                                     for tb in tbl_boxes)]
                lines = []
                for b in order_blocks(blocks, pw):
                    for ln in norm_space(b[4]).splitlines():
                        if ln.strip():
                            lines.append(ln.strip())

                figs = export_images(page, doc, out_root / "figures" / did, pno) if want_images else []
                all_figs += figs
                pages_out.append({"page": pno, "lines": lines, "tables": md_tables,
                                  "figures": [f["file"] for f in figs]})
        return {"doc_id": did, "file": pdf.name, "ok": True,
                "pages": pages_out, "figures": all_figs}
    except Exception as e:
        return {"doc_id": did, "file": pdf.name, "ok": False, "error": f"{type(e).__name__}: {e}"}


def render_markdown(rec: dict) -> str:
    """写成带 <!--page:N--> 标记的 Markdown，清洗阶段还能按页操作。"""
    parts = [f"<!--doc:{rec['doc_id']}-->", f"# {Path(rec['file']).stem}", ""]
    for p in rec["pages"]:
        parts.append(f"<!--page:{p['page']}-->")
        parts += p["lines"]
        for t in p["tables"]:
            parts += ["", t, ""]
        for f in p["figures"]:
            parts.append(f"![figure]({f})")
        parts.append("")
    return "\n".join(parts)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--triage", type=Path, default=DATA / "report" / "triage.jsonl")
    ap.add_argument("--out", type=Path, default=DATA / "raw_md")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 份，用于冒烟测试")
    ap.add_argument("--no-images", action="store_true", help="跳过图片导出（快很多）")
    a = ap.parse_args()

    docs = [r for r in read_jsonl(a.triage) if r["ok"]]
    if a.limit:
        docs = docs[: a.limit]
    a.out.mkdir(parents=True, exist_ok=True)

    tasks = [(d["doc_id"], d["path"], a.out, not a.no_images) for d in docs]
    log.info("解析 %d 份（images=%s, workers=%d）…", len(tasks), not a.no_images, a.workers)

    manifest, done, failed = [], 0, 0
    with ProcessPoolExecutor(a.workers) as ex:
        for fut in as_completed([ex.submit(parse_one, t) for t in tasks]):
            rec = fut.result()
            done += 1
            if not rec["ok"]:
                failed += 1
                log.warning("失败 %s: %s", rec["file"], rec["error"])
                continue
            (a.out / f"{rec['doc_id']}.md").write_text(render_markdown(rec), encoding="utf-8")
            dump_json(a.out / f"{rec['doc_id']}.json", rec)
            manifest.append({"doc_id": rec["doc_id"], "file": rec["file"],
                             "n_pages": len(rec["pages"]),
                             "n_tables": sum(len(p["tables"]) for p in rec["pages"]),
                             "n_figures": len(rec["figures"])})
            if done % 50 == 0:
                log.info("  进度 %d/%d", done, len(tasks))

    write_jsonl(DATA / "report" / "parse_manifest.jsonl", manifest)
    log.info("完成 %d，失败 %d", len(manifest), failed)
    log.info("表格 %d 张，图片 %d 张",
             sum(m["n_tables"] for m in manifest), sum(m["n_figures"] for m in manifest))


if __name__ == "__main__":
    main()
