#!/usr/bin/env bash
# 公共库：日志、配置加载、校验、备份、执行封装。
set -Eeuo pipefail

RRB_PROJECT_DIR="${RRB_PROJECT_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}"
RRB_CONFIG_FILE="${RRB_CONFIG_FILE:-$RRB_PROJECT_DIR/config.env}"
RRB_DRY_RUN="${RRB_DRY_RUN:-false}"
RRB_YES="${RRB_YES:-false}"
RRB_VERBOSE="${RRB_VERBOSE:-false}"
export RRB_PROJECT_DIR RRB_CONFIG_FILE RRB_DRY_RUN RRB_YES RRB_VERBOSE
SCRIPT_DIR="$RRB_PROJECT_DIR/scripts"
LOG_FILE="/var/log/reality-relay-bootstrap.log"
BACKUP_ROOT="/root/reality-relay-bootstrap-backups"
PHASE_NAME="${PHASE_NAME:-manual}"
BACKUP_DIR="${BACKUP_DIR:-}"

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  LOG_FILE="/tmp/reality-relay-bootstrap.log"
  BACKUP_ROOT="/tmp/reality-relay-bootstrap-backups"
fi

trap 'rc=$?; echo "[ERROR] line=${LINENO} cmd=${BASH_COMMAND} rc=${rc}" >&2; exit $rc' ERR

_ts() { date '+%F %T'; }
log() {
  local line
  line="[$(_ts)] $*"
  printf '%s\n' "$line" >>"$LOG_FILE"
  if [[ "$RRB_VERBOSE" == "true" ]]; then
    printf '%s\n' "$line" >&2
  fi
}
info() { log "INFO: $*"; }
warn() {
  local line
  line="[$(_ts)] WARN: $*"
  printf '%s\n' "$line" >>"$LOG_FILE"
  printf '%s\n' "$line" >&2
}
die() {
  local line
  line="[$(_ts)] ERROR: $*"
  printf '%s\n' "$line" >>"$LOG_FILE"
  printf '%s\n' "$line" >&2
  exit 1
}

is_dry_run() { [[ "$RRB_DRY_RUN" == "true" ]]; }
require_root() { [[ $EUID -eq 0 ]] || die "必须以 root 运行：sudo bash bootstrap.sh ..."; }

run() {
  if is_dry_run; then
    log "DRY-RUN: $*"
  else
    log "+ $*"
    if [[ "$RRB_VERBOSE" == "true" ]]; then
      "$@" 2>&1 | tee -a "$LOG_FILE"
    else
      "$@" >>"$LOG_FILE" 2>&1
    fi
  fi
}

have() { command -v "$1" >/dev/null 2>&1; }

source "$SCRIPT_DIR/00-platform.sh"

user_home() {
  local user="$1" entry home
  entry="$(getent passwd "$user" || true)"
  if [[ -n "$entry" ]]; then
    local _name _passwd _uid _gid _gecos _shell
    IFS=: read -r _name _passwd _uid _gid _gecos home _shell <<<"$entry"
    [[ -n "$home" ]] || die "无法获取 $user 的 home"
    printf '%s\n' "$home"
    return 0
  fi

  if is_dry_run; then
    printf '/home/%s\n' "$user"
    return 0
  fi

  die "用户不存在，无法获取 home：$user"
}

