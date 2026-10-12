#!/usr/bin/env bash
# 可选自有域名前置：在 Cloudflare 上准备灰云 A/AAAA 记录。
set -Eeuo pipefail
PHASE_NAME="cloudflare-dns"
source "${RRB_PROJECT_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}/scripts/00-lib.sh"
require_root
load_config

if ! custom_domain_enabled; then
  info "ENABLE_CUSTOM_DOMAIN=false，跳过 Cloudflare DNS 准备。"
  exit 0
fi

if [[ "$CLOUDFLARE_ENSURE_DNS_RECORDS" != "true" ]]; then
  info "CLOUDFLARE_ENSURE_DNS_RECORDS=false，跳过 Cloudflare DNS 记录创建。"
  exit 0
fi

pkg_install ca-certificates python3

args=(
  python3 "$SCRIPT_DIR/14-setup-cloudflare-dns.py"
  --config-env "$RRB_CONFIG_FILE"
  --generator "$SCRIPT_DIR/08-generate-singbox-config.py"
)
if is_dry_run; then
  args+=(--dry-run)
fi

"${args[@]}"
