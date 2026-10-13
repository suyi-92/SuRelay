#!/usr/bin/env bash
# 安装 UFW，并先放行当前 SSH 端口。不要在这里删除任何旧规则。
set -Eeuo pipefail
PHASE_NAME="ufw"
source "${RRB_PROJECT_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}/scripts/00-lib.sh"
require_root
load_config
require_supported_os

if [[ "$ENABLE_UFW" != "true" ]]; then
  info "ENABLE_UFW=false，跳过 UFW 安装。"
  exit 0
fi

backup_path /etc/ufw || true
if [[ "${RRB_SKIP_PACKAGE_INSTALL:-false}" != true ]] || ! command -v ufw >/dev/null 2>&1; then
  pkg_install ufw
fi
ensure_ufw_ipv6

run ufw allow "${SSH_PORT}/tcp" comment "reality-relay-bootstrap SSH current port"
info "已先放行当前 SSH 端口 $SSH_PORT/tcp。启用 UFW 后必须另开窗口测试 SSH。"
