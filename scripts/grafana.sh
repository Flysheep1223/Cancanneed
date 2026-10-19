#!/bin/bash
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="13.2.4"
ARCHIVE="grafana_13.2.4_38252365904_darwin_arm64.tar.gz"
SHA256="a910966466359212f1d875748a72a4638c7d3724448d0c369210b31d763b165d"
DOWNLOAD_URL="https://storage.googleapis.com/grafana-downloads/grafana/release/$VERSION/$ARCHIVE"
FALLBACK_URL="https://dl.grafana.com/grafana/release/$VERSION/$ARCHIVE"
INSTALL="$ROOT/.runtime/grafana-$VERSION"
PUBLIC="$ROOT/.runtime/grafana-public-$VERSION"
PIDFILE="$ROOT/.runtime/grafana.pid"

fail() { printf '%s\n' "$*" >&2; exit 1; }

load_env() {
  [[ -f "$ROOT/.env" ]] || fail "缺少 .env，请先执行：$0 install"
  set -a
  # .env 是由本项目管理员维护的 shell 配置文件。
  source "$ROOT/.env"
  set +a
  export GF_SERVER_HTTP_ADDR="${GF_SERVER_HTTP_ADDR:-127.0.0.1}"
  export GF_SERVER_HTTP_PORT="${GF_SERVER_HTTP_PORT:-3000}"
  export GF_SERVER_STATIC_ROOT_PATH="$PUBLIC"
  export GF_PATHS_DATA="$ROOT/data/grafana"
  export GF_PATHS_LOGS="$ROOT/logs"
  export GF_PATHS_PLUGINS="$ROOT/data/plugins"
  export GF_PATHS_PROVISIONING="$ROOT/config/provisioning"
  HEALTH_URL="http://127.0.0.1:$GF_SERVER_HTTP_PORT/api/health"
}

install_grafana() {
  [[ "$(uname -s)/$(uname -m)" == "Darwin/arm64" ]] ||
    fail "本部署脚本适用于 macOS Apple Silicon。"
  mkdir -p "$ROOT/.runtime/downloads" "$ROOT/data/grafana" \
    "$ROOT/data/plugins" "$ROOT/logs" \
    "$ROOT/config/provisioning/"{datasources,dashboards,alerting,plugins,access-control}
  if [[ ! -f "$ROOT/.env" ]]; then
    local password
    password="$(openssl rand -hex 18)"
    printf 'GF_SECURITY_ADMIN_USER=admin\nGF_SECURITY_ADMIN_PASSWORD=%s\nGF_SERVER_HTTP_ADDR=127.0.0.1\nGF_SERVER_HTTP_PORT=3000\n' \
      "$password" > "$ROOT/.env"
    chmod 600 "$ROOT/.env"
    printf '已生成配置：%s/.env（默认免登录访问）\n' "$ROOT"
  fi
  if [[ -f "$INSTALL/.installed" ]]; then
    return
  fi
  local archive_path="$ROOT/.runtime/downloads/$ARCHIVE"
  if [[ ! -f "$archive_path" ]]; then
    printf '正在下载 Grafana %s（macOS ARM64）…\n' "$VERSION"
    if ! curl -fLsS --retry 2 --connect-timeout 15 --max-time 1800 \
      "$DOWNLOAD_URL" -o "$archive_path.partial"; then
      curl -fLsS --retry 2 --connect-timeout 15 --max-time 1800 \
        "$FALLBACK_URL" -o "$archive_path.partial"
    fi
    mv "$archive_path.partial" "$archive_path"
  fi
  [[ "$(shasum -a 256 "$archive_path" | awk '{print $1}')" == "$SHA256" ]] ||
    fail "SHA256 校验失败，请删除 $archive_path 后重试。"
  printf 'SHA256 校验通过，正在解压…\n'
  mkdir -p "$INSTALL"
  tar -xzf "$archive_path" -C "$INSTALL" --strip-components=1
  [[ -x "$INSTALL/bin/grafana" ]] || fail "安装包未包含 Grafana 可执行文件。"
  touch "$INSTALL/.installed"
  printf 'Grafana %s 安装完成。\n' "$VERSION"
}

