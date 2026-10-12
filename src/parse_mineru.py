"""Step 1（可选增强）— 解析后端 B：MinerU。

什么时候需要它：
  PyMuPDF 的弱点是不理解版面语义 —— 双栏研报的左侧栏（分析师信息、相关报告）
  会被插进正文中间，跨页表格会断开，图表里的数据完全拿不到。
  clean.py 里那一批 `侧边栏残渣` 规则就是在给这个问题打补丁。
  MinerU 用 DocLayout-YOLO 做版面区域分类，能从根上解决。

建议用法：不必全量跑，先用 triage 标出的 needs_vlm 子集（140 份）跑 MinerU，
和 PyMuPDF 结果对比，值得再扩到全量。

环境隔离很重要：MinerU 会把 transformers 降到 4.x，
而本机现有 transformers 5.5.3 是训练要用的，所以务必装在独立 venv（见 scripts/setup_mineru.sh）。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from utils import DATA, get_logger, read_jsonl, write_jsonl

log = get_logger("mineru")


def run_mineru(pdf: Path, out_dir: Path, backend: str, device: str, mineru_bin: str) -> bool:
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [mineru_bin, "-p", str(pdf), "-o", str(out_dir), "-b", backend, "-d", device, "-l", "ch"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if r.returncode != 0:
            log.warning("%s 失败: %s", pdf.name, (r.stderr or r.stdout)[-300:])
            return False
        return True
    except subprocess.TimeoutExpired:
        log.warning("%s 超时", pdf.name)
        return False


def collect_md(out_dir: Path, doc_id: str, dest: Path) -> bool:
    """MinerU 产出目录结构为 <out>/<stem>/<backend>/<stem>.md。"""
    mds = sorted(out_dir.rglob("*.md"))
    if not mds:
        return False
    best = max(mds, key=lambda p: p.stat().st_size)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / f"{doc_id}.md").write_text(
        f"<!--doc:{doc_id}-->\n" + best.read_text(encoding="utf-8"), encoding="utf-8")
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--triage", type=Path, default=DATA / "report" / "triage.jsonl")
    ap.add_argument("--out", type=Path, default=DATA / "raw_md_mineru")
    ap.add_argument("--work", type=Path, default=DATA / "_mineru_work")
    ap.add_argument("--mineru-bin", default=os.environ.get("MINERU_BIN", "mineru"))
    ap.add_argument("--backend", default="pipeline",
                    help="pipeline=轻量CPU/GPU均可; vlm-transformers=更准但要显存")
    ap.add_argument("--device", default="cpu",
                    help="本机是 PPU-ZW810E 非 NVIDIA 卡，先用 cpu 验证能跑通再试 cuda")
    ap.add_argument("--only-needs-vlm", action="store_true",
                    help="只跑 triage 标记为图表密集的子集（推荐先这么试）")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    docs = [r for r in read_jsonl(a.triage) if r["ok"]]
    if a.only_needs_vlm:
        docs = [r for r in docs if r.get("needs_vlm")]
    if a.limit:
        docs = docs[: a.limit]
    log.info("MinerU 解析 %d 份（backend=%s device=%s）", len(docs), a.backend, a.device)

    rows = []
    for i, d in enumerate(docs, 1):
        wd = a.work / d["doc_id"]
        ok = run_mineru(Path(d["path"]), wd, a.backend, a.device, a.mineru_bin)
        ok = ok and collect_md(wd, d["doc_id"], a.out)
        rows.append({"doc_id": d["doc_id"], "file": d["file"], "ok": ok})
        if i % 10 == 0:
            log.info("  进度 %d/%d（成功 %d）", i, len(docs), sum(r["ok"] for r in rows))

    write_jsonl(DATA / "report" / "mineru_manifest.jsonl", rows)
    log.info("成功 %d / %d -> %s", sum(r["ok"] for r in rows), len(rows), a.out)
    log.info("下一步：python3 clean.py --src %s --out %s", a.out, DATA / "clean_mineru")


if __name__ == "__main__":
    main()
