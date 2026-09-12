#!/usr/bin/env bash
# 一键交互式安装入口：下载/定位项目、生成默认配置，并按安全阶段执行部署。
set -Eeuo pipefail

clear_screen() {
  if [[ -t 1 ]]; then
    if command -v clear >/dev/null 2>&1; then
      clear 2>/dev/null || printf '\033[2J\033[H'
    else
      printf '\033[2J\033[H'
    fi
  fi
}

clear_screen

REPO_URL="${RRB_REPO_URL:-https://github.com/suyi-92/reality-relay-bootstrap.git}"
INSTALL_DIR="${RRB_INSTALL_DIR:-/opt/reality-relay-bootstrap}"
DEFAULT_BRANCH="${RRB_BRANCH:-main}"
RUN_PHASES="${RRB_RUN_PHASES:-true}"
RRB_VERBOSE="${RRB_VERBOSE:-false}"
INSTALL_LOG_FILE="${RRB_INSTALL_LOG_FILE:-/tmp/reality-relay-bootstrap-install.log}"
INSTALL_MODE="full"
IPV6_NODE_ADDRESS=""
INPUT_TTY=""
export RRB_VERBOSE RUN_PHASES

BOLD=$'\033[1m'
DIM=$'\033[2m'
RED=$'\033[31m'
GREEN=$'\033[32m'
YELLOW=$'\033[33m'
BLUE=$'\033[34m'
CYAN=$'\033[36m'
RESET=$'\033[0m'

line() { printf '%b\n' "${DIM}────────────────────────────────────────────────────────────${RESET}"; }
info() { printf '%b\n' "${BLUE}INFO${RESET} $*" >&2; }
warn() { printf '%b\n' "${YELLOW}WARN${RESET} $*" >&2; }
die() { printf '%b\n' "${RED}ERROR${RESET} $*" >&2; exit 1; }

usage() {
  cat <<'EOF'
用法：
  sudo bash install.sh
  sudo bash install.sh -6 [IPv6地址]
  sudo bash install.sh --add-ipv6-node [IPv6地址]

选项：
  -6, --add-ipv6-node [地址]  在已有部署上启用 IPv6 监听，并单独生成 v6 VLESS 节点
      --ipv6-address <地址>   与 -6 相同，但要求显式提供 IPv6 地址
  -h, --help                 显示帮助

-6 是维护模式：要求现有 config.env、sing-box 和 Reality 密钥都已就绪；
不会重跑 SSH 初始化，也不会轮换 VLESS UUID、Reality keypair 或 short-id。
EOF
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -6|--add-ipv6-node)
        INSTALL_MODE="ipv6-node"
        if [[ $# -ge 2 && "${2:-}" != -* ]]; then
          IPV6_NODE_ADDRESS="$2"
          shift 2
        else
          shift
        fi
        ;;
      --ipv6-address)
        [[ $# -ge 2 && -n "${2:-}" ]] || die "--ipv6-address 后必须填写 IPv6 地址。"
        INSTALL_MODE="ipv6-node"
        IPV6_NODE_ADDRESS="$2"
        shift 2
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      --)
        shift
        [[ $# -eq 0 ]] || die "不支持位置参数：$*"
        ;;
      *)
        die "不支持的参数：$1；使用 --help 查看可用选项。"
        ;;
    esac
  done
}

init_input_tty() {
  if [[ -n "${RRB_INPUT_TTY:-}" ]]; then
    INPUT_TTY="$RRB_INPUT_TTY"
  elif [[ -r /dev/tty ]]; then
    INPUT_TTY="/dev/tty"
  elif [[ -t 0 ]]; then
    INPUT_TTY="/dev/stdin"
  else
    die "需要交互式终端来填写配置；也可以使用 -6 <IPv6地址> 直接指定地址。"
  fi
}

run_logged() {
  if [[ "$RRB_VERBOSE" == "true" ]]; then
    "$@"
  else
    "$@" >>"$INSTALL_LOG_FILE" 2>&1
  fi
}

banner() {
  clear_screen
  printf '%b\n' "${CYAN}${BOLD}"
  cat <<'BANNER'
╭────────────────────────────────────────────────╮
│            Reality Relay Bootstrap             │
│              One-click installer               │
│        Server, SSH key, and relay exits        │
╰────────────────────────────────────────────────╯
BANNER
  printf '%b' "${RESET}"
}

read_default() {
  local prompt="$1" default_value="$2" value
  if [[ -n "$default_value" ]]; then
    if [[ "$INPUT_TTY" == "/dev/tty" ]]; then
      IFS= read -e -i "$default_value" -r -p "${prompt}: " value <"$INPUT_TTY" || true
    else
      printf '%b' "${BOLD}${prompt}${RESET} ${DIM}[默认: ${default_value}]${RESET}: " >"$INPUT_TTY"
      IFS= read -r value <"$INPUT_TTY" || true
      value="${value:-$default_value}"
    fi
  else
    printf '%b' "${BOLD}${prompt}${RESET}: " >"$INPUT_TTY"
    IFS= read -r value <"$INPUT_TTY" || true
  fi
  printf '%s\n' "$value"
}

read_secret_default() (
  local prompt="$1" default_value="$2" value="" key="" escape="" tty_fd tty_state
  exec {tty_fd}<>"$INPUT_TTY"
  tty_state="$(stty -g <&"$tty_fd")" || die "无法读取敏感输入终端状态。"
  # Keep echo disabled throughout typing/pasting; isolate cleanup from the installer's traps.
  trap 'stty "$tty_state" <&"$tty_fd" 2>/dev/null || true' EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
  stty -echo -icanon min 1 time 0 <&"$tty_fd" || die "无法关闭敏感输入的终端回显。"
  if [[ -n "$default_value" ]]; then
    printf '%b' "${BOLD}${prompt}${RESET} ${DIM}[默认: 已填入，可直接回车]${RESET}: " >&"$tty_fd"
  else
    printf '%b' "${BOLD}${prompt}${RESET}: " >&"$tty_fd"
  fi
  while true; do
    if ! IFS= read -r -N 1 key <&"$tty_fd"; then
      printf '\n' >&"$tty_fd"
      return 1
    fi
    # Ignore cursor keys and bracketed-paste markers without adding them to the secret.
    if [[ "$escape" == "start" ]]; then
      escape=""
      [[ "$key" == "[" || "$key" == "O" ]] && escape="sequence"
      continue
    elif [[ "$escape" == "sequence" ]]; then
      [[ "$key" == [@-~] ]] && escape=""
      continue
    fi
    case "$key" in
      $'\n'|$'\r') break ;;
      $'\177'|$'\b')
        if [[ -n "$value" ]]; then
          value="${value%?}"
          printf '\b \b' >&"$tty_fd"
        fi
        ;;
      $'\025')
        while [[ -n "$value" ]]; do
          value="${value%?}"
          printf '\b \b' >&"$tty_fd"
        done
        ;;
      $'\004') printf '\n' >&"$tty_fd"; return 1 ;;
      $'\033') escape="start" ;;
      [[:print:]]) value+="$key"; printf '*' >&"$tty_fd" ;;
    esac
  done
  printf '\n' >&"$tty_fd"
  printf '%s\n' "${value:-$default_value}"
)

