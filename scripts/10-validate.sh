#!/usr/bin/env bash
# 验证 SSH、fail2ban、UFW、sing-box、监听端口和家宽代理可用性。
set -Eeuo pipefail
PHASE_NAME="validate"
source "${RRB_PROJECT_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}/scripts/00-lib.sh"
require_root
load_config

SINGBOX_ONLY="false"
SKIP_PROXY_TESTS="false"
RESTART_SINGBOX="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --singbox-only) SINGBOX_ONLY="true"; shift ;;
    --skip-proxy-tests) SKIP_PROXY_TESTS="true"; shift ;;
    --restart-singbox) RESTART_SINGBOX="true"; shift ;;
    *) die "validate 参数不支持：$1" ;;
  esac
done

if [[ "$SINGBOX_ONLY" != "true" ]]; then
  info "检查 sshd 配置语法。"
  test_sshd_config

  if ! is_dry_run; then
    info "检查 sshd -T 最终生效值。"
    if [[ "$RRB_VERBOSE" == "true" ]]; then
      "$(sshd_bin)" -T -C "user=${ADMIN_USER},host=localhost,addr=127.0.0.1,lport=${SSH_PORT}" \
        | grep -Ei '^(permitrootlogin|pubkeyauthentication|passwordauthentication|kbdinteractiveauthentication|challengeresponseauthentication|authenticationmethods|permitemptypasswords|allowusers|maxauthtries|x11forwarding|allowagentforwarding|allowtcpforwarding|permittunnel|gatewayports)\b' \
        | tee -a "$LOG_FILE"
    else
      "$(sshd_bin)" -T -C "user=${ADMIN_USER},host=localhost,addr=127.0.0.1,lport=${SSH_PORT}" \
        | grep -Ei '^(permitrootlogin|pubkeyauthentication|passwordauthentication|kbdinteractiveauthentication|challengeresponseauthentication|authenticationmethods|permitemptypasswords|allowusers|maxauthtries|x11forwarding|allowagentforwarding|allowtcpforwarding|permittunnel|gatewayports)\b' \
        >>"$LOG_FILE"
    fi
  fi

  if [[ "$ENABLE_FAIL2BAN" == "true" ]]; then
    info "检查 fail2ban。"
    if ! is_dry_run; then
      fail2ban-client -t >>"$LOG_FILE" 2>&1
      service_active fail2ban || die "fail2ban 未运行"
      info "fail2ban 服务运行中。"
      if [[ "$RRB_VERBOSE" == "true" ]]; then
        fail2ban-client status sshd 2>&1 | tee -a "$LOG_FILE" || true
      else
        fail2ban-client status sshd >>"$LOG_FILE" 2>&1 || true
      fi
    fi
  fi

  if [[ "$ENABLE_UFW" == "true" ]]; then
    info "检查 UFW。"
    if ! is_dry_run; then
      LC_ALL=C ufw status | grep -q '^Status: active' || die "UFW 未启用；不能确认防火墙防护。"
      if [[ "$RRB_VERBOSE" == "true" ]]; then
        ufw status verbose 2>&1 | tee -a "$LOG_FILE"
      else
        ufw status verbose >>"$LOG_FILE" 2>&1
      fi
    fi
  fi
fi

if [[ "$SURELAY_SECURITY_ONLY" == "true" ]]; then
  if [[ "$RRB_VERBOSE" != "true" ]]; then
    printf '安全防护验证完成。\n'
  else
    cat <<EOF

安全防护验证完成。未检查 sing-box、订阅服务或节点端口。
EOF
  fi
  exit 0
fi

info "检查 sing-box 配置语法。"
python3 "$SCRIPT_DIR/08-generate-singbox-config.py" --config-env "$RRB_CONFIG_FILE" --check-only --quiet
singbox_check "$SINGBOX_CONFIG_PATH"

if [[ "$RESTART_SINGBOX" == "true" ]]; then
  if is_dry_run; then
    info "DRY-RUN: sing-box check 未实际执行；不会重启 sing-box，只展示将执行的命令。"
  else
    info "sing-box check 已通过，重启 sing-box。"
  fi
  restart_singbox
fi

if is_dry_run; then
  info "DRY-RUN: 跳过 sing-box 服务状态、监听端口和家宽代理实测。"
else
  info "检查 sing-box 服务状态。"
  service_active sing-box || die "sing-box 未运行"
  info "sing-box 服务运行中。"
fi

transport_output="$(python3 "$SCRIPT_DIR/08-generate-singbox-config.py" --config-env "$RRB_CONFIG_FILE" --print-transport-ports)" || die "代理入口端口解析失败。"
mapfile -t TRANSPORT_PORTS <<<"$transport_output"
PORTS=()
for transport in "${TRANSPORT_PORTS[@]}"; do
  [[ "$transport" =~ ^[0-9]+/(tcp|udp)$ ]] || die "无效代理监听：$transport"
  [[ "$transport" != */tcp ]] || PORTS+=("${transport%/*}")
done
if is_dry_run; then
  info "DRY-RUN: 实际运行后应监听端口：${TRANSPORT_PORTS[*]}"
else
  info "检查监听端口：${TRANSPORT_PORTS[*]}"
fi
if ! is_dry_run; then
  for transport in "${TRANSPORT_PORTS[@]}"; do
    port="${transport%/*}"
    network="${transport##*/}"
    if wait_for_port_listener "$port" 15 "$network"; then
      info "端口监听正常：$transport"
    else
      diagnose_singbox_listeners
      die "未发现监听端口：$transport；已等待 15 秒，详见上方 sing-box 状态和日志。"
    fi
  done
