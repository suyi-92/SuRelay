#!/usr/bin/env python3
# Managed by surelay source guard.
"""Service pre-start source ACL check; installed independently of the checkout."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import source_firewall
import source_policy


def check(payload, service="sing-box", *, listen_port=None, bind=None, ipv4=None, ipv6=None,
          runner=None, config=None, env=None):
    env = env or payload["env"]
    if service == "subscription":
        if not source_policy.enabled(env, "ENABLE_SUBSCRIPTION_SERVER"):
            raise source_policy.SourcePolicyError("订阅服务已在配置中关闭。")
        binding = payload.get("subscription", {})
        expected = source_policy.subscription_binding(env)
        if not binding.get("ready") or any(binding.get(key) != value for key, value in expected.items()):
            raise source_policy.SourcePolicyError("订阅服务尚未按当前绑定和白名单重新部署，请重新应用订阅配置。")
        actual = {"port": listen_port, "bind": bind, "ipv4": ipv4, "ipv6": ipv6}
        if any(value is not None and binding.get(key) != value for key, value in actual.items()):
            raise source_policy.SourcePolicyError("订阅旧服务的实际端口/绑定/地址族与已应用策略不符，请重新应用订阅配置。")
        config = {"inbounds": []}
    elif config is None:
        config = json.loads(Path(payload["singbox_config"]).read_text(encoding="utf-8"))
    plan = source_policy.source_policy_plan(env, config=config)
    applied = source_firewall.normalize_plan(payload["plan"])
    applied_ports = {entry["port"]: entry["sources"] for entry in applied["entries"]}
    if any(applied_ports.get(entry["port"]) != entry["sources"] for entry in plan["entries"]):
        raise source_policy.SourcePolicyError("实际监听入口/白名单与已应用的防火墙策略不符，请先重新应用配置。")
    if plan["entries"] and plan["ipv6"] and not applied["ipv6"]:
        raise source_policy.SourcePolicyError("实际 IPv6 入口尚无已应用的来源防护。")
    if applied["entries"]:
        source_firewall.check(applied, **({"runner": runner} if runner is not None else {}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--service", choices=("sing-box", "subscription"), default="sing-box")
    parser.add_argument("--port", type=int)
    parser.add_argument("--bind", default="unprepared")
    parser.add_argument("--ipv4", choices=("true", "false"))
    parser.add_argument("--ipv6", choices=("true", "false"))
    parser.add_argument("--execute", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        check(json.loads(args.plan.read_text(encoding="utf-8")), args.service, listen_port=args.port,
              bind=args.bind if args.service == 'subscription' else None,
              ipv4=None if args.ipv4 is None else args.ipv4 == 'true',
              ipv6=None if args.ipv6 is None else args.ipv6 == 'true')
        if args.execute:
            os.execv(args.execute[0], args.execute)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError) as exc:
        print(f"来源白名单检查失败，拒绝启动服务：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
