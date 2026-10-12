"""Step 2 — 清洗：去样板、去页眉页脚、去免责声明、断句重组、质量过滤。

两遍扫描：
  Pass 1  统计跨文档的高频行（各家券商的免责声明/评级说明模板）
  Pass 2  逐文档清洗 —— 文档内高频行(页眉页脚) + 跨文档模板 + 规则删除 + 质量打分

产出 data/clean/<doc_id>.md 与 data/report/clean_stats.jsonl。
"""
from __future__ import annotations

import argparse
import re
from collections import Counter
from pathlib import Path

from utils import (DATA, cjk_ratio, get_logger, merge_wrapped_lines, norm_space,
                   squeeze_blank, write_jsonl)

log = get_logger("clean")

PAGE_RE = re.compile(r"^<!--page:(\d+)-->$")
DOC_RE = re.compile(r"^<!--doc:([0-9a-f]+)-->$")

# ---- 尾部切断：命中即丢弃该行之后的全部内容（免责声明/评级说明/通讯录都在尾部）
TAIL_CUT = re.compile(
    r"^\s*(?:"
    r"免责\s*[声明条款]{2}|免责及评级说明|法律声明|特别声明|重要声明|重要提示与声明"
    r"|分析师\s*声明|分析师承诺|证券分析师承诺"
    r"|投资评级\s*(?:说明|标准|体系)|评级\s*(?:说明|标准|体系)|公司投资评级|行业投资评级"
    r"|信息披露|一般声明|适当性(?:声明|说明)"
    r"|分析师(?:与研究助理)?简介|研究助理简介|分析师与研究助理简介|研究团队简介"
    r"|联系我们|机构销售(?:团队|通讯录)?|销售(?:团队|服务)通讯录|研究院?销售团队"
    r")\s*[:：]?\s*$"
)

# ---- 整行删除：页眉页脚常客
LINE_DROP = [
    re.compile(r"^\s*\d{1,4}\s*$"),                                  # 纯页码
    re.compile(r"^\s*(?:第\s*)?\d{1,3}\s*[/／]\s*\d{1,3}\s*(?:页)?\s*$"),  # 3/12
    re.compile(r"^\s*请务必阅读.{0,40}$"),                            # 请务必阅读正文之后的免责条款
    re.compile(r"^\s*敬请参阅.{0,40}$"),
    re.compile(r"^\s*(?:证券)?研究报告\s*$"),
    re.compile(r"^\s*(?:执业)?证书?编号\s*[:：]?\s*[SsA-Z]?\d{6,}\s*$"),
    re.compile(r"^\s*S\d{13}\s*$"),                                   # SAC 执业编号
    re.compile(r"^\s*(?:https?://)?www\.[\w.-]+\.(?:com|cn|net)(?:\.cn)?\s*$"),
    re.compile(r"^\s*[\w.+-]+@[\w-]+\.[\w.]+\s*$"),                   # 纯邮箱行
    re.compile(r"^\s*(?:400|800)[-\s]?\d{3}[-\s]?\d{4}\s*$"),         # 400 电话
    re.compile(r"^\s*(?:\+?86[-\s]?)?1[3-9]\d{9}\s*$"),               # 手机号
    re.compile(r"^\s*(?:Tel|Fax|电话|传真|邮编|地址)\s*[:：].{0,60}$"),
    re.compile(r"^\s*[-—=_·.．*]{3,}\s*$"),                           # 分割线
    re.compile(r"^\s*!\[figure\]\(.*\)\s*$"),                         # 图片占位（单独存表，正文不留）
    re.compile(r"^.{2,60}[.．·]{5,}\s*\d{0,3}\s*$"),                  # 目录行：标题......12
    # 侧边栏残渣：擦掉证书号/邮箱后剩下的空标签，以及作者署名行
    re.compile(r"^\s*(?:分析师|研究助理|联系人|证券分析师)\s*[:：]\s*[一-鿿]{2,4}\s*"
               r"(?:(?:执业)?(?:证书|资格)?(?:编)?号|邮箱|电话)?\s*[:：]?\s*$"),
    re.compile(r"^\s*(?:邮箱|电话|执业证书号|证书编号|资格编号|执业编号)\s*[:：]\s*$"),
]

