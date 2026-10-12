"""Step 3 — 元数据挂载：把 半导体_研报列表.csv 的券商/日期/评级挂到每个 doc_id 上。

PDF 文件名是报告标题（下载时对非法字符做过替换），CSV 里是原始标题，
所以用归一化标题做主匹配，失败再退回去标点的模糊匹配。
"""
from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path

from utils import DATA, META_CSV, PDF_DIR, doc_id, get_logger, write_jsonl

log = get_logger("meta")

# 下载器把这些字符换成了全角或下划线，归一化时一并抹掉
_STRIP = re.compile(r"[\s:：/／\\|｜*?？\"'“”‘’<>《》()（）\[\]【】,，.。、_\-—~!！&%]+")


def tkey(s: str) -> str:
    return _STRIP.sub("", s).lower()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", type=Path, default=META_CSV)
    ap.add_argument("--pdf-dir", type=Path, default=PDF_DIR)
    ap.add_argument("--out", type=Path, default=DATA / "report" / "metadata.jsonl")
    a = ap.parse_args()

    with a.csv.open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    log.info("CSV %d 行", len(rows))

    by_title: dict[str, dict] = {}
    for r in rows:
        by_title.setdefault(tkey(r.get("title", "")), r)

    out, miss = [], []
    for pdf in sorted(a.pdf_dir.glob("*.pdf")):
        k = tkey(pdf.stem)
        m = by_title.get(k)
        if m is None:  # 标题被截断的情况：找唯一前缀匹配
            cands = [v for kk, v in by_title.items() if kk.startswith(k[:24]) and len(k) >= 24]
            m = cands[0] if len(cands) == 1 else None
        rec = {"doc_id": doc_id(pdf), "file": pdf.name, "title": pdf.stem}
        if m:
            rec.update(
                org=m.get("org_name", "").strip(),
                publish_date=(m.get("publish_date") or "")[:10],
                rating=m.get("rating", "").strip(),
                industry=m.get("industry", "").strip(),
                stock_name=m.get("stock_name", "").strip(),
                stock_code=m.get("stock_code", "").strip(),
                info_code=m.get("info_code", "").strip(),
                matched=True,
            )
        else:
            rec["matched"] = False
            miss.append(pdf.stem)
        out.append(rec)

    write_jsonl(a.out, out)
    ok = sum(r["matched"] for r in out)
    log.info("匹配 %d/%d (%.1f%%)", ok, len(out), 100 * ok / len(out))
    for t in miss[:10]:
        log.warning("  未匹配: %s", t[:60])
    orgs = {}
    for r in out:
        if r["matched"]:
            orgs[r["org"]] = orgs.get(r["org"], 0) + 1
    log.info("覆盖券商 %d 家，Top5: %s", len(orgs),
             sorted(orgs.items(), key=lambda x: -x[1])[:5])
    log.info("写出 -> %s", a.out)


if __name__ == "__main__":
    main()
