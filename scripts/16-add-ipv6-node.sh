#!/usr/bin/env bash
# 在已有部署上启用 IPv6 监听，并生成一套仅含 IPv6 地址的本地客户端节点。
set -Eeuo pipefail
PHASE_NAME="ipv6-node"
source "${RRB_PROJECT_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}/scripts/00-lib.sh"

ADDRESS=""
V6_NODES_OUT="/root/reality-relay-bootstrap-v6-nodes.txt"
V6_CLASH_OUT="/root/reality-relay-bootstrap-v6-clash.yaml"
V6_VLESS_OUT="/root/reality-relay-bootstrap-v6-vless.txt"

usage() {
  cat <<'EOF'
Usage: sudo bash scripts/16-add-ipv6-node.sh --address <IPv6地址>

该脚本只适用于已经完成 reality-relay-bootstrap 部署的服务器。
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --address)
      [[ $# -ge 2 && -n "${2:-}" ]] || die "--address 后必须填写 IPv6 地址。"
      ADDRESS="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "ipv6-node 参数不支持：$1"
      ;;
  esac
done

require_root
[[ -n "$ADDRESS" ]] || { usage >&2; die "缺少 --address。"; }
[[ -f "$RRB_CONFIG_FILE" ]] || die "找不到现有配置：$RRB_CONFIG_FILE"
have python3 || die "找不到 python3；已有部署不完整。"

normalized_ipv6="$(python3 "$SCRIPT_DIR/16-update-ipv6-config.py" \
  --config-env "$RRB_CONFIG_FILE" \
  --address "$ADDRESS" \
  --check-only)"

# 先按旧配置加载并确认部署材料完整，避免把 -6 误用成首次安装入口。
load_config
[[ -n "$(singbox_bin)" ]] || die "找不到 sing-box；请先完成常规部署。"
for secret_var in VLESS_UUID_PATH REALITY_PRIVATE_KEY_PATH REALITY_PUBLIC_KEY_PATH REALITY_SHORT_ID_PATH; do
  secret_path="${!secret_var}"
  [[ -s "$secret_path" ]] || die "已有部署缺少 $secret_var：$secret_path"
done

if [[ -n "$SERVER_IP_IPV4" ]] && have sysctl; then
  bindv6only="$(sysctl -n net.ipv6.bindv6only 2>/dev/null || true)"
  [[ "$bindv6only" != "1" ]] \
    || die "net.ipv6.bindv6only=1 会让 :: 监听无法同时服务 IPv4；为保护现有 v4 节点，本次未修改配置。"
fi

backup_path "$RRB_CONFIG_FILE"
backup_path "$SINGBOX_CONFIG_PATH"
V6_NODES_EXISTED="$([[ -e "$V6_NODES_OUT" ]] && echo true || echo false)"
V6_CLASH_EXISTED="$([[ -e "$V6_CLASH_OUT" ]] && echo true || echo false)"
V6_VLESS_EXISTED="$([[ -e "$V6_VLESS_OUT" ]] && echo true || echo false)"
backup_path "$V6_NODES_OUT"
backup_path "$V6_CLASH_OUT"
backup_path "$V6_VLESS_OUT"

ROLLBACK_ARMED="true"
CONFIG_BACKUP="$BACKUP_DIR/${RRB_CONFIG_FILE#/}"
SINGBOX_BACKUP="$BACKUP_DIR/${SINGBOX_CONFIG_PATH#/}"
UFW_DEFAULT_BACKUP="$BACKUP_DIR/etc/default/ufw"
UFW_DIR_BACKUP="$BACKUP_DIR/etc/ufw"
V6_NODES_BACKUP="$BACKUP_DIR/${V6_NODES_OUT#/}"
V6_CLASH_BACKUP="$BACKUP_DIR/${V6_CLASH_OUT#/}"
V6_VLESS_BACKUP="$BACKUP_DIR/${V6_VLESS_OUT#/}"

rollback_ipv6_node_on_exit() {
  local rc=$?
  trap - EXIT
  if [[ "$rc" -ne 0 && "${ROLLBACK_ARMED:-false}" == "true" ]]; then
    warn "IPv6 节点维护失败，正在恢复修改前的 config.env 和 sing-box 配置。"
    [[ -e "$CONFIG_BACKUP" ]] && cp -a "$CONFIG_BACKUP" "$RRB_CONFIG_FILE"
    [[ -e "$SINGBOX_BACKUP" ]] && cp -a "$SINGBOX_BACKUP" "$SINGBOX_CONFIG_PATH"
    if [[ "$V6_NODES_EXISTED" == "true" ]]; then cp -a "$V6_NODES_BACKUP" "$V6_NODES_OUT"; else rm -f -- "$V6_NODES_OUT"; fi
    if [[ "$V6_CLASH_EXISTED" == "true" ]]; then cp -a "$V6_CLASH_BACKUP" "$V6_CLASH_OUT"; else rm -f -- "$V6_CLASH_OUT"; fi
    if [[ "$V6_VLESS_EXISTED" == "true" ]]; then cp -a "$V6_VLESS_BACKUP" "$V6_VLESS_OUT"; else rm -f -- "$V6_VLESS_OUT"; fi
    if [[ -d "$UFW_DIR_BACKUP" ]]; then
      cp -a "$UFW_DIR_BACKUP/." /etc/ufw/
    fi
    if [[ -e "$UFW_DEFAULT_BACKUP" ]]; then
      cp -a "$UFW_DEFAULT_BACKUP" /etc/default/ufw
    fi
    if [[ -d "$UFW_DIR_BACKUP" || -e "$UFW_DEFAULT_BACKUP" ]]; then
      ufw --force reload >>"$LOG_FILE" 2>&1 || true
    fi
    if [[ -e "$SINGBOX_BACKUP" ]]; then
      systemctl restart sing-box >>"$LOG_FILE" 2>&1 || true
    fi
    warn "已尝试恢复；备份保留在：$BACKUP_DIR"
  fi
  exit "$rc"
}
trap rollback_ipv6_node_on_exit EXIT

normalized_ipv6="$(python3 "$SCRIPT_DIR/16-update-ipv6-config.py" \
  --config-env "$RRB_CONFIG_FILE" \
  --address "$normalized_ipv6")"
info "已更新现有配置：SERVER_IP_IPV6=$normalized_ipv6、ENABLE_IPV6_LISTEN=true、CLIENT_UDP=true、RESET_PROXY_KEYS=false。"

# 重新加载更新后的值；PROXY_IP_VERSION 保持原样，避免改变既有出口/DNS 策略。
load_config
[[ "$SERVER_IP_IPV6" == "$normalized_ipv6" ]] || die "config.env 中的 SERVER_IP_IPV6 更新失败。"

if [[ "$ENABLE_UFW" == "true" && -f /etc/default/ufw ]]; then
  if grep -Eq '^[[:space:]]*IPV6=(no|false)[[:space:]]*$' /etc/default/ufw; then
    backup_path /etc/default/ufw
    sed -i -E 's/^[[:space:]]*IPV6=(no|false)[[:space:]]*$/IPV6=yes/' /etc/default/ufw
    info "已启用 UFW 的 IPv6 规则支持。"
  elif ! grep -Eq '^[[:space:]]*IPV6=' /etc/default/ufw; then
    backup_path /etc/default/ufw
    printf '\nIPV6=yes\n' >>/etc/default/ufw
    info "已在 UFW 默认配置中启用 IPv6 规则支持。"
  fi
fi

info "检查更新后的配置，并先同步 UFW IPv4/IPv6 TCP 规则。"
python3 "$SCRIPT_DIR/08-generate-singbox-config.py" --config-env "$RRB_CONFIG_FILE" --check-only --quiet
bash "$SCRIPT_DIR/09-apply-firewall.sh"

info "重建并重启现有 sing-box；不会重新生成 VLESS/Reality 身份。"
python3 "$SCRIPT_DIR/08-generate-singbox-config.py" --config-env "$RRB_CONFIG_FILE" --write --quiet
bash "$SCRIPT_DIR/10-validate.sh" --singbox-only --skip-proxy-tests --restart-singbox

mapfile -t PORTS < <(python3 "$SCRIPT_DIR/08-generate-singbox-config.py" --config-env "$RRB_CONFIG_FILE" --print-ports)
for port in "${PORTS[@]}"; do
  if ! wait_for_local_tcp_family 6 "$port" 15; then
    diagnose_singbox_listeners
    die "tcp/$port 无法通过 ::1 建立 IPv6 TCP 连接；本地节点尚不可用。"
  fi
  if [[ -n "$SERVER_IP_IPV4" ]] && ! wait_for_local_tcp_family 4 "$port" 15; then
    diagnose_singbox_listeners
    die "tcp/$port 无法通过 127.0.0.1 建立 IPv4 TCP 连接；已停止交付以保护原有 v4 节点。"
  fi
done

python3 "$SCRIPT_DIR/11-output-nodes.py" \
  --config-env "$RRB_CONFIG_FILE" \
  --generator "$SCRIPT_DIR/08-generate-singbox-config.py" \
  --server-override "$SERVER_IP_IPV6" \
  --force-server-override \
  --name-suffix=-IPv6 \
  --clash-ipv6 true \
  --nodes-out "$V6_NODES_OUT" \
  --clash-out "$V6_CLASH_OUT" \
  --vless-out "$V6_VLESS_OUT"

ROLLBACK_ARMED="false"

cat <<EOF

IPv6 VLESS 本地节点已生成：

  VLESS 分享链接： $V6_VLESS_OUT
  VLESS 字段文本： $V6_NODES_OUT
  Mihomo/Clash：    $V6_CLASH_OUT

对齐项：复用现有 UUID、Reality public key、short-id、flow、SNI、指纹、ALPN 和全部入口端口；
Mihomo/Clash 节点显式设置 udp: true、ipv6: true。VLESS 的 UDP 流量封装在现有 TCP/Reality 入口内，
因此 UFW 只需开放对应 tcp 端口，不需要额外暴露同号 udp 端口。

请另外确认云厂商 IPv6 安全组已放行上面这些 VLESS TCP 端口。
配置备份目录：${BACKUP_DIR:-未创建}
EOF
