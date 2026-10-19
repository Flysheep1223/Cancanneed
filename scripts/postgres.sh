#!/bin/bash
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL="$ROOT/.runtime/postgresql-18"
BIN="$INSTALL/bin"
DATA="$ROOT/data/postgres"
MOUNT="$ROOT/.runtime/postgres-mount"
RUNTIME_CONFIG="$ROOT/.runtime/postgresql.conf"
ARCHIVE="$ROOT/.runtime/downloads/Postgres-2.9.6-18.dmg"
DOWNLOAD_URL="https://github.com/PostgresApp/PostgresApp/releases/download/v2.9.6/Postgres-2.9.6-18.dmg"
SHA256="9fc7d0dc08cf46dfd94bb32cbaaad81b41b37847a42d6dcb2f9fbd292813defb"

fail() { printf '%s\n' "$*" >&2; exit 1; }

append_default() {
  if ! /usr/bin/grep -q "^$1=" "$ROOT/.env"; then
    printf '\n%s=%s\n' "$1" "$2" >> "$ROOT/.env"
  fi
}

ensure_env() {
  mkdir -p "$ROOT/.runtime/downloads" "$ROOT/logs/postgres" "$ROOT/data"
  touch "$ROOT/.env"
  chmod 600 "$ROOT/.env"
  append_default POSTGRES_HOST 127.0.0.1
  append_default POSTGRES_PORT 5432
  append_default POSTGRES_DB metrics
  append_default POSTGRES_USER metrics_writer
  append_default POSTGRES_PASSWORD "$(openssl rand -hex 18)"
  append_default POSTGRES_ADMIN_PASSWORD "$(openssl rand -hex 18)"
  append_default GRAFANA_POSTGRES_USER grafana_reader
  append_default GRAFANA_POSTGRES_PASSWORD "$(openssl rand -hex 18)"
}

