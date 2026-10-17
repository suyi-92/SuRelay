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

write_sshd_policy <<'EOF'
# Managed by surelay phase1.
# Phase1 only enables public key authentication. It intentionally does NOT
# disable root login or password login. Final hardening is a separate phase.
PubkeyAuthentication yes
EOF

test_sshd_config
reload_ssh

# An existing UFW may still allow only the old SSH port. Open the configured
# port before asking for a second verification session; the final firewall
# phase removes stale SuRelay-managed SSH rules later.
if [[ "$ENABLE_UFW" == "true" ]] && command -v ufw >/dev/null 2>&1 \
  && ufw status 2>/dev/null | grep -q '^Status: active'; then
  run ufw allow "${SSH_PORT}/tcp" comment "surelay SSH current port"
  info "已为 SSH 公钥验证放行当前端口 $SSH_PORT/tcp。"
fi

cat <<EOF

SSH phase1 已完成；当前仍未禁用 root/密码登录。

请保留当前 SSH 窗口，另开一个新的 Windows PowerShell 测试 root key 登录：
$(if [[ -n "$(server_ipv4_hosts)" ]]; then
  printf 'IPv4:\n'
  for host in $(server_ipv4_hosts); do
    printf '  ssh -p %s -o PreferredAuthentications=publickey -o PasswordAuthentication=no %s@%s\n' "$SSH_PORT" "$ADMIN_USER" "$host"
  done
  printf '\n'
fi)
$(if [[ -n "$(server_ipv6_hosts)" ]]; then
  printf '\nIPv6:\n'
  for host in $(server_ipv6_hosts); do
    printf '  ssh -p %s -o PreferredAuthentications=publickey -o PasswordAuthentication=no %s@%s\n' "$SSH_PORT" "$ADMIN_USER" "$host"
  done
  printf '\n'
fi)
$(if [[ -z "$(server_ipv4_hosts)" && -z "$(server_ipv6_hosts)" ]]; then
  for host in $(server_hosts); do
    printf '  ssh -p %s -o PreferredAuthentications=publickey -o PasswordAuthentication=no %s@%s\n' "$SSH_PORT" "$ADMIN_USER" "$host"
  done
  printf '\n'
fi)

登录成功后在新窗口执行：
  whoami

确认输出为：
  root

确认后再执行最终加固：
  sudo CONFIRM_ROOT_KEY_LOGIN=yes bash bootstrap.sh --phase ssh-final
EOF
