#!/usr/bin/env bash
# 一键交互式安装入口：下载/定位项目、生成默认配置，并按安全阶段执行部署。
set -Eeuo pipefail
umask 077

REPO_URL="${RRB_REPO_URL:-https://github.com/suyi-92/SuRelay.git}"
INSTALL_DIR="${RRB_INSTALL_DIR:-/opt/SuRelay}"
# Existing deployments must be updated before creating a second checkout.
if [[ -z "${RRB_INSTALL_DIR:-}" && ! -e "$INSTALL_DIR" && -d /opt/reality-relay-bootstrap ]]; then
  INSTALL_DIR=/opt/reality-relay-bootstrap
fi
DEFAULT_BRANCH="${RRB_BRANCH:-main}"
RUN_PHASES="${RRB_RUN_PHASES:-true}"
RRB_VERBOSE="${RRB_VERBOSE:-false}"
INSTALL_LOG_FILE="${RRB_INSTALL_LOG_FILE:-/tmp/surelay-install.log}"
INSTALL_MODE="full"
REDEPLOY_HANDOFF_FILE="${RRB_REDEPLOY_HANDOFF:-/root/surelay-backups/redeploy-handoff.json}"
IPV6_NODE_ADDRESS=""
INPUT_TTY=""
MAINTENANCE_LOCK_FILE="${RRB_MAINTENANCE_LOCK_FILE:-/run/surelay.lock}"
MAINTENANCE_LOCK_FD=""
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
info() { printf '%b\n' "$*" >&2; }
warn() { printf '%b\n' "${YELLOW}提示：${RESET}$*" >&2; }
die() { printf '%b\n' "${RED}ERROR${RESET} $*" >&2; exit 1; }