apt_get() (
  # Isolate cleanup from callers' rollback traps. Retry only lock contention;
  # failed downloads, package errors and dpkg failures must remain visible.
  local lock_timeout="${RRB_APT_LOCK_TIMEOUT:-300}" deadline delay attempt_log waiting=false
  [[ "$lock_timeout" =~ ^[0-9]{1,5}$ ]] || die "RRB_APT_LOCK_TIMEOUT 必须是 0–99999 秒。"
  deadline=$((SECONDS + 10#$lock_timeout))
  attempt_log="$(mktemp)" || die "无法创建 APT 临时日志。"
  trap 'rm -f -- "$attempt_log"' EXIT
  while true; do
    # Native lock waiting is unavailable in older supported APT versions and
    # does not cover apt lists locks, so use one bounded policy for both phases.
    # DEBIAN_FRONTEND only controls debconf, not dpkg's conffile questions.
    # Keep existing configuration and never consume installer/SSH terminal input.
    # Stream to the persistent log as well, so a running APT command is observable.
    if LC_ALL=C DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=0 \
      -o Dpkg::Options::=--force-confold "$@" </dev/null 2>&1 \
      | tee "$attempt_log" >>"$LOG_FILE"; then
      return 0
    fi
    if grep -Eq '^E: Could not get lock .*([Ii]t is held by process|Resource temporarily unavailable)' "$attempt_log"; then
      if (( SECONDS >= deadline )); then
        tail -n 8 "$attempt_log" >&2 || true
        die "APT 锁等待超时（${lock_timeout} 秒）；请等待其他包管理进程结束后重试。完整日志见 $LOG_FILE"
      fi
      if [[ "$waiting" == false ]]; then
        warn "APT 正被其他包管理进程占用，等待释放后继续（最多 ${lock_timeout} 秒）。"
        waiting=true
      fi
      delay=$((deadline - SECONDS))
      (( delay <= 5 )) || delay=5
      if (( delay > 0 )); then
        sleep "$delay" || exit $?
      fi
    else
      tail -n 12 "$attempt_log" >&2 || true
      die "apt-get $* 失败；完整日志见 $LOG_FILE"
    fi
  done
)

apt_install() {
  require_root
  export DEBIAN_FRONTEND=noninteractive
  if is_dry_run; then
    log "DRY-RUN: apt-get update && apt-get install -y $*"
  else
    info "安装/确认依赖：$*"
    apt_get update -y || exit $?
    apt_get install -y "$@" || exit $?
  fi
}

load_config() {
  [[ -f "$RRB_CONFIG_FILE" ]] || die "找不到配置文件：$RRB_CONFIG_FILE；请先 cp config.example.env config.env"
  set -a
  # shellcheck disable=SC1090
  source "$RRB_CONFIG_FILE"
  set +a

  : "${SERVER_ALIAS:=my-vps}"
  : "${SERVER_IP:=}"
  : "${SERVER_IP_IPV4:=}"
  : "${SERVER_IP_IPV6:=}"
  if [[ -n "$SERVER_IP" ]]; then
    if [[ "$SERVER_IP" == *:* && -z "$SERVER_IP_IPV6" ]]; then
      SERVER_IP_IPV6="$SERVER_IP"
    elif [[ "$SERVER_IP" != *:* && -z "$SERVER_IP_IPV4" ]]; then
      SERVER_IP_IPV4="$SERVER_IP"
    fi
  fi
  if [[ -z "$SERVER_IP" ]]; then
    if [[ -n "$SERVER_IP_IPV4" ]]; then
      SERVER_IP="$SERVER_IP_IPV4"
    elif [[ -n "$SERVER_IP_IPV6" ]]; then
      SERVER_IP="$SERVER_IP_IPV6"
    fi
  fi
  export SERVER_IP SERVER_IP_IPV4 SERVER_IP_IPV6
  : "${SSH_PORT:=22}"
  : "${INITIAL_USER:=root}"
  : "${ADMIN_USER:=root}"
  if [[ "$ADMIN_USER" != "root" ]]; then
    warn "ADMIN_USER=$ADMIN_USER 已废弃；当前统一使用 root，不再创建独立管理员用户。"
    ADMIN_USER="root"
  fi
  : "${ADMIN_PUBKEY:=}"
  : "${ADMIN_PUBKEYS:=}"
  if [[ -n "$ADMIN_PUBKEYS" ]]; then
    if [[ -n "$ADMIN_PUBKEY" ]]; then
      ADMIN_PUBKEY+=$'\n'
      ADMIN_PUBKEY+="$ADMIN_PUBKEYS"
    else
      ADMIN_PUBKEY="$ADMIN_PUBKEYS"
    fi
  fi
  export ADMIN_USER ADMIN_PUBKEY ADMIN_PUBKEYS
  : "${ENABLE_SFTP_USER:=false}"
  : "${SFTP_USER:=sftpuser}"
  : "${SFTP_PUBKEY:=}"
  : "${USE_233BOY_INSTALLER:=false}"
  : "${INSTALL_SINGBOX_METHOD:=auto}"
  : "${PROXY_PROTOCOL:=vless-reality}"
  : "${PROXY_IP_VERSION:=ipv4}"
  : "${MODE_443:=direct}"
  if [[ "$MODE_443" != "direct" ]]; then
    warn "MODE_443=$MODE_443 已废弃；当前固定按 direct 处理，不再生成域名/IP 分流规则。"
    MODE_443="direct"
  fi
  : "${DIRECT_PORT:=443}"
  : "${HOME_PORT_START:=51043}"
  : "${HOME_PORT_END:=65535}"
  : "${ENABLE_UFW:=true}"
  : "${ENABLE_FAIL2BAN:=true}"
  : "${FAIL2BAN_SSH_LOG:=/var/log/messages}"
  : "${ENABLE_IPV6_LISTEN:=false}"
  : "${CSV_PATH:=./home-proxies.csv}"
  : "${UPSTREAM_NODES_PATH:=./upstream-nodes.txt}"
  : "${RRB_STATE_DIR:=/etc/reality-relay-bootstrap}"
  export RRB_STATE_DIR
  : "${ENABLE_CUSTOM_DOMAIN:=false}"
  : "${CUSTOM_DOMAIN:=}"
  : "${CUSTOM_DOMAIN_PROVIDER:=cloudflare}"
  : "${LE_EMAIL:=}"
  : "${LE_STAGING:=false}"
  : "${CLOUDFLARE_API_TOKEN:=}"
  : "${CLOUDFLARE_API_TOKEN_FILE:=$RRB_STATE_DIR/cloudflare.ini}"
  : "${CLOUDFLARE_ZONE_NAME:=}"
  : "${CLOUDFLARE_ACCOUNT_ID:=}"
  : "${CLOUDFLARE_CREATE_ZONE:=false}"
  : "${CLOUDFLARE_ENSURE_DNS_RECORDS:=true}"
  : "${CLOUDFLARE_DNS_OVERWRITE_EXISTING:=true}"
  : "${CLOUDFLARE_REQUIRE_ACTIVE_ZONE:=true}"
  : "${CLOUDFLARE_DNS_TTL:=1}"
  : "${CLOUDFLARE_DNS_PROPAGATION_SECONDS:=60}"
  : "${NGINX_FALLBACK_HOST:=127.0.0.1}"
  : "${NGINX_FALLBACK_PORT:=8443}"
  : "${NGINX_FALLBACK_ROOT:=/var/www/reality-fallback}"
  : "${NGINX_FALLBACK_CONF:=auto}"
  resolve_platform_config
  : "${SINGBOX_CONFIG_PATH:=/etc/sing-box/config.json}"
  : "${VLESS_UUID_PATH:=$RRB_STATE_DIR/vless-uuid.txt}"
  : "${VLESS_FLOW:=xtls-rprx-vision}"
  : "${REALITY_PRIVATE_KEY_PATH:=$RRB_STATE_DIR/reality-private.key}"
  : "${REALITY_PUBLIC_KEY_PATH:=$RRB_STATE_DIR/reality-public.key}"
  : "${REALITY_SHORT_ID_PATH:=$RRB_STATE_DIR/reality-short-id.txt}"

  DEFAULT_RRB_STATE_DIR="/etc/reality-relay-bootstrap"
  if [[ "$RRB_STATE_DIR" != "$DEFAULT_RRB_STATE_DIR" ]]; then
    [[ "$VLESS_UUID_PATH" == "$DEFAULT_RRB_STATE_DIR/vless-uuid.txt" ]] && VLESS_UUID_PATH="$RRB_STATE_DIR/vless-uuid.txt"
    [[ "$REALITY_PRIVATE_KEY_PATH" == "$DEFAULT_RRB_STATE_DIR/reality-private.key" ]] && REALITY_PRIVATE_KEY_PATH="$RRB_STATE_DIR/reality-private.key"
    [[ "$REALITY_PUBLIC_KEY_PATH" == "$DEFAULT_RRB_STATE_DIR/reality-public.key" ]] && REALITY_PUBLIC_KEY_PATH="$RRB_STATE_DIR/reality-public.key"
    [[ "$REALITY_SHORT_ID_PATH" == "$DEFAULT_RRB_STATE_DIR/reality-short-id.txt" ]] && REALITY_SHORT_ID_PATH="$RRB_STATE_DIR/reality-short-id.txt"
    [[ "$CLOUDFLARE_API_TOKEN_FILE" == "$DEFAULT_RRB_STATE_DIR/cloudflare.ini" ]] && CLOUDFLARE_API_TOKEN_FILE="$RRB_STATE_DIR/cloudflare.ini"
  fi

  : "${REALITY_SERVER_NAME:=www.microsoft.com}"
  : "${REALITY_HANDSHAKE_SERVER:=www.microsoft.com}"
  : "${REALITY_HANDSHAKE_PORT:=443}"
  : "${REALITY_MAX_TIME_DIFFERENCE:=1m}"
  : "${CLIENT_FINGERPRINT:=chrome}"
  : "${CLIENT_UDP:=true}"
  : "${CLIENT_ALPN:=h2,http/1.1}"
  : "${CLASH_MIXED_PORT:=7890}"
  : "${ENABLE_SUBSCRIPTION_SERVER:=true}"
  : "${SUBSCRIPTION_PORT:=}"
  : "${SUBSCRIPTION_INTERNAL_PORT:=51040}"
  : "${SUBSCRIPTION_LISTEN_PORT:=}"
  : "${SUBSCRIPTION_DIR:=/etc/reality-relay-bootstrap/subscription}"
  : "${RESET_PROXY_KEYS:=false}"
  : "${SUBSCRIPTION_TARGET:=ClashMeta}"
  : "${SUBSCRIPTION_BASE_URL:=}"
  local raw_subscription_target="$SUBSCRIPTION_TARGET"
  if ! SUBSCRIPTION_TARGET="$(normalize_subscription_target "$SUBSCRIPTION_TARGET")"; then
    die "SUBSCRIPTION_TARGET 不支持：${SUBSCRIPTION_TARGET:-空}；当前只支持 ClashMeta"
  fi
  warn_legacy_subscription_target "$raw_subscription_target"

  apply_custom_domain_defaults
  apply_subscription_defaults
  validate_config_basics
  resolve_csv_path
  resolve_upstream_nodes_path
}

custom_domain_enabled() {
  [[ "${ENABLE_CUSTOM_DOMAIN:-false}" == "true" ]]
}

apply_custom_domain_defaults() {
  custom_domain_enabled || return 0
  [[ -n "${CUSTOM_DOMAIN:-}" ]] || return 0

  if [[ -z "${REALITY_SERVER_NAME:-}" || "$REALITY_SERVER_NAME" == "www.microsoft.com" ]]; then
    REALITY_SERVER_NAME="$CUSTOM_DOMAIN"
  fi
  if [[ -z "${REALITY_HANDSHAKE_SERVER:-}" || "$REALITY_HANDSHAKE_SERVER" == "www.microsoft.com" ]]; then
    REALITY_HANDSHAKE_SERVER="$NGINX_FALLBACK_HOST"
  fi
  if [[ -z "${REALITY_HANDSHAKE_PORT:-}" || "$REALITY_HANDSHAKE_PORT" == "443" ]]; then
    REALITY_HANDSHAKE_PORT="$NGINX_FALLBACK_PORT"
  fi
  if [[ "$ENABLE_SUBSCRIPTION_SERVER" == "true" && -z "${SUBSCRIPTION_BASE_URL:-}" ]]; then
    SUBSCRIPTION_BASE_URL="https://$CUSTOM_DOMAIN"
    if [[ "$DIRECT_PORT" != "443" ]]; then
      SUBSCRIPTION_BASE_URL+=":$DIRECT_PORT"
    fi
  fi
  if [[ -z "${CLOUDFLARE_ZONE_NAME:-}" ]]; then
    CLOUDFLARE_ZONE_NAME="$(infer_cloudflare_zone_name "$CUSTOM_DOMAIN")"
  fi
}

apply_subscription_defaults() {
  if [[ "$ENABLE_SUBSCRIPTION_SERVER" != "true" ]]; then
    return 0
  fi

  if custom_domain_enabled; then
    if [[ -n "${SUBSCRIPTION_PORT:-}" ]]; then
      SUBSCRIPTION_LISTEN_PORT="$SUBSCRIPTION_PORT"
    else
      SUBSCRIPTION_LISTEN_PORT="$SUBSCRIPTION_INTERNAL_PORT"
    fi
  else
    if [[ -z "${SUBSCRIPTION_PORT:-}" ]]; then
      SUBSCRIPTION_PORT="$SUBSCRIPTION_INTERNAL_PORT"
    fi
    SUBSCRIPTION_LISTEN_PORT="$SUBSCRIPTION_PORT"
  fi
}

subscription_public_enabled() {
  [[ "${ENABLE_SUBSCRIPTION_SERVER:-false}" == "true" ]] || return 1
  if custom_domain_enabled && [[ -z "${SUBSCRIPTION_PORT:-}" ]]; then
    return 1
  fi
  [[ -n "${SUBSCRIPTION_PORT:-}" ]]
}

infer_cloudflare_zone_name() {
  local domain="$1" parts count
  IFS='.' read -r -a parts <<<"$domain"
  count="${#parts[@]}"
  if (( count >= 2 )); then
    printf '%s.%s\n' "${parts[count-2]}" "${parts[count-1]}"
  else
    printf '%s\n' "$domain"
  fi
}

server_ipv4_hosts() {
  if [[ -n "${SERVER_IP_IPV4:-}" ]]; then
    printf '%s\n' "$SERVER_IP_IPV4"
  elif [[ -n "${SERVER_IP:-}" && "$SERVER_IP" != *:* ]]; then
    printf '%s\n' "$SERVER_IP"
  fi
}

server_ipv6_hosts() {
  if [[ -n "${SERVER_IP_IPV6:-}" ]]; then
    printf '%s\n' "$SERVER_IP_IPV6"
  elif [[ -n "${SERVER_IP:-}" && "$SERVER_IP" == *:* ]]; then
    printf '%s\n' "$SERVER_IP"
  fi
}

server_hosts() {
  local fallback="${1:-服务器IP}" printed="false"
  while IFS= read -r host; do
    [[ -n "$host" ]] || continue
    printf '%s\n' "$host"
    printed="true"
  done < <(server_ipv4_hosts)
  while IFS= read -r host; do
    [[ -n "$host" ]] || continue
    printf '%s\n' "$host"
    printed="true"
  done < <(server_ipv6_hosts)
  [[ "$printed" == "true" ]] || printf '%s\n' "$fallback"
}

url_host() {
  local host="$1"
  if [[ "$host" == *:* && "$host" != \[*\] ]]; then
    printf '[%s]\n' "$host"
  else
    printf '%s\n' "$host"
  fi
}

normalize_subscription_target() {
  local raw="${1:-ClashMeta}" lower compact
  lower="$(printf '%s' "$raw" | tr '[:upper:]' '[:lower:]')"
  compact="$(printf '%s' "$lower" | tr -d ' _-')"
  case "$compact" in
    mihomo|clash|clashmeta) printf 'ClashMeta\n' ;;
    vless|vlessreality|reality|v2ray|v2rayn|v2rayng|quantumultx|quanx|quantumult|qx|shadowrocket) printf 'ClashMeta\n' ;;
    *) return 1 ;;
  esac
}

