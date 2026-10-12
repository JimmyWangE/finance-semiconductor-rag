#!/usr/bin/env bash
set -euo pipefail
RAG_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="$RAG_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export TOKENIZERS_PARALLELISM=false
exec "$RAG_ROOT/.venv-rag/bin/python" -m semi_rag "$@"
