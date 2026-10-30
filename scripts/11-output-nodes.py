#!/usr/bin/env python3
"""Generate local import links and Mihomo/sing-box client configurations."""
from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import quote, urlencode


# One entry per supported output format, not per client application. Clients
# consuming the same format share a URL; the panel renders this catalog.
SUBSCRIPTION_TARGETS = {"ClashMeta": "Mihomo / Clash Meta"}
RESERVED_NODE_NAMES = {"PROXY", "DIRECT", "REJECT", "REJECT-DROP", "PASS", "GLOBAL", "COMPATIBLE"}


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
        protocol = item.get("type", "vless")
        if protocol != "vless":
            node = {"name": item["name"] + name_suffix, "type": protocol,
                    "server": server, "port": int(item["port"]), "kind": item.get("kind", "direct")}
            node["password"] = gen.protocol_password(env, protocol, uuid)
            if protocol == "hysteria2":
                node.update(sni=env["HY2_SERVER_NAME"], alpn=["h3"], udp=udp,
                            **{"skip-cert-verify": gen.as_bool(env, "HY2_INSECURE")})
            elif protocol == "ss":
                node.update(cipher=env["SHADOWSOCKS_METHOD"], udp=udp)
            else:
                node.update(username=env["PROXY_USERNAME"], udp=False)
            nodes.append(node)
            continue
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
    return unique_node_names(nodes)