usage() {
  cat <<'EOF'
用法：
  sudo bash install.sh
  sudo bash install.sh --update
  sudo bash install.sh --security-only
  sudo bash install.sh --uninstall
  sudo bash install.sh --reset
  sudo bash install.sh --redeploy
  sudo bash install.sh -6 [IPv6地址]
  sudo bash install.sh --add-ipv6-node [IPv6地址]

选项：
      --update              更新脚本并安装 sr 命令，保留现有服务、配置和节点身份
      --security-only       只配置 SSH、fail2ban 和 UFW，不安装 sing-box、订阅或节点
      --uninstall           备份后卸载本项目部署，保留 SSH 和系统软件包
      --reset               备份并卸载项目配置与服务，再重新填写配置安装
      --redeploy            清空 SuRelay 属性并重新引导部署，新 SSH 验证后关闭旧入口
  -6, --add-ipv6-node [地址]  在已有部署上启用 IPv6 监听，并单独生成 v6 VLESS 节点
      --ipv6-address <地址>   与 -6 相同，但要求显式提供 IPv6 地址
  -h, --help                 显示帮助

-6 是维护模式：要求现有 config.env、sing-box 和 Reality 密钥都已就绪；
不会重跑 SSH 初始化，也不会轮换 VLESS UUID、Reality keypair 或 short-id。
--reset 会使旧节点和订阅失效；显示清单后需输入 RESET。保留 SSH 登录通道、
系统软件包和其他证书；不能与 -6 同时使用。
--redeploy 会重新生成端口和节点身份，要求明确录入管理员公钥（可填写原公钥）；
验证新端口与本次公钥登录成功后，清理未选用的旧 SuRelay 公钥、SSH 设置和端口规则。
确认词为 REDEPLOY；存在已清理的未完成交接时，继续原安装。
--security-only 会创建安全配置并执行 SSH 二阶段加固、fail2ban 和 UFW；不会创建
sing-box、订阅服务、Reality 密钥或任何客户端节点。已有项目配置不会被重置。
EOF
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --uninstall)
        [[ "$INSTALL_MODE" == "full" ]] || die "--uninstall 不能与其他维护模式合用。"
        INSTALL_MODE="uninstall"
        shift
        ;;
      --update)
        [[ "$INSTALL_MODE" == "full" ]] || die "--update 不能与其他维护模式合用。"
        INSTALL_MODE="update"
        shift
        ;;
      --security-only)
        [[ "$INSTALL_MODE" == "full" ]] || die "--security-only 不能与其他维护模式合用。"
        INSTALL_MODE="security-only"
        shift
        ;;
      --reset)
        [[ "$INSTALL_MODE" != "ipv6-node" ]] || die "--reset 不能与 -6 同时使用。"
        [[ "$INSTALL_MODE" == "full" ]] || die "--reset 不能与其他维护模式合用。"
        INSTALL_MODE="reset"
        shift
        ;;
      --redeploy)
        [[ "$INSTALL_MODE" == "full" ]] || die "--redeploy 不能与其他维护模式合用。"
        INSTALL_MODE="redeploy"
        shift
        ;;
      -6|--add-ipv6-node)
        [[ "$INSTALL_MODE" != "reset" ]] || die "--reset 不能与 -6 同时使用。"
        [[ "$INSTALL_MODE" == "full" ]] || die "-6 不能与其他维护模式合用。"
        INSTALL_MODE="ipv6-node"
        if [[ $# -ge 2 && "${2:-}" != -* ]]; then
          IPV6_NODE_ADDRESS="$2"
          shift 2
        else
          shift
        fi
        ;;
      --ipv6-address)
        [[ "$INSTALL_MODE" != "reset" ]] || die "--reset 不能与 -6 同时使用。"
        [[ "$INSTALL_MODE" == "full" ]] || die "--ipv6-address 不能与其他维护模式合用。"
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

acquire_maintenance_lock() {
  # A panel already owns this lock when it launches install.sh for install or
  # reset. Keep that ownership across the child process instead of opening a
  # second flock on the same file.
  if [[ "${RRB_MANAGEMENT_LOCK_HELD:-false}" == "true" ]]; then
    return 0
  fi
  command -v flock >/dev/null 2>&1 || die "找不到 flock，无法防止安装与管理操作并发运行。"
  exec {MAINTENANCE_LOCK_FD}>"$MAINTENANCE_LOCK_FILE" \
    || die "无法打开维护锁：$MAINTENANCE_LOCK_FILE"
  if ! flock -n "$MAINTENANCE_LOCK_FD"; then
    die "另一个 SuRelay 安装或管理会话正在运行，请先等待它结束。"
  fi
  export RRB_MANAGEMENT_LOCK_HELD=true
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

prepare_install_log() {
  [[ ! -L "$INSTALL_LOG_FILE" ]] || die "安装日志不能为符号链接：$INSTALL_LOG_FILE"
  if [[ -e "$INSTALL_LOG_FILE" ]]; then
    [[ -f "$INSTALL_LOG_FILE" && -O "$INSTALL_LOG_FILE" ]] \
      || die "安装日志必须是当前用户拥有的普通文件：$INSTALL_LOG_FILE"
  else
    (umask 077; set -o noclobber; : >"$INSTALL_LOG_FILE") \
      || die "无法创建安装日志：$INSTALL_LOG_FILE"
  fi
  chmod 600 "$INSTALL_LOG_FILE" || die "无法设置安装日志权限：$INSTALL_LOG_FILE"
}

detail() {
  prepare_install_log
  printf '%s\n' "$*" >>"$INSTALL_LOG_FILE"
  if [[ "$RRB_VERBOSE" == "true" ]]; then info "$*"; fi
}

run_logged() {
  prepare_install_log
  if [[ "$RRB_VERBOSE" == "true" ]]; then
    "$@"
  else
    "$@" >>"$INSTALL_LOG_FILE" 2>&1
  fi
}

run_install_step() {
  local label="$1" status=0 log_offset
  shift
  prepare_install_log
  log_offset="$(wc -c <"$INSTALL_LOG_FILE")"
  info "开始：$label"
  printf '\n--- %s ---\n' "$label" >>"$INSTALL_LOG_FILE"
  run_logged "$@" || status=$?
  if (( status != 0 )); then
    warn "阶段失败：$label（退出码 $status）；详细日志：$INSTALL_LOG_FILE"
    return "$status"
  fi
  if [[ "$RRB_VERBOSE" == "true" ]]; then
    info "完成：$label"
  elif tail -c "+$((log_offset + 1))" "$INSTALL_LOG_FILE" \
      | grep -Ei '(^|[[:space:]])WARN(ING)?([ :]|$)|警告|告警' >/dev/null; then
    warn "$label 已完成，有提示请查看日志。"
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
  # A terminating parent can send HUP while the terminal is being restored.
  trap 'trap "" HUP INT TERM; stty "$tty_state" <&"$tty_fd" 2>/dev/null || true' EXIT
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

  section "管理员 SSH 公钥"
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

ssh_connection_port() {
  local port="${SSH_CONNECTION:-}"
  port="${port##* }"
  if [[ "$port" =~ ^[0-9]+$ ]] && (( port >= 1 && port <= 65535 )); then
    printf '%s\n' "$port"
  fi
}

detect_current_ssh_port() {
  local port
  port="$(ssh_connection_port)"
  if [[ -n "$port" ]]; then
    printf '%s\n' "$port"
    return 0
  fi
  if command -v sshd >/dev/null 2>&1; then
    port="$(sshd -T 2>/dev/null | awk '$1 == "port" {print $2; exit}')"
    if [[ "$port" =~ ^[0-9]+$ ]] && (( port >= 1 && port <= 65535 )); then
      printf '%s\n' "$port"
      return 0
    fi
  fi
  printf '22\n'
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
  local package_log="/tmp/surelay-install-packages.log"
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
    [[ -z "$(git -C "$INSTALL_DIR" status --porcelain)" ]] || die "项目有未提交修改；保留现场，停止更新。"
    [[ "$(git -C "$INSTALL_DIR" branch --show-current)" == "$DEFAULT_BRANCH" ]] || die "当前分支不是 $DEFAULT_BRANCH，停止自动更新。"
    run_logged git -C "$INSTALL_DIR" fetch --all --prune
    [[ -z "$(git -C "$INSTALL_DIR" rev-list "origin/$DEFAULT_BRANCH..HEAD")" ]] || die "存在本地独有提交，停止更新。"
    run_logged git -C "$INSTALL_DIR" merge --ff-only "origin/$DEFAULT_BRANCH"
  else
    mkdir -p "$(dirname "$INSTALL_DIR")"
    run_logged git clone --branch "$DEFAULT_BRANCH" "$REPO_URL" "$INSTALL_DIR"
  fi
  printf '%s\n' "$INSTALL_DIR"
}

write_config() {
  local project_dir="$1" server_alias="$2" server_ip_ipv4="$3" server_ip_ipv6="$4" ssh_port="$5" admin_pubkey="$6" direct_port="$7" enable_subscription_server="$8" subscription_port="$9" subscription_target="${10}" reset_proxy_keys="${11}" home_port_start="${12}" home_port_end="${13}" enable_custom_domain="${14}" custom_domain="${15}" cloudflare_zone_name="${16}" le_email="${17}" cloudflare_api_token="${18}" nginx_fallback_port="${19}" security_only="${20:-false}"
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
SURELAY_SECURITY_ONLY=$(shell_quote "$security_only")
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
RRB_STATE_DIR="/etc/surelay"
ENABLE_CUSTOM_DOMAIN=$(shell_quote "$enable_custom_domain")
CUSTOM_DOMAIN=$(shell_quote "$custom_domain")
CUSTOM_DOMAIN_PROVIDER="cloudflare"
LE_EMAIL=$(shell_quote "$le_email")
LE_STAGING="false"
CLOUDFLARE_API_TOKEN=$(shell_quote "$cloudflare_api_token")
CLOUDFLARE_API_TOKEN_FILE="/etc/surelay/cloudflare.ini"
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
NGINX_FALLBACK_ROOT="/var/www/surelay-fallback"
NGINX_FALLBACK_CONF="auto"
SINGBOX_CONFIG_PATH="/etc/sing-box/config.json"

PROXY_PROTOCOL="vless-reality"
PROXY_IP_VERSION=$(shell_quote "$proxy_ip_version")
RESET_PROXY_KEYS=$(shell_quote "$reset_proxy_keys")
VLESS_UUID_PATH="/etc/surelay/vless-uuid.txt"
VLESS_FLOW="xtls-rprx-vision"
REALITY_PRIVATE_KEY_PATH="/etc/surelay/reality-private.key"
REALITY_PUBLIC_KEY_PATH="/etc/surelay/reality-public.key"
REALITY_SHORT_ID_PATH="/etc/surelay/reality-short-id.txt"
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
SUBSCRIPTION_DIR="/etc/surelay/subscription"
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
  local phase="$1" label="$1"
  case "$phase" in
    preflight) label="检查系统与依赖" ;;
    ssh-phase1) label="准备 SSH 公钥登录" ;;
    ssh-final) label="应用 SSH 最终加固" ;;
    fail2ban) label="安装并配置 fail2ban" ;;
    singbox) label="安装 sing-box、生成节点及配置域名" ;;
    firewall) label="应用防火墙规则" ;;
    validate)
      if [[ "${SURELAY_SECURITY_ONLY:-false}" == "true" || "$INSTALL_MODE" == "security-only" ]]; then
        label="验证 SSH、防火墙和登录防护"
      else
        label="验证服务与节点"
      fi
      ;;
  esac
  RRB_SHOW_RESULT=false run_install_step "$label" bash bootstrap.sh --phase "$phase"
}

