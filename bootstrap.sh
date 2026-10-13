#!/usr/bin/env bash
# surelay 总入口。所有危险步骤都拆成阶段执行，避免一次性锁机。
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_DIR="$PROJECT_DIR/scripts"
export RRB_PROJECT_DIR="$PROJECT_DIR"

PHASE=""
RRB_CONFIG_FILE="$PROJECT_DIR/config.env"
RRB_DRY_RUN="false"
RRB_YES="false"
RRB_VERBOSE="${RRB_VERBOSE:-false}"
export RRB_CONFIG_FILE RRB_DRY_RUN RRB_YES RRB_VERBOSE

usage() {
  cat <<'EOF'
Usage: sudo bash bootstrap.sh --phase <phase> [--config config.env] [--dry-run] [--yes] [--verbose]

Phases:
  preflight       检测系统、配置和 SSH 基础条件
  ssh-phase1      准备 root/SFTP 公钥登录，不禁 root/密码
  ssh-final       二阶段 SSH 最终加固；必须 CONFIRM_ROOT_KEY_LOGIN=yes
  fail2ban        安装并配置 fail2ban
  singbox         安装 sing-box、生成 VLESS+Reality 多入口配置并重启
  subscription    安装/更新可选订阅端口
  cloudflare-dns  可选在 Cloudflare 准备 DNS only / 灰云 A/AAAA 记录
  custom-domain   可选配置 Cloudflare DNS-01 证书和本机 Nginx fallback
  firewall        安装/配置 UFW，只开放 SSH、443 和实际中转端口
  validate        验证 SSH、fail2ban、UFW、sing-box、监听端口、家宽代理和上游节点
  output-nodes    可选生成 /root/surelay-nodes.txt 与 /root/surelay-clash.yaml
  rollback        回滚 SSH 加固、sing-box 配置；可选处理 UFW
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --phase) PHASE="${2:-}"; shift 2 ;;
    --config) RRB_CONFIG_FILE="${2:-}"; export RRB_CONFIG_FILE; shift 2 ;;
    --dry-run) RRB_DRY_RUN="true"; export RRB_DRY_RUN; shift ;;
    --yes|-y) RRB_YES="true"; export RRB_YES; shift ;;
    --verbose) RRB_VERBOSE="true"; export RRB_VERBOSE; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

[[ -n "$PHASE" ]] || { usage >&2; exit 2; }

run_phase() {
  local script="$1"
  shift || true
  bash "$SCRIPT_DIR/$script" "$@"
}

case "$PHASE" in
  preflight) run_phase 01-preflight.sh ;;
  ssh-phase1)
    run_phase 02-setup-root-ssh.sh
    run_phase 03-ssh-hardening-phase1.sh
    ;;
  ssh-final) run_phase 04-ssh-hardening-final.sh ;;
  fail2ban) run_phase 05-install-fail2ban.sh ;;
  singbox)
    run_phase 07-install-singbox.sh
    python3 "$SCRIPT_DIR/08-generate-singbox-config.py" --config-env "$RRB_CONFIG_FILE" --write --quiet
    run_phase 10-validate.sh --singbox-only --skip-proxy-tests --restart-singbox
    RRB_SHOW_RESULT=false run_phase 13-setup-subscription.sh
    run_phase 14-setup-cloudflare-dns.sh
    run_phase 15-setup-custom-domain.sh
    if [[ "$RRB_DRY_RUN" != "true" && "${RRB_SHOW_RESULT:-true}" == "true" ]]; then
      python3 "$SCRIPT_DIR/11-output-nodes.py" --config-env "$RRB_CONFIG_FILE" \
        --generator "$SCRIPT_DIR/08-generate-singbox-config.py" --skip-node-files --quiet --print-summary
    fi
    ;;
  firewall) run_phase 09-apply-firewall.sh ;;
  validate) run_phase 10-validate.sh ;;
  output-nodes) run_phase 11-output-nodes-wrapper.sh ;;
  subscription) run_phase 13-setup-subscription.sh ;;
  cloudflare-dns) run_phase 14-setup-cloudflare-dns.sh ;;
  custom-domain)
    run_phase 14-setup-cloudflare-dns.sh
    run_phase 15-setup-custom-domain.sh
    ;;
  rollback) run_phase 12-rollback.sh ;;
  *) echo "Unknown phase: $PHASE" >&2; usage >&2; exit 2 ;;
esac
