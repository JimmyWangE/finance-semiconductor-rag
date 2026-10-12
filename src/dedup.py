"""Step 4 — 段落级近重复消除。

研报之间大量互相抄（尤其周报的行业动态、公司简介段落），文件级去重完全不够。
这里用自实现的 MinHash + LSH banding，无第三方依赖：
  - 段落切分 -> char 5-gram shingle -> 128 维 MinHash -> 16 band × 8 row
  - 同桶候选做真实 Jaccard 校验，超过阈值判定重复，保留最早出现的那份

产出 data/dedup/<doc_id>.md 与 data/report/dedup_stats.json。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path

from utils import DATA, get_logger, squeeze_blank

log = get_logger("dedup")

NUM_PERM = 128
BANDS = 16
ROWS = NUM_PERM // BANDS
SHINGLE = 5
MIN_PARA_CHARS = 40          # 太短的段落（小标题）不参与去重，否则会误杀
JACCARD_THR = 0.80
MASK = (1 << 61) - 1         # Mersenne prime，做通用哈希族


def _perms(n: int) -> list[tuple[int, int]]:
    """确定性生成哈希族参数，保证多次运行结果可复现。"""
    out = []
    for i in range(n):
        h = hashlib.blake2b(str(i).encode(), digest_size=16).digest()
        a = int.from_bytes(h[:8], "big") % MASK or 1
        b = int.from_bytes(h[8:], "big") % MASK
        out.append((a, b))
    return out


PERMS = _perms(NUM_PERM)


def shingles(text: str) -> set[int]:
    t = re.sub(r"\s+", "", text)
    if len(t) < SHINGLE:
        return set()
    return {int.from_bytes(hashlib.blake2b(t[i:i + SHINGLE].encode(), digest_size=8).digest(), "big")
            for i in range(len(t) - SHINGLE + 1)}


def minhash(sh: set[int]) -> tuple[int, ...]:
    if not sh:
        return tuple([0] * NUM_PERM)
    return tuple(min(((a * s + b) & MASK) for s in sh) for a, b in PERMS)


def jaccard(a: set[int], b: set[int]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def split_paras(text: str) -> list[str]:
    """清洗器输出里 merge_wrapped_lines 已把断行拼成完整段落，故一行即一段。

    不要按空行切 —— 空行是页边界，那样切出来的是页级块（~780 字符），
    嵌在页中间的共用段落（公司简介、行业动态）就漏掉了。
    """
    return [p.strip() for p in text.split("\n") if p.strip()]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, default=DATA / "clean")
    ap.add_argument("--out", type=Path, default=DATA / "dedup")
    ap.add_argument("--thr", type=float, default=JACCARD_THR)
    a = ap.parse_args()

    files = sorted(a.src.glob("*.md"))
    log.info("段落级去重：%d 份文档", len(files))

    docs: dict[str, list[str]] = {}
    units: list[tuple[str, int, set[int], tuple[int, ...]]] = []
    for f in files:
        paras = split_paras(f.read_text(encoding="utf-8"))
        docs[f.stem] = paras
        for i, p in enumerate(paras):
            if len(re.sub(r"\s+", "", p)) < MIN_PARA_CHARS:
                continue
            sh = shingles(p)
            if sh:
                units.append((f.stem, i, sh, minhash(sh)))
    log.info("参与去重的段落 %d 条", len(units))

    buckets: dict[tuple, list[int]] = defaultdict(list)
    for idx, (_, _, _, sig) in enumerate(units):
        for b in range(BANDS):
            buckets[(b, sig[b * ROWS:(b + 1) * ROWS])].append(idx)

    drop: set[int] = set()
    checked: set[tuple[int, int]] = set()
    for members in buckets.values():
        if len(members) < 2:
            continue
        keep = members[0]                      # 桶内第一条为代表（文件序稳定）
        for m in members[1:]:
            if m in drop:
                continue
            pair = (keep, m)
            if pair in checked:
                continue
            checked.add(pair)
            if jaccard(units[keep][2], units[m][2]) >= a.thr:
                drop.add(m)

    drop_by_doc: dict[str, set[int]] = defaultdict(set)
    for idx in drop:
        d, i, _, _ = units[idx]
        drop_by_doc[d].add(i)

    a.out.mkdir(parents=True, exist_ok=True)
    for stale in a.out.glob("*.md"):
        stale.unlink()
    kept_chars = dropped_chars = 0
    for did, paras in docs.items():
        bad = drop_by_doc.get(did, set())
        keep = [p for i, p in enumerate(paras) if i not in bad]
        dropped_chars += sum(len(paras[i]) for i in bad)
        text = squeeze_blank("\n".join(keep))
        kept_chars += len(text)
        if text:
            (a.out / f"{did}.md").write_text(text, encoding="utf-8")

    stats = {
        "docs": len(docs), "paras_considered": len(units),
        "paras_dropped": len(drop),
        "drop_rate": round(len(drop) / max(1, len(units)), 4),
        "kept_chars": kept_chars, "dropped_chars": dropped_chars,
        "jaccard_thr": a.thr,
    }
    (DATA / "report" / "dedup_stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("重复段落 %d 条 (%.1f%%)，删除 %.2fM 字符", len(drop), 100 * stats["drop_rate"],
             dropped_chars / 1e6)
    log.info("最终语料 %.2fM 字符 -> %s", kept_chars / 1e6, a.out)


if __name__ == "__main__":
    main()
