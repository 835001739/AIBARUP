#!/usr/bin/env bash
#
# AIBAR 一键启动脚本
# =================
# 一条命令拉起整套本地环境：ComfyUI（:8188）→ AIBAR（:8099），并把
# AIBAR 的「ComfyUI 桥梁扩展」同步进 ComfyUI，使「在 ComfyUI 中打开并载入
# 工作流 + 提示词」的深链接可用。
#
# 用法：
#   ./scripts/up.sh              等价于 start
#   ./scripts/up.sh start        启动（已运行的服务会跳过，不会重复拉起）
#   ./scripts/up.sh stop         停止
#   ./scripts/up.sh restart      重启
#   （stop / restart 可加 --force：连同「非本脚本启动」的端口占用者一起结束。
#    默认不这么做——端口上可能是别的东西，杀错了麻烦更大。）
#   ./scripts/up.sh status       查看运行状态与地址
#   ./scripts/up.sh open         在浏览器打开 AIBAR
#   ./scripts/up.sh bridge       只同步 ComfyUI 桥梁扩展
#   ./scripts/up.sh logs         跟踪两个服务的日志（Ctrl-C 退出）
#
# 可用环境变量覆盖（一般用不上，脚本默认从 AIBAR 的 .env 读取）：
#   AIBAR_DIR / COMFYUI_DIR / COMFYUI_PYTHON / AIBAR_HOST / AIBAR_PORT /
#   COMFYUI_HOST / COMFYUI_PORT / BOOT_TIMEOUT_COMFYUI / BOOT_TIMEOUT_AIBAR
#
# 说明：ComfyUI 必须用「装了 torch 的那个 Python」启动（本机是 conda 的
# comfyui 环境），用 AIBAR 自己的 .venv 会缺依赖直接退出。脚本的
# COMFYUI_PYTHON 解析顺序就是按这个坑设计的。

set -uo pipefail

# ---------------------------------------------------------------- 基础配置

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AIBAR_DIR="${AIBAR_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
ENV_FILE="$AIBAR_DIR/.env"

# 从 .env 读配置（只取第一个匹配，去掉 export 前缀与引号）
env_get() {
  local key="$1" file="$2" val q='"'
  [ -f "$file" ] || { printf ''; return 0; }
  val="$(sed -n -E "s/^[[:space:]]*(export[[:space:]]+)?${key}[[:space:]]*=[[:space:]]*(.*)\$/\2/p" "$file" | head -1)"
  val="${val%$'\r'}"
  val="${val#\"}"; val="${val%\"}"
  val="${val#\'}"; val="${val%\'}"
  printf '%s' "$val"
}

AIBAR_HOST="${AIBAR_HOST:-$(env_get AIBAR_HOST "$ENV_FILE")}"; AIBAR_HOST="${AIBAR_HOST:-127.0.0.1}"
AIBAR_PORT="${AIBAR_PORT:-$(env_get AIBAR_PORT "$ENV_FILE")}"; AIBAR_PORT="${AIBAR_PORT:-8099}"
COMFYUI_HOST="${COMFYUI_HOST:-$(env_get COMFYUI_HOST "$ENV_FILE")}"; COMFYUI_HOST="${COMFYUI_HOST:-127.0.0.1}"
COMFYUI_PORT="${COMFYUI_PORT:-$(env_get COMFYUI_PORT "$ENV_FILE")}"; COMFYUI_PORT="${COMFYUI_PORT:-8188}"
COMFYUI_DIR="${COMFYUI_DIR:-$(env_get COMFYUI_DIR "$ENV_FILE")}"

# ComfyUI 解释器的三级回退：.env → conda comfyui 环境 → 系统 python3
_CONDA_COMFYUI="/usr/local/Caskroom/miniconda/base/envs/comfyui/bin/python"
COMFYUI_PYTHON="${COMFYUI_PYTHON:-$(env_get COMFYUI_PYTHON "$ENV_FILE")}"
if [ -z "$COMFYUI_PYTHON" ] || [ ! -x "$COMFYUI_PYTHON" ]; then
  if [ -x "$_CONDA_COMFYUI" ]; then
    COMFYUI_PYTHON="$_CONDA_COMFYUI"
  else
    COMFYUI_PYTHON="$(command -v python3 || true)"
  fi
fi

AIBAR_PYTHON="$AIBAR_DIR/.venv/bin/python"
[ -x "$AIBAR_PYTHON" ] || AIBAR_PYTHON="$(command -v python3 || true)"