# ---- 行内擦除
INLINE_SUB = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), ""),
    (re.compile(r"(?:执业)?(?:证书|资格)编号\s*[:：]?\s*[SsA-Z]\d{6,}"), ""),
    (re.compile(r"S\d{13}"), ""),
]

# ---- 需要整段丢弃的版权/机密声明（多出现在封面）
COPYRIGHT_HINT = re.compile(r"未经.{0,12}(?:书面)?许可|保留.{0,6}法律|版权所有|All rights reserved|高度机密")

# ---- 质量过滤阈值
MIN_DOC_CHARS = 500
MIN_CJK_RATIO = 0.25
MAX_DUP_LINE_RATIO = 0.35

# 跨文档样板的最小长度：低于此长度的高频行是真实小标题（风险提示/投资要点），不是样板
GLOBAL_BOILER_MIN_LEN = 12
# 尾部切断只在文档后半段生效，否则目录里的『投资评级说明』会把整篇砍掉
TAIL_CUT_MIN_FRAC = 0.5


def split_pages(md: str) -> list[list[str]]:
    pages, cur = [], []
    for ln in md.splitlines():
        if PAGE_RE.match(ln):
            pages.append(cur)
            cur = []
        elif DOC_RE.match(ln):
            continue
        else:
            cur.append(ln)
    pages.append(cur)
    return [p for p in pages if p]


def norm_key(s: str) -> str:
    """样板行比对用的归一化键：去数字/空白，避免『第3页』这类带变量的行漏网。"""
    return re.sub(r"[\d\s]+", "", norm_space(s))[:60]


# ------------------------------------------------------------------ Pass 1
def boiler_candidate(ln: str) -> str | None:
    """能否作为跨文档样板候选。

    只有『足够长』的整句才可能是共用模板（免责声明句、评级定义句）。
    短行如『风险提示』『投资要点』是真实小标题，绝不能因高频就删；
    表格行与页标记也必须排除，否则会打散表格结构。
    """
    s = ln.strip()
    if not s or s.startswith("|") or s.startswith("<!--") or s.startswith("!["):
        return None
    k = norm_key(s)
    return k if GLOBAL_BOILER_MIN_LEN <= len(k) <= 60 else None


def collect_global_boilerplate(files: list[Path], min_docs: int) -> set[str]:
    """出现在 >= min_docs 份不同文档里的长行 = 跨券商共用模板。"""
    df = Counter()
    for f in files:
        keys = set()
        for ln in f.read_text(encoding="utf-8").splitlines():
            k = boiler_candidate(ln)
            if k:
                keys.add(k)
        df.update(keys)
    boiler = {k for k, c in df.items() if c >= min_docs}
    log.info("跨文档样板行 %d 条（阈值 ≥%d 份文档）", len(boiler), min_docs)
    for k, c in df.most_common(15):
        if c >= min_docs:
            log.info("    [%3d] %s", c, k[:50])
    return boiler


# ------------------------------------------------------------------ Pass 2
def doc_boilerplate(pages: list[list[str]], ratio: float = 0.35) -> set[str]:
    """出现在本文档 >=ratio 页面上的短行 = 页眉/页脚/水印。"""
    if len(pages) < 4:
        return set()
    df = Counter()
    for p in pages:
        df.update({norm_key(ln) for ln in p
                   if not ln.lstrip().startswith(("|", "!["))
                   and 2 <= len(norm_key(ln)) <= 40})
    thr = max(2, int(len(pages) * ratio))
    return {k for k, c in df.items() if c >= thr}


