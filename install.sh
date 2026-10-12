#!/usr/bin/env bash
# 一键交互式安装入口：下载/定位项目、生成默认配置，并按安全阶段执行部署。
set -Eeuo pipefail
umask 077

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
YELLOW=$'\033[33m'
BLUE=$'\033[34m'
CYAN=$'\033[36m'
RESET=$'\033[0m'
if [[ ! -t 1 || -n "${NO_COLOR:-}" || "${TERM:-}" == "dumb" ]]; then
  BOLD='' DIM='' RED='' YELLOW='' BLUE='' CYAN='' RESET=''
fi

line() { printf '%b\n' "${DIM}────────────────────────────────────────────────────────────${RESET}"; }
section() {
  printf '\n'
  line
  printf '%b\n' "${CYAN}${BOLD}$*${RESET}"
  line
}
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
    if ! IFS= read -r value <"$INPUT_TTY"; then
      printf '\n' >"$INPUT_TTY"
      warn "确认输入已中断，暂停操作。"
      return 1
    fi
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

public_ipv4() {
  local ip=""
  if command -v curl >/dev/null 2>&1; then
    ip="$(curl -4fsS --max-time 4 https://api.ipify.org 2>/dev/null || true)"
  fi
  if [[ ! "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    ip="$(ip -4 -o addr show scope global 2>/dev/null | awk '{sub(/\/.*/, "", $4); print $4; exit}')"
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

ensure_entry_dependencies() {
  local ID="" dependency missing=false
  for dependency in git curl python3; do
    command -v "$dependency" >/dev/null 2>&1 || missing=true
  done
  [[ "$missing" == true ]] || return 0
  # This runs before the checkout exists, so it cannot use scripts/00-lib.sh.
  # shellcheck disable=SC1090
  source "${RRB_OS_RELEASE_FILE:-/etc/os-release}"
  info "安装 git/curl/python3 基础依赖。"
  local package_log="/tmp/reality-relay-bootstrap-install-packages.log"
  case "$ID" in
    alpine)
      apk add --no-cache --wait 60 bash git curl ca-certificates python3 >"$package_log" 2>&1 </dev/null \
        || { tail -n 40 "$package_log" >&2; die "APK 基础依赖安装失败；请检查 main/community 软件源。"; }
      ;;
    ubuntu|debian)
      apt-get update -y >"$package_log" 2>&1 </dev/null \
        || { tail -n 40 "$package_log" >&2; die "apt-get update 失败。"; }
      DEBIAN_FRONTEND=noninteractive apt-get -o Dpkg::Options::=--force-confold install -y git curl ca-certificates python3 >>"$package_log" 2>&1 </dev/null \
        || { tail -n 60 "$package_log" >&2; die "安装基础依赖失败。"; }
      ;;
    *) die "安装入口不支持当前系统：$ID" ;;
  esac
}

ensure_project() {
  local self_dir
  self_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" 2>/dev/null && pwd -P || true)"
  if [[ -n "$self_dir" && -f "$self_dir/bootstrap.sh" && -f "$self_dir/config.example.env" ]]; then
    printf '%s\n' "$self_dir"
    return 0
  fi

  info "当前是一键远程执行模式，将项目安装到：$INSTALL_DIR"
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
INSTALL_SINGBOX_METHOD="auto"
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
NGINX_FALLBACK_CONF="auto"
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

ensure_proxy_lists() {
  local project_dir="$1"
  if [[ ! -e "$project_dir/home-proxies.csv" ]]; then
    printf 'tag,type,server,server_port,username,password,network,listen_port\n' >"$project_dir/home-proxies.csv"
    chmod 600 "$project_dir/home-proxies.csv"
  fi
  if [[ ! -e "$project_dir/upstream-nodes.txt" ]]; then
    printf 'tag,node_url,listen_port\n' >"$project_dir/upstream-nodes.txt"
    chmod 600 "$project_dir/upstream-nodes.txt"
  fi
}

run_install_phase() {
  local phase="$1" label="$1" status=0
  case "$phase" in
    preflight) label="检查系统与依赖" ;;
    ssh-phase1) label="准备 SSH 公钥登录" ;;
    ssh-final) label="应用 SSH 最终加固" ;;
    fail2ban) label="安装并配置 fail2ban" ;;
    singbox) label="安装 sing-box、生成节点及配置域名" ;;
    firewall) label="应用防火墙规则" ;;
    validate) label="验证服务与节点" ;;
  esac
  info "开始：$label"
  if [[ "$RRB_VERBOSE" == "true" ]]; then
    RRB_SHOW_RESULT=false bash bootstrap.sh --phase "$phase" || status=$?
  else
    # Routine stdout goes to the log; warnings and failures stay visible on stderr.
    RRB_SHOW_RESULT=false bash bootstrap.sh --phase "$phase" >>"$INSTALL_LOG_FILE" || status=$?
  fi
  if (( status != 0 )); then
    warn "阶段失败：$label（退出码 $status）；请查看 $INSTALL_LOG_FILE 和 /var/log/reality-relay-bootstrap.log。"
    return "$status"
  fi
  info "完成：$label"
}

