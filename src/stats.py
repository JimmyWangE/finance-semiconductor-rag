"""Step 5 — 语料体检报告：把各阶段统计汇总成一份可直接写进论文的表。"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from pathlib import Path

from utils import DATA, cjk_ratio, get_logger, read_jsonl

log = get_logger("stats")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--final", type=Path, default=DATA / "dedup")
    ap.add_argument("--out", type=Path, default=DATA / "report" / "corpus_report.md")
    a = ap.parse_args()

    tri = {r["doc_id"]: r for r in read_jsonl(DATA / "report" / "triage.jsonl")}
    meta = {r["doc_id"]: r for r in read_jsonl(DATA / "report" / "metadata.jsonl")}
    clean = {r["doc_id"]: r for r in read_jsonl(DATA / "report" / "clean_stats.jsonl")}
    dedup = json.loads((DATA / "report" / "dedup_stats.json").read_text(encoding="utf-8"))

    docs = []
    for f in sorted(a.final.glob("*.md")):
        t = f.read_text(encoding="utf-8")
        m = meta.get(f.stem, {})
        docs.append({
            "doc_id": f.stem, "chars": len(t), "cjk": cjk_ratio(t),
            "org": m.get("org", "?"), "date": m.get("publish_date", "")[:7],
            "rating": m.get("rating") or "无", "pages": tri.get(f.stem, {}).get("pages", 0),
        })

    chars = [d["chars"] for d in docs]
    orgs = Counter(d["org"] for d in docs)
    months = Counter(d["date"] for d in docs if d["date"])
    ratings = Counter(d["rating"] for d in docs)

    L = []
    L.append("# 半导体研报语料体检报告\n")
    L.append("## 1. 漏斗\n")
    L.append("| 阶段 | 文档数 | 字符量 |")
    L.append("|---|---|---|")
    raw_ch = sum(r["chars"] for r in tri.values() if r["ok"])
    cl_ch = sum(r["chars"] for r in clean.values())
    L.append(f"| 原始 PDF 抽取 | {sum(1 for r in tri.values() if r['ok'])} | {raw_ch/1e6:.2f}M |")
    L.append(f"| 清洗后（去样板/免责/页眉脚） | {sum(1 for r in clean.values() if r['verdict']=='ok')} | {cl_ch/1e6:.2f}M |")
    L.append(f"| 段落级去重后 | {len(docs)} | {sum(chars)/1e6:.2f}M |")
    L.append(f"\n清洗丢弃 {100*(1-cl_ch/raw_ch):.1f}% 字符；去重丢弃 {100*dedup['drop_rate']:.1f}% 段落"
             f"（{dedup['paras_dropped']}/{dedup['paras_considered']}）。")
    L.append(f"\n估算训练 token ≈ **{sum(chars)*0.75/1e6:.2f}M**"
             " （中文约 0.75 token/字）。此量级只够 SFT，不足以做继续预训练。\n")

    L.append("## 2. 质量判定分布\n")
    for k, v in Counter(r["verdict"] for r in clean.values()).most_common():
        L.append(f"- `{k}`: {v}")

    L.append("\n## 3. 单文档规模\n")
    L.append(f"- 字符数 中位 {statistics.median(chars):.0f} / 均值 {statistics.mean(chars):.0f} "
             f"/ 最大 {max(chars)} / 最小 {min(chars)}")
    L.append(f"- 中文字符占比 中位 {statistics.median(d['cjk'] for d in docs):.2f}")
    long_docs = sum(1 for c in chars if c * 0.75 > 8192)
    L.append(f"- 超过 8192 token 的文档 {long_docs} 份 → 构造 SFT 样本时需切块或用长上下文")

    L.append("\n## 4. 券商分布（Top 15）\n")
    L.append("| 券商 | 报告数 |")
    L.append("|---|---|")
    for o, c in orgs.most_common(15):
        L.append(f"| {o} | {c} |")
    L.append(f"\n共 {len(orgs)} 家券商。")

    L.append("\n## 5. 时间分布\n")
    L.append("| 月份 | 报告数 |")
    L.append("|---|---|")
    for mo in sorted(months):
        L.append(f"| {mo} | {months[mo]} |")
    L.append("\n**注意：训练/测试集必须按时间切分**（用最晚的月份做 held-out），"
             "随机切分会因研报间互相引用而泄漏。\n")

    L.append("## 6. 评级分布\n")
    for r, c in ratings.most_common():
        L.append(f"- {r}: {c}")

    L.append("\n## 7. 待办：图表信息缺口\n")
    need_vlm = [r for r in tri.values() if r.get("ok") and r.get("needs_vlm")]
    L.append(f"- {len(need_vlm)} 份文档文字密度低或图密度高，核心结论在图里，"
             "纯文本抽取会丢信息 → 需 Step 2 用 VLM（Qwen3-VL）对导出的 PNG 做图表转述")
    figs = sum(len(list((DATA / 'raw_md' / 'figures' / r['doc_id']).glob('*.png')))
               for r in tri.values() if (DATA / 'raw_md' / 'figures' / r['doc_id']).is_dir())
    L.append(f"- 已导出候选图 {figs} 张（已过滤 logo / 分割线）")

    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text("\n".join(L) + "\n", encoding="utf-8")
    log.info("报告写出 -> %s", a.out)
    print("\n".join(L))


if __name__ == "__main__":
    main()
