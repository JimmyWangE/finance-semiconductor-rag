#!/usr/bin/env bash
# Step 1 全链路：分诊 -> 解析 -> 元数据 -> 清洗 -> 去重 -> 体检报告
set -euo pipefail
cd "$(dirname "$0")/src"

W="${WORKERS:-12}"
echo "=============== 0/5 分诊 ==============="; python3 triage.py   --workers "$W"
echo "=============== 1/5 解析 ==============="; python3 parse_pymupdf.py --workers "$W" 2>&1 | grep -v pymupdf_layout
echo "=============== 2/5 元数据 ============="; python3 metadata.py
echo "=============== 3/5 清洗 ==============="; python3 clean.py
echo "=============== 4/5 去重 ==============="; python3 dedup.py
echo "=============== 5/5 报告 ==============="; python3 stats.py >/dev/null
echo
echo "最终语料: data/dedup/*.md"
echo "体检报告: data/report/corpus_report.md"
