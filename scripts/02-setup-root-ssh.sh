#!/usr/bin/env bash
# 为 root 安装 SSH 公钥，并可选创建 SFTP-only 用户。
set -Eeuo pipefail
PHASE_NAME="setup-root-ssh"
source "${RRB_PROJECT_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}/scripts/00-lib.sh"
require_root
load_config
require_supported_os

pkg_install openssh-server openssl

get_pubkey_for_root() {
  if [[ -n "$ADMIN_PUBKEY" ]]; then
    printf '%s\n' "$ADMIN_PUBKEY"
  elif [[ -s /root/.ssh/authorized_keys ]]; then
    cat /root/.ssh/authorized_keys
  else
    return 1
  fi
}

validate_pubkey_text() {
  local key_file="$1"
  awk 'NF && $1 !~ /^#/ { if ($1 !~ /^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp[0-9]+|sk-ssh-ed25519@openssh.com|sk-ecdsa-sha2-nistp256@openssh.com)$/) exit 1 }' "$key_file"
}

create_user_if_needed() {
  local user="$1" shell="$2"
  if id "$user" >/dev/null 2>&1; then
    info "用户已存在：$user"
    run usermod -s "$shell" "$user"
  else
    if getent group "$user" >/dev/null; then
      run useradd -m -g "$user" -s "$shell" "$user"
    else
      run useradd -m -U -s "$shell" "$user"
    fi
  fi
}

install_authorized_keys() {
  local user="$1" key_text="$2"
  local home
  home="$(user_home "$user")"
  if is_dry_run; then
    log "DRY-RUN: install authorized_keys for $user at $home/.ssh/authorized_keys"
    return 0
  fi
  install -d -m 700 -o "$user" -g "$user" "$home/.ssh"
  local tmp
  tmp="$(mktemp)"
  printf '%s\n' "$key_text" | awk 'NF && $1 !~ /^#/' > "$tmp"
  validate_pubkey_text "$tmp" || { rm -f "$tmp"; die "$user 的 SSH 公钥格式不正确"; }
  touch "$home/.ssh/authorized_keys"
  chown "$user:$user" "$home/.ssh/authorized_keys"
  chmod 600 "$home/.ssh/authorized_keys"
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    grep -qxF "$line" "$home/.ssh/authorized_keys" || printf '%s\n' "$line" >> "$home/.ssh/authorized_keys"
  done < "$tmp"
  rm -f "$tmp"
  chown -R "$user:$user" "$home/.ssh"
  chmod 700 "$home/.ssh"
  chmod 600 "$home/.ssh/authorized_keys"
  chmod go-w "$home" || true
}

root_keys="$(get_pubkey_for_root || true)"
[[ -n "$root_keys" ]] || die "没有可用 ADMIN_PUBKEY，也无法从 /root/.ssh/authorized_keys 复制。"

install_authorized_keys root "$root_keys"

if [[ "$ENABLE_SFTP_USER" == "true" ]]; then
  NOLOGIN="$(command -v nologin || echo /usr/sbin/nologin)"
  sftp_keys="${SFTP_PUBKEY:-$root_keys}"
  create_user_if_needed "$SFTP_USER" "$NOLOGIN"
  prepare_sftp_password
  install_authorized_keys "$SFTP_USER" "$sftp_keys"
  if is_dry_run; then
    log "DRY-RUN: create /sftp/${SFTP_USER}/upload chroot"
  else
    mkdir -p "/sftp/${SFTP_USER}/upload"
    chown root:root /sftp "/sftp/${SFTP_USER}"
    chmod 755 /sftp "/sftp/${SFTP_USER}"
    chown "${SFTP_USER}:${SFTP_USER}" "/sftp/${SFTP_USER}/upload"
    chmod 750 "/sftp/${SFTP_USER}/upload"
  fi
fi

if [[ "$ENABLE_SFTP_USER" == "true" ]]; then
  sftp_status="true ($SFTP_USER)"
else
  sftp_status="false"
fi

cat <<EOF

用户准备完成：
  SSH 用户：root
  SFTP 用户：$sftp_status

下一步会写入 phase1 SSH drop-in，只开启公钥登录，不禁 root/密码。
EOF
