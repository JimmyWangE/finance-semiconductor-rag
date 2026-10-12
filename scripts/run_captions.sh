#!/usr/bin/env bash
# 图表转述守护脚本。
#
# 为什么需要它：PPU 的多模态 kernel 会偶发让 vLLM EngineCore 硬崩
# （实测 concurrency 16 撑约 18 张，concurrency 4 撑约 2000 张，都不是显存问题）。
# caption_figures.py 有熔断和断点续跑，所以只要"崩了就重启再续跑"即可跑完全量。
#
# 用法：bash scripts/run_captions.sh [concurrency]
set -uo pipefail

PROJ="$(cd "$(dirname "$0")/.." && pwd)"
VLLM_ROOT="${VLLM_ROOT:-$PROJ/../../deployLLm}"
VLLM_START_SCRIPT="${VLLM_START_SCRIPT:-$VLLM_ROOT/start_qwen3_vllm_api.sh}"
CONC="${1:-4}"
MAX_ROUNDS="${MAX_ROUNDS:-20}"
SERVER_LOG="${SERVER_LOG:-/tmp/vllm_server.log}"
CAPTION_LOG="${CAPTION_LOG:-/tmp/caption_full.log}"
KEY_FILE="${VLLM_API_KEY_FILE:-$VLLM_ROOT/.vllm_api_key}"
CAPTIONS="$PROJ/data/captions/captions.jsonl"

count_ok() { [[ -f "$CAPTIONS" ]] && grep -c '"ok": true' "$CAPTIONS" || echo 0; }

ensure_server() {
  for _ in $(seq 1 3); do
    if curl -s -m 5 --noproxy '*' http://127.0.0.1:190/v1/models \
         -H "Authorization: Bearer $(<"$KEY_FILE")" >/dev/null 2>&1; then
      return 0
    fi
    echo "[supervisor] 服务不可用，重启 …"
    pkill -f "vllm.entrypoints.openai.api_server" 2>/dev/null
    sleep 10
    nohup bash "$VLLM_START_SCRIPT" > "$SERVER_LOG" 2>&1 &
    for i in $(seq 1 40); do        # 加载约 135s，给到 10 分钟
      sleep 15
      curl -s -m 5 --noproxy '*' http://127.0.0.1:190/v1/models >/dev/null 2>&1 && \
        { echo "[supervisor] 服务就绪（${i}0s）"; return 0; }
    done
  done
  echo "[supervisor] 服务反复起不来，放弃" >&2
  return 1
}

for round in $(seq 1 "$MAX_ROUNDS"); do
  before=$(count_ok)
  echo "=========== 第 $round 轮（已完成 $before 张）==========="
  ensure_server || exit 1
  ( cd "$PROJ/src" && python3 caption_figures.py --concurrency "$CONC" ) 2>&1 | tee -a "$CAPTION_LOG" | tail -3
  after=$(count_ok)

  if ! grep -q "熔断触发" <(tail -30 "$CAPTION_LOG"); then
    echo "[supervisor] 本轮无熔断，全量完成。累计 $after 张。"
    exit 0
  fi
  if (( after <= before )); then
    # 一轮下来毫无进展，说明不是偶发崩溃，而是有图必然打死引擎；继续循环只会空转
    echo "[supervisor] 本轮零进展（$before -> $after），存在必然导致崩溃的输入，停止。" >&2
    exit 1
  fi
  echo "[supervisor] 本轮推进 $((after - before)) 张，重启后继续 …"
done

echo "[supervisor] 达到最大轮次 $MAX_ROUNDS，累计 $(count_ok) 张。"