run_install_flow() {
  local project_dir="$1" ssh_host
  cd "$project_dir"
  if [[ "$RUN_PHASES" != "true" ]]; then
    info "配置已就绪：$project_dir/config.env；未执行部署。"
    return 0
  fi
  # Use the saved settings, including ports and file paths customized through config.env.
  source "$project_dir/config.env"
  ssh_host="${SERVER_IP_IPV4:-${SERVER_IP_IPV6:-${SERVER_IP:-}}}"
  # Prefer the already working SSH address when it is one of the configured endpoints.
  if [[ -n "${SSH_CONNECTION:-}" ]]; then
    local _client_ip _client_port connected_host _connected_port
    read -r _client_ip _client_port connected_host _connected_port <<<"$SSH_CONNECTION"
    if [[ "$connected_host" == "${SERVER_IP_IPV4:-}" || "$connected_host" == "${SERVER_IP_IPV6:-}" ]]; then
      ssh_host="$connected_host"
    fi
  fi

  section "部署准备"
  info "开始部署；详细日志：$INSTALL_LOG_FILE"
  run_install_phase preflight
  run_install_phase ssh-phase1

  section "SSH 公钥登录验证"
  printf '另开终端执行，输出 root 后确认；请保留当前 SSH 窗口：\n'
  printf 'ssh -p %q -o BatchMode=yes -o PreferredAuthentications=publickey -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no root@%q whoami\n' "${SSH_PORT:-22}" "$ssh_host"
  if read_yes_no "已确认公钥登录成功，继续关闭密码登录？" yes; then
    section "应用配置与验证"
    CONFIRM_ROOT_KEY_LOGIN=yes run_install_phase ssh-final
  else
    warn "已停在 ssh-phase1；验证后按 README 执行 ssh-final。"
    return 0
  fi

  run_install_phase fail2ban
  run_install_phase singbox
  run_install_phase firewall
  run_install_phase validate
  python3 "$project_dir/scripts/11-output-nodes.py" --config-env "$project_dir/config.env" \
    --generator "$project_dir/scripts/08-generate-singbox-config.py" --skip-node-files --quiet --print-summary
}

run_ipv6_node_flow() {
  local project_dir="$1" detected_ipv6 normalized_ipv6
  [[ -f "$project_dir/config.env" ]] || die "-6 维护模式要求已有配置：$project_dir/config.env"
  [[ -x "$project_dir/scripts/16-add-ipv6-node.sh" || -f "$project_dir/scripts/16-add-ipv6-node.sh" ]] \
    || die "当前分支缺少 IPv6 节点维护脚本。"

  section "启用 IPv6 节点"
  if [[ -z "$IPV6_NODE_ADDRESS" ]]; then
    detected_ipv6="$(public_ipv6)"
    IPV6_NODE_ADDRESS="$(read_default "SERVER_IP_IPv6" "$detected_ipv6")"
  fi
  [[ -n "$IPV6_NODE_ADDRESS" ]] || die "未获取到公网 IPv6；请先在 VPS 控制台启用 IPv6，再执行 install.sh -6 <真实公网IPv6地址>。"
  normalized_ipv6="$(normalize_ipv6 "$IPV6_NODE_ADDRESS")" || die "IPv6 地址校验失败。"

  local -a command=(bash "$project_dir/scripts/16-add-ipv6-node.sh" --address "$normalized_ipv6")
  if [[ "$RRB_VERBOSE" == "true" ]]; then
    RRB_PROJECT_DIR="$project_dir" RRB_CONFIG_FILE="$project_dir/config.env" RRB_SHOW_RESULT=false "${command[@]}"
  else
    RRB_PROJECT_DIR="$project_dir" RRB_CONFIG_FILE="$project_dir/config.env" RRB_SHOW_RESULT=false "${command[@]}" >>"$INSTALL_LOG_FILE"
  fi
  python3 "$project_dir/scripts/11-output-nodes.py" --config-env "$project_dir/config.env" \
    --generator "$project_dir/scripts/08-generate-singbox-config.py" --skip-node-files --quiet --print-summary
}

