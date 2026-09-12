#!/usr/bin/env python3
"""Generate VLESS+Reality node text and Clash/Mihomo YAML."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import quote, urlencode


SUBSCRIPTION_TARGETS = {"ClashMeta"}


def load_generator(path: Path):
    spec = importlib.util.spec_from_file_location("gen", path)
    gen = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(gen)
    return gen


def q(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def yaml_bool(value: bool) -> str:
    return "true" if value else "false"


def normalize_subscription_target(value: str) -> str:
    compact = re.sub(r"[\s_-]+", "", (value or "ClashMeta").lower())
    aliases = {
        "mihomo": "ClashMeta",
        "clash": "ClashMeta",
        "clashmeta": "ClashMeta",
        "vless": "ClashMeta",
        "vlessreality": "ClashMeta",
        "reality": "ClashMeta",
        "v2ray": "ClashMeta",
        "v2rayn": "ClashMeta",
        "v2rayng": "ClashMeta",
        "qx": "ClashMeta",
        "quanx": "ClashMeta",
        "quantumult": "ClashMeta",
        "quantumultx": "ClashMeta",
        "shadowrocket": "ClashMeta",
    }
    target = aliases.get(compact, compact)
    if target not in SUBSCRIPTION_TARGETS:
        raise SystemExit(f"SUBSCRIPTION_TARGET 不支持：{value}")
    return target


def get_server_ip(env: Dict[str, str], server_override: str = "", force_server_override: bool = False) -> str:
    if force_server_override and server_override.strip():
        return server_override.strip()
    if env.get("ENABLE_CUSTOM_DOMAIN", "false").lower() == "true" and env.get("CUSTOM_DOMAIN", "").strip():
        return env["CUSTOM_DOMAIN"].strip()
    if server_override.strip():
        return server_override.strip()
    if env.get("SERVER_IP", "").strip():
        return env["SERVER_IP"].strip()
    if env.get("SERVER_IP_IPV4", "").strip():
        return env["SERVER_IP_IPV4"].strip()
    if env.get("SERVER_IP_IPV6", "").strip():
        return env["SERVER_IP_IPV6"].strip()
    try:
        proc = subprocess.run(
            ["curl", "-4", "-fsS", "--max-time", "10", "https://ifconfig.me"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        ip = proc.stdout.strip()
        if ip:
            return ip
    except Exception:
        pass
    raise SystemExit("SERVER_IP 为空且无法自动获取公网 IP；请在 config.env 填写 SERVER_IP_IPV4 或 SERVER_IP_IPV6。")


def build_nodes(
    env: Dict[str, str],
    rows: List[Dict[str, Any]],
    gen,
    server_override: str = "",
    name_suffix: str = "",
    force_server_override: bool = False,
) -> List[Dict[str, Any]]:
    uuid = gen.read_vless_uuid(env)
    public_key = gen.read_reality_public_key(env)
    short_id = gen.read_reality_short_id(env)
    server = get_server_ip(env, server_override, force_server_override)
    alpn = [item.strip() for item in env["CLIENT_ALPN"].split(",") if item.strip()]
    udp = gen.as_bool(env, "CLIENT_UDP")

    nodes = []
    for item in gen.node_list(env, rows):
        node: Dict[str, Any] = {
            "name": item["name"] + name_suffix,
            "type": "vless",
            "server": server,
            "port": int(item["port"]),
            "uuid": uuid,
            "tls": True,
            "servername": env["REALITY_SERVER_NAME"],
            "client-fingerprint": env["CLIENT_FINGERPRINT"],
            "reality-opts": {
                "public-key": public_key,
                "short-id": short_id,
            },
            "udp": udp,
            "alpn": alpn,
            "kind": item.get("kind", ""),
        }
        if env.get("VLESS_FLOW", ""):
            node["flow"] = env["VLESS_FLOW"]
        nodes.append(node)
    return nodes


def interleave_nodes(primary: List[Dict[str, Any]], secondary: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    nodes: List[Dict[str, Any]] = []
    longest = max(len(primary), len(secondary))
    for index in range(longest):
        if index < len(primary):
            nodes.append(primary[index])
        if index < len(secondary):
            nodes.append(secondary[index])
    return nodes


def node_to_text(node: Dict[str, Any]) -> str:
    alpn = ",".join(node["alpn"])
    lines = [
        f"name={node['name']}",
        "type=vless",
        f"server={node['server']}",
        f"port={node['port']}",
        f"uuid={node['uuid']}",
    ]
    if node.get("flow"):
        lines.append(f"flow={node['flow']}")
    lines.extend(
        [
            "tls=true",
            f"servername={node['servername']}",
            f"client-fingerprint={node['client-fingerprint']}",
            f"reality-public-key={node['reality-opts']['public-key']}",
            f"reality-short-id={node['reality-opts']['short-id']}",
            f"udp={str(node['udp']).lower()}",
            f"alpn={alpn}",
        ]
    )
    return "\n".join(lines) + "\n"


def node_to_vless_uri(node: Dict[str, Any]) -> str:
    server = str(node["server"]).strip().strip("[]")
    authority_server = f"[{server}]" if ":" in server else server
    params = [
        ("encryption", "none"),
        ("security", "reality"),
        ("type", "tcp"),
        ("sni", node["servername"]),
        ("fp", node["client-fingerprint"]),
        ("pbk", node["reality-opts"]["public-key"]),
        ("sid", node["reality-opts"]["short-id"]),
        ("spx", "/"),
    ]
    if node.get("flow"):
        params.insert(1, ("flow", node["flow"]))
    if node.get("alpn"):
        params.append(("alpn", ",".join(node["alpn"])))
    query = urlencode(params, quote_via=quote)
    fragment = quote(str(node["name"]), safe="")
    return f"vless://{node['uuid']}@{authority_server}:{node['port']}?{query}#{fragment}"


def write_private_file(path_value: str, text: str) -> None:
    path = Path(path_value)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(path)
        os.chmod(path, 0o600)
    finally:
        if tmp.exists():
            tmp.unlink()


def build_clash_yaml(env: Dict[str, str], nodes: List[Dict[str, Any]], clash_ipv6: str = "auto") -> str:
    names = [node["name"] for node in nodes]
    ipv6_enabled = env["PROXY_IP_VERSION"] in {"ipv6", "dual"} if clash_ipv6 == "auto" else clash_ipv6 == "true"
    lines = [
        f"mixed-port: {env['CLASH_MIXED_PORT']}",
        "allow-lan: false",
        "mode: rule",
        "log-level: info",
        f"ipv6: {yaml_bool(ipv6_enabled)}",
        "",
        "proxies:",
    ]
    for node in nodes:
        lines.extend(
            [
                f"  - name: {q(node['name'])}",
                "    type: vless",
                f"    server: {q(node['server'])}",
                f"    port: {node['port']}",
                f"    uuid: {q(node['uuid'])}",
            ]
        )
        if node.get("flow"):
            lines.append(f"    flow: {q(node['flow'])}")
        lines.extend(
            [
                "    network: tcp",
                "    tls: true",
                f"    servername: {q(node['servername'])}",
                f"    client-fingerprint: {q(node['client-fingerprint'])}",
                f"    udp: {yaml_bool(node['udp'])}",
                "    reality-opts:",
                f"      public-key: {q(node['reality-opts']['public-key'])}",
                f"      short-id: {q(node['reality-opts']['short-id'])}",
                "    alpn:",
            ]
        )
        for item in node["alpn"]:
            lines.append(f"      - {q(item)}")
        lines.append("")
    lines.extend(
        [
            "proxy-groups:",
            "  - name: PROXY",
            "    type: select",
            "    proxies:",
        ]
    )
    for name in names:
        lines.append(f"      - {q(name)}")
    lines.extend(["      - DIRECT", "", "rules:", "  - MATCH,PROXY", ""])
    return "\n".join(lines)


def build_subscription_payload(target: str, clash_yaml: str, nodes: List[Dict[str, Any]]) -> str:
    target = normalize_subscription_target(target)
    if target == "ClashMeta":
        return clash_yaml
    raise SystemExit(f"SUBSCRIPTION_TARGET 不支持：{target}")


def mask(value: str) -> str:
    if len(value) <= 8:
        return "***"
    return value[:4] + "..." + value[-4:]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config-env", required=True)
    ap.add_argument("--generator", required=True)
    ap.add_argument("--nodes-out", default="/root/reality-relay-bootstrap-nodes.txt")
    ap.add_argument("--clash-out", default="/root/reality-relay-bootstrap-clash.yaml")
    ap.add_argument("--vless-out", default="")
    ap.add_argument("--server-override", default="")
    ap.add_argument("--force-server-override", action="store_true")
    ap.add_argument("--name-suffix", default="")
    ap.add_argument("--extra-server", default="")
    ap.add_argument("--extra-name-suffix", default="")
    ap.add_argument("--clash-ipv6", choices=["auto", "true", "false"], default="auto")
    ap.add_argument("--subscription-target", default="")
    ap.add_argument("--subscription-out", default="")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--skip-node-files", action="store_true")
    args = ap.parse_args()

    env_path = Path(args.config_env).resolve()
    gen = load_generator(Path(args.generator))
    env = gen.defaults(gen.parse_env_file(env_path))
    gen.validate_env(env)
    rows = gen.load_rows(env, env_path.parent, allow_empty=True)
    nodes = build_nodes(
        env,
        rows,
        gen,
        args.server_override,
        args.name_suffix,
        args.force_server_override,
    )
    if args.extra_server:
        extra_nodes = build_nodes(
            env,
            rows,
            gen,
            args.extra_server,
            args.extra_name_suffix,
            args.force_server_override,
        )
        nodes = interleave_nodes(nodes, extra_nodes)

    nodes_text = "\n---\n".join(node_to_text(node) for node in nodes) + "\n"
    clash_yaml = build_clash_yaml(env, nodes, args.clash_ipv6)
    vless_text = "\n".join(node_to_vless_uri(node) for node in nodes) + "\n"

    if not args.skip_node_files:
        for out, text in [(args.nodes_out, nodes_text), (args.clash_out, clash_yaml)]:
            write_private_file(out, text)
        if args.vless_out:
            write_private_file(args.vless_out, vless_text)

    if args.subscription_out:
        target = normalize_subscription_target(args.subscription_target or env.get("SUBSCRIPTION_TARGET", "ClashMeta"))
        subscription_payload = build_subscription_payload(target, clash_yaml, nodes)
        write_private_file(args.subscription_out, subscription_payload)
        if not args.quiet:
            print(f"已生成 {target} 订阅文件：{args.subscription_out}")

    if not args.quiet and not args.skip_node_files:
        print(f"已生成节点文件：{args.nodes_out}")
        print(f"已生成 Clash/Mihomo 文件：{args.clash_out}")
        if args.vless_out:
            print(f"已生成 VLESS 分享链接文件：{args.vless_out}")
        print("控制台脱敏摘要：")
        for node in nodes:
            print(
                f"  {node['name']}: {node['server']}:{node['port']} "
                f"servername={node['servername']} uuid={mask(node['uuid'])} "
                f"reality-public-key={mask(node['reality-opts']['public-key'])} "
                f"short-id={mask(node['reality-opts']['short-id'])} udp={str(node['udp']).lower()}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
