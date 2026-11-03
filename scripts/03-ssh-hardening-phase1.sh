#!/usr/bin/env bash
# SSH Phase1：只保证公钥登录开启；绝不禁 root/密码。
set -Eeuo pipefail
PHASE_NAME="ssh-phase1"
source "${RRB_PROJECT_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}/scripts/00-lib.sh"
require_root
load_config

backup_path /etc/ssh/sshd_config
[[ -d /etc/ssh/sshd_config.d ]] && backup_path /etc/ssh/sshd_config.d
ensure_sshd_dropin_include

if [[ "${RRB_RESET_INSTALL:-false}" != "true" && -z "${RRB_REDEPLOY_HANDOFF:-}" ]]; then
  write_sshd_policy <<'EOF'
# Managed by surelay phase1.
# Phase1 only enables public key authentication. It intentionally does NOT
# disable root login or password login. Final hardening is a separate phase.
PubkeyAuthentication yes
EOF
fi

test_sshd_config
reload_ssh
configure_sshd_ports stage
if [[ -n "${RRB_REDEPLOY_HANDOFF:-}" ]]; then
  if is_dry_run; then
    log "DRY-RUN: restrict new SSH port to newly configured public keys"
  else
    python3 "$SCRIPT_DIR/surelay_redeploy.py" --restrict-new-port --config-env "$RRB_CONFIG_FILE" --handoff "$RRB_REDEPLOY_HANDOFF"
  fi
fi

# An existing UFW may still allow only the old SSH port. Open the configured
# port before asking for a second verification session; the final firewall
# phase removes stale SuRelay-managed SSH rules later.
if [[ "$ENABLE_UFW" == "true" ]] && command -v ufw >/dev/null 2>&1 \
  && ufw status 2>/dev/null | grep -q '^Status: active'; then
  run ufw allow "${SSH_PORT}/tcp" comment "surelay SSH current port"
  info "已为 SSH 公钥验证放行当前端口 $SSH_PORT/tcp。"
fi

# The installer owns the single login check and confirmation prompt.
if [[ "${RRB_INSTALL_FLOW:-false}" != "true" ]]; then
  printf '请保留当前 SSH 窗口，另开终端执行；输出 root 后再继续最终加固：\n'
  for host in $(server_hosts); do
    printf '  ssh -p %s -o BatchMode=yes -o PreferredAuthentications=publickey -o PasswordAuthentication=no %s@%s whoami\n' "$SSH_PORT" "$ADMIN_USER" "$host"
  done
  printf '  sudo CONFIRM_ROOT_KEY_LOGIN=yes bash bootstrap.sh --phase ssh-final\n'
fi
