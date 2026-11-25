# Managed by surelay source guard.
"""Source policy shared by configuration generation and installed service guards."""
from __future__ import annotations

import ipaddress
import re
from typing import Any, Dict, Iterable, List


WEB_PORTS = {80, 443, 8080, 8443}
POLICY_KEYS = (
    "PROXY_SOURCE_ALLOWLIST", "WEB_SOURCE_ALLOWLIST", "TCP_PUBLIC_PORT_MAP",
    "ENABLE_UFW", "ENABLE_IPV6_LISTEN", "SURELAY_SECURITY_ONLY",
    "DIRECT_PORT", "SSH_PORT", "ENABLE_HTTP", "HTTP_PORT", "ENABLE_SOCKS5", "SOCKS5_PORT",
    "ENABLE_SHADOWSOCKS", "SHADOWSOCKS_PORT", "ENABLE_SUBSCRIPTION_SERVER",
    "ENABLE_CUSTOM_DOMAIN", "SUBSCRIPTION_PORT", "SUBSCRIPTION_INTERNAL_PORT",
    "SUBSCRIPTION_LISTEN_PORT", "SERVER_IP_IPV4", "SERVER_IP_IPV6",
)


class SourcePolicyError(ValueError):
    pass


def enabled(env: Dict[str, str], key: str) -> bool:
    return str(env.get(key, "false")).lower() == "true"


def parse_allowlist(value: str, key: str = "来源白名单") -> List[str]:
    networks = []
    for token in re.split(r"[,\s]+", value.strip()):
        if not token:
            continue
        try:
            network = ipaddress.ip_network(token, strict=False)
        except ValueError as exc:
            raise SourcePolicyError(f"{key} 只能填写 IP 或 CIDR，不能填写域名：{token}") from exc
        if network.prefixlen == 0:
            raise SourcePolicyError(f"{key} 不允许 /0 全网放行。")
        if network.version == 6 and network.network_address.ipv4_mapped is not None:
            raise SourcePolicyError(f"{key} 请把 IPv4 映射地址写成普通 IPv4/CIDR。")
        networks.append(network)
    result = []
    for version in (4, 6):
        collapsed = list(ipaddress.collapse_addresses(n for n in networks if n.version == version))
        if any(n.prefixlen == 0 for n in collapsed):
            raise SourcePolicyError(f"{key} 合并后覆盖全网，必须限制为指定来源。")
        result.extend(str(n) for n in collapsed)
    return result


def port(value: Any, label: str) -> int:
    try:
        number = int(value)
    except (ValueError, TypeError) as exc:
        raise SourcePolicyError(f"{label} 必须是有效端口。") from exc
    if not 1 <= number <= 65535:
        raise SourcePolicyError(f"{label} 必须在 1–65535 之间。")
    return number


def parse_public_port_map(value: str) -> Dict[int, set]:
    result: Dict[int, set] = {}
    for item in re.split(r"[,\s]+", value.strip()):
        if not item:
            continue
        parts = item.split(":")
        if len(parts) != 2 or not all(part.isdecimal() for part in parts):
            raise SourcePolicyError("TCP_PUBLIC_PORT_MAP 使用 本机端口:公网端口，例如 55443:443。")
        local, public = (port(part, "TCP_PUBLIC_PORT_MAP") for part in parts)
        result.setdefault(local, set()).add(public)
    return result


def public_subscription_port(env: Dict[str, str]):
    if not enabled(env, "ENABLE_SUBSCRIPTION_SERVER"):
        return None
    value = env.get("SUBSCRIPTION_PORT", "").strip()
    if not value and not enabled(env, "ENABLE_CUSTOM_DOMAIN"):
        value = env.get("SUBSCRIPTION_INTERNAL_PORT", "51040")
    return port(value, "SUBSCRIPTION_PORT") if value else None


def subscription_binding(env: Dict[str, str]) -> Dict[str, Any]:
    public = public_subscription_port(env)
    local_port = port(env.get("SUBSCRIPTION_LISTEN_PORT") or public or env.get("SUBSCRIPTION_INTERNAL_PORT", "51040"), "SUBSCRIPTION_LISTEN_PORT")
    return {"port": local_port, "bind": "public" if public is not None else "localhost",
            "ipv4": True if public is None else bool(env.get("SERVER_IP_IPV4")) or enabled(env, "ENABLE_CUSTOM_DOMAIN"),
            "ipv6": False if public is None else bool(env.get("SERVER_IP_IPV6"))}


