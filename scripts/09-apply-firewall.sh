#!/usr/bin/env bash
# 配置 UFW：只开放 SSH、443/DIRECT_PORT 和 CSV 实际使用端口。
set -Eeuo pipefail
PHASE_NAME="firewall"
source "${RRB_PROJECT_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}/scripts/00-lib.sh"
require_root
load_config
require_supported_os

if [[ "$ENABLE_UFW" != "true" ]]; then
  info "ENABLE_UFW=false，跳过防火墙配置。"
  exit 0
fi

bash "$SCRIPT_DIR/06-install-ufw.sh"

mapfile -t PORTS < <(python3 "$SCRIPT_DIR/08-generate-singbox-config.py" --config-env "$RRB_CONFIG_FILE" --print-ports)
[[ "${#PORTS[@]}" -ge 1 ]] || die "未解析到 VLESS+Reality 端口。"

cleanup_rrb_ufw_rules() {
  local desired_file stale_file status_file number
  desired_file="$(mktemp)"
  stale_file="$(mktemp)"
  status_file="$(mktemp)"

  printf '%s/tcp\n' "$SSH_PORT" >"$desired_file"
  for port in "${PORTS[@]}"; do
    printf '%s/tcp\n' "$port" >>"$desired_file"
  done
  if subscription_public_enabled; then
    printf '%s/tcp\n' "$SUBSCRIPTION_PORT" >>"$desired_file"
  fi

  if ! command -v ufw >/dev/null 2>&1; then
    log "DRY-RUN: ufw 尚未安装，跳过本项目旧规则计算。"
    rm -f "$desired_file" "$stale_file" "$status_file"
    return 0
  fi

  if ! ufw status numbered >"$status_file" 2>/dev/null; then
    warn "读取 UFW 规则失败，跳过本项目旧规则清理。"
    rm -f "$desired_file" "$stale_file" "$status_file"
    return 0
  fi

  python3 - "$desired_file" "$status_file" >"$stale_file" <<'PY'
import re
import sys
from pathlib import Path

desired = {line.strip() for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines() if line.strip()}
stale = []
for line in Path(sys.argv[2]).read_text(encoding="utf-8", errors="replace").splitlines():
    if "reality-relay-bootstrap" not in line:
        continue
    number_match = re.search(r"^\[\s*(\d+)\]", line)
    port_match = re.search(r"\]\s+(\d+)/(tcp|udp)", line)
    if not number_match or not port_match:
        continue
    key = f"{port_match.group(1)}/{port_match.group(2)}"
    if key not in desired:
        stale.append(int(number_match.group(1)))
for number in sorted(set(stale), reverse=True):
    print(number)
PY

  if [[ -s "$stale_file" ]]; then
    info "清理不再使用的本项目 UFW 规则。"
    if is_dry_run; then
      log "DRY-RUN: 将删除本项目旧 UFW 规则编号：$(tr '\n' ' ' <"$stale_file")"
      rm -f "$desired_file" "$stale_file" "$status_file"
      return 0
    fi
    while IFS= read -r number; do
      [[ -n "$number" ]] || continue
      run ufw --force delete "$number"
    done <"$stale_file"
  fi
  rm -f "$desired_file" "$stale_file" "$status_file"
}

cleanup_rrb_ufw_rules

info "准备开放实际使用的 VLESS+Reality TCP 端口：${PORTS[*]}"
for port in "${PORTS[@]}"; do
  validate_port_value="$port"
  [[ "$validate_port_value" =~ ^[0-9]+$ ]] || die "端口不是数字：$port"
  run ufw allow "${port}/tcp" comment "reality-relay-bootstrap sing-box VLESS+Reality ${port}"
done

if subscription_public_enabled; then
  info "准备开放订阅 HTTP 端口：$SUBSCRIPTION_PORT"
  run ufw allow "${SUBSCRIPTION_PORT}/tcp" comment "reality-relay-bootstrap subscription ${SUBSCRIPTION_PORT}"
elif [[ "$ENABLE_SUBSCRIPTION_SERVER" == "true" ]]; then
  info "未设置公网订阅端口；订阅服务仅本机监听 tcp/$SUBSCRIPTION_LISTEN_PORT 并通过 443 fallback 反代。"
fi

if is_dry_run; then
  log "DRY-RUN: ufw --force enable && ufw status verbose"
else
  ufw --force enable >>"$LOG_FILE" 2>&1
  if [[ "$RRB_VERBOSE" == "true" ]]; then
    ufw status verbose 2>&1 | tee -a "$LOG_FILE"
  else
    ufw status verbose >>"$LOG_FILE" 2>&1
  fi
fi

if is_alpine; then service_enable ufw; fi

cat <<EOF

UFW 已按最小端口开放。服务商安全组/云防火墙也必须手动放行：

  SSH:        tcp/$SSH_PORT
$(printf '  VLESS+Reality:     tcp/%s\n' "${PORTS[@]}")
$(if subscription_public_enabled; then printf '  Subscription:       tcp/%s\n' "$SUBSCRIPTION_PORT"; fi)

不要无脑开放从 ${HOME_PORT_START} 起的整段端口，除非你确认所有端口都会使用。
启用 UFW 后请另开窗口测试 SSH：
$(if [[ -n "$(server_ipv4_hosts)" ]]; then
  printf 'IPv4:\n'
  for host in $(server_ipv4_hosts); do
    printf '  ssh -p %s %s@%s\n' "$SSH_PORT" "$ADMIN_USER" "$host"
  done
  printf '\n'
fi)
$(if [[ -n "$(server_ipv6_hosts)" ]]; then
  printf '\nIPv6:\n'
  for host in $(server_ipv6_hosts); do
    printf '  ssh -p %s %s@%s\n' "$SSH_PORT" "$ADMIN_USER" "$host"
  done
  printf '\n'
fi)
EOF