read_yes_no() {
  local prompt="$1" default_value="${2:-yes}" value suffix
  case "${default_value,,}" in
    y|yes|true|1)
      default_value="yes"
      suffix="Y/n"
      ;;
    n|no|false|0)
      default_value="no"
      suffix="y/N"
      ;;
    *)
      die "read_yes_no 默认值必须是 yes 或 no，当前：$default_value"
      ;;
  esac
  while true; do
    printf '%b' "${BOLD}${prompt}${RESET} ${DIM}[${suffix}]${RESET}: " >"$INPUT_TTY"
    IFS= read -r value <"$INPUT_TTY" || true
    if [[ -z "$value" ]]; then
      [[ "$default_value" == "yes" ]]
      return
    fi
    case "${value,,}" in
      y|yes) return 0 ;;
      n|no) return 1 ;;
      *) warn "请输入 yes 或 no。" ;;
    esac
  done
}

shell_quote() {
  printf '%q' "$1"
}

config_env_value() {
  local config_file="$1" key="$2" fallback="$3" raw value
  if [[ ! -f "$config_file" ]]; then
    printf '%s\n' "$fallback"
    return 0
  fi
  raw="$(awk -v key="$key" '
    $0 ~ "^[[:space:]]*(export[[:space:]]+)?" key "[[:space:]]*=" {
      sub(/^[[:space:]]*(export[[:space:]]+)?[^=]+=/, "")
      print
      exit
    }
  ' "$config_file")"
  if [[ -z "$raw" ]]; then
    printf '%s\n' "$fallback"
    return 0
  fi
  value="$(printf '%s' "$raw" | sed -E "s/[[:space:]]+#.*$//; s/^[[:space:]]+//; s/[[:space:]]+$//; s/^[\"']//; s/[\"']$//")"
  case "$value" in
    true|false) printf '%s\n' "$value" ;;
    *) printf '%s\n' "$fallback" ;;
  esac
}

csv_escape() {
  local value="$1"
  if [[ "$value" == *","* || "$value" == *"\""* || "$value" == *$'\n'* ]]; then
    value="${value//\"/\"\"}"
    printf '"%s"' "$value"
  else
    printf '%s' "$value"
  fi
}

default_authorized_keys() {
  awk 'NF && $1 !~ /^#/ { print }' /root/.ssh/authorized_keys 2>/dev/null || true
}