run_install_flow() {
  local project_dir="$1" ssh_host current_ssh_port configured_ssh_port security_only_mode=false
  local ssh_confirm_prompt="已确认公钥登录成功，继续关闭密码登录？" ssh_confirm_default=yes
  cd "$project_dir"
  if [[ "$RUN_PHASES" != "true" ]]; then
    info "配置已就绪：$project_dir/config.env；未执行部署。"
    return 0
  fi
  # Use the saved settings, including ports and file paths customized through config.env.
  source "$project_dir/config.env"
  export RRB_INSTALL_FLOW=true
  configured_ssh_port="${SSH_PORT:-22}"
  if [[ "$INSTALL_MODE" == "security-only" || "${SURELAY_SECURITY_ONLY:-false}" == "true" || "${RRB_SECURITY_ONLY:-false}" == "true" ]]; then
    security_only_mode=true
    export RRB_SECURITY_ONLY=true
  fi
  current_ssh_port="$(ssh_connection_port)"
  if [[ -n "$current_ssh_port" && "$current_ssh_port" != "$configured_ssh_port" ]]; then
    detail "当前 SSH 会话使用端口 $current_ssh_port，但 config.env 记录为 $configured_ssh_port；验证命令将按配置端口执行。"
  fi
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
  info "详细日志：$INSTALL_LOG_FILE"
  run_install_phase preflight
  if [[ "$INSTALL_MODE" == "reset" || "${RRB_RESET_INSTALL:-false}" == "true" ]]; then
    # Keep the working SSH policy until the user has verified the new keys.
    run_install_step "准备管理员公钥" env RRB_PROJECT_DIR="$project_dir" RRB_CONFIG_FILE="$project_dir/config.env" \
      bash "$project_dir/scripts/02-setup-root-ssh.sh"
    run_install_step "准备 SSH 验证端口" env RRB_PROJECT_DIR="$project_dir" RRB_CONFIG_FILE="$project_dir/config.env" RRB_RESET_INSTALL=true \
      bash "$project_dir/scripts/03-ssh-hardening-phase1.sh"
  else
    run_install_phase ssh-phase1
  fi

  section "SSH 公钥登录验证"
  printf '另开终端执行，输出 root 后确认；请保留当前 SSH 窗口：\n'
  printf 'ssh -p %q -o BatchMode=yes -o PreferredAuthentications=publickey -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no root@%q whoami\n' "$configured_ssh_port" "$ssh_host"
  if [[ "${RRB_REDEPLOY:-false}" == true ]]; then
    ssh_confirm_prompt="已用本次填写的公钥通过新端口登录，清理旧入口并继续？"
    ssh_confirm_default=no
  fi
  if read_yes_no "$ssh_confirm_prompt" "$ssh_confirm_default"; then
    section "应用配置与验证"
    CONFIRM_ROOT_KEY_LOGIN=yes run_install_phase ssh-final
  else
    if [[ "${RRB_REDEPLOY:-false}" == true ]]; then
      warn "引导部署已停在新 SSH 验证；旧登录通道仍保留。解决登录后运行 sr install 继续同一部署。"
    elif [[ "$INSTALL_MODE" == "reset" || "${RRB_RESET_INSTALL:-false}" == "true" ]]; then
      warn "重装已停在公钥验证；解决登录后使用 RRB_RESET_INSTALL=true bash install.sh 继续。"
    else
      warn "安装已暂停，节点尚未部署；修改配置后运行 sr install 继续。"
    fi
    INSTALL_PAUSED=true
    return 0
  fi

  run_install_phase fail2ban
  if [[ "$security_only_mode" == true ]]; then
    run_install_phase firewall
    run_install_phase validate
    printf '\n安全防护部署完成：未安装 sing-box、订阅服务或客户端节点。\n'
    return 0
  fi
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
  run_install_step "启用 IPv6 节点" env RRB_PROJECT_DIR="$project_dir" RRB_CONFIG_FILE="$project_dir/config.env" \
    RRB_SHOW_RESULT=false "${command[@]}"
  python3 "$project_dir/scripts/11-output-nodes.py" --config-env "$project_dir/config.env" \
    --generator "$project_dir/scripts/08-generate-singbox-config.py" --skip-node-files --quiet --print-summary
}