warn_legacy_subscription_target() {
  local raw="${1:-ClashMeta}" lower compact
  lower="$(printf '%s' "$raw" | tr '[:upper:]' '[:lower:]')"
  compact="$(printf '%s' "$lower" | tr -d ' _-')"
  case "$compact" in
    ""|mihomo|clash|clashmeta) ;;
    *) warn "SUBSCRIPTION_TARGET=$raw 已废弃；当前只生成 ClashMeta/Mihomo 订阅。" ;;
  esac
}

subscription_target_label() {
  case "${1:-$SUBSCRIPTION_TARGET}" in
    ClashMeta) printf 'ClashMeta\n' ;;
    *) printf '%s\n' "${1:-$SUBSCRIPTION_TARGET}" ;;
  esac
}

subscription_token() {
  if [[ -r "${VLESS_UUID_PATH:-}" ]]; then
    tr -d '\r\n' < "$VLESS_UUID_PATH"
  else
    printf '<token>'
  fi
}

validate_bool() {
  local name="$1" value="${!1}"
  case "$value" in true|false) ;; *) die "$name 必须是 true 或 false，当前：$value" ;; esac
}

validate_port() {
  local name="$1" value="${!1}"
  [[ "$value" =~ ^[0-9]+$ ]] || die "$name 必须是数字端口，当前：$value"
  (( value >= 1 && value <= 65535 )) || die "$name 超出端口范围 1-65535：$value"
}

