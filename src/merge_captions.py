"""Step 2b — 把 VLM 图表转述回填进语料。

清洗阶段把 `![figure](...)` 占位行删掉了（它对训练没价值），
这里按 doc_id + page 把转述文本作为一个正文段落插回去，
位置放在该页正文之后，形成「页面文字 + 图表转述」的完整上下文。

产出 data/corpus/<doc_id>.md —— 这才是最终喂给 SFT 构造的语料。
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

from utils import DATA, get_logger, read_jsonl, squeeze_blank

log = get_logger("merge")

# 转述里这几个字段没有信息量，回填时压掉，避免大量重复模板污染训练语料
DROP_FIELDS = re.compile(r"^【(?:类型)】\s*", re.M)


def compact(vlm: str) -> str:
    """把五段式转述压成一段连续文字，去掉格式标记。"""
    parts: dict[str, str] = {}
    cur = None
    for ln in vlm.splitlines():
        m = re.match(r"^\s*【(.+?)】\s*(.*)$", ln)
        if m:
            cur = m.group(1)
            parts[cur] = m.group(2).strip()
        elif cur:
            parts[cur] = (parts[cur] + " " + ln.strip()).strip()
    if not parts:
        return re.sub(r"\s+", " ", vlm).strip()

    seg = []
    if parts.get("主题"):
        seg.append(parts["主题"].rstrip("。"))
    if parts.get("坐标轴") and parts["坐标轴"] != "无":
        seg.append("坐标轴：" + parts["坐标轴"].rstrip("。"))
    if parts.get("关键数据"):
        kd = re.sub(r"\s*[\-\*•]\s*|\s*\d+\.\s+", "；", parts["关键数据"])
        kd = re.sub(r"[；\s]{2,}", "；", kd).strip("； ")
        seg.append("关键数据：" + kd)
    if parts.get("结论"):
        seg.append(parts["结论"].rstrip("。"))
    return "。".join(s for s in seg if s) + "。"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", type=Path, default=DATA / "dedup",
                    help="已清洗去重的正文目录")
    ap.add_argument("--captions", type=Path, default=DATA / "captions" / "captions.jsonl")
    ap.add_argument("--raw", type=Path, default=DATA / "raw_md")
    ap.add_argument("--out", type=Path, default=DATA / "corpus")
    ap.add_argument("--min-chars", type=int, default=40,
                    help="转述短于此长度视为无效，丢弃")
    a = ap.parse_args()

    if not a.captions.is_file():
        log.error("找不到 %s，先跑 caption_figures.py", a.captions)
        return

    caps: dict[str, list[tuple[int, str]]] = defaultdict(list)
    n_raw = n_used = 0
    for r in read_jsonl(a.captions):
        n_raw += 1
        if not r.get("ok") or r.get("invalid"):
            continue
        txt = compact(r.get("vlm_text", ""))
        if len(txt) < a.min_chars:
            continue
        caps[r["doc_id"]].append((r["page"], txt))
        n_used += 1
    log.info("转述记录 %d 条，可用 %d 条，覆盖 %d 份文档", n_raw, n_used, len(caps))

    a.out.mkdir(parents=True, exist_ok=True)
    for stale in a.out.glob("*.md"):
        stale.unlink()

    added_chars = 0
    docs = sorted(a.clean.glob("*.md"))
    for f in docs:
        body = f.read_text(encoding="utf-8")
        figs = sorted(caps.get(f.stem, []))
        if figs:
            block = "\n".join(f"【图表】{t}" for _, t in figs)
            body = body.rstrip() + "\n\n" + block
            added_chars += len(block)
        (a.out / f.name).write_text(squeeze_blank(body), encoding="utf-8")

    total = sum(len((a.out / f.name).read_text(encoding="utf-8")) for f in docs)
    log.info("回填 %.2fM 字符图表信息，最终语料 %.2fM 字符（%d 份）",
             added_chars / 1e6, total / 1e6, len(docs))
    log.info("写出 -> %s", a.out)


if __name__ == "__main__":
    main()