collect_admin_pubkeys() {
  local keys_file="$1" count=0 key keys_fd
  local key_pattern='^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp[0-9]+|sk-ssh-ed25519@openssh.com|sk-ecdsa-sha2-nistp256@openssh.com)[[:space:]]+[A-Za-z0-9+/]+={0,2}([[:space:]].*)?$'
  : >"$keys_file"

  line
  printf '%b\n' "${CYAN}${BOLD}管理员 SSH 公钥${RESET}"
  printf '%b\n' "${DIM}请一次性粘贴多行 SSH 公钥，每行一把；输入完成后，再输入一个空行结束。${RESET}"
  printf '%b\n' "${DIM}至少输入一把公钥。若明确跳过本次录入，请单独输入大写 SKIP；后续 SSH 部署将使用 root 已有的 authorized_keys。${RESET}"
  exec {keys_fd}<"$INPUT_TTY"
  while true; do
    printf '%b' "> " >"$INPUT_TTY"
    if ! IFS= read -r key <&"$keys_fd"; then
      exec {keys_fd}<&-
      die "SSH 公钥输入已中断；未确认本次录入。请重新运行安装，若要跳过请明确输入 SKIP。"
    fi
    key="${key//$'\033[200~'/}"
    key="${key//$'\033[201~'/}"
    key="${key%$'\r'}"
    key="${key#"${key%%[![:space:]]*}"}"
    key="${key%"${key##*[![:space:]]}"}"
    if [[ "$key" == "SKIP" ]]; then
      if (( count > 0 )); then
        warn "已录入 ${count} 把公钥；请用空行确认，SKIP 仅用于尚未录入公钥时跳过。"
        continue
      fi
      exec {keys_fd}<&-
      warn "已明确跳过本次公钥录入。若 root 尚无可用 authorized_keys，后续 SSH 部署检查仍会停止。"
      return 0
    fi
    if [[ -z "$key" ]]; then
      if (( count > 0 )); then
        break
      fi
      warn "尚未输入管理员 SSH 公钥；请至少粘贴一把公钥，或明确输入 SKIP 跳过。"
      continue
    fi
    if [[ ! "$key" =~ $key_pattern ]]; then
      warn "这一行不是支持的 SSH 公钥格式，未保存。请重新粘贴完整公钥，每行一把。"
      continue
    fi
    printf '%s\n' "$key" >>"$keys_file"
    count=$((count + 1))
  done
  exec {keys_fd}<&-
  info "已接收 ${count} 把管理员 SSH 公钥。"
}

read_bool_value() {
  local prompt="$1" default_value="$2" value
  while true; do
    value="$(read_default "$prompt" "$default_value")"
    case "$value" in
      true|false) printf '%s\n' "$value"; return 0 ;;
      y|Y|yes|YES|Yes) printf 'true\n'; return 0 ;;
      n|N|no|NO|No) printf 'false\n'; return 0 ;;
      *) warn "$prompt 必须是 true 或 false。" ;;
    esac
  done
}

url_host() {
  local host="$1"
  if [[ "$host" == *:* && "$host" != \[*\] ]]; then
    printf '[%s]\n' "$host"
  else
    printf '%s\n' "$host"
  fi
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

subscription_target_label() {
  case "$1" in
    ClashMeta) printf 'ClashMeta\n' ;;
    *) printf '%s\n' "$1" ;;
  esac
}

subscription_token() {
  local token_path="/etc/reality-relay-bootstrap/vless-uuid.txt"
  if [[ -r "$token_path" ]]; then
    tr -d '\r\n' < "$token_path"
  else
    printf '<token>'
  fi
}