def unique_node_names(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Preserve existing VLESS labels; rename only conflicting export names."""
    result = [dict(node) for node in nodes]
    original = {node["name"] for node in nodes}
    used = set(RESERVED_NODE_NAMES)
    # Additional protocol labels must yield to legacy exit names, including
    # labels from the IPv6 copy. Also reserve names that occur later so a new
    # numeric suffix cannot take a user's existing, otherwise unique label.
    indices = sorted(range(len(result)), key=lambda index: result[index].get("type", "vless") != "vless")
    for index in indices:
        node = result[index]
        name = node["name"]
        candidate = name
        if candidate in used:
            suffix = 2
            while f"{name}-{suffix}" in used or f"{name}-{suffix}" in original:
                suffix += 1
            candidate = f"{name}-{suffix}"
        node["name"] = candidate
        used.add(candidate)
    return result


def interleave_nodes(primary: List[Dict[str, Any]], secondary: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    combined = unique_node_names([*primary, *secondary])
    primary, secondary = combined[:len(primary)], combined[len(primary):]
    nodes: List[Dict[str, Any]] = []
    longest = max(len(primary), len(secondary))
    for index in range(longest):
        if index < len(primary):
            nodes.append(primary[index])
        if index < len(secondary):
            nodes.append(secondary[index])
    return nodes


def build_client_nodes(env: Dict[str, str], rows: List[Dict[str, Any]], gen) -> List[Dict[str, Any]]:
    """Use the same endpoints for online subscriptions and locally imported nodes."""
    server = get_server_ip(env)
    primary = build_nodes(env, rows, gen, name_suffix="-IPv6" if ":" in server else "")
    ipv6 = env.get("SERVER_IP_IPV6", "").strip()
    if ipv6 and ipv6 != server and gen.as_bool(env, "ENABLE_IPV6_LISTEN"):
        secondary = build_nodes(env, rows, gen, ipv6, "-IPv6", force_server_override=True)
        return interleave_nodes(primary, secondary)
    return primary


def subscription_url(env: Dict[str, str], token: str, target: str = "ClashMeta") -> str:
    if target not in SUBSCRIPTION_TARGETS:
        raise ValueError(f"不支持的订阅格式：{target}")
    if env.get("ENABLE_SUBSCRIPTION_SERVER", "true") != "true":
        return ""
    base = env.get("SUBSCRIPTION_BASE_URL", "").rstrip("/")
    if not base:
        host = env.get("SERVER_IP_IPV4") or env.get("SERVER_IP_IPV6") or env.get("SERVER_IP", "")
        host = f"[{host.strip('[]')}]" if ":" in host else host
        base = f"http://{host}:{env['SUBSCRIPTION_LISTEN_PORT']}"
    return f"{base}/sub/{token}?{urlencode({'target': target})}"


def subscription_links(env: Dict[str, str], token: str):
    """Return the available format labels and URLs for all presentation modes."""
    if env.get("ENABLE_SUBSCRIPTION_SERVER", "true") != "true":
        return []
    return [(label, subscription_url(env, token, target)) for target, label in SUBSCRIPTION_TARGETS.items()]


def print_client_summary(env: Dict[str, str], nodes: List[Dict[str, Any]], project_dir: Path) -> None:
    color = sys.stdout.isatty() and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"
    bold, cyan, green, reset = ("\033[1m", "\033[36m", "\033[32m", "\033[0m") if color else ("", "", "", "")
    divider = "─" * 60
    print(f"\n{divider}\n{bold}节点与订阅{reset}\n{divider}")
    if any(":" in node["server"] for node in nodes):
        print("IPv6 节点：已生成（客户端和 VPS 均需 IPv6 可达）。")
    elif env.get("SERVER_IP_IPV6", "").strip():
        ipv6 = env.get("SERVER_IP_IPV6", "").strip()
        reason = "未启用监听" if ipv6 else "未配置公网地址"
        command = f"cd {shlex.quote(str(project_dir))} && sudo bash install.sh -6"
        if ipv6:
            command += f" {shlex.quote(ipv6)}"
        print(f"IPv6 节点：{reason}；VPS 需有可用公网 IPv6。\n开启：{command}")
    for label, url in subscription_links(env, nodes[0]["uuid"]):
        print(f"{cyan}{bold}订阅链接（{label}）{reset}\n{cyan}{url}{reset}")
    print(f"\n{divider}\n{green}{bold}本地节点（复制协议链接导入）{reset}")
    for node in nodes:
        print(f"{green}{node_to_uri(node)}{reset}\n")
    print(divider)


def node_to_text(node: Dict[str, Any]) -> str:
    if node["type"] != "vless":
        return "\n".join(f"{key}={value if not isinstance(value, (dict, list, bool)) else json.dumps(value, ensure_ascii=False)}"
                         for key, value in node.items() if key != "kind") + "\n"
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


def node_to_uri(node: Dict[str, Any]) -> str:
    if node["type"] == "vless":
        return node_to_vless_uri(node)
    server = str(node["server"]).strip().strip("[]")
    host = f"[{server}]" if ":" in server else server
    authority = f"{host}:{node['port']}"
    fragment = quote(node["name"], safe="")
    if node["type"] == "ss":
        userinfo = base64.urlsafe_b64encode(f"{node['cipher']}:{node['password']}".encode()).decode().rstrip("=")
        return f"ss://{userinfo}@{authority}#{fragment}"
    if node["type"] == "hysteria2":
        query = urlencode({"sni": node["sni"], "insecure": "1" if node["skip-cert-verify"] else "0", "alpn": "h3"})
        return f"hysteria2://{quote(node['password'], safe='')}@{authority}/?{query}#{fragment}"
    userinfo = f"{quote(node['username'], safe='')}:{quote(node['password'], safe='')}"
    return f"{node['type']}://{userinfo}@{authority}#{fragment}"


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
    ipv6_enabled = (
        env["PROXY_IP_VERSION"] in {"ipv6", "dual"} or any(":" in node["server"] for node in nodes)
    ) if clash_ipv6 == "auto" else clash_ipv6 == "true"
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
        if node["type"] != "vless":
            lines.extend([f"  - name: {q(node['name'])}", f"    type: {node['type']}",
                          f"    server: {q(node['server'])}", f"    port: {node['port']}"])
            for key, value in node.items():
                if key not in {"name", "type", "server", "port", "kind"}:
                    lines.append(f"    {key}: {q(value)}")
            lines.append("")
            continue
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


def build_singbox_client_config(env: Dict[str, str], nodes: List[Dict[str, Any]]) -> Dict[str, Any]:
    outbounds = []
    for node in nodes:
        protocol = node["type"]
        item = {"type": {"socks5": "socks", "ss": "shadowsocks"}.get(protocol, protocol),
                "tag": node["name"], "server": node["server"], "server_port": node["port"]}
        if protocol == "vless":
            item["uuid"] = node["uuid"]
            if node.get("flow"):
                item["flow"] = node["flow"]
            item["tls"] = {"enabled": True, "server_name": node["servername"], "alpn": node["alpn"],
                           "utls": {"enabled": True, "fingerprint": node["client-fingerprint"]},
                           "reality": {"enabled": True, "public_key": node["reality-opts"]["public-key"],
                                       "short_id": node["reality-opts"]["short-id"]}}
        elif protocol == "hysteria2":
            item["password"] = node["password"]
            item["tls"] = {"enabled": True, "server_name": node["sni"], "alpn": ["h3"],
                           "insecure": node["skip-cert-verify"]}
        elif protocol == "ss":
            item.update(method=node["cipher"], password=node["password"])
            if not node["udp"]:
                item["network"] = "tcp"
        else:
            item.update(username=node["username"], password=node["password"])
            if protocol == "socks5":
                item.update(version="5", network="tcp")
        outbounds.append(item)
    return {"log": {"level": "info"},
            "inbounds": [{"type": "mixed", "tag": "mixed-in", "listen": "127.0.0.1",
                          "listen_port": int(env["CLASH_MIXED_PORT"])}],
            "outbounds": [{"type": "selector", "tag": "PROXY", "outbounds": [item["tag"] for item in outbounds]},
                          *outbounds], "route": {"final": "PROXY"}}


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
    ap.add_argument("--nodes-out", default="/root/surelay-nodes.txt")
    ap.add_argument("--clash-out", default="/root/surelay-clash.yaml")
    ap.add_argument("--vless-out", default="")
    ap.add_argument("--singbox-out", default="")
    ap.add_argument("--server-override", default="")
    ap.add_argument("--force-server-override", action="store_true")
    ap.add_argument("--name-suffix", default="")
    ap.add_argument("--extra-server", default="")
    ap.add_argument("--extra-name-suffix", default="")
    ap.add_argument("--clash-ipv6", choices=["auto", "true", "false"], default="auto")
    ap.add_argument("--subscription-target", default="")
    ap.add_argument("--subscription-out", default="")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--print-summary", action="store_true", help="Print one subscription URL and all VLESS import links")
    ap.add_argument("--skip-node-files", action="store_true")
    args = ap.parse_args()

    env_path = Path(args.config_env).resolve()
    gen = load_generator(Path(args.generator))
    env = gen.defaults(gen.parse_env_file(env_path))
    gen.validate_env(env)
    rows = gen.load_rows(env, env_path.parent, allow_empty=True)
    if args.server_override or args.name_suffix or args.extra_server or args.force_server_override:
        nodes = build_nodes(env, rows, gen, args.server_override, args.name_suffix, args.force_server_override)
    else:
        nodes = build_client_nodes(env, rows, gen)
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
    vless_text = "\n".join(node_to_uri(node) for node in nodes) + "\n"

    if not args.skip_node_files:
        for out, text in [(args.nodes_out, nodes_text), (args.clash_out, clash_yaml)]:
            write_private_file(out, text)
        if args.vless_out:
            write_private_file(args.vless_out, vless_text)
        if args.singbox_out:
            write_private_file(args.singbox_out, json.dumps(build_singbox_client_config(env, nodes), ensure_ascii=False, indent=2) + "\n")

    if args.subscription_out:
        target = normalize_subscription_target(args.subscription_target or env.get("SUBSCRIPTION_TARGET", "ClashMeta"))
        subscription_payload = build_subscription_payload(target, clash_yaml, nodes)
        write_private_file(args.subscription_out, subscription_payload)
        if not args.quiet:
            print(f"已生成 {target} 订阅文件：{args.subscription_out}")

    if args.print_summary:
        print_client_summary(env, nodes, env_path.parent)
    elif not args.quiet and not args.skip_node_files:
        print(f"已生成节点文件：{args.nodes_out}")
        print(f"已生成 Clash/Mihomo 文件：{args.clash_out}")
        if args.vless_out:
            print(f"已生成协议分享链接文件：{args.vless_out}")
        print("控制台脱敏摘要：")
        for node in nodes:
            print(f"  {node['name']}: {node['type']} {node['server']}:{node['port']} udp={str(node['udp']).lower()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