validate_nonnegative_int() {
  local name="$1" value="${!1}"
  [[ "$value" =~ ^[0-9]+$ ]] || die "$name 必须是非负整数，当前：$value"
}

validate_user_name() {
  local name="$1" value="${!1}"
  [[ "$value" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || die "$name 用户名不合法：$value"
}

validate_config_basics() {
  validate_port SSH_PORT
  validate_port DIRECT_PORT
  [[ "$DIRECT_PORT" != "$SSH_PORT" ]] || die "DIRECT_PORT 不能与 SSH_PORT 相同"
  validate_port HOME_PORT_START
  validate_port HOME_PORT_END
  validate_bool ENABLE_SFTP_USER
  validate_bool USE_233BOY_INSTALLER
  validate_bool ENABLE_UFW
  validate_bool ENABLE_FAIL2BAN
  validate_bool ENABLE_IPV6_LISTEN
  validate_bool ENABLE_CUSTOM_DOMAIN
  validate_bool LE_STAGING
  validate_bool CLOUDFLARE_CREATE_ZONE
  validate_bool CLOUDFLARE_ENSURE_DNS_RECORDS
  validate_bool CLOUDFLARE_DNS_OVERWRITE_EXISTING
  validate_bool CLOUDFLARE_REQUIRE_ACTIVE_ZONE
  validate_bool CLIENT_UDP
  validate_bool ENABLE_SUBSCRIPTION_SERVER
  validate_bool RESET_PROXY_KEYS
  validate_port NGINX_FALLBACK_PORT
  validate_nonnegative_int CLOUDFLARE_DNS_TTL
  validate_nonnegative_int CLOUDFLARE_DNS_PROPAGATION_SECONDS
  validate_user_name ADMIN_USER
  validate_user_name SFTP_USER
  [[ "$ENABLE_SFTP_USER" != true || "$SFTP_USER" != root ]] || die "SFTP_USER 不能是 root"
  normalize_subscription_target "$SUBSCRIPTION_TARGET" >/dev/null || die "SUBSCRIPTION_TARGET 不支持：$SUBSCRIPTION_TARGET"
  (( HOME_PORT_START <= HOME_PORT_END )) || die "HOME_PORT_START 不能大于 HOME_PORT_END"
  [[ "$MODE_443" == "direct" ]] || die "MODE_443 当前只支持 direct"
  [[ "$INSTALL_SINGBOX_METHOD" == "apt" || "$INSTALL_SINGBOX_METHOD" == "233boy" || "$INSTALL_SINGBOX_METHOD" == "release" ]] || die "INSTALL_SINGBOX_METHOD 只能是 auto、apt、release 或 233boy"
  check_platform_install_method
  [[ "$FAIL2BAN_SSH_LOG" =~ ^/[A-Za-z0-9._/-]+$ ]] || die "FAIL2BAN_SSH_LOG 必须是无空格的绝对路径"
  [[ "$PROXY_PROTOCOL" == "vless-reality" ]] || die "PROXY_PROTOCOL 当前只支持 vless-reality"
  [[ "$PROXY_IP_VERSION" == "ipv4" || "$PROXY_IP_VERSION" == "ipv6" || "$PROXY_IP_VERSION" == "dual" ]] || die "PROXY_IP_VERSION 只能是 ipv4、ipv6 或 dual"
  validate_port REALITY_HANDSHAKE_PORT
  validate_port SUBSCRIPTION_INTERNAL_PORT
  if [[ "$ENABLE_SUBSCRIPTION_SERVER" == "true" ]]; then
    [[ -n "$SUBSCRIPTION_LISTEN_PORT" ]] || die "SUBSCRIPTION_LISTEN_PORT 不能为空"
    validate_port SUBSCRIPTION_LISTEN_PORT
    if [[ -n "${SUBSCRIPTION_PORT:-}" ]]; then
      validate_port SUBSCRIPTION_PORT
    fi
    [[ "$SUBSCRIPTION_LISTEN_PORT" != "$SSH_PORT" ]] || die "SUBSCRIPTION_LISTEN_PORT 不能与 SSH_PORT 相同"
    [[ "$SUBSCRIPTION_LISTEN_PORT" != "$DIRECT_PORT" ]] || die "SUBSCRIPTION_LISTEN_PORT 不能与 DIRECT_PORT 相同"
    (( SUBSCRIPTION_LISTEN_PORT < HOME_PORT_START || SUBSCRIPTION_LISTEN_PORT > HOME_PORT_END )) || die "SUBSCRIPTION_LISTEN_PORT 不能落在 HOME_PORT_START/HOME_PORT_END 范围内"
  fi
  [[ "$SUBSCRIPTION_DIR" == /* ]] || die "SUBSCRIPTION_DIR 必须是绝对路径"
  [[ "$SUBSCRIPTION_DIR" =~ ^/[A-Za-z0-9._/-]+$ ]] || die "SUBSCRIPTION_DIR 只能包含英文、数字、点、下划线、短横线和斜杠"
  [[ -z "$SUBSCRIPTION_BASE_URL" || "$SUBSCRIPTION_BASE_URL" =~ ^https?://[^[:space:]]+$ ]] || die "SUBSCRIPTION_BASE_URL 必须以 http:// 或 https:// 开头"
  [[ "$CUSTOM_DOMAIN_PROVIDER" == "cloudflare" ]] || die "CUSTOM_DOMAIN_PROVIDER 当前只支持 cloudflare"
  [[ "$CLOUDFLARE_API_TOKEN_FILE" == /* ]] || die "CLOUDFLARE_API_TOKEN_FILE 必须是绝对路径"
  [[ "$NGINX_FALLBACK_ROOT" == /* ]] || die "NGINX_FALLBACK_ROOT 必须是绝对路径"
  [[ "$NGINX_FALLBACK_CONF" == /* ]] || die "NGINX_FALLBACK_CONF 必须是绝对路径"
  if custom_domain_enabled; then
    [[ -n "$CUSTOM_DOMAIN" ]] || die "ENABLE_CUSTOM_DOMAIN=true 时 CUSTOM_DOMAIN 不能为空"
    [[ "$CUSTOM_DOMAIN" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9]$ && "$CUSTOM_DOMAIN" == *.* && "$CUSTOM_DOMAIN" != *..* ]] || die "CUSTOM_DOMAIN 不合法：$CUSTOM_DOMAIN"
    [[ -n "$CLOUDFLARE_ZONE_NAME" ]] || die "ENABLE_CUSTOM_DOMAIN=true 时 CLOUDFLARE_ZONE_NAME 不能为空"
    [[ "$CLOUDFLARE_ZONE_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9]$ && "$CLOUDFLARE_ZONE_NAME" == *.* && "$CLOUDFLARE_ZONE_NAME" != *..* ]] || die "CLOUDFLARE_ZONE_NAME 不合法：$CLOUDFLARE_ZONE_NAME"
    (( CLOUDFLARE_DNS_TTL == 1 || CLOUDFLARE_DNS_TTL >= 60 )) || die "CLOUDFLARE_DNS_TTL 必须为 1（自动）或 >=60"
    [[ "$LE_EMAIL" =~ ^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$ ]] || die "ENABLE_CUSTOM_DOMAIN=true 时 LE_EMAIL 必须是有效邮箱"
    if [[ "$CLOUDFLARE_CREATE_ZONE" == "true" ]]; then
      [[ -n "$CLOUDFLARE_ACCOUNT_ID" ]] || die "CLOUDFLARE_CREATE_ZONE=true 时 CLOUDFLARE_ACCOUNT_ID 不能为空"
    fi
    [[ "$NGINX_FALLBACK_PORT" != "$DIRECT_PORT" ]] || die "NGINX_FALLBACK_PORT 不能与 DIRECT_PORT 相同"
    [[ "$NGINX_FALLBACK_PORT" != "$SSH_PORT" ]] || die "NGINX_FALLBACK_PORT 不能与 SSH_PORT 相同"
    [[ "$NGINX_FALLBACK_PORT" != "$SUBSCRIPTION_LISTEN_PORT" ]] || die "NGINX_FALLBACK_PORT 不能与 SUBSCRIPTION_LISTEN_PORT 相同"
    [[ "$REALITY_SERVER_NAME" == "$CUSTOM_DOMAIN" ]] || die "启用自有域名时 REALITY_SERVER_NAME 必须等于 CUSTOM_DOMAIN"
    [[ "$REALITY_HANDSHAKE_SERVER" == "$NGINX_FALLBACK_HOST" ]] || die "启用自有域名时 REALITY_HANDSHAKE_SERVER 必须等于 NGINX_FALLBACK_HOST"
    [[ "$REALITY_HANDSHAKE_PORT" == "$NGINX_FALLBACK_PORT" ]] || die "启用自有域名时 REALITY_HANDSHAKE_PORT 必须等于 NGINX_FALLBACK_PORT"
  fi
  [[ "$VLESS_FLOW" == "" || "$VLESS_FLOW" == "xtls-rprx-vision" ]] || die "VLESS_FLOW 目前建议为空或 xtls-rprx-vision，当前：$VLESS_FLOW"
  [[ "$REALITY_SERVER_NAME" =~ ^[A-Za-z0-9._-]+$ ]] || die "REALITY_SERVER_NAME 不合法：$REALITY_SERVER_NAME"
  [[ "$REALITY_HANDSHAKE_SERVER" =~ ^[A-Za-z0-9._-]+$ ]] || die "REALITY_HANDSHAKE_SERVER 不合法：$REALITY_HANDSHAKE_SERVER"
  if [[ "$DIRECT_PORT" != "443" ]]; then
    warn "DIRECT_PORT 当前不是 443：$DIRECT_PORT；请确认客户端和安全组同步。"
  fi
}

resolve_csv_path() {
  if [[ "$CSV_PATH" != /* ]]; then
    CSV_PATH="$RRB_PROJECT_DIR/${CSV_PATH#./}"
  fi
  export CSV_PATH
}

resolve_upstream_nodes_path() {
  if [[ "$UPSTREAM_NODES_PATH" != /* ]]; then
    UPSTREAM_NODES_PATH="$RRB_PROJECT_DIR/${UPSTREAM_NODES_PATH#./}"
  fi
  export UPSTREAM_NODES_PATH
}

require_supported_os() {
  local os_release="${RRB_OS_RELEASE_FILE:-/etc/os-release}"
  [[ -r "$os_release" ]] || die "无法读取 $os_release"
  # shellcheck disable=SC1091
  source "$os_release"
  local os_id="${ID:-}" ver="${VERSION_ID:-}"
  case "$os_id:$ver" in
    ubuntu:22.04|ubuntu:24.04|debian:10|debian:10.*|debian:11|debian:11.*|debian:12|debian:12.*|debian:13|debian:13.*|alpine:3.19|alpine:3.19.*)
      info "系统受支持：$PRETTY_NAME"
      ;;
    *)
      die "当前系统不在默认支持范围：${PRETTY_NAME:-unknown}。支持 Ubuntu 22.04/24.04、Debian 10-13 和 Alpine 3.19，为防锁机已停止。"
      ;;
  esac
}

init_backup_dir() {
  [[ -n "$BACKUP_DIR" ]] && return 0
  local stamp
  stamp="$(date '+%Y%m%d-%H%M%S')"
  BACKUP_DIR="$BACKUP_ROOT/backup-${stamp}-${PHASE_NAME}"
  if is_dry_run; then
    log "DRY-RUN: mkdir -p $BACKUP_DIR"
  else
    mkdir -p "$BACKUP_DIR"
    chmod 700 "$BACKUP_ROOT" "$BACKUP_DIR" 2>/dev/null || true
    printf 'phase=%s\ndate=%s\nconfig=%s\n' "$PHASE_NAME" "$(_ts)" "$RRB_CONFIG_FILE" > "$BACKUP_DIR/MANIFEST.txt"
  fi
  export BACKUP_DIR
}

backup_path() {
  local path="$1"
  [[ -e "$path" ]] || { info "备份跳过，不存在：$path"; return 0; }
  init_backup_dir
  local rel="${path#/}"
  local dest="$BACKUP_DIR/$rel"
  # Nested phases share BACKUP_DIR. Keep the original snapshot, particularly
  # /etc/default/ufw before -6 changes it, rather than overwriting it in firewall.
  if [[ -e "$dest" || -L "$dest" ]]; then
    log "保留本阶段首次备份：$dest"
    return 0
  fi
  if is_dry_run; then
    log "DRY-RUN: cp -a $path $dest"
  else
    mkdir -p "$(dirname "$dest")"
    cp -a "$path" "$dest"
    log "已备份：$path -> $dest"
  fi
}

ssh_service_name() {
  if is_alpine; then printf 'sshd\n'; return; fi
  local units
  units="$(systemctl list-unit-files 2>/dev/null || true)"
  if grep -q '^ssh\.service' <<<"$units"; then
    echo ssh
  elif grep -q '^sshd\.service' <<<"$units"; then
    echo sshd
  elif systemctl status ssh >/dev/null 2>&1; then
    echo ssh
  else
    echo sshd
  fi
}

remove_legacy_sshd_dropins() {
  local path
  for path in /etc/ssh/sshd_config.d/00-reality-relay-bootstrap-{phase1,hardening}.conf; do
    if is_dry_run; then
      log "DRY-RUN: remove legacy managed SSH drop-in $path"
    else
      rm -f -- "$path"
    fi
  done
}

write_sshd_policy() {
  if is_dry_run; then
    log "DRY-RUN: write the current phase SSH policy at the start of /etc/ssh/sshd_config"
    cat >/dev/null
  else
    python3 "$SCRIPT_DIR/sshd_policy.py" --config /etc/ssh/sshd_config --apply || return $?
  fi
  # Both SSH phases back up sshd_config and the drop-in directory before calling this.
  remove_legacy_sshd_dropins
  info "本阶段 SSH 策略已优先放置于主配置开头。"
}

remove_sshd_policy() {
  if is_dry_run; then
    log "DRY-RUN: remove the managed SSH policy block from /etc/ssh/sshd_config"
  else
    python3 "$SCRIPT_DIR/sshd_policy.py" --config /etc/ssh/sshd_config --remove || return $?
  fi
  remove_legacy_sshd_dropins
}

sshd_bin() {
  if [[ -x /usr/sbin/sshd ]]; then
    echo /usr/sbin/sshd
  else
    command -v sshd || true
  fi
}

test_sshd_config() {
  local bin
  bin="$(sshd_bin)"
  [[ -n "$bin" ]] || die "找不到 sshd；请安装 openssh-server"
  run "$bin" -t
}

reload_ssh() {
  local svc
  svc="$(ssh_service_name)"
  service_reload "$svc"
  if ! is_dry_run; then
    service_wait_active "$svc" && info "SSH 服务已重载：$svc" || die "无法确认 SSH 服务状态：$svc"
  fi
}

ensure_sshd_dropin_include() {
  local conf="/etc/ssh/sshd_config"
  [[ -f "$conf" ]] || die "找不到 $conf"
  if is_dry_run; then
    log "DRY-RUN: mkdir -p /etc/ssh/sshd_config.d"
  else
    mkdir -p /etc/ssh/sshd_config.d
  fi
  local include_line first_match
  include_line="$(grep -nE '^\s*Include\s+/etc/ssh/sshd_config\.d/\*\.conf\b' "$conf" | head -n1 | cut -d: -f1 || true)"
  first_match="$(grep -nE '^\s*Match\b' "$conf" | head -n1 | cut -d: -f1 || true)"
  if [[ -n "$include_line" ]]; then
    if [[ -n "$first_match" && "$include_line" -gt "$first_match" ]]; then
      die "$conf 的 Include 出现在 Match 块之后，不适合自动写入早期 drop-in。请先手动整理 sshd_config。"
    fi
    info "已检测到 sshd drop-in Include：line $include_line"
    return 0
  fi
  warn "$conf 未发现 /etc/ssh/sshd_config.d/*.conf Include，将在文件开头安全插入。"
  backup_path "$conf"
  if is_dry_run; then
    log "DRY-RUN: prepend Include to $conf"
  else
    local tmp
    tmp="$(mktemp)"
    printf 'Include /etc/ssh/sshd_config.d/*.conf\n' > "$tmp"
    cat "$conf" >> "$tmp"
    cat "$tmp" > "$conf"
    rm -f "$tmp"
  fi
}

singbox_bin() {
  if is_alpine && [[ -x /usr/local/bin/sing-box ]]; then
    echo /usr/local/bin/sing-box
  elif [[ -x /etc/sing-box/bin/sing-box ]]; then
    echo /etc/sing-box/bin/sing-box
  elif command -v sing-box >/dev/null 2>&1; then
    command -v sing-box
  else
    true
  fi
}

singbox_check() {
  local bin config
  config="${1:-$SINGBOX_CONFIG_PATH}"
  bin="$(singbox_bin)"
  if [[ -z "$bin" ]]; then
    if is_dry_run; then
      log "DRY-RUN: sing-box check -c $config"
      return 0
    fi
    die "找不到 sing-box 可执行文件"
  fi
  run "$bin" check -c "$config"
}

restart_singbox() {
  service_enable sing-box
  service_restart sing-box
  if ! is_dry_run; then
    service_wait_active sing-box && info "sing-box 服务已启动。" || die "无法确认 sing-box 服务状态。"
  fi
}

port_line_matches() {
  local port="$1" line p
  while read -r line; do
    [[ -z "$line" ]] && continue
    p="$(awk '{print $4}' <<<"$line")"
    p="${p##*:}"
    [[ "$p" == "$port" ]] && { printf '%s\n' "$line"; return 0; }
  done < <(ss -H -ltnp 2>/dev/null || ss -H -ltn 2>/dev/null || true)
  return 1
}

port_in_use_by_other() {
  local port="$1" lines
  lines="$(port_line_matches "$port" || true)"
  [[ -z "$lines" ]] && return 1
  if grep -qi 'sing-box' <<<"$lines"; then
    return 1
  fi
  printf '%s\n' "$lines" >&2
  return 0
}

wait_for_port_listener() {
  local port="$1" timeout="${2:-15}" deadline
  deadline=$((SECONDS + timeout))
  while (( SECONDS <= deadline )); do
    if port_line_matches "$port" >/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 1
}

local_tcp_connects() {
  local family="$1" port="$2"
  python3 - "$family" "$port" <<'PY'
import socket
import sys

family = sys.argv[1]
port = int(sys.argv[2])
af, host = (socket.AF_INET6, "::1") if family == "6" else (socket.AF_INET, "127.0.0.1")
sock = socket.socket(af, socket.SOCK_STREAM)
sock.settimeout(1.0)
try:
    sock.connect((host, port))
finally:
    sock.close()
PY
}

wait_for_local_tcp_family() {
  local family="$1" port="$2" timeout="${3:-15}" deadline
  [[ "$family" == "4" || "$family" == "6" ]] || return 2
  deadline=$((SECONDS + timeout))
  while (( SECONDS <= deadline )); do
    if local_tcp_connects "$family" "$port" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

diagnose_singbox_listeners() {
  warn "当前 TCP 监听端口如下（用于排查 sing-box 未监听问题）："
  (ss -H -ltnp 2>/dev/null || ss -H -ltn 2>/dev/null || true) | tee -a "$LOG_FILE" >&2
  if is_alpine; then
    rc-service sing-box status 2>&1 | tee -a "$LOG_FILE" >&2 || true
    tail -n 80 /var/log/reality-relay/sing-box.log 2>/dev/null | tee -a "$LOG_FILE" >&2 || true
    return
  fi
  if command -v systemctl >/dev/null 2>&1; then
    warn "sing-box systemd 状态摘要："
    systemctl --no-pager --full status sing-box 2>&1 | tail -n 40 | tee -a "$LOG_FILE" >&2 || true
  fi
  if command -v journalctl >/dev/null 2>&1; then
    warn "sing-box 最近日志："
    journalctl -u sing-box --no-pager -n 80 2>&1 | tee -a "$LOG_FILE" >&2 || true
  fi
}

write_root_file() {
  local path="$1" mode="$2"
  local tmp
  tmp="$(mktemp)"
  cat > "$tmp"
  if is_dry_run; then
    log "DRY-RUN: install -m $mode $path"
    rm -f "$tmp"
  else
    install -m "$mode" -o root -g root "$tmp" "$path"
    rm -f "$tmp"
  fi
}
