#!/usr/bin/env python3
"""Focused, read-only panel checks; temporary probes never alter live services."""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time

from surelay_core import Error, GEN, Manager, ROOT, run


def endpoint(host, port):
    return f"[{host.strip('[]')}]:{port}" if ":" in host else f"{host}:{port}"


def probe_config(outbound, listen_port, env):
    return {
        "log": {"level": "error"},
        "dns": {
            "servers": [{"type": "https", "tag": "dns-cloudflare", "server": "1.1.1.1", "server_port": 443, "path": "/dns-query"}],
            "final": "dns-cloudflare", "strategy": GEN.dns_strategy(env),
        },
        "inbounds": [{"type": "socks", "tag": "probe", "listen": "127.0.0.1", "listen_port": listen_port}],
        "outbounds": [outbound],
        "route": {"auto_detect_interface": True, "default_domain_resolver": "dns-cloudflare", "final": outbound["tag"]},
    }


def stop(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


def probe(binary, outbound, env):
    """Exercise the proxy protocol and credentials, not only its TCP port."""
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        listen_port = reservation.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="surelay-probe-") as folder:
        config = Path(folder) / "probe.json"
        config.write_text(json.dumps(probe_config(outbound, listen_port, env)), encoding="utf-8")
        os.chmod(config, 0o600)
        # Credentials stay in a private temporary file, never in argv/output.
        with subprocess.Popen([binary, "run", "-c", str(config)], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL) as process:
            try:
                deadline = time.monotonic() + 5
                while True:
                    if process.poll() is not None:
                        return False, "临时测试内核启动失败，请检查节点参数和内核版本。"
                    try:
                        with socket.create_connection(("127.0.0.1", listen_port), timeout=0.2):
                            break
                    except OSError:
                        if time.monotonic() >= deadline:
                            return False, "临时测试入口未就绪。"
                        time.sleep(0.1)
                started = time.monotonic()
                result = subprocess.run([
                    "curl", "--silent", "--show-error", "--fail", "--noproxy", "",
                    "--connect-timeout", "5", "--max-time", "12", "--proxy", f"socks5h://127.0.0.1:{listen_port}",
                    "https://ifconfig.me/ip",
                ], capture_output=True, text=True, timeout=15)
                if result.returncode:
                    reason = {5: "域名解析失败", 6: "域名解析失败", 7: "连接失败", 22: "检测站点返回 HTTP 错误",
                              28: "请求超时", 35: "TLS 握手失败", 60: "TLS 证书校验失败", 97: "代理握手或认证失败"}
                    return False, reason.get(result.returncode, f"代理请求失败（curl {result.returncode}）")
                try:
                    address = ipaddress.ip_address(result.stdout.strip())
                except ValueError:
                    return False, "检测站点未返回有效 IP，暂时无法确认。"
                return True, f"出口 IP {address} · 请求耗时 {round((time.monotonic() - started) * 1000)} ms"
            except subprocess.TimeoutExpired:
                return False, "检测请求超时。"
            finally:
                stop(process)


def check_exits(manager):
    rows = manager.rows()
    binary = manager.core_binary()
    items = [("服务器直连", "本机网络", {"type": "direct", "tag": "probe-direct"})]
    items += [(row["tag"], f"{row['type'].upper()} · {endpoint(row['server'], row['server_port'])}", GEN.build_home_outbound(row))
              for row in rows]
    print(f"出口连通性 · 共 {len(items)} 项", flush=True)
    print("通过各出口请求 HTTPS 检测站点；不会修改配置或重启现有服务。\n", flush=True)
    passed = 0
    for index, (name, address, outbound) in enumerate(items, 1):
        print(f"{index}/{len(items)}  {name}\n目标  {address}", flush=True)
        try:
            ok, detail = probe(binary, outbound, manager.env)
        except OSError as exc:
            ok, detail = False, f"无法启动检测（{exc.__class__.__name__}）。"
        print(f"{'通过' if ok else '失败'}  {detail}\n", flush=True)
        passed += int(ok)
    print(f"检查完成：通过 {passed} 项，失败 {len(items) - passed} 项。", flush=True)
    print("结果反映 VPS 到检测站点的连接；客户端到 VPS 的公网入口需从客户端验证。", flush=True)


def check_config(manager):
    print("检查范围：节点配置、代理/订阅服务和本机监听。\n", flush=True)
    failures = 0
    def report(label, ok, detail=""):
        nonlocal failures
        failures += int(not ok)
        print(f"{'通过' if ok else '失败'}  {label}" + (f" · {detail}" if detail else ""), flush=True)
    try:
        rows = manager.check()
    except (Error, GEN.ConfigError, ValueError):
        report("配置参数与出口列表", False, "存在无效字段或端口冲突，请核对相应设置。")
        return
    report("配置参数与出口列表", True)
    result = run([manager.core_binary(), "check", "-c", manager.path("SINGBOX_CONFIG_PATH")], capture=True, check=False)
    report("sing-box 配置语法", result.returncode == 0, "" if result.returncode == 0 else "检查失败，请查看代理服务日志。")
    active = manager.active("sing-box")
    report("代理服务", active, "运行中" if active else "未运行")
    hosts = ["127.0.0.1"] if manager.env.get("SERVER_IP_IPV4") else []
    if manager.env["ENABLE_IPV6_LISTEN"] == "true":
        hosts.append("::1")
    listeners = [("代理入口", host, port) for port in GEN.port_list(manager.env, rows) for host in hosts]
    if manager.env["ENABLE_SUBSCRIPTION_SERVER"] == "true":
        active = manager.active(manager.env["SUBSCRIPTION_SERVICE"])
        report("订阅服务", active, "运行中" if active else "未运行")
        domain = manager.env["ENABLE_CUSTOM_DOMAIN"] == "true"
        local_only = domain and not manager.env.get("SUBSCRIPTION_PORT")
        sub_hosts = ["127.0.0.1"] if manager.env.get("SERVER_IP_IPV4") or domain else []
        if manager.env.get("SERVER_IP_IPV6") and not local_only:
            sub_hosts.append("::1")
        listeners += [("订阅入口", host, int(manager.env["SUBSCRIPTION_LISTEN_PORT"])) for host in sub_hosts]
    for label, host, port in listeners:
        try:
            with socket.create_connection((host, port), timeout=1):
                ok = True
        except OSError:
            ok = False
        report(label, ok, endpoint(host, port))
    print(f"\n检查完成：{'全部通过' if not failures else str(failures) + ' 项需要处理'}。", flush=True)
    print("本机监听通过后，公网访问仍取决于安全组、端口映射和客户端网络。", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", type=Path, default=ROOT)
    parser.add_argument("--mode", choices=("exits", "config"), required=True)
    args = parser.parse_args()
    os.umask(0o077)
    manager = Manager(args.project_dir)
    if not manager.env:
        raise Error("尚未配置 SuRelay，请先完成安装。")
    (check_exits if args.mode == "exits" else check_config)(manager)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (Error, OSError, GEN.ConfigError) as exc:
        print(f"检查未完成：{exc}", flush=True)
        raise SystemExit(1)