BRIDGE_SRC="$AIBAR_DIR/comfyui_bridge/AIBAR-Bridge"
BRIDGE_DST="$COMFYUI_DIR/custom_nodes/AIBAR-Bridge"

RUN_DIR="$AIBAR_DIR/tmp"
LOG_DIR="$RUN_DIR"
AIBAR_PID_FILE="$RUN_DIR/aibar.pid"
COMFYUI_PID_FILE="$RUN_DIR/comfyui.pid"
AIBAR_LOG="$LOG_DIR/aibar.log"
COMFYUI_LOG="$LOG_DIR/comfyui.log"

AIBAR_URL="http://$AIBAR_HOST:$AIBAR_PORT"
COMFYUI_URL="http://$COMFYUI_HOST:$COMFYUI_PORT"
AIBAR_HEALTH="$AIBAR_URL/api/status"
COMFYUI_HEALTH="$COMFYUI_URL/system_stats"

BOOT_TIMEOUT_COMFYUI="${BOOT_TIMEOUT_COMFYUI:-240}"
BOOT_TIMEOUT_AIBAR="${BOOT_TIMEOUT_AIBAR:-60}"

# --force：停止时连"不是本脚本启动"的端口占用者一起结束
FORCE=0

# ---------------------------------------------------------------- 输出

if [ -t 1 ]; then
  C_RESET=$'\033[0m'; C_DIM=$'\033[2m'; C_OK=$'\033[32m'
  C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_BOLD=$'\033[1m'
else
  C_RESET=''; C_DIM=''; C_OK=''; C_WARN=''; C_ERR=''; C_BOLD=''
fi

log()   { printf '%s[up]%s %s\n' "$C_OK" "$C_RESET" "$*"; }
info()  { printf '%s[up]%s %s\n' "$C_DIM" "$C_RESET" "$*"; }
warn()  { printf '%s[warn]%s %s\n' "$C_WARN" "$C_RESET" "$*" >&2; }
err()   { printf '%s[error]%s %s\n' "$C_ERR" "$C_RESET" "$*" >&2; }
head1() { printf '\n%s%s%s\n' "$C_BOLD" "$*" "$C_RESET"; }

# ---------------------------------------------------------------- 工具

port_pid() { lsof -ti tcp:"$1" 2>/dev/null | head -1; }

wait_health() {
  # wait_health <名称> <健康地址> <超时秒数>
  local name="$1" url="$2" timeout="$3" i=0
  while [ "$i" -lt "$timeout" ]; do
    if curl -fsS -m 3 "$url" >/dev/null 2>&1; then
      return 0
    fi
    i=$((i + 1))
    # 前 15 秒安静等待，之后每 5 秒报一次进度（ComfyUI 冷启动可能要好几分钟）
    if [ "$i" -ge 15 ] && [ $((i % 5)) -eq 0 ]; then
      info "等待 $name 就绪… ${i}s/${timeout}s"
    fi
    sleep 1
  done
  return 1
}

# ---------------------------------------------------------------- 桥梁扩展

sync_bridge() {
  if [ ! -d "$BRIDGE_SRC" ]; then
    warn "未找到桥梁扩展源目录：${BRIDGE_SRC}（跳过同步）"
    return 0
  fi
  if [ -z "$COMFYUI_DIR" ] || [ ! -d "$COMFYUI_DIR" ]; then
    warn "COMFYUI_DIR 未配置或不存在（${COMFYUI_DIR}），跳过桥梁同步"
    return 0
  fi

  if [ -d "$BRIDGE_DST" ] && diff -rq "$BRIDGE_SRC" "$BRIDGE_DST" >/dev/null 2>&1; then
    log "ComfyUI 桥梁扩展已是最新"
    return 0
  fi

  mkdir -p "$BRIDGE_DST" || { warn "无法创建 $BRIDGE_DST"; return 0; }
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete "$BRIDGE_SRC"/ "$BRIDGE_DST"/
  else
    cp -R "$BRIDGE_SRC"/. "$BRIDGE_DST"/
  fi
  log "已同步 ComfyUI 桥梁扩展 → ${BRIDGE_DST#$COMFYUI_DIR/}"
  if [ -n "$(port_pid "$COMFYUI_PORT")" ]; then
    warn "ComfyUI 正在运行，桥梁扩展需重启 ComfyUI 才会生效：./scripts/up.sh restart"
  fi
}

# ---------------------------------------------------------------- 启动