configure_install() {
  local project_dir="$1" default_ip
  if [[ -f "$project_dir/config.env" ]]; then
    info "沿用现有 config.env 和出口列表；修改方法见 README。"
    ensure_proxy_lists "$project_dir"
    return 0
  fi
  default_ip="$(public_ipv4)"

  section "基础信息"
  local server_alias server_ip_ipv4 server_ip_ipv6 ssh_port admin_pubkey
  local enable_custom_domain custom_domain cloudflare_zone_name le_email cloudflare_api_token
  server_alias="$(read_default "SERVER_ALIAS" "")"
  server_ip_ipv4="$(read_default "SERVER_IP_IPv4" "$default_ip")"
  printf 'IPv6：填写或保留探测到的公网地址即启用；无地址可留空，安装后用 install.sh -6 补充。\n'
  server_ip_ipv6="$(read_default "SERVER_IP_IPv6（填入即启用）" "$(public_ipv6)")"
  ssh_port="$(read_default "SSH_PORT" "22")"

  section "Cloudflare 域名（灰云 DNS）"
  enable_custom_domain="$(read_bool_value "ENABLE_CUSTOM_DOMAIN" "false")"
  custom_domain=""
  cloudflare_zone_name=""
  le_email=""
  cloudflare_api_token=""
  if [[ "$enable_custom_domain" == "true" ]]; then
    custom_domain="$(read_default "CUSTOM_DOMAIN" "")"
    cloudflare_zone_name="$(read_default "CLOUDFLARE_ZONE_NAME" "$(infer_cloudflare_zone_name "$custom_domain")")"
    le_email="$(read_default "LE_EMAIL" "")"
    cloudflare_api_token="$(read_secret_default "CLOUDFLARE_API_TOKEN" "")"
    [[ -n "$custom_domain" && -n "$cloudflare_zone_name" && -n "$le_email" && -n "$cloudflare_api_token" ]] \
      || die "启用 CF 域名时，请完整填写域名、Zone、邮箱和 Token。"
  fi
  [[ -n "$server_alias" ]] || server_alias="$custom_domain"
  [[ -n "$server_alias" ]] || die "SERVER_ALIAS 不能为空。"
  [[ -n "$server_ip_ipv4" || -n "$server_ip_ipv6" ]] || die "IPv4 和 IPv6 至少填写一个。"
  if [[ -n "$server_ip_ipv6" ]]; then
    server_ip_ipv6="$(normalize_ipv6 "$server_ip_ipv6")"
  fi

  ADMIN_KEYS_FILE="$(mktemp)"
  trap 'rm -f "${ADMIN_KEYS_FILE:-}"' EXIT
  collect_admin_pubkeys "$ADMIN_KEYS_FILE"
  admin_pubkey="$(cat "$ADMIN_KEYS_FILE")"
  write_config "$project_dir" "$server_alias" "$server_ip_ipv4" "$server_ip_ipv6" "$ssh_port" "$admin_pubkey" \
    443 true '' ClashMeta false 51043 65535 "$enable_custom_domain" "$custom_domain" \
    "$cloudflare_zone_name" "$le_email" "$cloudflare_api_token" 8443
  ensure_proxy_lists "$project_dir"
}

main() {
  parse_args "$@"
  if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
    die "请使用 root 运行，例如：sudo bash install.sh"
  fi
  if [[ "$INSTALL_MODE" == "full" || -z "$IPV6_NODE_ADDRESS" ]]; then
    init_input_tty
  fi
  info "Reality Relay Bootstrap"
  local project_dir
  ensure_entry_dependencies
  project_dir="$(ensure_project)"
  (export RRB_PROJECT_DIR="$project_dir"; source "$project_dir/scripts/00-lib.sh"; require_supported_os)
  if [[ "$INSTALL_MODE" == "ipv6-node" ]]; then
    run_ipv6_node_flow "$project_dir"
    return 0
  fi
  configure_install "$project_dir"
  run_install_flow "$project_dir"
}

main "$@"
