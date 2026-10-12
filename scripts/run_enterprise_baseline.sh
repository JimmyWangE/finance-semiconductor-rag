#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
URL="${RAG_BASELINE_URL:-http://127.0.0.1:189}"
OUT="${RAG_BASELINE_OUTPUT:-$ROOT/data/rag/eval/benchmarks/${STAMP}-enterprise}"

cd "$ROOT"
PYTHONPATH=src .venv-rag/bin/python -m semi_rag.benchmark \
  --url "$URL" \
  --dataset data/rag/eval/manual_approved.jsonl \
  --quality-dataset data/rag/eval/manual_approved.jsonl \
  --chunks data/rag/index/current/chunks.jsonl \
  --sections retrieval quality concurrency stress \
  --endpoints search ask \
  --concurrency 1,2,4,8,16 \
  --rates 1,2,4,8 \
  --requests 50 \
  --stage-seconds 30 \
  --max-inflight 32 \
  --timeout 180 \
  --max-p95-ms 30000 \
  --max-rejection-rate 0.20 \
  --output "$OUT" "$@"

echo "Baseline report: $OUT/report.md"
