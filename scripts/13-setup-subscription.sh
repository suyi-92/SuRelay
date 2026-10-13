#!/usr/bin/env bash
# 可选订阅端口：按已填写的服务器地址生成一份 token 保护的订阅。
set -Eeuo pipefail
PHASE_NAME="subscription"
source "${RRB_PROJECT_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}/scripts/00-lib.sh"
require_root
load_config
require_supported_os

SERVICE_NAME="$SUBSCRIPTION_SERVICE"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
SERVER_INSTALL_DIR="/usr/local/lib/surelay"
SERVER_INSTALL_PATH="$SERVER_INSTALL_DIR/subscription-server.py"

subscription_target_file() {
  case "$SUBSCRIPTION_TARGET" in
    ClashMeta) printf 'clashmeta.yaml\n' ;;
    *) die "SUBSCRIPTION_TARGET 不支持：$SUBSCRIPTION_TARGET" ;;
  esac
}

if [[ "$ENABLE_SUBSCRIPTION_SERVER" != "true" ]]; then
  info "ENABLE_SUBSCRIPTION_SERVER=false，跳过订阅服务。"
  if service_exists "$SERVICE_NAME"; then
    service_disable "$SERVICE_NAME"
    info "已停用订阅服务：$SERVICE_NAME"
  fi
  exit 0
fi

if [[ "${RRB_SKIP_PACKAGE_INSTALL:-false}" != true ]] || ! command -v python3 >/dev/null 2>&1; then
  pkg_install python3
fi

if is_dry_run; then
  log "DRY-RUN: mkdir -p $SUBSCRIPTION_DIR"
  log "DRY-RUN: generate subscription files into $SUBSCRIPTION_DIR"
else
  mkdir -p "$SUBSCRIPTION_DIR/all" "$SERVER_INSTALL_DIR"
  chmod 700 "$SUBSCRIPTION_DIR" "$SUBSCRIPTION_DIR/all"
  install -m 0755 -o root -g root "$SCRIPT_DIR/13-subscription-server.py" "$SERVER_INSTALL_PATH"

  rm -f "$SUBSCRIPTION_DIR/all/"*.yaml "$SUBSCRIPTION_DIR/all/"*.txt

  target_file="$(subscription_target_file)"
  args=(
    python3 "$SCRIPT_DIR/11-output-nodes.py"
    --config-env "$RRB_CONFIG_FILE"
    --generator "$SCRIPT_DIR/08-generate-singbox-config.py"
    --nodes-out "$SUBSCRIPTION_DIR/all/nodes.txt"
    --clash-out "$SUBSCRIPTION_DIR/all/clash.yaml"
    --subscription-target "$SUBSCRIPTION_TARGET"
    --subscription-out "$SUBSCRIPTION_DIR/all/$target_file"
    --skip-node-files
    --quiet
  )
  "${args[@]}"
  chmod 600 "$SUBSCRIPTION_DIR/all/"*
fi

bind_mode="public"
sub_ipv4="$([[ -n "$SERVER_IP_IPV4" ]] && echo true || echo false)"
sub_ipv6="$([[ -n "$SERVER_IP_IPV6" ]] && echo true || echo false)"
custom_domain_enabled && sub_ipv4="true"
if ! subscription_public_enabled; then
  bind_mode="localhost"
  sub_ipv4="true"
  sub_ipv6="false"
fi
if is_alpine; then
  install_openrc_service "$SERVICE_NAME" /usr/bin/python3 "$SUBSCRIPTION_DIR" "" \
    "$SERVER_INSTALL_PATH" --root "$SUBSCRIPTION_DIR" --port "$SUBSCRIPTION_LISTEN_PORT" \
    --ipv4 "$sub_ipv4" --ipv6 "$sub_ipv6" --bind "$bind_mode" \
    --token-file "$VLESS_UUID_PATH" --default-target "$SUBSCRIPTION_TARGET"
else
  run mkdir -p /etc/systemd/system
  write_root_file "$SERVICE_FILE" 0644 <<EOF_SERVICE
[Unit]
Description=SuRelay subscription file server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$SUBSCRIPTION_DIR
ExecStart=/usr/bin/python3 $SERVER_INSTALL_PATH --root $SUBSCRIPTION_DIR --port $SUBSCRIPTION_LISTEN_PORT --ipv4 $sub_ipv4 --ipv6 $sub_ipv6 --bind $bind_mode --token-file $VLESS_UUID_PATH --default-target $SUBSCRIPTION_TARGET
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF_SERVICE
fi
service_daemon_reload
service_enable "$SERVICE_NAME"
service_restart "$SERVICE_NAME"
if ! is_dry_run; then
  service_wait_active "$SERVICE_NAME" || die "订阅服务未运行。"
  if [[ "$sub_ipv4" == true ]]; then
    wait_for_local_tcp_family 4 "$SUBSCRIPTION_LISTEN_PORT" 15 || die "订阅服务尚未监听 IPv4：$SUBSCRIPTION_LISTEN_PORT"
  fi
  if [[ "$sub_ipv6" == true ]]; then
    wait_for_local_tcp_family 6 "$SUBSCRIPTION_LISTEN_PORT" 15 || die "订阅服务尚未监听 IPv6：$SUBSCRIPTION_LISTEN_PORT"
  fi
fi

if ! is_dry_run && [[ "${RRB_SHOW_RESULT:-true}" == "true" ]]; then
  python3 "$SCRIPT_DIR/11-output-nodes.py" --config-env "$RRB_CONFIG_FILE" \
    --generator "$SCRIPT_DIR/08-generate-singbox-config.py" --skip-node-files --quiet --print-summary
fi
