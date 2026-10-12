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
      "$(sshd_bin)" -T -C "user=${ADMIN_USER},host=localhost,addr=127.0.0.1" \
        | grep -Ei '^(permitrootlogin|pubkeyauthentication|passwordauthentication|kbdinteractiveauthentication|challengeresponseauthentication|authenticationmethods|permitemptypasswords|allowusers|maxauthtries|x11forwarding|allowagentforwarding|allowtcpforwarding|permittunnel|gatewayports)\b' \
        | tee -a "$LOG_FILE"
    else
      "$(sshd_bin)" -T -C "user=${ADMIN_USER},host=localhost,addr=127.0.0.1" \
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

mapfile -t PORTS < <(python3 "$SCRIPT_DIR/08-generate-singbox-config.py" --config-env "$RRB_CONFIG_FILE" --print-ports)
if is_dry_run; then
  info "DRY-RUN: 实际运行后应监听端口：${PORTS[*]}"
else
  info "检查监听端口：${PORTS[*]}"
fi
if ! is_dry_run; then
  for port in "${PORTS[@]}"; do
    if wait_for_port_listener "$port" 15; then
      info "端口监听正常：$port"
    else
      diagnose_singbox_listeners
      die "未发现监听端口：$port；已等待 15 秒，详见上方 sing-box 状态和日志。"
    fi
  done
fi

if [[ "$ENABLE_SUBSCRIPTION_SERVER" == "true" && "$SINGBOX_ONLY" != "true" ]]; then
  if is_dry_run; then
    info "DRY-RUN: 实际运行后应检查订阅服务监听端口：$SUBSCRIPTION_LISTEN_PORT"
  else
    info "检查订阅服务。"
    service_active reality-relay-subscription || die "订阅服务未运行"
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
    python3 - "$RRB_CONFIG_FILE" "$SCRIPT_DIR/08-generate-singbox-config.py" "$SINGBOX_BIN" <<'PY'
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

env_path = Path(sys.argv[1]).resolve()
gen_path = Path(sys.argv[2]).resolve()
singbox_bin = sys.argv[3]
spec = importlib.util.spec_from_file_location("gen", gen_path)
gen = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(gen)

base = env_path.parent
env = gen.defaults(gen.parse_env_file(env_path))
rows = gen.load_rows(env, base, allow_empty=True)
home_rows = [row for row in rows if row.get("source") == "home"]
upstream_rows = [row for row in rows if row.get("source") == "upstream"]
verbose = os.environ.get("RRB_VERBOSE", "false").lower() == "true"
failures = []

if not home_rows and verbose:
    print("没有家宽代理行，跳过代理本身测试。")

for row in home_rows:
    proto = "socks5h" if row["type"] in {"socks", "socks5"} else "http"
    proxy = f"{proto}://{row['server']}:{row['server_port']}"
    cmd = [
        "curl",
        "--silent",
        "--show-error",
        "--max-time",
        "20",
        "--proxy",
        proxy,
        "https://ifconfig.me",
    ]
    if row["username"] or row["password"]:
        cmd[7:7] = ["--proxy-user", f"{row['username']}:{row['password']}"]
    if verbose:
        print(f"测试 {row['tag']} ({row['type']}://{row['server']}:{row['server_port']}) ...", flush=True)
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        failures.append(f"家宽代理测试失败：{row['tag']}")
        print(f"WARN: 家宽代理测试失败：{row['tag']}；部署继续。", file=sys.stderr)
        if verbose:
            print(proc.stderr.strip() or proc.stdout.strip(), file=sys.stderr)
        continue
    if verbose:
        print(f"  出口 IP: {proc.stdout.strip()[:80]}")


def free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_local_port(port: int, proc, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def upstream_test_config(row, port: int):
    outbound = dict(row["outbound"])
    return {
        "log": {"level": "warn", "timestamp": True},
        "dns": {
            "servers": [
                {
                    "type": "https",
                    "tag": "dns-cloudflare",
                    "server": "1.1.1.1",
                    "server_port": 443,
                    "path": "/dns-query",
                },
                {
                    "type": "https",
                    "tag": "dns-google",
                    "server": "8.8.8.8",
                    "server_port": 443,
                    "path": "/dns-query",
                },
            ],
            "final": "dns-cloudflare",
            "strategy": gen.dns_strategy(env),
            "cache_capacity": 256,
        },
        "inbounds": [
            {
                "type": "socks",
                "tag": "test-socks",
                "listen": "127.0.0.1",
                "listen_port": port,
            }
        ],
        "outbounds": [
            outbound,
            {"type": "direct", "tag": "direct"},
            {"type": "block", "tag": "block"},
        ],
        "route": {
            "auto_detect_interface": True,
            "default_domain_resolver": "dns-cloudflare",
            "rules": [
                {"inbound": "test-socks", "action": "route", "outbound": outbound["tag"]},
            ],
            "final": "block",
        },
    }


def stop_proc(proc) -> str:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)
    stderr = proc.stderr.read() if proc.stderr else ""
    return "\n".join(stderr.strip().splitlines()[-20:])


if upstream_rows and verbose:
    print("测试每个上游节点本身是否可用；会临时启动本地 socks 入口。")
for row in upstream_rows:
    port = free_local_port()
    if verbose:
        print(f"测试 {row['tag']} ({row['type']}://{row['server']}:{row['server_port']}) ...", flush=True)
    with tempfile.TemporaryDirectory() as td:
        config_path = Path(td) / "sing-box-upstream-test.json"
        config_path.write_text(json.dumps(upstream_test_config(row, port), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(config_path, 0o600)
        proc = subprocess.Popen(
            [singbox_bin, "run", "-c", str(config_path)],
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            if not wait_local_port(port, proc):
                stderr = stop_proc(proc)
                failures.append(f"上游节点测试失败：{row['tag']}，临时 sing-box 未监听本地测试端口")
                print(f"WARN: 上游节点测试失败：{row['tag']}；部署继续。", file=sys.stderr)
                if stderr and verbose:
                    print(stderr, file=sys.stderr)
                continue
            cmd = [
                "curl",
                "--silent",
                "--show-error",
                "--max-time",
                "25",
                "--proxy",
                f"socks5h://127.0.0.1:{port}",
                "https://ifconfig.me",
            ]
            curl_proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if curl_proc.returncode != 0:
                stderr = stop_proc(proc)
                failures.append(f"上游节点测试失败：{row['tag']}")
                print(f"WARN: 上游节点测试失败：{row['tag']}；部署继续。", file=sys.stderr)
                if verbose:
                    print(curl_proc.stderr.strip() or curl_proc.stdout.strip(), file=sys.stderr)
                if stderr and verbose:
                    print(stderr, file=sys.stderr)
                continue
            if verbose:
                print(f"  出口 IP: {curl_proc.stdout.strip()[:80]}")
        finally:
            stop_proc(proc)
if not upstream_rows and verbose:
    print("没有上游节点行，跳过上游节点测试。")
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