public_ipv4() {
  local ip=""
  if command -v curl >/dev/null 2>&1; then
    ip="$(curl -4fsS --max-time 4 https://api.ipify.org 2>/dev/null || true)"
  fi
  if [[ ! "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    ip="$(hostname -I 2>/dev/null | tr ' ' '\n' | awk '/^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/ {print; exit}')"
  fi
  printf '%s\n' "$ip"
}

public_ipv6() {
  local ip=""
  if command -v curl >/dev/null 2>&1; then
    ip="$(curl -6fsS --max-time 4 https://api64.ipify.org 2>/dev/null || true)"
  fi
  if [[ -z "$ip" ]] && command -v ip >/dev/null 2>&1; then
    ip="$(ip -6 -o addr show scope global 2>/dev/null | awk '{sub(/\/.*/, "", $4); print $4; exit}')"
  fi
  printf '%s\n' "$ip"
}

normalize_ipv6() {
  local value="$1"
  command -v python3 >/dev/null 2>&1 || die "找不到 python3，无法校验 IPv6 地址。"
  python3 - "$value" <<'PY'
import ipaddress
import sys

try:
    address = ipaddress.IPv6Address(sys.argv[1].strip().strip("[]"))
except ValueError as exc:
    raise SystemExit(f"ERROR: IPv6 地址不合法：{sys.argv[1]}（{exc}）")
if address.is_unspecified or address.is_loopback or address.is_multicast or address.is_link_local:
    raise SystemExit(f"ERROR: 不能把 {address} 用作公网 VLESS 节点地址。")
print(address.compressed)
PY
}

ensure_project() {
  local self_dir
  self_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" 2>/dev/null && pwd -P || true)"
  if [[ -n "$self_dir" && -f "$self_dir/bootstrap.sh" && -f "$self_dir/config.example.env" ]]; then
    printf '%s\n' "$self_dir"
    return 0
  fi

  info "当前是一键远程执行模式，将项目安装到：$INSTALL_DIR"
  if ! command -v git >/dev/null 2>&1; then
    info "安装 git/curl 基础依赖。"
    local apt_log="/tmp/reality-relay-bootstrap-install-apt.log"
    apt-get update -y >"$apt_log" 2>&1 || { tail -n 40 "$apt_log" >&2 || true; die "apt-get update 失败。"; }
    DEBIAN_FRONTEND=noninteractive apt-get install -y git curl ca-certificates >>"$apt_log" 2>&1 || { tail -n 60 "$apt_log" >&2 || true; die "安装 git/curl 失败。"; }
  fi

  if [[ -d "$INSTALL_DIR/.git" ]]; then
    info "检测到已有项目目录，拉取最新代码。"
    run_logged git -C "$INSTALL_DIR" fetch --all --prune
    run_logged git -C "$INSTALL_DIR" checkout "$DEFAULT_BRANCH"
    run_logged git -C "$INSTALL_DIR" pull --ff-only
  else
    mkdir -p "$(dirname "$INSTALL_DIR")"
    run_logged git clone --branch "$DEFAULT_BRANCH" "$REPO_URL" "$INSTALL_DIR"
  fi
  printf '%s\n' "$INSTALL_DIR"
}

write_config() {
  local project_dir="$1" server_alias="$2" server_ip_ipv4="$3" server_ip_ipv6="$4" ssh_port="$5" admin_pubkey="$6" direct_port="$7" enable_subscription_server="$8" subscription_port="$9" subscription_target="${10}" reset_proxy_keys="${11}" home_port_start="${12}" home_port_end="${13}" enable_custom_domain="${14}" custom_domain="${15}" cloudflare_zone_name="${16}" le_email="${17}" cloudflare_api_token="${18}" nginx_fallback_port="${19}"
  local server_ip="$server_ip_ipv4"
  local proxy_ip_version="ipv4"
  local enable_ipv6_listen="false"
  local reality_server_name="www.microsoft.com"
  local reality_handshake_server="www.microsoft.com"
  local reality_handshake_port="443"
  local subscription_base_url=""
  [[ -n "$server_ip" ]] || server_ip="$server_ip_ipv6"
  if [[ -n "$server_ip_ipv4" && -n "$server_ip_ipv6" ]]; then
    proxy_ip_version="dual"
  elif [[ -z "$server_ip_ipv4" && -n "$server_ip_ipv6" ]]; then
    proxy_ip_version="ipv6"
  fi
  [[ -z "$server_ip_ipv6" ]] || enable_ipv6_listen="true"
  if [[ "$enable_custom_domain" == "true" ]]; then
    reality_server_name="$custom_domain"
    reality_handshake_server="127.0.0.1"
    reality_handshake_port="$nginx_fallback_port"
    subscription_base_url="https://$custom_domain"
  fi
  cat >"$project_dir/config.env" <<EOF_CONFIG
# Generated by install.sh. Sensitive values; do not publish.
SERVER_ALIAS=$(shell_quote "$server_alias")
SERVER_IP=$(shell_quote "$server_ip")
SERVER_IP_IPV4=$(shell_quote "$server_ip_ipv4")
SERVER_IP_IPV6=$(shell_quote "$server_ip_ipv6")
SSH_PORT=$(shell_quote "$ssh_port")
INITIAL_USER="root"
ADMIN_USER="root"
ADMIN_PUBKEY=$(shell_quote "$admin_pubkey")
ADMIN_PUBKEYS=""
ENABLE_SFTP_USER="false"
SFTP_USER="sftpuser"
USE_233BOY_INSTALLER="false"
INSTALL_SINGBOX_METHOD="apt"
MODE_443="direct"
DIRECT_PORT=$(shell_quote "$direct_port")
HOME_PORT_START=$(shell_quote "$home_port_start")
HOME_PORT_END=$(shell_quote "$home_port_end")
ENABLE_UFW="true"
ENABLE_FAIL2BAN="true"
ENABLE_IPV6_LISTEN=$(shell_quote "$enable_ipv6_listen")

SFTP_PUBKEY=""
CSV_PATH="./home-proxies.csv"
UPSTREAM_NODES_PATH="./upstream-nodes.txt"
RRB_STATE_DIR="/etc/reality-relay-bootstrap"
ENABLE_CUSTOM_DOMAIN=$(shell_quote "$enable_custom_domain")
CUSTOM_DOMAIN=$(shell_quote "$custom_domain")
CUSTOM_DOMAIN_PROVIDER="cloudflare"
LE_EMAIL=$(shell_quote "$le_email")
LE_STAGING="false"
CLOUDFLARE_API_TOKEN=$(shell_quote "$cloudflare_api_token")
CLOUDFLARE_API_TOKEN_FILE="/etc/reality-relay-bootstrap/cloudflare.ini"
CLOUDFLARE_ZONE_NAME=$(shell_quote "$cloudflare_zone_name")
CLOUDFLARE_ACCOUNT_ID=""
CLOUDFLARE_CREATE_ZONE="false"
CLOUDFLARE_ENSURE_DNS_RECORDS="true"
CLOUDFLARE_DNS_OVERWRITE_EXISTING="true"
CLOUDFLARE_REQUIRE_ACTIVE_ZONE="true"
CLOUDFLARE_DNS_TTL="1"
CLOUDFLARE_DNS_PROPAGATION_SECONDS="60"
NGINX_FALLBACK_HOST="127.0.0.1"
NGINX_FALLBACK_PORT=$(shell_quote "$nginx_fallback_port")
NGINX_FALLBACK_ROOT="/var/www/reality-fallback"
NGINX_FALLBACK_CONF="/etc/nginx/sites-available/reality-relay-bootstrap.conf"
SINGBOX_CONFIG_PATH="/etc/sing-box/config.json"

PROXY_PROTOCOL="vless-reality"
PROXY_IP_VERSION=$(shell_quote "$proxy_ip_version")
RESET_PROXY_KEYS=$(shell_quote "$reset_proxy_keys")
VLESS_UUID_PATH="/etc/reality-relay-bootstrap/vless-uuid.txt"
VLESS_FLOW="xtls-rprx-vision"
REALITY_PRIVATE_KEY_PATH="/etc/reality-relay-bootstrap/reality-private.key"
REALITY_PUBLIC_KEY_PATH="/etc/reality-relay-bootstrap/reality-public.key"
REALITY_SHORT_ID_PATH="/etc/reality-relay-bootstrap/reality-short-id.txt"
REALITY_SERVER_NAME=$(shell_quote "$reality_server_name")
REALITY_HANDSHAKE_SERVER=$(shell_quote "$reality_handshake_server")
REALITY_HANDSHAKE_PORT=$(shell_quote "$reality_handshake_port")
REALITY_MAX_TIME_DIFFERENCE="1m"

CLIENT_FINGERPRINT="chrome"
CLIENT_UDP="true"
CLIENT_ALPN="h2,http/1.1"
CLASH_MIXED_PORT="7890"

ENABLE_SUBSCRIPTION_SERVER=$(shell_quote "$enable_subscription_server")
SUBSCRIPTION_PORT=$(shell_quote "$subscription_port")
SUBSCRIPTION_INTERNAL_PORT="51040"
SUBSCRIPTION_TARGET=$(shell_quote "$subscription_target")
SUBSCRIPTION_BASE_URL=$(shell_quote "$subscription_base_url")
SUBSCRIPTION_DIR="/etc/reality-relay-bootstrap/subscription"
EOF_CONFIG
  chmod 600 "$project_dir/config.env"
}

write_home_proxies() {
  local project_dir="$1" rows_file="$2"
  {
    printf 'tag,type,server,server_port,username,password,network,listen_port\n'
    cat "$rows_file"
  } >"$project_dir/home-proxies.csv"
  chmod 600 "$project_dir/home-proxies.csv"
}

write_upstream_nodes() {
  local project_dir="$1" rows_file="$2"
  {
    printf 'tag,node_url,listen_port\n'
    cat "$rows_file"
  } >"$project_dir/upstream-nodes.txt"
  chmod 600 "$project_dir/upstream-nodes.txt"
}

tag_from_upstream_url() {
  local url="$1" fallback="$2" tag=""
  if [[ "$url" == *"#"* ]]; then
    tag="${url##*#}"
  fi
  tag="$(printf '%s' "${tag:-$fallback}" | sed 's/%20/-/g; s/[^A-Za-z0-9_-]/-/g; s/^-*//; s/-*$//')"
  [[ -n "$tag" ]] || tag="$fallback"
  printf '%s\n' "$tag"
}

port_used_in_file() {
  local port="$1" file="$2"
  [[ -s "$file" ]] || return 1
  awk -F',' -v port="$port" 'NF >= 2 && $2 == port { found=1 } END { exit found ? 0 : 1 }' "$file"
}

next_available_relay_port() {
  local start="$1" end="$2" home_rows_file="$3" upstream_rows_file="$4" port
  for (( port=start; port<=end; port++ )); do
    port_used_in_file "$port" "$home_rows_file" && continue
    port_used_in_file "$port" "$upstream_rows_file" && continue
    printf '%s\n' "$port"
    return 0
  done
  die "HOME_PORT_START/HOME_PORT_END 范围内没有可用端口；请扩大端口范围。"
}

collect_home_proxies_bulk() {
  local rows_file="$1" line_value any=false
  printf '%b\n' "${DIM}可直接粘贴多行 CSV，字段为：tag,type,server,server_port,username,password,network,listen_port${RESET}"
  printf '%b\n' "${DIM}支持带表头；粘贴完成后输入空行结束。listen_port 可留空自动分配，例如：home-01,socks5,1.2.3.4,1080,,secret,tcp,${RESET}"
  while true; do
    printf '%b' "> " >"$INPUT_TTY"
    IFS= read -r line_value <"$INPUT_TTY" || true
    [[ -n "$line_value" ]] || break
    [[ "$line_value" =~ ^[[:space:]]*# ]] && continue
    if [[ "$line_value" =~ ^[[:space:]]*tag[[:space:]]*,[[:space:]]*listen_port[[:space:]]*, ]]; then
      continue
    fi
    printf '%s\n' "$line_value" >>"$rows_file"
    any=true
  done
  [[ "$any" == "true" ]]
}

collect_home_proxies_one_by_one() {
  local rows_file="$1" index=1
  while read_yes_no "是否添加第 ${index} 个家宽代理出口？" "$([[ $index -eq 1 ]] && echo yes || echo no)"; do
    local tag listen_port type server server_port username password network
    tag="$(read_default "  tag" "home-$(printf '%02d' "$index")")"
    type="$(read_default "  type (socks5/http)" "socks5")"
    server="$(read_default "  server" "")"
    server_port="$(read_default "  server_port" "1080")"
    username="$(read_default "  username" "")"
    password="$(read_secret_default "  password" "")"
    network="$(read_default "  network" "tcp")"
    listen_port="$(read_default "  listen_port (留空自动分配)" "")"
    [[ -n "$server" ]] || die "家宽代理 server 不能为空；如没有代理，请重新运行并选择不添加。"
    printf '%s,%s,%s,%s,%s,%s,%s,%s\n' \
      "$(csv_escape "$tag")" \
      "$(csv_escape "$type")" \
      "$(csv_escape "$server")" \
      "$(csv_escape "$server_port")" \
      "$(csv_escape "$username")" \
      "$(csv_escape "$password")" \
      "$(csv_escape "$network")" \
      "$(csv_escape "$listen_port")" >>"$rows_file"
    index=$((index + 1))
  done
}

collect_home_proxies() {
  local rows_file="$1"
  : >"$rows_file"
  line
  printf '%b\n' "${CYAN}${BOLD}家宽代理配置${RESET}"
  printf '%b\n' "${DIM}每一行会生成一个 VLESS+Reality 入口端口；如暂时没有家宽代理，可以不添加。${RESET}"
  if read_yes_no "是否一次性粘贴多行家宽代理 CSV？" yes; then
    collect_home_proxies_bulk "$rows_file" || warn "没有粘贴任何家宽代理行。"
  fi
  if [[ ! -s "$rows_file" ]]; then
    if read_yes_no "未添加家宽代理，是否跳过家宽代理配置？" yes; then
      return 0
    fi
    collect_home_proxies_one_by_one "$rows_file"
  fi
}

collect_upstream_nodes() {
  local rows_file="$1" home_rows_file="$2" port_start="$3" port_end="$4"
  local line_value any=false index=1
  : >"$rows_file"

  line
  printf '%b\n' "${CYAN}${BOLD}上游节点配置${RESET}"
  printf '%b\n' "${DIM}可选：把别人给你的普通 vless:// 或 VLESS Reality 节点接到新的入口端口。${RESET}"
  if ! read_yes_no "是否粘贴上游节点分享链接或 CSV？" yes; then
    return 0
  fi

  printf '%b\n' "${DIM}每行一个 vless:// 节点链接，脚本会自动分配端口；也支持 CSV：tag,node_url,listen_port。空行结束。${RESET}"
  printf '%b\n' "${DIM}示例：vless://uuid@example.com:443?type=tcp&security=reality&flow=xtls-rprx-vision&fp=chrome&sni=www.microsoft.com&pbk=xxx&sid=abcd&spx=%2F#relay${RESET}"
  while true; do
    printf '%b' "> " >"$INPUT_TTY"
    IFS= read -r line_value <"$INPUT_TTY" || true
    [[ -n "$line_value" ]] || break
    [[ "$line_value" =~ ^[[:space:]]*# ]] && continue
    if [[ "$line_value" =~ ^[[:space:]]*tag[[:space:]]*, ]]; then
      continue
    fi
    if [[ "$line_value" =~ ^[^,]+,[[:space:]]*vless:// || "$line_value" =~ ^[^,]+,[[:space:]]*[0-9]+,[[:space:]]*vless:// ]]; then
      printf '%s\n' "$line_value" >>"$rows_file"
      any=true
      index=$((index + 1))
      continue
    fi
    case "$line_value" in
      vless://*)
        local tag
        tag="$(tag_from_upstream_url "$line_value" "upstream-$(printf '%02d' "$index")")"
        printf '%s,%s,\n' "$(csv_escape "$tag")" "$(csv_escape "$line_value")" >>"$rows_file"
        any=true
        index=$((index + 1))
        ;;
      *)
        warn "暂不支持这一行；请粘贴 vless://，或 tag,node_url,listen_port CSV。"
        ;;
    esac
  done
  [[ "$any" == "true" ]] || warn "未添加上游节点。"
}

run_install_flow() {
  local project_dir="$1" server_ip_ipv4="$2" server_ip_ipv6="$3" ssh_port="$4" enable_subscription_server="$5" subscription_port="$6" subscription_target="$7" enable_custom_domain="$8" custom_domain="$9"
  cd "$project_dir"
  line
  printf '%b\n' "${GREEN}${BOLD}配置文件已生成：${RESET}"
  printf '  %s\n' "$project_dir/config.env" "$project_dir/home-proxies.csv" "$project_dir/upstream-nodes.txt"

  if [[ "$RUN_PHASES" != "true" ]]; then
    warn "RRB_RUN_PHASES=$RUN_PHASES，仅生成配置，不执行部署阶段。"
    return 0
  fi

  line
  printf '%b\n' "${CYAN}${BOLD}开始分阶段部署${RESET}"
  bash bootstrap.sh --phase preflight
  bash bootstrap.sh --phase ssh-phase1

  line
  printf '%b\n\n' "${YELLOW}${BOLD}安全确认：不要关闭当前 SSH 窗口。${RESET}"
  printf '请另开一个终端，确认下面命令可以通过 root 公钥登录：\n\n'
  if [[ -n "$server_ip_ipv4" ]]; then
    printf 'IPv4:\n'
    printf '  ssh -p %q -o PreferredAuthentications=publickey -o PasswordAuthentication=no root@%q\n' "$ssh_port" "$server_ip_ipv4"
  fi
  if [[ -n "$server_ip_ipv6" ]]; then
    printf '\nIPv6:\n'
    printf '  ssh -p %q -o PreferredAuthentications=publickey -o PasswordAuthentication=no root@%q\n' "$ssh_port" "$server_ip_ipv6"
  fi
  printf '\n  whoami\n\n'
  printf '确认输出为 root 后，再回到这里继续。\n'
  if read_yes_no "我已确认 root 公钥登录正常，继续 SSH final 加固？" yes; then
    CONFIRM_ROOT_KEY_LOGIN=yes bash bootstrap.sh --phase ssh-final
  else
    warn "已暂停在 ssh-phase1。之后可手动执行：sudo CONFIRM_ROOT_KEY_LOGIN=yes bash bootstrap.sh --phase ssh-final"
    return 0
  fi

  bash bootstrap.sh --phase fail2ban
  bash bootstrap.sh --phase singbox
  bash bootstrap.sh --phase firewall
  bash bootstrap.sh --phase validate

  line
  printf '%b\n' "${GREEN}${BOLD}部署完成。${RESET}"
  if [[ "$enable_subscription_server" == "true" ]]; then
    local token target_label target_path
    token="$(subscription_token)"
    target_label="$(subscription_target_label "$subscription_target")"
    target_path="/sub/${token}?target=${subscription_target}"
    printf '\n订阅链接：\n'
    if [[ "$enable_custom_domain" == "true" ]]; then
      printf '  %s:  https://%s%s\n' "$target_label" "$custom_domain" "$target_path"
    fi
    if [[ -n "$subscription_port" && -n "$server_ip_ipv4" ]]; then
      local h4
      h4="$(url_host "$server_ip_ipv4")"
      printf 'IPv4:\n'
      printf '  %s:  http://%s:%s%s\n' "$target_label" "$h4" "$subscription_port" "$target_path"
    fi
    if [[ -n "$subscription_port" && -n "$server_ip_ipv6" ]]; then
      local h6
      h6="$(url_host "$server_ip_ipv6")"
      printf '\nIPv6:\n'
      printf '  %s:  http://%s:%s%s\n' "$target_label" "$h6" "$subscription_port" "$target_path"
    fi
    if [[ "$enable_custom_domain" == "true" ]]; then
      printf '\n说明：订阅内容按 CUSTOM_DOMAIN 生成，普通 HTTPS 流量经 sing-box Reality fallback 转到本机 Nginx。\n'
      if [[ -n "$subscription_port" ]]; then
        printf '同时启用了直接 HTTP 订阅端口；请确认 UFW 和服务商安全组放行 tcp/%s。\n' "$subscription_port"
      else
        printf '未设置 SUBSCRIPTION_PORT，仅显示域名 HTTPS 订阅；内部订阅服务监听本机默认端口。\n'
      fi
    else
      printf '\n说明：订阅内容按 SERVER_IP_IPV4/SERVER_IP_IPV6 生成；如果两者都填写，IPv4/IPv6 链接都返回同一份完整节点，IPv6 节点名称追加 -IPv6。\n'
      printf '如客户端无法导入 IPv6 字面量订阅地址，有 IPv4 时直接使用 IPv4 订阅地址即可。\n'
      printf '内置订阅服务是 HTTP；如需 HTTPS，请在外层接入带证书的反向代理。\n'
    fi
  else
    printf '\n订阅服务未启用。\n'
  fi
  printf '\n可选：如需在服务器本地生成节点文件，再执行：\n'
  printf '  sudo bash bootstrap.sh --phase output-nodes\n'
}

run_ipv6_node_flow() {
  local project_dir="$1" detected_ipv6 normalized_ipv6
  [[ -f "$project_dir/config.env" ]] || die "-6 维护模式要求已有配置：$project_dir/config.env"
  [[ -x "$project_dir/scripts/16-add-ipv6-node.sh" || -f "$project_dir/scripts/16-add-ipv6-node.sh" ]] \
    || die "当前分支缺少 IPv6 节点维护脚本。"

  if [[ -z "$IPV6_NODE_ADDRESS" ]]; then
    detected_ipv6="$(public_ipv6)"
    IPV6_NODE_ADDRESS="$(read_default "SERVER_IP_IPv6" "$detected_ipv6")"
  fi
  [[ -n "$IPV6_NODE_ADDRESS" ]] || die "IPv6 地址不能为空。"
  normalized_ipv6="$(normalize_ipv6 "$IPV6_NODE_ADDRESS")" || die "IPv6 地址校验失败。"

  line
  printf '%b\n' "${CYAN}${BOLD}IPv6 节点维护模式${RESET}"
  printf '  项目目录：%s\n' "$project_dir"
  printf '  IPv6 地址：%s\n' "$normalized_ipv6"
  printf '%b\n' "${DIM}只复用现有 VLESS/Reality 身份并补充 v6；不会执行 SSH 初始化或重置密钥。${RESET}"

  RRB_PROJECT_DIR="$project_dir" \
  RRB_CONFIG_FILE="$project_dir/config.env" \
  RRB_VERBOSE="$RRB_VERBOSE" \
    bash "$project_dir/scripts/16-add-ipv6-node.sh" --address "$normalized_ipv6"
}

main() {
  parse_args "$@"
  if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
    die "请使用 root 运行，例如：sudo bash install.sh"
  fi
  if [[ "$INSTALL_MODE" == "full" || -z "$IPV6_NODE_ADDRESS" ]]; then
    init_input_tty
  fi
  banner
  local project_dir default_ip
  project_dir="$(ensure_project)"
  if [[ "$INSTALL_MODE" == "ipv6-node" ]]; then
    run_ipv6_node_flow "$project_dir"
    return 0
  fi
  default_ip="$(public_ipv4)"

  line
  printf '%b\n' "${CYAN}${BOLD}基础信息${RESET}"
  local server_alias server_ip_ipv4 server_ip_ipv6 ssh_port admin_pubkey direct_port enable_subscription_server subscription_port subscription_target reset_proxy_keys reset_proxy_keys_default home_port_start home_port_end enable_custom_domain custom_domain cloudflare_zone_name le_email cloudflare_api_token nginx_fallback_port
  reset_proxy_keys_default="$(config_env_value "$project_dir/config.env" "RESET_PROXY_KEYS" "false")"
  server_alias="$(read_default "SERVER_ALIAS" "")"
  server_ip_ipv4="$(read_default "SERVER_IP_IPv4" "$default_ip")"
  server_ip_ipv6="$(read_default "SERVER_IP_IPv6" "")"
  ssh_port="$(read_default "SSH_PORT" "22")"
  direct_port="$(read_default "DIRECT_PORT" "443")"

  line
  printf '%b\n' "${CYAN}${BOLD}自有域名配置${RESET}"
  printf '%b\n' "${DIM}默认假设域名托管在 Cloudflare，DNS 记录保持灰云 / DNS only；公网 tcp/443 由 sing-box 监听，本机 8443 给 Nginx fallback。${RESET}"
  enable_custom_domain="$(read_bool_value "ENABLE_CUSTOM_DOMAIN" "false")"
  custom_domain=""
  cloudflare_zone_name=""
  le_email=""
  cloudflare_api_token=""
  nginx_fallback_port="8443"
  if [[ "$enable_custom_domain" == "true" ]]; then
    custom_domain="$(read_default "CUSTOM_DOMAIN" "")"
    cloudflare_zone_name="$(read_default "CLOUDFLARE_ZONE_NAME" "$(infer_cloudflare_zone_name "$custom_domain")")"
    le_email="$(read_default "LE_EMAIL" "")"
    cloudflare_api_token="$(read_secret_default "CLOUDFLARE_API_TOKEN" "")"
    nginx_fallback_port="$(read_default "NGINX_FALLBACK_PORT" "8443")"
    [[ -n "$custom_domain" ]] || die "CUSTOM_DOMAIN 不能为空。"
    [[ -n "$cloudflare_zone_name" ]] || die "CLOUDFLARE_ZONE_NAME 不能为空。"
    [[ -n "$le_email" ]] || die "LE_EMAIL 不能为空。"
    [[ -n "$cloudflare_api_token" ]] || die "CLOUDFLARE_API_TOKEN 不能为空；需要 Cloudflare DNS-01 申请证书。"
    [[ "$direct_port" == "443" ]] || die "启用自有域名时 DIRECT_PORT 必须为 443。"
  fi

  line
  printf '%b\n' "${CYAN}${BOLD}节点订阅配置${RESET}"
  printf '%b\n' "${DIM}RESET_PROXY_KEYS=false 会保留现有 VLESS UUID、Reality keypair 和 short-id；修改落地机时通常保持 false。只有需要作废旧节点时才设为 true。${RESET}"
  reset_proxy_keys="$(read_bool_value "RESET_PROXY_KEYS" "$reset_proxy_keys_default")"
  enable_subscription_server="$(read_bool_value "ENABLE_SUBSCRIPTION_SERVER" "true")"
  subscription_port=""
  subscription_target="ClashMeta"
  if [[ "$enable_subscription_server" == "true" ]]; then
    if [[ "$enable_custom_domain" == "true" ]]; then
      subscription_port="$(read_default "SUBSCRIPTION_PORT（可选公网直连 HTTP 订阅端口；留空则只显示域名订阅）" "")"
    else
      subscription_port="$(read_default "SUBSCRIPTION_PORT" "51040")"
    fi
  fi
  home_port_start="$(read_default "HOME_PORT_START" "51043")"
  home_port_end="65535"

  if [[ -z "$server_alias" && -n "$custom_domain" ]]; then
    server_alias="$custom_domain"
  fi
  [[ -n "$server_alias" ]] || die "SERVER_ALIAS 不能为空。"
  [[ -n "$server_ip_ipv4" || -n "$server_ip_ipv6" ]] || die "SERVER_IP_IPv4 和 SERVER_IP_IPv6 至少填写一个。"

  ADMIN_KEYS_FILE="$(mktemp)"
  ROWS_FILE="$(mktemp)"
  UPSTREAM_ROWS_FILE="$(mktemp)"
  trap 'rm -f "${ADMIN_KEYS_FILE:-}" "${ROWS_FILE:-}" "${UPSTREAM_ROWS_FILE:-}"' EXIT
  ADMIN_USER="root" collect_admin_pubkeys "$ADMIN_KEYS_FILE"
  admin_pubkey="$(cat "$ADMIN_KEYS_FILE")"
  collect_home_proxies "$ROWS_FILE"
  collect_upstream_nodes "$UPSTREAM_ROWS_FILE" "$ROWS_FILE" "$home_port_start" "$home_port_end"

  write_config "$project_dir" "$server_alias" "$server_ip_ipv4" "$server_ip_ipv6" "$ssh_port" "$admin_pubkey" "$direct_port" "$enable_subscription_server" "$subscription_port" "$subscription_target" "$reset_proxy_keys" "$home_port_start" "$home_port_end" "$enable_custom_domain" "$custom_domain" "$cloudflare_zone_name" "$le_email" "$cloudflare_api_token" "$nginx_fallback_port"
  write_home_proxies "$project_dir" "$ROWS_FILE"
  write_upstream_nodes "$project_dir" "$UPSTREAM_ROWS_FILE"
  run_install_flow "$project_dir" "$server_ip_ipv4" "$server_ip_ipv6" "$ssh_port" "$enable_subscription_server" "$subscription_port" "$subscription_target" "$enable_custom_domain" "$custom_domain"
}

main "$@"