start_comfyui() {
  head1 "启动 ComfyUI"
  if [ -n "$(port_pid "$COMFYUI_PORT")" ]; then
    log "ComfyUI 已在运行（端口 ${COMFYUI_PORT}），跳过"
    return 0
  fi
  if [ ! -d "$COMFYUI_DIR" ]; then
    err "未找到 ComfyUI 目录：${COMFYUI_DIR}（可在 .env 里设置 COMFYUI_DIR）"
    return 1
  fi
  if [ ! -x "$COMFYUI_PYTHON" ]; then
    err "未找到可用的 ComfyUI 解释器：$COMFYUI_PYTHON"
    return 1
  fi

  sync_bridge
  info "解释器：$COMFYUI_PYTHON"
  info "工作目录：$COMFYUI_DIR"

  # 彻底脱离当前终端：stdin 接 /dev/null（否则 ComfyUI 读 stdin 会卡住），
  # nohup 免疫 SIGHUP，disown 让它不再属于当前 shell 的作业表。
  (
    cd "$COMFYUI_DIR" || exit 1
    nohup "$COMFYUI_PYTHON" main.py \
      --listen "$COMFYUI_HOST" --port "$COMFYUI_PORT" \
      </dev/null >>"$COMFYUI_LOG" 2>&1 &
    echo $! > "$COMFYUI_PID_FILE"
    disown 2>/dev/null || true
  )

  if wait_health "ComfyUI" "$COMFYUI_HEALTH" "$BOOT_TIMEOUT_COMFYUI"; then
    log "ComfyUI 就绪：$COMFYUI_URL"
    return 0
  fi
  err "ComfyUI 启动超时（${BOOT_TIMEOUT_COMFYUI}s）。最近日志："
  tail -n 20 "$COMFYUI_LOG" 2>/dev/null | sed 's/^/    /' >&2
  return 1
}

start_aibar() {
  head1 "启动 AIBAR"
  if [ -n "$(port_pid "$AIBAR_PORT")" ]; then
    log "AIBAR 已在运行（端口 ${AIBAR_PORT}），跳过"
    return 0
  fi
  if [ ! -x "$AIBAR_PYTHON" ]; then
    err "未找到可用的 Python 解释器（期望 $AIBAR_DIR/.venv/bin/python）"
    return 1
  fi

  mkdir -p "$RUN_DIR"
  info "解释器：$AIBAR_PYTHON"
  info "工作目录：$AIBAR_DIR"

  (
    cd "$AIBAR_DIR" || exit 1
    nohup "$AIBAR_PYTHON" app.py </dev/null >>"$AIBAR_LOG" 2>&1 &
    echo $! > "$AIBAR_PID_FILE"
    disown 2>/dev/null || true
  )

  if wait_health "AIBAR" "$AIBAR_HEALTH" "$BOOT_TIMEOUT_AIBAR"; then
    log "AIBAR 就绪：$AIBAR_URL"
    return 0
  fi
  err "AIBAR 启动超时（${BOOT_TIMEOUT_AIBAR}s）。最近日志："
  tail -n 20 "$AIBAR_LOG" 2>/dev/null | sed 's/^/    /' >&2
  return 1
}

do_start() {
  mkdir -p "$RUN_DIR"
  # ComfyUI 先起：它冷启动慢，且 AIBAR 启动后会立即探测 ComfyUI 状态
  start_comfyui
  local comfy_rc=$?
  start_aibar
  local aibar_rc=$?

  head1 "结果"
  printf '  AIBAR    %s\n' "$AIBAR_URL"
  printf '  ComfyUI  %s\n' "$COMFYUI_URL"
  if [ "$comfy_rc" -eq 0 ] && [ "$aibar_rc" -eq 0 ]; then
    log "两个服务都已就绪。日志：$AIBAR_LOG / $COMFYUI_LOG"
    return 0
  fi
  err "部分服务未就绪，看日志排查：$AIBAR_LOG / $COMFYUI_LOG"
  return 1
}

# ---------------------------------------------------------------- 停止

kill_pid() {
  # kill_pid <名称> <pid>：先 TERM，最多等 10 秒，仍不死再 KILL
  local name="$1" pid="$2" i
  kill "$pid" 2>/dev/null || return 0
  for i in $(seq 1 10); do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 1
  done
  warn "$name 未响应退出信号，强制结束 PID $pid"
  kill -9 "$pid" 2>/dev/null || true
  sleep 1
}