def public_listener(inbound: Dict[str, Any]) -> bool:
    try:
        return not ipaddress.ip_address(inbound.get("listen", "0.0.0.0")).is_loopback
    except ValueError:
        # An unknown/nonliteral bind must never exempt a public source policy.
        return True


def source_policy_plan(env: Dict[str, str], rows: Iterable[Dict[str, Any]] = (),
                       config=None, *, check_mapping: bool = True) -> Dict[str, Any]:
    proxy_sources = parse_allowlist(env.get("PROXY_SOURCE_ALLOWLIST", ""), "PROXY_SOURCE_ALLOWLIST")
    web_sources = parse_allowlist(env.get("WEB_SOURCE_ALLOWLIST", ""), "WEB_SOURCE_ALLOWLIST")
    mapping = parse_public_port_map(env.get("TCP_PUBLIC_PORT_MAP", ""))
    ipv6 = enabled(env, "ENABLE_IPV6_LISTEN")
    endpoints = []
    known = {port(env.get("SSH_PORT", "22"), "SSH_PORT")}
    if config is None:
        if not enabled(env, "SURELAY_SECURITY_ONLY"):
            endpoints.append((port(env.get("DIRECT_PORT", "443"), "DIRECT_PORT"), "web"))
            endpoints.extend((port(row["listen_port"], "listen_port"), "web") for row in rows)
            for key in ("HTTP", "SOCKS5"):
                if enabled(env, f"ENABLE_{key}"):
                    endpoints.append((port(env.get(f"{key}_PORT"), f"{key}_PORT"), "proxy"))
            if enabled(env, "ENABLE_SHADOWSOCKS"):
                known.add(port(env.get("SHADOWSOCKS_PORT"), "SHADOWSOCKS_PORT"))
    else:
        for inbound in config.get("inbounds", []):
            if not public_listener(inbound):
                continue
            p = port(inbound.get("listen_port"), "实际入站 listen_port")
            known.add(p)
            bind = str(inbound.get("listen", "0.0.0.0"))
            ipv6 = ipv6 or ":" in bind
            if inbound.get("type") in {"http", "socks", "mixed"}:
                endpoints.append((p, "proxy"))
            elif inbound.get("tls", {}).get("reality", {}).get("enabled"):
                endpoints.append((p, "web"))
    subscription = public_subscription_port(env)
    if subscription is not None:
        endpoints.append((subscription, "web"))
        ipv6 = ipv6 or bool(env.get("SERVER_IP_IPV6"))
    known.update(p for p, _ in endpoints)
    if config is None and check_mapping and set(mapping) - known:
        unknown = ", ".join(map(str, sorted(set(mapping) - known)))
        raise SourcePolicyError(f"TCP_PUBLIC_PORT_MAP 包含未启用的本机 TCP 端口：{unknown}")
    required = {}
    for p, kind in endpoints:
        if kind == "proxy":
            if not proxy_sources:
                raise SourcePolicyError(f"公网 HTTP/SOCKS 端口 {p} 必须配置 PROXY_SOURCE_ALLOWLIST；密码不能替代来源白名单。")
            sources = proxy_sources
        elif {p, *mapping.get(p, ())} & WEB_PORTS:
            if not web_sources:
                raise SourcePolicyError(f"Web 伪装/订阅端口 {p} 或其公网映射使用 80/443/8080/8443，必须配置 WEB_SOURCE_ALLOWLIST。")
            sources = web_sources
        else:
            continue
        if not enabled(env, "ENABLE_UFW"):
            raise SourcePolicyError("受限公网入口必须设置 ENABLE_UFW=true，并先下发、核验来源防火墙规则。")
        # Conflicting roles cannot safely share a listening port with different
        # policies. Ordinary port collision checks catch this earlier too.
        if p in required and required[p] != sources:
            raise SourcePolicyError(f"端口 {p} 存在不同的来源策略，请为服务使用不同端口。")
        required[p] = sources
    return {"version": 1, "ipv6": ipv6,
            "entries": [{"port": p, "sources": required[p]} for p in sorted(required)]}


def validate_source_policy(env: Dict[str, str], rows=None) -> None:
    source_policy_plan(env, rows or (), check_mapping=rows is not None)


def guard_payload(env: Dict[str, str], rows=()) -> Dict[str, Any]:
    return {"owner": "surelay-source-guard-v1", "env": {key: env.get(key, "") for key in POLICY_KEYS},
            "plan": source_policy_plan(env, rows),
            "subscription": {**subscription_binding(env), "ready": False},
            "singbox_config": env["SINGBOX_CONFIG_PATH"]}
