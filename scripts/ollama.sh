#!/usr/bin/env bash
# 本地 GPT 运行时（Ollama）启停脚本。
#
# AIBAR 只依赖 OpenAI-compatible 接口，Ollama 只是推荐的默认运行时。
# 应用与模型都放在 data/ollama/ 下（已被 .gitignore 忽略），不污染仓库。
#
# 用法：
#   ./scripts/ollama.sh start    # 启动并等待 /api/version
#   ./scripts/ollama.sh stop
#   ./scripts/ollama.sh status
#   ./scripts/ollama.sh pull qwen2.5:3b
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE_DIR="$ROOT/data/ollama"
HOME_DIR="$STATE_DIR/home"
MODELS_DIR="$STATE_DIR/models"
PID_FILE="$ROOT/tmp/ollama.pid"
LOG_FILE="$ROOT/tmp/ollama.log"
HOST="${OLLAMA_HOST:-127.0.0.1:11434}"

find_bin() {
  local candidates=(
    "${OLLAMA_BIN:-}"
    "$STATE_DIR/Ollama.app/Contents/Resources/ollama"
    "$ROOT/tmp/ollama/Ollama.app/Contents/Resources/ollama"
  )
  for candidate in "${candidates[@]}"; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

is_up() {
  curl -fsS -m 2 "http://$HOST/api/version" >/dev/null 2>&1
}

cmd_start() {
  if is_up; then
    echo "[ollama] 已在运行：http://$HOST"
    return 0
  fi
  local bin
  if ! bin="$(find_bin)"; then
    echo "[ollama] 找不到 Ollama 可执行文件" >&2
    echo "         请把 Ollama.app 放到 data/ollama/，或设置 OLLAMA_BIN" >&2
    return 1
  fi
  mkdir -p "$HOME_DIR" "$MODELS_DIR" "$(dirname "$PID_FILE")"
  echo "[ollama] 启动：$bin"
  OLLAMA_MODELS="$MODELS_DIR" OLLAMA_HOST="$HOST" HOME="$HOME_DIR" \
    nohup "$bin" serve >>"$LOG_FILE" 2>&1 &
  echo $! >"$PID_FILE"
  for _ in $(seq 1 40); do
    if is_up; then
      echo "[ollama] 就绪：http://$HOST（模型目录 $MODELS_DIR）"
      return 0
    fi
    sleep 1
  done
  echo "[ollama] 启动超时，查看日志：$LOG_FILE" >&2
  return 1
}

cmd_stop() {
  if [ -f "$PID_FILE" ]; then
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      for _ in $(seq 1 20); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.5
      done
    fi
    rm -f "$PID_FILE"
  fi
  echo "[ollama] 已停止"
}

cmd_status() {
  if is_up; then
    echo "[ollama] 运行中：http://$HOST"
    echo "         模型目录：$MODELS_DIR"
  else
    echo "[ollama] 未运行"
    return 1
  fi
}

cmd_pull() {
  local model="${1:-qwen2.5:3b}"
  local bin
  if ! bin="$(find_bin)"; then
    echo "[ollama] 找不到 Ollama 可执行文件" >&2
    return 1
  fi
  cmd_start >/dev/null || return 1
  OLLAMA_MODELS="$MODELS_DIR" OLLAMA_HOST="$HOST" HOME="$HOME_DIR" \
    "$bin" pull "$model"
}

case "${1:-status}" in
  start) cmd_start ;;
  stop) cmd_stop ;;
  status) cmd_status ;;
  pull) shift; cmd_pull "${1:-}" ;;
  *)
    echo "用法：$0 {start|stop|status|pull [模型名]}" >&2
    exit 2
    ;;
esac
