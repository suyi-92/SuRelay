#!/usr/bin/env bash
# 配置 UFW：只开放 SSH、订阅和实际使用的 TCP/UDP 入口。
set -Eeuo pipefail
PHASE_NAME="firewall"
source "${RRB_PROJECT_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}/scripts/00-lib.sh"
require_root
load_config
require_supported_os

source_policy_plan='{"version":1,"ipv6":false,"entries":[]}'
source_guard_needed=false
source_core_was_active=false
source_subscription_was_active=false
if [[ "$SURELAY_SECURITY_ONLY" != "true" ]]; then
  source_policy_plan="$(python3 "$SCRIPT_DIR/08-generate-singbox-config.py" --config-env "$RRB_CONFIG_FILE" --print-source-policy)" || die "来源白名单计划校验失败。"
  source_guard_needed="$(printf '%s\n' "$source_policy_plan" | python3 -c 'import json,sys; print("true" if json.load(sys.stdin)["entries"] else "false")')" || die "来源白名单计划格式不正确。"
  [[ ! -e "$RRB_STATE_DIR/source-firewall.json" ]] || source_guard_needed=true
fi

if [[ "$SURELAY_SECURITY_ONLY" != "true" ]]; then
  if is_dry_run; then
    log "DRY-RUN: 来源白名单变更前停止运行中的 sing-box 和订阅服务。"
  else
    if service_active sing-box; then
      service_stop sing-box || die "无法停止旧代理服务；未修改来源白名单。"
      source_core_was_active=true
    fi
    if service_active "$SUBSCRIPTION_SERVICE"; then
      service_stop "$SUBSCRIPTION_SERVICE" || die "无法停止旧订阅服务；未修改来源白名单。"
      source_subscription_was_active=true
    fi
  fi
fi

restore_source_services() {
  [[ "${RRB_SOURCE_KEEP_STOPPED:-false}" != true ]] || return 0
  if [[ "$source_core_was_active" == true ]]; then
    singbox_check "$SINGBOX_CONFIG_PATH" || die "旧代理配置未通过检查；请重新应用 singbox 阶段。"
    service_restart sing-box || die "来源白名单已应用，但代理服务尚未恢复。"
  fi
  if [[ "$source_subscription_was_active" == true ]]; then
    RRB_SOURCE_POLICY_READY=true RRB_SHOW_RESULT=false bash "$SCRIPT_DIR/13-setup-subscription.sh" \
      || die "来源白名单已应用，但订阅服务尚未按新监听配置恢复。"
  fi
}

if [[ "$ENABLE_UFW" != "true" ]]; then
  # The generator rejects restricted listeners without UFW before this branch.
  if [[ "$SURELAY_SECURITY_ONLY" != "true" ]]; then
    install_source_policy_guard || die "无法安装来源白名单启动检查。"
    restore_source_services
  fi
  info "ENABLE_UFW=false，跳过防火墙配置。"
  exit 0
fi

bash "$SCRIPT_DIR/06-install-ufw.sh"

if [[ "$SURELAY_SECURITY_ONLY" == "true" ]]; then
  info "安全防护模式：只确保 SSH 端口 $SSH_PORT/tcp，保留现有其他 UFW 规则。"
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

安全防护模式已应用：
  SSH: tcp/$SSH_PORT

未安装或配置 sing-box、订阅服务和客户端节点；已有其他 UFW 规则保持不变。
EOF
  exit 0
fi

transport_output="$(python3 "$SCRIPT_DIR/08-generate-singbox-config.py" --config-env "$RRB_CONFIG_FILE" --print-transport-ports)" || die "代理入口端口解析失败。"
mapfile -t TRANSPORT_PORTS <<<"$transport_output"
[[ -n "$transport_output" ]] || die "未解析到代理入口端口。"

cleanup_rrb_ufw_rules() {
  local desired_file stale_file status_file number
  desired_file="$(mktemp)"
  stale_file="$(mktemp)"
  status_file="$(mktemp)"

  printf '%s/tcp\n' "$SSH_PORT" >"$desired_file"
  for transport in "${TRANSPORT_PORTS[@]}"; do
    printf '%s\n' "$transport" >>"$desired_file"
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
    if "surelay" not in line and "reality-relay-bootstrap" not in line:
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

info "准备开放实际使用的代理 TCP/UDP 端口：${TRANSPORT_PORTS[*]}"
for transport in "${TRANSPORT_PORTS[@]}"; do
  [[ "$transport" =~ ^[0-9]+/(tcp|udp)$ ]] || die "无效代理监听：$transport"
  run ufw allow "$transport" comment "surelay sing-box ${transport}"
done

if subscription_public_enabled; then
  info "准备开放订阅 HTTP 端口：$SUBSCRIPTION_PORT"
  run ufw allow "${SUBSCRIPTION_PORT}/tcp" comment "surelay subscription ${SUBSCRIPTION_PORT}"
elif [[ "$ENABLE_SUBSCRIPTION_SERVER" == "true" ]]; then
  info "未设置公网订阅端口；订阅服务仅本机监听 tcp/$SUBSCRIPTION_LISTEN_PORT 并通过 $DIRECT_PORT fallback 反代。"
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

if declare -F install_source_policy_guard >/dev/null; then
  install_source_policy_guard || die "无法安装来源白名单启动检查。"
else
  die "缺少来源白名单启动检查；请使用同一版本的完整 SuRelay 脚本。"
fi
if [[ "$source_guard_needed" == true ]]; then
  if is_dry_run; then
    printf '%s\n' "$source_policy_plan" | python3 "$SCRIPT_DIR/source_firewall.py" --apply --dry-run >>"$LOG_FILE" 2>&1 \
      || die "来源白名单规则预检失败。"
  else
    printf '%s\n' "$source_policy_plan" | run python3 "$SCRIPT_DIR/source_firewall.py" --apply --persist "$RRB_STATE_DIR/source-firewall.json" \
      || die "来源白名单未完整下发；服务保持停止，请修正后重新应用。"
  fi
fi

if is_alpine; then service_enable ufw; fi

restore_source_services

cat <<EOF

UFW 已按最小端口开放。服务商安全组/云防火墙也必须手动放行：

  SSH:        tcp/$SSH_PORT
$(printf '  Proxy:             %s\n' "${TRANSPORT_PORTS[@]}")
$(if subscription_public_enabled; then printf '  Subscription:       tcp/%s\n' "$SUBSCRIPTION_PORT"; fi)

以上为本机监听端口；NAT 主机须确认服务商映射，外部 SSH 命令使用服务商分配的公网 SSH 端口。
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
