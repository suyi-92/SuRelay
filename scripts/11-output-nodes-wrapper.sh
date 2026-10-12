#!/usr/bin/env bash
# 生成客户端节点信息文件。输出文件包含 VLESS UUID、Reality public key 和 short-id，权限必须是 600。
set -Eeuo pipefail
PHASE_NAME="output-nodes"
source "${RRB_PROJECT_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}/scripts/00-lib.sh"
require_root
load_config

if is_dry_run; then
  log "DRY-RUN: generate /root/reality-relay-bootstrap-nodes.txt and /root/reality-relay-bootstrap-clash.yaml"
  exit 0
fi

args=(
  python3 "$SCRIPT_DIR/11-output-nodes.py"
  --config-env "$RRB_CONFIG_FILE"
  --generator "$SCRIPT_DIR/08-generate-singbox-config.py"
  --nodes-out /root/reality-relay-bootstrap-nodes.txt
  --clash-out /root/reality-relay-bootstrap-clash.yaml
  --vless-out /root/reality-relay-bootstrap-vless.txt
  --print-summary
  --quiet
)

if [[ "$ENABLE_SUBSCRIPTION_SERVER" == "true" ]]; then
  RRB_SHOW_RESULT=false bash "$SCRIPT_DIR/13-setup-subscription.sh"
fi
"${args[@]}"
