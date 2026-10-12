"""公共工具：路径、JSONL 读写、日志、文本规范化。"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Iterator

PROJ = Path(__file__).resolve().parent.parent
DATA = PROJ / "data"
PDF_DIR = Path(os.environ.get("RAG_PDF_DIR", PROJ / "data/pdfs"))
META_CSV = Path(os.environ.get("RAG_METADATA_CSV", PROJ / "data/半导体_研报列表.csv"))


def get_logger(name: str) -> logging.Logger:
    lg = logging.getLogger(name)
    if not lg.handlers:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%H:%M:%S"))
        lg.addHandler(h)
        lg.setLevel(logging.INFO)
    return lg


# ---------------------------------------------------------------- jsonl io
def write_jsonl(path: Path, rows: Iterable[dict]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            n += 1
    return n


def read_jsonl(path: Path) -> Iterator[dict]:
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def doc_id(pdf_path: Path) -> str:
    """文件名稳定哈希，作为全流程主键。"""
    return hashlib.sha1(pdf_path.name.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- text norm
_CJK = re.compile(r"[一-鿿]")
# 句末标点：用于判断一行是否被 PDF 硬换行截断
_SENT_END = tuple("。！？；：…”』》）)】!?;:.")
_BULLET = re.compile(r"^\s*(?:[•·▪◆■□●○\-—*]|\(?\d{1,2}[\.、\)）]|[一二三四五六七八九十]{1,3}[、\.]|#{1,6}\s)")


def norm_space(s: str) -> str:
    """全角空格/零宽字符归一，压缩空白；不动换行。"""
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("​", "").replace("﻿", "").replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    return s


def cjk_ratio(s: str) -> float:
    if not s:
        return 0.0
    return len(_CJK.findall(s)) / len(s)


def merge_wrapped_lines(lines: list[str]) -> list[str]:
    """把 PDF 硬换行造成的断句重新拼回整段。

    规则：上一行不以句末标点结尾、且下一行不是标题/列表项，则视为同一段续行。
    中文直接拼接（不插空格），中英交界补空格。
    """
    out: list[str] = []
    for ln in lines:
        ln = ln.rstrip()
        if not ln:
            out.append("")
            continue
        if (
            out
            and out[-1]
            and not out[-1].endswith(_SENT_END)
            and not _BULLET.match(ln)
            and not out[-1].startswith("|")   # 表格行不合并
            and not ln.startswith("|")
            and not out[-1].startswith("#")
        ):
            prev = out[-1]
            joiner = "" if (_CJK.search(prev[-1]) or _CJK.search(ln[0])) else " "
            out[-1] = prev + joiner + ln
        else:
            out.append(ln)
    return out


def squeeze_blank(text: str) -> str:
    """连续空行压成一个。"""
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def dump_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
