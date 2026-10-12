#!/usr/bin/env bash
# 在独立 venv 里安装 MinerU，避免把本机 transformers 5.5.3 降级（训练要用）。
# 用法：bash scripts/setup_mineru.sh && export MINERU_BIN=<打印出来的路径>
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="${VENV:-$ROOT/.venv_mineru}"
PYPI="${PYPI:-https://pypi.org/simple}"      # 内网源没有 mineru，必须走公网源

echo ">>> 创建 venv: $VENV"
python3 -m venv "$VENV"
"$VENV/bin/pip" install -q -U pip -i "$PYPI"

echo ">>> 安装 mineru[core]"
"$VENV/bin/pip" install -q -i "$PYPI" "mineru[core]"

echo ">>> 预下载模型权重（走 ModelScope，国内快且本机可达）"
export MINERU_MODEL_SOURCE=modelscope
"$VENV/bin/mineru-models-download" -s modelscope -m pipeline || {
  echo "!! 权重下载失败，可改用 HF 镜像： export HF_ENDPOINT=https://hf-mirror.com"; exit 1; }

echo
echo "完成。请执行："
echo "  export MINERU_MODEL_SOURCE=modelscope"
echo "  export MINERU_BIN=$VENV/bin/mineru"
echo
echo "先冒烟测 3 份（本机是 PPU-ZW810E 非 NVIDIA 卡，先用 cpu 确认能跑通）："
echo "  cd src && python3 parse_mineru.py --only-needs-vlm --limit 3 --device cpu"
