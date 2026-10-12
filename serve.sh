#!/usr/bin/env bash
# 起 vLLM 指向指定模型，并等到 /v1/models 真正可用才返回。
#
# 用法：  bash serve.sh <模型目录> [日志路径]
# 停止：  bash serve.sh --stop
#
# 两个坑（Step 2 踩过）：
#   - 本机 http_proxy 会把 127.0.0.1 的请求也代理掉，探活必须 --noproxy '*'
#   - stop 脚本依赖 PID 文件，文件丢了就失效，这里直接按进程名兜底
set -uo pipefail
RAG_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VLLM_ROOT="${VLLM_ROOT:-$RAG_ROOT/../../deployLLm}"
VLLM_START_SCRIPT="${VLLM_START_SCRIPT:-$VLLM_ROOT/start_qwen3_vllm_api.sh}"
VLLM_API_KEY_FILE="${VLLM_API_KEY_FILE:-$VLLM_ROOT/.vllm_api_key}"

stop_all() {
  pkill -TERM -f "vllm.entrypoints.openai.api_server" 2>/dev/null || true
  sleep 8
  pkill -KILL -f "vllm.entrypoints.openai.api_server" 2>/dev/null || true
  pkill -KILL -f "VLLM::" 2>/dev/null || true
  sleep 5
  echo "vLLM 已停止"
  nvidia-smi --query-gpu=index,memory.used --format=csv,noheader
}

if [[ "${1:-}" == "--stop" ]]; then stop_all; exit 0; fi

MODEL="${1:?用法: serve.sh <模型目录>}"
LOG="${2:-/tmp/vllm_serve.log}"
[[ -d "$MODEL" ]] || { echo "模型目录不存在: $MODEL" >&2; exit 1; }

stop_all >/dev/null 2>&1
echo ">>> 启动 vLLM -> $MODEL"
VLLM_MODEL_PATH="$MODEL" nohup bash "$VLLM_START_SCRIPT" > "$LOG" 2>&1 &

KEY=$(cat "$VLLM_API_KEY_FILE" 2>/dev/null || echo EMPTY)
for i in $(seq 1 180); do
  if curl -sS --noproxy '*' -H "Authorization: Bearer $KEY" \
       http://127.0.0.1:189/v1/models 2>/dev/null | grep -q '"id"'; then
    echo ">>> 就绪（等待 ${i}0 秒）"
    curl -sS --noproxy '*' -H "Authorization: Bearer $KEY" \
      http://127.0.0.1:189/v1/models 2>/dev/null | head -c 200; echo
    exit 0
  fi
  if grep -qiE "Traceback|Error loading|CUDA out of memory" "$LOG" 2>/dev/null; then
    echo ">>> 启动失败，日志尾部："; tail -20 "$LOG"; exit 1
  fi
  sleep 10
done
echo ">>> 超时未就绪，日志尾部："; tail -20 "$LOG"; exit 1
