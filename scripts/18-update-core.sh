#!/usr/bin/env bash
# Update only the core; preserve configuration, service definitions and identity.
set -Eeuo pipefail
source "$(cd -- "$(dirname -- "$0")" && pwd)/00-lib.sh"
require_root
load_config
require_supported_os
[[ "$RESET_PROXY_KEYS" == false ]] || die "内核更新要求 RESET_PROXY_KEYS=false。"
bin="$(singbox_bin)"
[[ -n "$bin" && -f "$bin" ]] || die "请先完成 sing-box 安装。"
backup_path "$bin"
saved="$BACKUP_DIR/$bin"
rollback_core() {
  rc=$?
  if [[ "$rc" != 0 && -f "$saved" ]]; then
    cp -a "$saved" "$bin"
    service_restart sing-box || true
    warn "内核更新失败，已尝试恢复原二进制。包管理器记录可能仍为新版本，请检查日志。"
  fi
  exit "$rc"
}
trap rollback_core EXIT
if is_alpine; then
  install_singbox_release
elif [[ "$INSTALL_SINGBOX_METHOD" == apt && "$bin" == /usr/bin/sing-box ]]; then
  apt_install sing-box
else
  die "此部署使用自定义或 233boy 内核；请沿用其内核更新方式，SuRelay 保留现有二进制。"
fi
singbox_check "$SINGBOX_CONFIG_PATH"
service_restart sing-box
service_wait_active sing-box || die "新内核服务未启动。"
info "sing-box 内核已更新，配置与节点身份保持不变。"