def clean_doc(md: str, global_boiler: set[str]) -> tuple[str, dict]:
    pages = split_pages(md)
    local_boiler = doc_boilerplate(pages)
    raw_lines = sum(len(p) for p in pages)

    kept: list[str] = []
    cut_at_tail = False
    tail_zone = int(len(pages) * TAIL_CUT_MIN_FRAC)
    for pi, p in enumerate(pages):
        if cut_at_tail:
            break
        for ln in p:
            ln = norm_space(ln).strip()
            if not ln:
                continue
            if pi >= tail_zone and TAIL_CUT.match(ln):
                cut_at_tail = True
                break
            k = norm_key(ln)
            is_table = ln.startswith("|")
            if not is_table and (k in local_boiler or k in global_boiler):
                continue
            # 先擦行内敏感串，再判整行丢弃：这样『邮箱:xx@yy』会先变成『邮箱:』再被空标签规则命中
            if not is_table:
                for pat, rep in INLINE_SUB:
                    ln = pat.sub(rep, ln)
                ln = norm_space(ln).strip()
                if any(r.match(ln) for r in LINE_DROP):
                    continue
                if COPYRIGHT_HINT.search(ln):
                    continue
                ln = ln.strip(" ，,、")
            if len(ln) >= 2:
                kept.append(ln)
        kept.append("")            # 页边界当段落边界，避免跨页误合并

    text = squeeze_blank("\n".join(merge_wrapped_lines(kept)))
    stats = {
        "raw_lines": raw_lines,
        "kept_lines": len([l for l in text.splitlines() if l.strip()]),
        "chars": len(text),
        "cjk_ratio": round(cjk_ratio(text), 3),
        "tail_cut": cut_at_tail,
        "local_boiler": len(local_boiler),
    }
    return text, stats


def quality_verdict(text: str, st: dict) -> str:
    if st["chars"] < MIN_DOC_CHARS:
        return "too_short"
    if st["cjk_ratio"] < MIN_CJK_RATIO:
        return "low_cjk"
    lines = [l for l in text.splitlines() if len(l) > 10]
    if lines:
        dup = 1 - len(set(lines)) / len(lines)
        if dup > MAX_DUP_LINE_RATIO:
            return "repetitive"
    return "ok"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, default=DATA / "raw_md")
    ap.add_argument("--out", type=Path, default=DATA / "clean")
    ap.add_argument("--min-docs", type=int, default=0,
                    help="跨文档样板阈值；0 表示按 max(5, 5%%*文档数) 自动取")
    a = ap.parse_args()

    files = sorted(a.src.glob("*.md"))
    if not files:
        log.error("没有找到 %s/*.md，先跑 parse_pymupdf.py", a.src)
        return
    min_docs = a.min_docs or max(5, int(len(files) * 0.05))
    log.info("清洗 %d 份文档", len(files))

    global_boiler = collect_global_boilerplate(files, min_docs)
    a.out.mkdir(parents=True, exist_ok=True)
    for stale in a.out.glob("*.md"):    # 清掉上一轮残留，否则被过滤掉的文档还留在目录里
        stale.unlink()

    stats_rows, verdicts = [], Counter()
    for f in files:
        text, st = clean_doc(f.read_text(encoding="utf-8"), global_boiler)
        v = quality_verdict(text, st)
        verdicts[v] += 1
        st.update(doc_id=f.stem, verdict=v)
        stats_rows.append(st)
        if v == "ok":
            (a.out / f.name).write_text(text, encoding="utf-8")

    write_jsonl(DATA / "report" / "clean_stats.jsonl", stats_rows)
    tot_raw = sum(s["raw_lines"] for s in stats_rows)
    tot_kept = sum(s["kept_lines"] for s in stats_rows)
    log.info("行数 %d -> %d（删除 %.1f%%）", tot_raw, tot_kept, 100 * (1 - tot_kept / max(1, tot_raw)))
    log.info("字符总量 %.2fM", sum(s["chars"] for s in stats_rows) / 1e6)
    log.info("质量判定 %s", dict(verdicts))
    log.info("写出 -> %s", a.out)


if __name__ == "__main__":
    main()