stop_one() {
  # stop_one <名称> <pid 文件> <端口>
  local name="$1" pidfile="$2" port="$3" pid="" other
  if [ -f "$pidfile" ]; then
    pid="$(cat "$pidfile" 2>/dev/null || true)"
  fi
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    kill_pid "$name" "$pid"
    log "$name 已停止（PID ${pid}）"
  else
    info "$name 未在运行"
  fi
  rm -f "$pidfile"

  # 端口仍被占用，说明进程不是本脚本拉的。默认只提醒——端口上的可能是别的
  # 东西，贸然杀掉风险太大；加 --force 才一并清理。
  other="$(port_pid "$port")"
  if [ -n "$other" ]; then
    if [ "$FORCE" = "1" ]; then
      warn "$name 端口 $port 被进程 $other 占用（--force），正在结束"
      kill_pid "$name" "$other"
    else
      warn "$name 端口 $port 仍被进程 $other 占用（非本脚本启动，未处理；可加 --force 强制结束）"
    fi
  fi
}

do_stop() {
  head1 "停止服务"
  stop_one "AIBAR" "$AIBAR_PID_FILE" "$AIBAR_PORT"
  stop_one "ComfyUI" "$COMFYUI_PID_FILE" "$COMFYUI_PORT"
}

# ---------------------------------------------------------------- 状态

status_one() {
  # status_one <名称> <健康地址> <站点地址> <pid 文件> <端口>
  local name="$1" url="$2" site="$3" pidfile="$4" port="$5"
  local pid="" mark state
  [ -f "$pidfile" ] && pid="$(cat "$pidfile" 2>/dev/null || true)"
  if curl -fsS -m 3 "$url" >/dev/null 2>&1; then
    mark="${C_OK}●${C_RESET}"; state="运行中"
  else
    mark="${C_DIM}○${C_RESET}"; state="未运行"
  fi
  printf '  %s %-8s %-10s %s\n' "$mark" "$name" "$state" "$site"
  if [ -n "$pid" ]; then
    printf '      %sPID %s（记录于 %s）%s\n' "$C_DIM" "$pid" "$pidfile" "$C_RESET"
  fi
  if [ -n "$(port_pid "$port")" ] && [ "$state" = "未运行" ]; then
    printf '      %s端口 %s 被其它进程占用且健康检查未通过%s\n' "$C_WARN" "$port" "$C_RESET"
  fi
}

do_status() {
  head1 "运行状态"
  status_one "AIBAR" "$AIBAR_HEALTH" "$AIBAR_URL" "$AIBAR_PID_FILE" "$AIBAR_PORT"
  status_one "ComfyUI" "$COMFYUI_HEALTH" "$COMFYUI_URL" "$COMFYUI_PID_FILE" "$COMFYUI_PORT"

  printf '\n  %sComfyUI 桥梁扩展%s：' "$C_BOLD" "$C_RESET"
  if [ -f "$BRIDGE_DST/js/aibar_bridge.js" ]; then
    printf '%s已安装%s\n' "$C_OK" "$C_RESET"
  else
    printf '%s未安装%s（运行 ./scripts/up.sh bridge 安装）\n' "$C_WARN" "$C_RESET"
  fi
  printf '  %s日志%s：%s\n' "$C_BOLD" "$C_RESET" "$AIBAR_LOG"
  printf '       %s\n' "$COMFYUI_LOG"
}

# ---------------------------------------------------------------- 入口

usage() {
  sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
}

main() {
  local cmd="${1:-start}"
  # 允许「命令在前、--force 在后」和「--force 在前」两种写法
  case "$cmd" in
    -f|--force)       FORCE=1; cmd="${2:-start}" ;;
  esac
  case "${2:-}" in
    -f|--force)       FORCE=1 ;;
  esac
  case "$cmd" in
    start)            do_start ;;
    stop)             do_stop ;;
    restart)          do_stop; do_start ;;
    status|st)        do_status ;;
    bridge)           head1 "同步 ComfyUI 桥梁扩展"; sync_bridge ;;
    open)
      log "打开 $AIBAR_URL"
      open "$AIBAR_URL" 2>/dev/null || true
      ;;
    logs)
      info "跟踪日志（Ctrl-C 退出）：$AIBAR_LOG / $COMFYUI_LOG"
      tail -f "$AIBAR_LOG" "$COMFYUI_LOG" 2>/dev/null
      ;;
    -h|--help|help)   usage ;;
    *)                err "未知命令：$cmd"; usage; exit 2 ;;
  esac
}

main "$@"
