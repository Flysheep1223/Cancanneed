#!/bin/bash
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIDFILE="$ROOT/.runtime/demo.pid"
LOGFILE="$ROOT/logs/demo.log"

fail() { printf '%s\n' "$*" >&2; exit 1; }

running_pid() {
  [[ -f "$PIDFILE" ]] || return 1
  local pid command
  pid="$(cat "$PIDFILE")"
  [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
  command="$(ps -p "$pid" -o command= 2>/dev/null)" || return 1
  [[ "$command" == "/bin/bash $ROOT/scripts/demo.sh run" ]] || return 1
  printf '%s\n' "$pid"
}

write_samples() {
  "$ROOT/scripts/postgres.sh" psql -q -o /dev/null \
    -v "lookback_minutes=$1" -f "$ROOT/config/postgres/demo-samples.sql"
}

seed() {
  "$ROOT/scripts/postgres.sh" psql -q -f "$ROOT/config/postgres/demo-schema.sql"
  write_samples 120
  printf '已补齐最近两小时模拟数据（重复时间点不会重复写入）。\n'
}

stop() {
  local pid attempt
  if pid="$(running_pid)"; then
    kill "$pid"
    for ((attempt = 0; attempt < 12; attempt++)); do
      if ! running_pid >/dev/null; then
        break
      fi
      sleep 1
    done
    running_pid >/dev/null && fail "模拟采集器仍在退出，请稍后检查。"
    rm -f "$PIDFILE"
    printf '模拟采集器已停止，历史数据保留。\n'
  else
    printf '模拟采集器未运行。\n'
  fi
}

status() {
  local pid
  if pid="$(running_pid)"; then
    printf '模拟采集器运行中，PID：%s；每 5 秒生成 3 个请求样本。\n' "$pid"
  else
    printf '模拟采集器未运行。\n'
  fi
  "$ROOT/scripts/postgres.sh" reader -c "
    SELECT labels->>'model' AS model,
           count(*) FILTER (WHERE metric_name = 'ttft_ms') AS requests,
           count(*) AS metric_rows,
           min(event_time) AS first_sample,
           max(event_time) AS latest_sample
    FROM public.metric_samples
    WHERE labels->>'source' = 'grafana_demo_v1'
    GROUP BY 1 ORDER BY 1;"
}

case "${1:-status}" in
  seed) seed ;;
  start)
    if running_pid >/dev/null; then
      status
      exit 0
    fi
    mkdir -p "$ROOT/.runtime" "$ROOT/logs"
    seed
    nohup /bin/bash "$ROOT/scripts/demo.sh" run \
      >>"$LOGFILE" 2>&1 </dev/null &
    printf '%s\n' "$!" > "$PIDFILE"
    sleep 1
    running_pid >/dev/null || fail "启动失败，请查看 logs/demo.log。"
    printf '模拟采集器已在后台启动，每 5 秒写入一批；保留最近 6 小时。\n'
    ;;
  stop) stop ;;
  status) status ;;
  clean)
    stop
    "$ROOT/scripts/postgres.sh" psql -c "
      DELETE FROM public.metric_samples
      WHERE labels->>'source' = 'grafana_demo_v1';"
    printf '已清理 grafana_demo_v1 模拟数据。\n'
    ;;
  run)
    trap 'exit 0' TERM INT
    trap 'if [[ -f "$PIDFILE" && "$(cat "$PIDFILE")" == "$$" ]]; then rm -f "$PIDFILE"; fi' EXIT
    had_error=0
    while true; do
      if write_samples 0 2>"$ROOT/.runtime/demo-last-error.log"; then
        if [[ "$had_error" == 1 ]]; then
          printf '%s 数据库写入恢复。\n' "$(date -u +%FT%TZ)"
        fi
        had_error=0
      else
        if [[ "$had_error" == 0 ]]; then
          printf '%s 写入失败，将每 5 秒重试；错误见 .runtime/demo-last-error.log。\n' \
            "$(date -u +%FT%TZ)"
        fi
        had_error=1
      fi
      sleep 5
    done
    ;;
  *) fail "用法：$0 {start|stop|status|seed|clean}" ;;
esac