configure_install() {
  local project_dir="$1" default_ip
  if [[ -f "$project_dir/config.env" ]]; then
    info "沿用现有 config.env 和出口列表；修改方法见 README。"
    if [[ "$INSTALL_MODE" != "security-only" ]] && ! grep -Eq "^[[:space:]]*SURELAY_SECURITY_ONLY=(true|'true')" "$project_dir/config.env"; then
      ensure_proxy_lists "$project_dir"
    fi
    return 0
  fi
  default_ip="$(public_ipv4)"
  local -a wizard_args=(--project-dir "$project_dir" --ipv4 "$default_ip")
  [[ "$INSTALL_MODE" != "security-only" ]] || wizard_args+=(--security-only)
  RRB_INPUT_TTY="$INPUT_TTY" python3 "$project_dir/scripts/surelay_install.py" "${wizard_args[@]}" \
    || die "安装向导未完成；未执行部署。"
  if [[ "$INSTALL_MODE" != "security-only" ]]; then
    ensure_proxy_lists "$project_dir"
  fi
}

resume_redeploy() {
  local project_dir="$1" handoff_status
  handoff_status="$(python3 "$project_dir/scripts/surelay_redeploy.py" --resume-status \
    --project-dir "$project_dir" --handoff "$REDEPLOY_HANDOFF_FILE")" \
    || die "引导部署交接记录未通过检查；保留现场，请按备份说明恢复。"
  case "$handoff_status" in
    none|finalized) return 0 ;;
    removed)
      [[ "$INSTALL_MODE" == full || "$INSTALL_MODE" == redeploy ]] \
        || die "引导式重新布置尚未完成，请运行 sr install 继续；不能切换到其他部署模式。"
      # Continue the original install instead of clearing an already removed deployment.
      INSTALL_MODE=full
      export RRB_REDEPLOY=true RRB_RESET_INSTALL=true RRB_SECURITY_ONLY=false
      export RRB_REDEPLOY_HANDOFF="$REDEPLOY_HANDOFF_FILE"
      detail "继续尚未完成的引导部署；新 SSH 验证后才清理旧登录入口。"
      ;;
    *) die "未知的引导部署状态，停止安装：$handoff_status" ;;
  esac
}