prepare_ui() {
  local entry name
  mkdir -p "$PUBLIC/views"
  # 复用官方静态资源，仅生成包含免登录跳转的首页模板。
  for entry in "$INSTALL/public/"*; do
    name="${entry##*/}"
    [[ "$name" == "views" ]] && continue
    ln -sfn "$entry" "$PUBLIC/$name"
  done
  for entry in "$INSTALL/public/views/"*; do
    name="${entry##*/}"
    [[ "$name" == "index.html" ]] && continue
    ln -sfn "$entry" "$PUBLIC/views/$name"
  done
  awk '
    FNR == NR { snippet = snippet $0 ORS; next }
    { print }
    /<head>/ { printf "%s", snippet; inserted = 1 }
    END { if (!inserted) exit 1 }
  ' "$ROOT/config/grafana-no-login.html" "$INSTALL/public/views/index.html" \
    > "$PUBLIC/views/index.html.tmp" ||
    fail "无法生成 Grafana 首页模板，请检查官方模板中的 <head> 标记。"
  mv "$PUBLIC/views/index.html.tmp" "$PUBLIC/views/index.html"
}

running_pid() {
  [[ -f "$PIDFILE" ]] || return 1
  local pid command
  pid="$(cat "$PIDFILE")"
  [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
  command="$(ps -p "$pid" -o command= 2>/dev/null)" || return 1
  # 验证进程归属，避免 PID 被复用后误操作其他程序。
  [[ "$command" == "$INSTALL/bin/grafana server "* ||
     "$command" == "/bin/bash $ROOT/scripts/grafana.sh run" ]] || return 1
  printf '%s\n' "$pid"
}

wait_healthy() {
  local attempt
  for ((attempt = 0; attempt < 45; attempt++)); do
    kill -0 "$(cat "$PIDFILE")" 2>/dev/null ||
      fail "Grafana 进程已退出，请查看 $ROOT/logs/ 下的日志。"
    if curl -fsS --connect-timeout 1 --max-time 2 "$HEALTH_URL" >/dev/null 2>&1; then
      printf 'Grafana 已就绪：http://localhost:%s\n打开即可使用（默认免登录）。\n' \
        "$GF_SERVER_HTTP_PORT"
      return
    fi
    sleep 1
  done
  fail "启动后健康检查未通过，请查看 $ROOT/logs/ 下的日志。"
}

start_grafana() {
  install_grafana
  load_env
  # Provision a public template only when there is no private batch dashboard.
  mkdir -p "$ROOT/.runtime/ark-dashboards"
  if [[ ! -f "$ROOT/.runtime/ark-dashboards/ark-model-tpm.json" ]]; then
    cp "$ROOT/data_ark/grafana/dashboards/ark-model-tpm.json" \
      "$ROOT/.runtime/ark-dashboards/ark-model-tpm.json"
  fi
  if running_pid >/dev/null; then
    wait_healthy
    return
  fi
  if lsof -nP -iTCP:"$GF_SERVER_HTTP_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    fail "端口 $GF_SERVER_HTTP_PORT 已被占用，请修改 .env 中的 GF_SERVER_HTTP_PORT。"
  fi
  nohup /bin/bash "$ROOT/scripts/grafana.sh" run \
    >/dev/null 2>"$ROOT/logs/launcher.log" </dev/null &
  printf '%s\n' "$!" > "$PIDFILE"
  wait_healthy
}

stop_grafana() {
  local server_pid attempt
  if server_pid="$(running_pid)"; then
    kill "$server_pid"
    for ((attempt = 0; attempt < 15; attempt++)); do
      if ! running_pid >/dev/null; then
        break
      fi
      sleep 1
    done
    if running_pid >/dev/null; then
      fail "进程 $server_pid 仍在退出，请稍后再次检查。"
    fi
    rm -f "$PIDFILE"
    printf 'Grafana 已停止，数据保留在 %s/data/。\n' "$ROOT"
  else
    printf 'Grafana 未运行。\n'
  fi
}

case "${1:-status}" in
  install) install_grafana ;;
  start) start_grafana ;;
  stop) stop_grafana ;;
  restart) stop_grafana; start_grafana ;;
  status)
    load_env
    server_pid="$(running_pid)" ||
      fail "Grafana 未运行，请执行：$0 start"
    printf 'Grafana 正在运行，PID：%s\n' "$server_pid"
    curl -fsS --max-time 5 "$HEALTH_URL" || fail "Grafana 健康检查失败。"
    printf '\n访问地址：http://localhost:%s\n' "$GF_SERVER_HTTP_PORT"
    ;;
  logs)
    mkdir -p "$ROOT/logs"
    touch "$ROOT/logs/grafana.log"
    exec tail -n 60 -F "$ROOT/logs/grafana.log"
    ;;
  run)
    load_env
    prepare_ui
    cd "$INSTALL"
    exec "$INSTALL/bin/grafana" server --homepath "$INSTALL" \
      --config "$ROOT/config/grafana.ini"
    ;;
  *) fail "用法：$0 {install|start|stop|restart|status|logs}" ;;
esac