load_env() {
  [[ -f "$ROOT/.env" ]] || fail "请先执行：$0 setup"
  set -a
  source "$ROOT/.env"
  set +a
  : "${POSTGRES_HOST:?}" "${POSTGRES_PORT:?}" "${POSTGRES_DB:?}"
  : "${POSTGRES_USER:?}" "${POSTGRES_PASSWORD:?}" "${POSTGRES_ADMIN_PASSWORD:?}"
  : "${GRAFANA_POSTGRES_USER:?}" "${GRAFANA_POSTGRES_PASSWORD:?}"
  [[ "$POSTGRES_HOST" == "127.0.0.1" ]] || fail "本地部署的 POSTGRES_HOST 必须为 127.0.0.1。"
  [[ "$POSTGRES_PORT" =~ ^[0-9]+$ ]] &&
    ((10#$POSTGRES_PORT > 0 && 10#$POSTGRES_PORT < 65536)) || fail "无效的数据库端口。"
  export PGCONNECT_TIMEOUT=5 PGSSLMODE=disable
}

install_postgres() {
  [[ "$(uname -s)/$(uname -m)" == "Darwin/arm64" ]] ||
    fail "本部署脚本适用于 macOS Apple Silicon。"
  [[ ! -f "$INSTALL/.installed" ]] || return 0
  mkdir -p "$ROOT/.runtime/downloads" "$MOUNT"
  if [[ ! -f "$ARCHIVE" ]]; then
    printf '正在下载 PostgreSQL 18.6（Postgres.app 二进制包）…\n'
    curl -fLsS --retry 2 --connect-timeout 15 --max-time 900 \
      "$DOWNLOAD_URL" -o "$ARCHIVE.partial"
    mv "$ARCHIVE.partial" "$ARCHIVE"
  fi
  [[ "$(shasum -a 256 "$ARCHIVE" | awk '{print $1}')" == "$SHA256" ]] ||
    fail "安装包 SHA256 校验失败，请删除 $ARCHIVE 后重试。"
  hdiutil attach -quiet -readonly -nobrowse -mountpoint "$MOUNT" "$ARCHIVE"
  trap 'hdiutil detach -quiet "$MOUNT" >/dev/null 2>&1 || true' EXIT
  ditto "$MOUNT/Postgres.app/Contents/Versions/18" "$INSTALL"
  hdiutil detach -quiet "$MOUNT"
  trap - EXIT
  "$BIN/postgres" --version
  "$BIN/psql" --version
  touch "$INSTALL/.installed"
}

init_cluster() {
  [[ ! -f "$DATA/PG_VERSION" ]] || return 0
  printf '%s\n' "$POSTGRES_ADMIN_PASSWORD" > "$ROOT/.runtime/postgres-init-password"
  trap 'rm -f "$ROOT/.runtime/postgres-init-password"' EXIT
  "$BIN/initdb" -D "$DATA" -U postgres --encoding=UTF8 --locale=C \
    --auth-local=scram-sha-256 --auth-host=scram-sha-256 \
    --pwfile="$ROOT/.runtime/postgres-init-password" \
    > "$ROOT/logs/postgres-init.log" 2>&1 ||
    fail "数据库初始化失败，请查看 logs/postgres-init.log。"
  rm -f "$ROOT/.runtime/postgres-init-password"
  trap - EXIT
}

write_config() {
  # PostgreSQL 配置文件的单引号需要写成两个单引号。
  local config_root
  config_root="$(printf '%s' "$ROOT" | sed "s/'/''/g")"
  cat > "$RUNTIME_CONFIG" <<EOF
include = '$config_root/config/postgresql.conf'
data_directory = '$config_root/data/postgres'
hba_file = '$config_root/data/postgres/pg_hba.conf'
ident_file = '$config_root/data/postgres/pg_ident.conf'
port = $POSTGRES_PORT
log_directory = '$config_root/logs/postgres'
EOF
}

is_running() {
  [[ -x "$BIN/pg_ctl" && -f "$DATA/PG_VERSION" ]] &&
    "$BIN/pg_ctl" -D "$DATA" status >/dev/null 2>&1
}

admin_sql() {
  local database="$1"
  shift
  PGPASSWORD="$POSTGRES_ADMIN_PASSWORD" "$BIN/psql" -X -q \
    -v ON_ERROR_STOP=1 -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" \
    -U postgres -d "$database" "$@"
}

start_postgres() {
  ensure_env
  load_env
  install_postgres
  init_cluster
  write_config
  if ! is_running; then
    if lsof -nP -iTCP:"$POSTGRES_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
      fail "端口 $POSTGRES_PORT 已被占用，请修改 .env 的 POSTGRES_PORT。"
    fi
    # pg_ctl 会将数据库作为后台进程启动。
    "$BIN/pg_ctl" -D "$DATA" -l "$ROOT/logs/postgres-startup.log" \
      -o "-c config_file=\"$RUNTIME_CONFIG\"" -w -t 45 start
  fi
  admin_sql postgres -Atc 'SELECT 1' >/dev/null
  printf 'PostgreSQL 已就绪：%s:%s\n' "$POSTGRES_HOST" "$POSTGRES_PORT"
}

bootstrap_database() {
  admin_sql postgres -f "$ROOT/config/postgres/roles.sql"
  admin_sql "$POSTGRES_DB" -f "$ROOT/config/postgres/schema.sql"
  touch "$DATA/.project-initialized"
  printf '业务库 %s 已配置，写入账号 %s，Grafana 只读账号 %s。\n' \
    "$POSTGRES_DB" "$POSTGRES_USER" "$GRAFANA_POSTGRES_USER"
}

stop_postgres() {
  if is_running; then
    "$BIN/pg_ctl" -D "$DATA" -m fast -w -t 45 stop
  else
    printf 'PostgreSQL 未运行。\n'
  fi
}

action="${1:-status}"
[[ $# -eq 0 ]] || shift
case "$action" in
  setup) start_postgres; bootstrap_database ;;
  start)
    start_postgres
    [[ -f "$DATA/.project-initialized" ]] || bootstrap_database
    ;;
  stop) stop_postgres ;;
  restart)
    stop_postgres
    start_postgres
    [[ -f "$DATA/.project-initialized" ]] || bootstrap_database
    ;;
  status)
    load_env
    is_running || fail "PostgreSQL 未运行，请执行：$0 start"
    "$BIN/pg_ctl" -D "$DATA" status
    admin_sql "$POSTGRES_DB" -c 'SELECT version(), current_database(), now();'
    ;;
  psql|reader|admin)
    load_env
    case "$action" in
      psql) db_user="$POSTGRES_USER"; db_password="$POSTGRES_PASSWORD" ;;
      reader) db_user="$GRAFANA_POSTGRES_USER"; db_password="$GRAFANA_POSTGRES_PASSWORD" ;;
      admin) db_user=postgres; db_password="$POSTGRES_ADMIN_PASSWORD" ;;
    esac
    export PGPASSWORD="$db_password"
    exec "$BIN/psql" -X -v ON_ERROR_STOP=1 -h "$POSTGRES_HOST" \
      -p "$POSTGRES_PORT" -U "$db_user" -d "$POSTGRES_DB" "$@"
    ;;
  backup)
    load_env
    mkdir -p "$ROOT/data/backups"
    backup_file="$ROOT/data/backups/$POSTGRES_DB-$(date +%Y%m%d-%H%M%S).dump"
    PGPASSWORD="$POSTGRES_ADMIN_PASSWORD" "$BIN/pg_dump" \
      -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U postgres \
      -d "$POSTGRES_DB" -Fc -f "$backup_file"
    printf '已备份：%s\n' "$backup_file"
    ;;
  *) fail "用法：$0 {setup|start|stop|restart|status|psql|reader|admin|backup}" ;;
esac