main() {
  parse_args "$@"
  if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
    die "请使用 root 运行，例如：sudo bash install.sh"
  fi
  acquire_maintenance_lock
  if [[ ( "$INSTALL_MODE" == "reset" || "$INSTALL_MODE" == "redeploy" ) && "$RUN_PHASES" != "true" ]]; then
    die "--$INSTALL_MODE 必须执行完整重装，不能与 RRB_RUN_PHASES=false 同时使用。"
  fi
  if [[ "$INSTALL_MODE" != "update" && ( "$INSTALL_MODE" == "full" || -z "$IPV6_NODE_ADDRESS" ) ]]; then
    init_input_tty
  fi
  detail "SuRelay"
  local project_dir
  ensure_entry_dependencies
  project_dir="$(ensure_project)"
  (export RRB_PROJECT_DIR="$project_dir"; source "$project_dir/scripts/00-lib.sh"; require_supported_os)
  if [[ "$INSTALL_MODE" == "uninstall" ]]; then
    python3 "$project_dir/scripts/17-reset-install.py" --project-dir "$project_dir" --uninstall --apply
    return 0
  fi
  if [[ "$INSTALL_MODE" == "update" ]]; then
    # A local invocation has not gone through ensure_project's remote update.
    bash "$project_dir/surelay.sh" update
    info "更新完成。输入 sr 打开 SuRelay；节点与订阅保持原样。"
    return 0
  fi
  if [[ "$INSTALL_MODE" == "full" || "$INSTALL_MODE" == "redeploy" || "$INSTALL_MODE" == "security-only" || "$INSTALL_MODE" == "ipv6-node" ]]; then
    resume_redeploy "$project_dir"
  fi
  run_logged bash "$project_dir/surelay.sh" --install-command
  if [[ "$INSTALL_MODE" == "ipv6-node" ]]; then
    run_ipv6_node_flow "$project_dir"
    return 0
  fi
  if [[ "$INSTALL_MODE" == "reset" ]]; then
    # Read the effective SSH port without sourcing or validating stale config.
    RESET_SSH_PORT="${SSH_CONNECTION:-}"
    RESET_SSH_PORT="${RESET_SSH_PORT##* }"
    if [[ ! "$RESET_SSH_PORT" =~ ^[0-9]+$ ]]; then
      RESET_SSH_PORT="$(sshd -T 2>/dev/null | awk '$1 == "port" {print $2; exit}' || true)"
    fi
    RESET_SSH_PORT="${RESET_SSH_PORT:-22}"
    RRB_INPUT_TTY="$INPUT_TTY" python3 "$project_dir/scripts/17-reset-install.py" --project-dir "$project_dir" --apply
    export RRB_RESET_INSTALL=true
  fi
  if [[ "$INSTALL_MODE" == "redeploy" ]]; then
    RRB_INPUT_TTY="$INPUT_TTY" python3 "$project_dir/scripts/17-reset-install.py" \
      --project-dir "$project_dir" --redeploy --apply --handoff-file "$REDEPLOY_HANDOFF_FILE"
    resume_redeploy "$project_dir"
  fi
  configure_install "$project_dir"
  run_install_flow "$project_dir"
  if [[ "${INSTALL_PAUSED:-false}" != true && "$RUN_PHASES" == true ]]; then
    info "安装完成。输入 sr 打开 SuRelay 管理菜单。"
  fi
}

main "$@"
