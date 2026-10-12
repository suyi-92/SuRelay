#!/usr/bin/env bash
# 回滚 SSH 加固和 sing-box 配置；UFW 可选禁用或恢复。
set -Eeuo pipefail
PHASE_NAME="rollback"
source "${RRB_PROJECT_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}/scripts/00-lib.sh"
require_root
load_config

if [[ "${CONFIRM_ROLLBACK:-}" != "yes" ]]; then
  die "回滚是危险操作。请设置 CONFIRM_ROLLBACK=yes 后重新执行。"
fi

latest_backup() {
  if [[ -n "${ROLLBACK_BACKUP_DIR:-}" ]]; then
    [[ -d "$ROLLBACK_BACKUP_DIR" ]] || die "ROLLBACK_BACKUP_DIR 不存在：$ROLLBACK_BACKUP_DIR"
    printf '%s\n' "$ROLLBACK_BACKUP_DIR"
    return
  fi
  find "$BACKUP_ROOT" -maxdepth 1 -type d -name 'backup-*' 2>/dev/null | sort | tail -n 1
}

copy_safe() {
  local src="$1" dst="$2"
  if is_dry_run; then
    log "DRY-RUN: cp -a $src $dst"
  else
    cp -a "$src" "$dst"
  fi
}

remove_dir_safe() {
  local path="$1"
  if is_dry_run; then
    log "DRY-RUN: rm -rf $path"
  else
    rm -rf "$path"
  fi
}

move_safe() {
  local src="$1" dst="$2"
  if is_dry_run; then
    log "DRY-RUN: mv $src $dst"
  else
    mv "$src" "$dst"
  fi
}

ROLLBACK_DIR="$(latest_backup || true)"
[[ -n "$ROLLBACK_DIR" ]] || warn "没有找到备份目录，将仅移除本项目管理的 SSH 策略。"

backup_path /etc/ssh/sshd_config || true
[[ -d /etc/ssh/sshd_config.d ]] && backup_path /etc/ssh/sshd_config.d || true
backup_path /etc/sing-box/config.json || true
backup_path /etc/ufw || true
backup_path /etc/default/ufw || true

info "移除本项目 SSH 策略块和旧版 drop-in。"
remove_sshd_policy

if is_dry_run; then
  log "DRY-RUN: remove managed SFTP Match block from /etc/ssh/sshd_config"
else
  python3 - "$SFTP_USER" <<'PY'
import re
import sys
from pathlib import Path

user = sys.argv[1]
path = Path("/etc/ssh/sshd_config")
if path.exists():
    start = f"# BEGIN reality-relay-bootstrap SFTP user {user}"
    end = f"# END reality-relay-bootstrap SFTP user {user}"
    text = path.read_text(encoding="utf-8", errors="replace")
    text = re.sub(re.escape(start) + r".*?" + re.escape(end) + r"\n?", "", text, flags=re.S)
    path.write_text(text, encoding="utf-8")
PY
fi

if [[ "${RESTORE_FULL_SSH_BACKUP:-no}" == "yes" && -n "$ROLLBACK_DIR" ]]; then
  if [[ -f "$ROLLBACK_DIR/etc/ssh/sshd_config" ]]; then
    info "恢复 sshd_config：$ROLLBACK_DIR/etc/ssh/sshd_config"
    copy_safe "$ROLLBACK_DIR/etc/ssh/sshd_config" /etc/ssh/sshd_config
  fi
  if [[ -d "$ROLLBACK_DIR/etc/ssh/sshd_config.d" ]]; then
    info "恢复 sshd_config.d：$ROLLBACK_DIR/etc/ssh/sshd_config.d"
    remove_dir_safe /etc/ssh/sshd_config.d
    copy_safe "$ROLLBACK_DIR/etc/ssh/sshd_config.d" /etc/ssh/sshd_config.d
  fi
fi

test_sshd_config
reload_ssh

if [[ -n "$ROLLBACK_DIR" && -f "$ROLLBACK_DIR/etc/sing-box/config.json" ]]; then
  info "恢复 sing-box config.json。"
  copy_safe "$ROLLBACK_DIR/etc/sing-box/config.json" /etc/sing-box/config.json
  singbox_check /etc/sing-box/config.json
  restart_singbox
else
  warn "未找到可恢复的 /etc/sing-box/config.json 备份。"
fi

if [[ "${RESTORE_233BOY_CONF:-no}" == "yes" ]]; then
  disabled="$(find /etc/sing-box -maxdepth 1 -type d -name 'conf.233boy.disabled.*' 2>/dev/null | sort | tail -n1 || true)"
  if [[ -n "$disabled" ]]; then
    backup_path /etc/sing-box/conf || true
    remove_dir_safe /etc/sing-box/conf
    move_safe "$disabled" /etc/sing-box/conf
    info "已恢复 233boy conf 目录：$disabled -> /etc/sing-box/conf"
  else
    warn "未找到 conf.233boy.disabled.*"
  fi
fi

if [[ "${RESTORE_UFW_FROM_BACKUP:-no}" == "yes" && -n "$ROLLBACK_DIR" && -d "$ROLLBACK_DIR/etc/ufw" ]]; then
  info "恢复 /etc/ufw。"
  remove_dir_safe /etc/ufw
  copy_safe "$ROLLBACK_DIR/etc/ufw" /etc/ufw
  if [[ -f "$ROLLBACK_DIR/etc/default/ufw" ]]; then
    copy_safe "$ROLLBACK_DIR/etc/default/ufw" /etc/default/ufw
  fi
  run ufw --force reload
fi

if [[ "${ROLLBACK_DISABLE_UFW:-no}" == "yes" ]]; then
  info "禁用 UFW。"
  if is_dry_run; then
    log "DRY-RUN: ufw disable"
  else
    ufw disable 2>&1 | tee -a "$LOG_FILE"
  fi
  if is_alpine; then service_disable ufw; fi
fi

cat <<EOF

回滚流程完成。建议立刻另开窗口测试 SSH：
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

可选环境变量：
  RESTORE_FULL_SSH_BACKUP=yes      恢复完整 SSH 备份
  RESTORE_UFW_FROM_BACKUP=yes      恢复 /etc/ufw 备份
  ROLLBACK_BACKUP_DIR=/path        指定要恢复的备份目录
  ROLLBACK_DISABLE_UFW=yes         回滚时直接禁用 UFW
  RESTORE_233BOY_CONF=yes          恢复 233boy 默认 conf 目录
EOF