fi

if [[ "$ENABLE_SUBSCRIPTION_SERVER" == "true" && "$SINGBOX_ONLY" != "true" ]]; then
  if is_dry_run; then
    info "DRY-RUN: 实际运行后应检查订阅服务监听端口：$SUBSCRIPTION_LISTEN_PORT"
  else
    info "检查订阅服务。"
    service_active "$SUBSCRIPTION_SERVICE" || die "订阅服务未运行"
    wait_for_port_listener "$SUBSCRIPTION_LISTEN_PORT" 15 || die "订阅服务未监听端口：$SUBSCRIPTION_LISTEN_PORT"
    info "订阅服务端口正常：$SUBSCRIPTION_LISTEN_PORT"
  fi
fi

if custom_domain_enabled && [[ "$SINGBOX_ONLY" != "true" ]]; then
  if is_dry_run; then
    info "DRY-RUN: 实际运行后应检查 Nginx fallback：$NGINX_FALLBACK_HOST:$NGINX_FALLBACK_PORT"
  else
    info "检查自有域名 Nginx fallback。"
    service_active nginx || die "Nginx 未运行"
    wait_for_port_listener "$NGINX_FALLBACK_PORT" 15 || die "Nginx fallback 未监听端口：$NGINX_FALLBACK_PORT"
    timeout 10 openssl s_client -brief -connect "$NGINX_FALLBACK_HOST:$NGINX_FALLBACK_PORT" -servername "$CUSTOM_DOMAIN" </dev/null >>"$LOG_FILE" 2>&1 \
      || die "Nginx fallback TLS 验证失败：$NGINX_FALLBACK_HOST:$NGINX_FALLBACK_PORT"
    info "Nginx fallback 正常：$NGINX_FALLBACK_HOST:$NGINX_FALLBACK_PORT"
  fi
fi

if [[ "$SKIP_PROXY_TESTS" != "true" ]]; then
  info "测试每个家宽代理和上游节点是否可用；不会把代理密码或节点密钥写入日志。"
  if ! is_dry_run; then
    SINGBOX_BIN="$(singbox_bin)"
    [[ -n "$SINGBOX_BIN" ]] || die "找不到 sing-box 可执行文件"
    python3 - "$RRB_CONFIG_FILE" "$SCRIPT_DIR" "$SINGBOX_BIN" <<'PY'
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv[2]).resolve()))
from surelay_diagnostics import GEN as gen, endpoint, probe, probe_families

env_path = Path(sys.argv[1]).resolve()
singbox_bin = sys.argv[3]
env = gen.defaults(gen.parse_env_file(env_path))
rows = gen.load_rows(env, env_path.parent, allow_empty=True)
verbose = os.environ.get("RRB_VERBOSE", "false").lower() == "true"
failures = []

if not rows and verbose:
    print("没有家宽代理或上游节点行，跳过出口测试。")
for family in probe_families(env):
    for row in rows:
        label = f"{row['tag']} · IPv{family[-1]}"
        if verbose:
            print(f"测试 {label} ({row['type']}://{endpoint(row['server'], row['server_port'])}) ...", flush=True)
        try:
            ok, detail = probe(singbox_bin, gen.build_home_outbound(row), env, family)
        except OSError as exc:
            ok, detail = False, f"无法启动检测（{exc.__class__.__name__}）。"
        if not ok:
            failures.append(label)
            print(f"WARN: 代理测试失败：{label}；部署继续。", file=sys.stderr)
        if verbose:
            print(f"  {detail}", flush=True)
if failures:
    print("WARN: 代理连通性检查存在失败项，仅告警不阻断：" + "；".join(failures), file=sys.stderr)
PY
  fi
fi

if [[ "$RRB_VERBOSE" != "true" ]]; then
  printf '验证完成。\n'
  exit 0
fi

cat <<EOF

验证完成。建议从外部 Debian/Linux 继续测试：

$(if [[ -n "$(server_ipv4_hosts)" ]]; then
  printf 'IPv4:\n'
  for host in $(server_ipv4_hosts); do
    for port in "${PORTS[@]}"; do
      printf "  python3 -c 'import socket,sys; s=socket.create_connection((sys.argv[1], int(sys.argv[2])), 5); s.close(); print(\"TCP OK\")' %q %q\n" "$host" "$port"
    done
    printf '\n'
    printf '  ssh -p %s -o PreferredAuthentications=publickey -o PasswordAuthentication=no %s@%s\n' "$SSH_PORT" "$ADMIN_USER" "$host"
    printf '  ssh -p %s -o PubkeyAuthentication=no -o PreferredAuthentications=password %s@%s\n' "$SSH_PORT" "$ADMIN_USER" "$host"
  done
  printf '\n'
fi)
$(if [[ -n "$(server_ipv6_hosts)" ]]; then
  printf '\nIPv6:\n'
  for host in $(server_ipv6_hosts); do
    for port in "${PORTS[@]}"; do
      printf "  python3 -c 'import socket,sys; s=socket.create_connection((sys.argv[1], int(sys.argv[2])), 5); s.close(); print(\"TCP OK\")' %q %q\n" "$host" "$port"
    done
    printf '\n'
    printf '  ssh -p %s -o PreferredAuthentications=publickey -o PasswordAuthentication=no %s@%s\n' "$SSH_PORT" "$ADMIN_USER" "$host"
    printf '  ssh -p %s -o PubkeyAuthentication=no -o PreferredAuthentications=password %s@%s\n' "$SSH_PORT" "$ADMIN_USER" "$host"
  done
  printf '\n'
fi)
EOF
