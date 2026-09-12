#!/usr/bin/env python3
"""
Generate /etc/sing-box/config.json from config.env and home-proxies.csv.

Security notes:
- Inbound protocol is VLESS + Reality.
- This script never prints VLESS UUID, Reality private key, or home proxy passwords.
- CSV is parsed with Python's csv module, so quoted commas are supported.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

TAG_RE = re.compile(r"^[A-Za-z0-9_-]+$")
DOMAIN_RE = re.compile(r"^[A-Za-z0-9._-]+$")
CUSTOM_DOMAIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9]$")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
SHORT_ID_RE = re.compile(r"^[0-9a-fA-F]{0,16}$")
REQUIRED_COLUMNS = {
    "tag",
    "listen_port",
    "type",
    "server",
    "server_port",
    "username",
    "password",
    "network",
}
REQUIRED_UPSTREAM_COLUMNS = {
    "tag",
    "listen_port",
    "node_url",
}
HOME_PROXY_TYPES = {"socks5", "socks", "http"}
UPSTREAM_SCHEMES = {"vless"}

SUBSCRIPTION_TARGETS = {"ClashMeta"}


class ConfigError(SystemExit):
    pass


def project_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def parse_env_file(path: Path) -> Dict[str, str]:
    if not path.exists():
        raise ConfigError(f"找不到配置文件：{path}")
    env: Dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            continue
        if value:
            try:
                parts = shlex.split(value, comments=False, posix=True)
                env[key] = parts[0] if parts else ""
            except ValueError:
                env[key] = value.strip("'\"")
        else:
            env[key] = ""
    return env


def defaults(env: Dict[str, str]) -> Dict[str, str]:
    merged = dict(env)
    state_dir = merged.get("RRB_STATE_DIR") or "/etc/reality-relay-bootstrap"
    d = {
        "SERVER_ALIAS": "my-vps",
        "SERVER_IP": "",
        "SERVER_IP_IPV4": "",
        "SERVER_IP_IPV6": "",
        "SSH_PORT": "22",
        "INITIAL_USER": "root",
        "ADMIN_USER": "root",
        "ADMIN_PUBKEY": "",
        "ADMIN_PUBKEYS": "",
        "ENABLE_SFTP_USER": "false",
        "SFTP_USER": "sftpuser",
        "USE_233BOY_INSTALLER": "false",
        "INSTALL_SINGBOX_METHOD": "apt",
        "PROXY_PROTOCOL": "vless-reality",
        "PROXY_IP_VERSION": "ipv4",
        "MODE_443": "direct",
        "DIRECT_PORT": "443",
        "HOME_PORT_START": "51043",
        "HOME_PORT_END": "65535",
        "ENABLE_UFW": "true",
        "ENABLE_FAIL2BAN": "true",
        "ENABLE_IPV6_LISTEN": "false",
        "CSV_PATH": "./home-proxies.csv",
        "UPSTREAM_NODES_PATH": "./upstream-nodes.txt",
        "RRB_STATE_DIR": state_dir,
        "ENABLE_CUSTOM_DOMAIN": "false",
        "CUSTOM_DOMAIN": "",
        "CUSTOM_DOMAIN_PROVIDER": "cloudflare",
        "LE_EMAIL": "",
        "LE_STAGING": "false",
        "CLOUDFLARE_API_TOKEN": "",
        "CLOUDFLARE_API_TOKEN_FILE": f"{state_dir}/cloudflare.ini",
        "CLOUDFLARE_ZONE_NAME": "",
        "CLOUDFLARE_ACCOUNT_ID": "",
        "CLOUDFLARE_CREATE_ZONE": "false",
        "CLOUDFLARE_ENSURE_DNS_RECORDS": "true",
        "CLOUDFLARE_DNS_OVERWRITE_EXISTING": "true",
        "CLOUDFLARE_REQUIRE_ACTIVE_ZONE": "true",
        "CLOUDFLARE_DNS_TTL": "1",
        "CLOUDFLARE_DNS_PROPAGATION_SECONDS": "60",
        "NGINX_FALLBACK_HOST": "127.0.0.1",
        "NGINX_FALLBACK_PORT": "8443",
        "NGINX_FALLBACK_ROOT": "/var/www/reality-fallback",
        "NGINX_FALLBACK_CONF": "/etc/nginx/sites-available/reality-relay-bootstrap.conf",
        "SINGBOX_CONFIG_PATH": "/etc/sing-box/config.json",
        "VLESS_UUID_PATH": f"{state_dir}/vless-uuid.txt",
        "VLESS_FLOW": "xtls-rprx-vision",
        "REALITY_PRIVATE_KEY_PATH": f"{state_dir}/reality-private.key",
        "REALITY_PUBLIC_KEY_PATH": f"{state_dir}/reality-public.key",
        "REALITY_SHORT_ID_PATH": f"{state_dir}/reality-short-id.txt",
        "REALITY_SERVER_NAME": "www.microsoft.com",
        "REALITY_HANDSHAKE_SERVER": "www.microsoft.com",
        "REALITY_HANDSHAKE_PORT": "443",
        "REALITY_MAX_TIME_DIFFERENCE": "1m",
        "CLIENT_FINGERPRINT": "chrome",
        "CLIENT_UDP": "true",
        "CLIENT_ALPN": "h2,http/1.1",
        "CLASH_MIXED_PORT": "7890",
        "ENABLE_SUBSCRIPTION_SERVER": "true",
        "SUBSCRIPTION_PORT": "",
        "SUBSCRIPTION_INTERNAL_PORT": "51040",
        "SUBSCRIPTION_LISTEN_PORT": "",
        "SUBSCRIPTION_DIR": f"{state_dir}/subscription",
        "RESET_PROXY_KEYS": "false",
        "SUBSCRIPTION_TARGET": "ClashMeta",
        "SUBSCRIPTION_BASE_URL": "",
    }
    for k, v in d.items():
        merged.setdefault(k, v)
    if merged.get("MODE_443", "direct") != "direct":
        print(f"WARN: MODE_443={merged.get('MODE_443')} 已废弃；当前固定按 direct 处理。", file=sys.stderr)
        merged["MODE_443"] = "direct"
    server_ip = merged.get("SERVER_IP", "")
    if server_ip and ":" in server_ip and not merged.get("SERVER_IP_IPV6"):
        merged["SERVER_IP_IPV6"] = server_ip
    elif server_ip and ":" not in server_ip and not merged.get("SERVER_IP_IPV4"):
        merged["SERVER_IP_IPV4"] = server_ip
    if not merged.get("SERVER_IP"):
        merged["SERVER_IP"] = merged.get("SERVER_IP_IPV4") or merged.get("SERVER_IP_IPV6") or ""
    if merged.get("ADMIN_USER", "root") != "root":
        print(f"WARN: ADMIN_USER={merged.get('ADMIN_USER')} 已废弃；当前统一使用 root。", file=sys.stderr)
        merged["ADMIN_USER"] = "root"

    merged["SUBSCRIPTION_TARGET"] = normalize_subscription_target(merged.get("SUBSCRIPTION_TARGET", "ClashMeta"))
    apply_custom_domain_defaults(merged)
    apply_subscription_defaults(merged)

    default_state_dir = "/etc/reality-relay-bootstrap"
    if merged["RRB_STATE_DIR"] != default_state_dir:
        default_paths = {
            "VLESS_UUID_PATH": "vless-uuid.txt",
            "REALITY_PRIVATE_KEY_PATH": "reality-private.key",
            "REALITY_PUBLIC_KEY_PATH": "reality-public.key",
            "REALITY_SHORT_ID_PATH": "reality-short-id.txt",
            "CLOUDFLARE_API_TOKEN_FILE": "cloudflare.ini",
        }
        for key, filename in default_paths.items():
            if merged.get(key) == f"{default_state_dir}/{filename}":
                merged[key] = f"{merged['RRB_STATE_DIR']}/{filename}"
    return merged


def custom_domain_enabled(env: Dict[str, str]) -> bool:
    return env.get("ENABLE_CUSTOM_DOMAIN", "false").lower() == "true"


def apply_custom_domain_defaults(env: Dict[str, str]) -> None:
    if not custom_domain_enabled(env):
        return
    domain = env.get("CUSTOM_DOMAIN", "").strip()
    if not domain:
        return
    if env.get("REALITY_SERVER_NAME", "") in {"", "www.microsoft.com"}:
        env["REALITY_SERVER_NAME"] = domain
    if env.get("REALITY_HANDSHAKE_SERVER", "") in {"", "www.microsoft.com"}:
        env["REALITY_HANDSHAKE_SERVER"] = env.get("NGINX_FALLBACK_HOST", "127.0.0.1")
    if env.get("REALITY_HANDSHAKE_PORT", "") in {"", "443"}:
        env["REALITY_HANDSHAKE_PORT"] = env.get("NGINX_FALLBACK_PORT", "8443")
    if env.get("ENABLE_SUBSCRIPTION_SERVER", "true").lower() == "true" and not env.get("SUBSCRIPTION_BASE_URL", ""):
        env["SUBSCRIPTION_BASE_URL"] = f"https://{domain}"
    if not env.get("CLOUDFLARE_ZONE_NAME", "").strip():
        env["CLOUDFLARE_ZONE_NAME"] = infer_cloudflare_zone_name(domain)


def apply_subscription_defaults(env: Dict[str, str]) -> None:
    if env.get("ENABLE_SUBSCRIPTION_SERVER", "true").lower() != "true":
        return

    if custom_domain_enabled(env):
        env["SUBSCRIPTION_LISTEN_PORT"] = env.get("SUBSCRIPTION_PORT", "").strip() or env["SUBSCRIPTION_INTERNAL_PORT"]
    else:
        if not env.get("SUBSCRIPTION_PORT", "").strip():
            env["SUBSCRIPTION_PORT"] = env["SUBSCRIPTION_INTERNAL_PORT"]
        env["SUBSCRIPTION_LISTEN_PORT"] = env["SUBSCRIPTION_PORT"]


def infer_cloudflare_zone_name(domain: str) -> str:
    labels = [part for part in domain.strip(".").split(".") if part]
    if len(labels) >= 2:
        return ".".join(labels[-2:])
    return domain


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
        raise ConfigError(f"SUBSCRIPTION_TARGET 不支持：{value}")
    if compact not in {"", "mihomo", "clash", "clashmeta"}:
        print(f"WARN: SUBSCRIPTION_TARGET={value} 已废弃；当前只生成 ClashMeta/Mihomo 订阅。", file=sys.stderr)
    return target


def as_bool(env: Dict[str, str], key: str) -> bool:
    value = env.get(key, "").lower()
    if value not in {"true", "false"}:
        raise ConfigError(f"{key} 必须是 true 或 false，当前：{env.get(key)}")
    return value == "true"


def as_port(env: Dict[str, str], key: str) -> int:
    value = env.get(key, "")
    if not value.isdigit():
        raise ConfigError(f"{key} 必须是数字端口，当前：{value}")
    port = int(value)
    if not 1 <= port <= 65535:
        raise ConfigError(f"{key} 超出端口范围 1-65535：{port}")
    return port


def as_nonnegative_int(env: Dict[str, str], key: str) -> int:
    value = env.get(key, "")
    if not value.isdigit():
        raise ConfigError(f"{key} 必须是非负整数，当前：{value}")
    return int(value)


def resolve_path(env: Dict[str, str], key: str, base: Path) -> Path:
    value = env[key]
    p = Path(value)
    if not p.is_absolute():
        p = base / value.lstrip("./")
    return p


def is_posix_absolute(value: str) -> bool:
    return value.startswith("/")


def validate_env(env: Dict[str, str]) -> None:
    as_port(env, "SSH_PORT")
    direct_port = as_port(env, "DIRECT_PORT")
    start = as_port(env, "HOME_PORT_START")
    end = as_port(env, "HOME_PORT_END")
    as_port(env, "REALITY_HANDSHAKE_PORT")
    as_port(env, "SUBSCRIPTION_INTERNAL_PORT")
    if env.get("SUBSCRIPTION_PORT", "").strip():
        as_port(env, "SUBSCRIPTION_PORT")
    subscription_listen_port = None
    if env.get("ENABLE_SUBSCRIPTION_SERVER", "true") == "true":
        subscription_listen_port = as_port(env, "SUBSCRIPTION_LISTEN_PORT")
    fallback_port = as_port(env, "NGINX_FALLBACK_PORT")
    dns_ttl = as_nonnegative_int(env, "CLOUDFLARE_DNS_TTL")
    as_nonnegative_int(env, "CLOUDFLARE_DNS_PROPAGATION_SECONDS")
    if start > end:
        raise ConfigError("HOME_PORT_START 不能大于 HOME_PORT_END")
    if env["PROXY_PROTOCOL"] != "vless-reality":
        raise ConfigError("PROXY_PROTOCOL 当前只支持 vless-reality")
    if env["PROXY_IP_VERSION"] not in {"ipv4", "ipv6", "dual"}:
        raise ConfigError("PROXY_IP_VERSION 只能是 ipv4、ipv6 或 dual")
    if env["MODE_443"] != "direct":
        raise ConfigError("MODE_443 当前只支持 direct")
    if not env["SERVER_ALIAS"].strip():
        raise ConfigError("SERVER_ALIAS 不能为空；它会作为 443 节点名称")
    if env["INSTALL_SINGBOX_METHOD"] not in {"apt", "233boy"}:
        raise ConfigError("INSTALL_SINGBOX_METHOD 只能是 apt 或 233boy")
    if env.get("VLESS_FLOW", "") not in {"", "xtls-rprx-vision"}:
        raise ConfigError("VLESS_FLOW 目前建议为空或 xtls-rprx-vision")
    for key in ["REALITY_SERVER_NAME", "REALITY_HANDSHAKE_SERVER"]:
        if not DOMAIN_RE.match(env[key]):
            raise ConfigError(f"{key} 不合法：{env[key]}")
    for key in [
        "ENABLE_SFTP_USER",
        "USE_233BOY_INSTALLER",
        "ENABLE_UFW",
        "ENABLE_FAIL2BAN",
        "ENABLE_IPV6_LISTEN",
        "ENABLE_CUSTOM_DOMAIN",
        "LE_STAGING",
        "CLOUDFLARE_CREATE_ZONE",
        "CLOUDFLARE_ENSURE_DNS_RECORDS",
        "CLOUDFLARE_DNS_OVERWRITE_EXISTING",
        "CLOUDFLARE_REQUIRE_ACTIVE_ZONE",
        "CLIENT_UDP",
        "ENABLE_SUBSCRIPTION_SERVER",
        "RESET_PROXY_KEYS",
    ]:
        as_bool(env, key)
    normalize_subscription_target(env.get("SUBSCRIPTION_TARGET", "ClashMeta"))
    if env["CUSTOM_DOMAIN_PROVIDER"] != "cloudflare":
        raise ConfigError("CUSTOM_DOMAIN_PROVIDER 当前只支持 cloudflare")
    for key in ["CLOUDFLARE_API_TOKEN_FILE", "NGINX_FALLBACK_ROOT", "NGINX_FALLBACK_CONF"]:
        if not is_posix_absolute(env[key]):
            raise ConfigError(f"{key} 必须是绝对路径")
    if env["ENABLE_SUBSCRIPTION_SERVER"] == "true":
        assert subscription_listen_port is not None
        if subscription_listen_port in {as_port(env, "SSH_PORT"), direct_port}:
            raise ConfigError("SUBSCRIPTION_LISTEN_PORT 不能与 SSH_PORT 或 DIRECT_PORT 相同")
        if start <= subscription_listen_port <= end:
            raise ConfigError("SUBSCRIPTION_LISTEN_PORT 不能落在 HOME_PORT_START/HOME_PORT_END 范围内")
    if custom_domain_enabled(env):
        domain = env.get("CUSTOM_DOMAIN", "").strip()
        if not domain:
            raise ConfigError("ENABLE_CUSTOM_DOMAIN=true 时 CUSTOM_DOMAIN 不能为空")
        if not CUSTOM_DOMAIN_RE.match(domain) or "." not in domain or ".." in domain:
            raise ConfigError(f"CUSTOM_DOMAIN 不合法：{domain}")
        zone_name = env.get("CLOUDFLARE_ZONE_NAME", "").strip()
        if not zone_name:
            raise ConfigError("ENABLE_CUSTOM_DOMAIN=true 时 CLOUDFLARE_ZONE_NAME 不能为空")
        if not CUSTOM_DOMAIN_RE.match(zone_name) or "." not in zone_name or ".." in zone_name:
            raise ConfigError(f"CLOUDFLARE_ZONE_NAME 不合法：{zone_name}")
        if dns_ttl not in {1} and dns_ttl < 60:
            raise ConfigError("CLOUDFLARE_DNS_TTL 必须为 1（自动）或 >=60")
        if env["CLOUDFLARE_CREATE_ZONE"] == "true" and not env.get("CLOUDFLARE_ACCOUNT_ID", "").strip():
            raise ConfigError("CLOUDFLARE_CREATE_ZONE=true 时 CLOUDFLARE_ACCOUNT_ID 不能为空")
        if not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", env.get("LE_EMAIL", "")):
            raise ConfigError("ENABLE_CUSTOM_DOMAIN=true 时 LE_EMAIL 必须是有效邮箱")
        if direct_port != 443:
            raise ConfigError("ENABLE_CUSTOM_DOMAIN=true 要求 DIRECT_PORT=443（公网 443 由 sing-box 接管）")
        occupied_ports = {direct_port, as_port(env, "SSH_PORT")}
        if subscription_listen_port is not None:
            occupied_ports.add(subscription_listen_port)
        if fallback_port in occupied_ports:
            raise ConfigError("NGINX_FALLBACK_PORT 不能与 DIRECT_PORT、SSH_PORT 或 SUBSCRIPTION_LISTEN_PORT 相同")
        if env["REALITY_SERVER_NAME"] != domain:
            raise ConfigError("启用自有域名时 REALITY_SERVER_NAME 必须等于 CUSTOM_DOMAIN")
        if env["REALITY_HANDSHAKE_SERVER"] != env["NGINX_FALLBACK_HOST"]:
            raise ConfigError("启用自有域名时 REALITY_HANDSHAKE_SERVER 必须等于 NGINX_FALLBACK_HOST")
        if env["REALITY_HANDSHAKE_PORT"] != env["NGINX_FALLBACK_PORT"]:
            raise ConfigError("启用自有域名时 REALITY_HANDSHAKE_PORT 必须等于 NGINX_FALLBACK_PORT")
    if direct_port != 443:
        print(f"WARN: DIRECT_PORT={direct_port}，不是默认 443。", file=sys.stderr)


def iter_csv_lines(path: Path) -> Iterable[str]:
    for line in path.read_text(encoding="utf-8-sig").splitlines(True):
        if line.strip() and not line.lstrip().startswith("#"):
            yield line


def check_tag_and_port(
    *,
    tag: str,
    listen_port: int,
    direct_port: int,
    start: int,
    end: int,
    used_tags: set[str],
    used_ports: set[int],
    label: str,
    lineno: int,
) -> None:
    if not TAG_RE.match(tag):
        raise ConfigError(f"{label} 第 {lineno} 行 tag 不合法：{tag}；只能用英文、数字、下划线、短横线")
    if tag in used_tags:
        raise ConfigError(f"{label} 第 {lineno} 行 tag 重复：{tag}")
    if not (1 <= listen_port <= 65535):
        raise ConfigError(f"{label} 第 {lineno} 行 listen_port 超出范围 1-65535：{listen_port}")
    if listen_port == direct_port:
        raise ConfigError(f"{label} 第 {lineno} 行 listen_port 不能使用 {direct_port}；该端口保留给 443 入口")
    if listen_port in used_ports:
        raise ConfigError(f"{label} 第 {lineno} 行 listen_port 重复：{listen_port}")
    if not (start <= listen_port <= end):
        raise ConfigError(f"{label} 第 {lineno} 行 listen_port={listen_port} 不在 HOME_PORT_START/HOME_PORT_END 范围 {start}-{end}")
    used_tags.add(tag)
    used_ports.add(listen_port)


def next_available_port(start: int, end: int, used_ports: set[int]) -> int:
    for port in range(start, end + 1):
        if port not in used_ports:
            return port
    raise ConfigError(f"没有可用上游节点入口端口；请调大 HOME_PORT_END 或移除未使用端口。")


def safe_tag(value: str, fallback: str, used_tags: set[str]) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", unquote(value or "")).strip("-_")
    if not cleaned or not TAG_RE.match(cleaned):
        cleaned = fallback
    candidate = cleaned
    index = 2
    while candidate in used_tags:
        candidate = f"{cleaned}-{index:02d}"
        index += 1
    return candidate


def normalize_home_csv_row(row: Dict[str, Any]) -> Dict[str, str]:
    normalized = {k: (row.get(k) or "").strip() for k in REQUIRED_COLUMNS}

    # Installer bulk mode has historically accepted old rows in this order:
    # tag,listen_port,type,server,server_port,username,password,network
    # If such a row is saved below the new header, remap it before validation.
    if (
        normalized["server"].lower() in HOME_PROXY_TYPES
        and (not normalized["type"] or normalized["type"].isdigit())
        and normalized["username"].isdigit()
    ):
        return {
            "tag": normalized["tag"],
            "listen_port": normalized["type"],
            "type": normalized["server"],
            "server": normalized["server_port"],
            "server_port": normalized["username"],
            "username": normalized["password"],
            "password": normalized["network"],
            "network": normalized["listen_port"] or "tcp",
        }

    return normalized


def looks_like_upstream_url(value: str) -> bool:
    return urlsplit((value or "").strip().replace("&amp;", "&")).scheme.lower() in UPSTREAM_SCHEMES


def normalize_upstream_csv_row(row: Dict[str, Any]) -> Dict[str, str]:
    normalized = {k: (row.get(k) or "").strip() for k in REQUIRED_UPSTREAM_COLUMNS}

    # Compatibility for old rows in this order: tag,listen_port,node_url.
    if looks_like_upstream_url(normalized["listen_port"]) and (
        not normalized["node_url"] or normalized["node_url"].isdigit()
    ):
        normalized["node_url"], normalized["listen_port"] = normalized["listen_port"], normalized["node_url"]

    return normalized


def first_query_value(params: Dict[str, List[str]], *names: str) -> str:
    compact = {re.sub(r"[\s_-]+", "", key).lower(): values for key, values in params.items()}
    for name in names:
        values = compact.get(re.sub(r"[\s_-]+", "", name).lower())
        if values:
            return unquote(values[0])
    return ""


def query_bool(params: Dict[str, List[str]], *names: str) -> bool:
    value = first_query_value(params, *names).lower()
    return value in {"1", "true", "yes", "y"}


def split_csv_items(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def split_host_port(url: str, scheme: str, lineno: int) -> Tuple[Any, str, int, Dict[str, List[str]]]:
    split = urlsplit(url.replace("&amp;", "&"))
    if split.scheme.lower() != scheme:
        raise ConfigError(f"upstream-nodes.txt 第 {lineno} 行协议应为 {scheme}://")
    host = split.hostname
    try:
        port = split.port
    except ValueError as exc:
        raise ConfigError(f"upstream-nodes.txt 第 {lineno} 行 server_port 不合法：{url}") from exc
    if not host or port is None:
        raise ConfigError(f"upstream-nodes.txt 第 {lineno} 行缺少 server 或 server_port：{url}")
    return split, host, port, parse_qs(split.query, keep_blank_values=True)


def userinfo_password(split: Any) -> str:
    if "@" not in split.netloc:
        return ""
    return unquote(split.netloc.rsplit("@", 1)[0])


def parse_vless_upstream(tag: str, listen_port: int, node_url: str, lineno: int) -> Dict[str, Any]:
    split, host, port, params = split_host_port(node_url, "vless", lineno)
    uuid = userinfo_password(split)
    if not UUID_RE.match(uuid):
        raise ConfigError(f"upstream-nodes.txt 第 {lineno} 行 VLESS UUID 不合法。")
    security = (first_query_value(params, "security") or "none").lower()
    if security not in {"none", "reality"}:
        raise ConfigError(f"upstream-nodes.txt 第 {lineno} 行 VLESS 上游目前只支持 security=none 或 security=reality。")
    transport = first_query_value(params, "type", "transport") or "tcp"
    if transport != "tcp":
        raise ConfigError(f"upstream-nodes.txt 第 {lineno} 行 VLESS 上游目前只支持 type=tcp。")
    encryption = first_query_value(params, "encryption") or "none"
    if encryption != "none":
        raise ConfigError(f"upstream-nodes.txt 第 {lineno} 行 VLESS encryption 目前只支持 none。")

    # type=tcp describes the VLESS transport, not the destination network.
    # Omit outbound.network so sing-box can relay both TCP and UDP payloads.
    outbound: Dict[str, Any] = {
        "type": "vless",
        "tag": f"out-{tag}",
        "server": host,
        "server_port": port,
        "uuid": uuid,
        "domain_resolver": "dns-cloudflare",
    }
    flow = first_query_value(params, "flow")
    if flow:
        outbound["flow"] = flow

    if security == "reality":
        public_key = first_query_value(params, "pbk", "public_key", "public-key", "reality-public-key")
        if not public_key:
            raise ConfigError(f"upstream-nodes.txt 第 {lineno} 行 VLESS Reality 缺少 pbk。")
        tls: Dict[str, Any] = {
            "enabled": True,
            "reality": {
                "enabled": True,
                "public_key": public_key,
            },
        }
        server_name = first_query_value(params, "sni", "server_name", "servername")
        if server_name:
            tls["server_name"] = server_name
        short_id = first_query_value(params, "sid", "short_id", "short-id")
        if short_id:
            tls["reality"]["short_id"] = short_id
        fingerprint = first_query_value(params, "fp", "fingerprint")
        if fingerprint:
            tls["utls"] = {"enabled": True, "fingerprint": fingerprint}
        alpn = split_csv_items(first_query_value(params, "alpn"))
        if alpn:
            tls["alpn"] = alpn
        if query_bool(params, "insecure", "allowInsecure"):
            tls["insecure"] = True
        outbound["tls"] = tls

    return {
        "tag": tag,
        "listen_port": listen_port,
        "type": "vless-reality" if security == "reality" else "vless",
        "server": host,
        "server_port": port,
        "source": "upstream",
        "outbound": outbound,
    }


def parse_upstream_node(tag: str, listen_port: int, node_url: str, lineno: int) -> Dict[str, Any]:
    scheme = urlsplit(node_url.strip().replace("&amp;", "&")).scheme.lower()
    if scheme == "vless":
        return parse_vless_upstream(tag, listen_port, node_url, lineno)
    raise ConfigError(f"upstream-nodes.txt 第 {lineno} 行暂不支持协议：{scheme or '空'}；当前只支持 vless:// 上游。")


def load_home_rows(
    env: Dict[str, str],
    base: Path,
    used_tags: set[str],
    used_ports: set[int],
    allow_empty: bool,
) -> List[Dict[str, Any]]:
    csv_path = resolve_path(env, "CSV_PATH", base)
    if not csv_path.exists():
        state_csv = Path(env["RRB_STATE_DIR"]) / "home-proxies.csv"
        if state_csv.exists():
            csv_path = state_csv
        else:
            raise ConfigError(f"找不到 CSV：{csv_path}")

    try:
        reader = csv.DictReader(iter_csv_lines(csv_path))
    except UnicodeDecodeError as exc:
        raise ConfigError(f"CSV 必须是 UTF-8：{exc}") from exc

    if not reader.fieldnames:
        if allow_empty:
            return []
        raise ConfigError("CSV 没有表头")

    missing = REQUIRED_COLUMNS - set(reader.fieldnames)
    if missing:
        raise ConfigError("CSV 表头缺少字段：" + ", ".join(sorted(missing)))

    direct_port = as_port(env, "DIRECT_PORT")
    start = as_port(env, "HOME_PORT_START")
    end = as_port(env, "HOME_PORT_END")
    rows: List[Dict[str, Any]] = []

    for lineno, row in enumerate(reader, start=2):
        normalized = normalize_home_csv_row(row)
        if not any(normalized.values()):
            continue

        tag = normalized["tag"]
        proto = normalized["type"].lower()
        network = (normalized.get("network") or "tcp").lower()

        try:
            listen_port = int(normalized["listen_port"]) if normalized["listen_port"] else next_available_port(start, end, used_ports)
            server_port = int(normalized["server_port"])
        except ValueError as exc:
            raise ConfigError(f"第 {lineno} 行 listen_port/server_port 必须是数字；listen_port 可留空自动分配") from exc

        if not (1 <= server_port <= 65535):
            raise ConfigError(f"第 {lineno} 行 server_port 超出范围 1-65535：{server_port}")
        check_tag_and_port(
            tag=tag,
            listen_port=listen_port,
            direct_port=direct_port,
            start=start,
            end=end,
            used_tags=used_tags,
            used_ports=used_ports,
            label="home-proxies.csv",
            lineno=lineno,
        )

        if proto not in {"socks5", "socks", "http"}:
            raise ConfigError(f"第 {lineno} 行 type 只支持 socks5 或 http")
        if network not in {"tcp", "udp"}:
            raise ConfigError(f"第 {lineno} 行 network 只支持 tcp 或 udp")
        if proto == "http" and network == "udp":
            print(f"WARN: 第 {lineno} 行 {tag} 是 HTTP 代理，通常只支持 TCP；生成配置将不为 HTTP 出站设置 UDP。", file=sys.stderr)
        if not normalized["server"]:
            raise ConfigError(f"第 {lineno} 行 server 不能为空")

        rows.append(
            {
                "tag": tag,
                "listen_port": listen_port,
                "type": "socks5" if proto == "socks" else proto,
                "server": normalized["server"],
                "server_port": server_port,
                "username": normalized["username"],
                "password": normalized["password"],
                "network": network,
                "source": "home",
            }
        )

    if not rows and not allow_empty:
        raise ConfigError("home-proxies.csv 没有任何家宽代理行。")
    return rows


def load_upstream_rows(env: Dict[str, str], base: Path, used_tags: set[str], used_ports: set[int]) -> List[Dict[str, Any]]:
    path = resolve_path(env, "UPSTREAM_NODES_PATH", base)
    if not path.exists():
        state_path = Path(env["RRB_STATE_DIR"]) / "upstream-nodes.txt"
        if state_path.exists():
            path = state_path
        else:
            return []

    try:
        lines = list(iter_csv_lines(path))
    except UnicodeDecodeError as exc:
        raise ConfigError(f"upstream-nodes.txt 必须是 UTF-8：{exc}") from exc
    if not lines:
        return []

    direct_port = as_port(env, "DIRECT_PORT")
    start = as_port(env, "HOME_PORT_START")
    end = as_port(env, "HOME_PORT_END")
    rows: List[Dict[str, Any]] = []
    first = lines[0].strip()

    first_fields = [item.strip().lower() for item in next(csv.reader([first]))]
    if REQUIRED_UPSTREAM_COLUMNS <= set(first_fields):
        reader = csv.DictReader(lines)
        missing = REQUIRED_UPSTREAM_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ConfigError("upstream-nodes.txt 表头缺少字段：" + ", ".join(sorted(missing)))
        iterable = []
        local_seen_ports: set[int] = set()
        for lineno, row in enumerate(reader, start=2):
            normalized = normalize_upstream_csv_row(row)
            if not any(normalized.values()):
                continue
            try:
                listen_port = int(normalized["listen_port"]) if normalized["listen_port"] else next_available_port(start, end, used_ports | local_seen_ports)
            except ValueError as exc:
                raise ConfigError(f"upstream-nodes.txt 第 {lineno} 行 listen_port 必须是数字；也可以留空自动分配") from exc
            local_seen_ports.add(listen_port)
            iterable.append((lineno, normalized["tag"], listen_port, normalized["node_url"]))
    else:
        iterable = []
        local_seen_tags: set[str] = set()
        local_seen_ports: set[int] = set()
        for index, raw in enumerate(lines, start=1):
            node_url = raw.strip()
            if not node_url:
                continue
            split = urlsplit(node_url.replace("&amp;", "&"))
            fragment_tag = safe_tag(split.fragment, f"upstream-{index:02d}", used_tags | local_seen_tags)
            listen_port = next_available_port(start, end, used_ports | local_seen_ports)
            local_seen_tags.add(fragment_tag)
            local_seen_ports.add(listen_port)
            iterable.append((index, fragment_tag, listen_port, node_url))

    for lineno, tag, listen_port, node_url in iterable:
        if not node_url:
            raise ConfigError(f"upstream-nodes.txt 第 {lineno} 行 node_url 不能为空")
        check_tag_and_port(
            tag=tag,
            listen_port=listen_port,
            direct_port=direct_port,
            start=start,
            end=end,
            used_tags=used_tags,
            used_ports=used_ports,
            label="upstream-nodes.txt",
            lineno=lineno,
        )
        rows.append(parse_upstream_node(tag, listen_port, node_url, lineno))
    return rows


def load_rows(env: Dict[str, str], base: Path, allow_empty: bool = True) -> List[Dict[str, Any]]:
    direct_port = as_port(env, "DIRECT_PORT")
    used_tags: set[str] = set()
    used_ports: set[int] = {direct_port}
    rows = load_home_rows(env, base, used_tags, used_ports, allow_empty=True)
    rows.extend(load_upstream_rows(env, base, used_tags, used_ports))
    if not rows and not allow_empty:
        raise ConfigError("没有任何家宽代理或上游节点行。")
    return rows


def read_secret_file(path: str, label: str) -> str:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"找不到 {label} 文件：{p}；请先运行 sudo bash bootstrap.sh --phase singbox")
    value = p.read_text(encoding="utf-8").strip()
    if not value:
        raise ConfigError(f"{label} 文件为空：{p}")
    return value


def read_vless_uuid(env: Dict[str, str]) -> str:
    value = read_secret_file(env["VLESS_UUID_PATH"], "VLESS UUID")
    if not UUID_RE.match(value):
        raise ConfigError(f"VLESS UUID 格式不合法：{env['VLESS_UUID_PATH']}")
    return value


def read_reality_private_key(env: Dict[str, str]) -> str:
    return read_secret_file(env["REALITY_PRIVATE_KEY_PATH"], "Reality private key")


def read_reality_public_key(env: Dict[str, str]) -> str:
    return read_secret_file(env["REALITY_PUBLIC_KEY_PATH"], "Reality public key")


def read_reality_short_id(env: Dict[str, str]) -> str:
    value = read_secret_file(env["REALITY_SHORT_ID_PATH"], "Reality short-id")
    if not SHORT_ID_RE.match(value) or len(value) % 2 != 0:
        raise ConfigError("Reality short-id 必须是 0-16 位、偶数长度的十六进制字符串")
    return value.lower()


def build_vless_reality_inbound(tag: str, port: int, uuid: str, private_key: str, short_id: str, env: Dict[str, str]) -> Dict[str, Any]:
    listen = "::" if as_bool(env, "ENABLE_IPV6_LISTEN") else "0.0.0.0"
    user: Dict[str, Any] = {"name": env["ADMIN_USER"], "uuid": uuid}
    if env.get("VLESS_FLOW", ""):
        user["flow"] = env["VLESS_FLOW"]

    reality: Dict[str, Any] = {
        "enabled": True,
        "handshake": {
            "server": env["REALITY_HANDSHAKE_SERVER"],
            "server_port": as_port(env, "REALITY_HANDSHAKE_PORT"),
        },
        "private_key": private_key,
        "short_id": [short_id],
    }
    if env.get("REALITY_MAX_TIME_DIFFERENCE", ""):
        reality["max_time_difference"] = env["REALITY_MAX_TIME_DIFFERENCE"]

    return {
        "type": "vless",
        "tag": tag,
        "listen": listen,
        "listen_port": port,
        "users": [user],
        "tls": {
            "enabled": True,
            "server_name": env["REALITY_SERVER_NAME"],
            "reality": reality,
        },
    }


def build_home_outbound(row: Dict[str, Any]) -> Dict[str, Any]:
    tag = f"out-{row['tag']}"
    if row.get("source") == "upstream":
        outbound = dict(row["outbound"])
        outbound["tag"] = tag
        return outbound
    if row["type"] in {"socks", "socks5"}:
        outbound: Dict[str, Any] = {
            "type": "socks",
            "tag": tag,
            "server": row["server"],
            "server_port": row["server_port"],
            "version": "5",
            "network": row["network"],
            "domain_resolver": "dns-cloudflare",
        }
        if row["username"]:
            outbound["username"] = row["username"]
        if row["password"]:
            outbound["password"] = row["password"]
        return outbound

    outbound = {
        "type": "http",
        "tag": tag,
        "server": row["server"],
        "server_port": row["server_port"],
        "domain_resolver": "dns-cloudflare",
    }
    if row["username"]:
        outbound["username"] = row["username"]
    if row["password"]:
        outbound["password"] = row["password"]
    return outbound


def dns_strategy(env: Dict[str, str]) -> str:
    strategies = {
        "ipv4": "ipv4_only",
        "ipv6": "ipv6_only",
        "dual": "prefer_ipv4",
    }
    return strategies[env["PROXY_IP_VERSION"]]


def build_config(env: Dict[str, str], rows: List[Dict[str, Any]], uuid: str, private_key: str, short_id: str) -> Dict[str, Any]:
    direct_port = as_port(env, "DIRECT_PORT")
    direct_inbound_tag = "vless-direct-443"

    inbounds = [build_vless_reality_inbound(direct_inbound_tag, direct_port, uuid, private_key, short_id, env)]
    for row in rows:
        inbounds.append(build_vless_reality_inbound(f"vless-{row['tag']}", row["listen_port"], uuid, private_key, short_id, env))

    outbounds: List[Dict[str, Any]] = [{"type": "direct", "tag": "direct"}]
    for row in rows:
        outbounds.append(build_home_outbound(row))
    outbounds.append({"type": "block", "tag": "block"})

    route_rules: List[Dict[str, Any]] = [
        {"inbound": direct_inbound_tag, "action": "route", "outbound": "direct"},
    ]
    for row in rows:
        route_rules.append({"inbound": f"vless-{row['tag']}", "action": "route", "outbound": f"out-{row['tag']}"})

    return {
        "log": {"level": "info", "timestamp": True},
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
            "strategy": dns_strategy(env),
            "cache_capacity": 4096,
        },
        "inbounds": inbounds,
        "outbounds": outbounds,
        "route": {
            "auto_detect_interface": True,
            "default_domain_resolver": "dns-cloudflare",
            "rules": route_rules,
            "final": "block",
        },
        "experimental": {"cache_file": {"enabled": True}},
    }


def port_list(env: Dict[str, str], rows: List[Dict[str, Any]]) -> List[int]:
    ports = [as_port(env, "DIRECT_PORT")]
    ports.extend(int(r["listen_port"]) for r in rows)
    return sorted(set(ports))


def node_list(env: Dict[str, str], rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    direct_port = as_port(env, "DIRECT_PORT")
    direct_name = env["SERVER_ALIAS"]
    nodes = [{"name": direct_name, "port": direct_port, "kind": "direct"}]
    for row in rows:
        nodes.append(
            {
                "name": row["tag"],
                "port": row["listen_port"],
                "kind": row.get("source", "home"),
                "home_tag": row["tag"],
            }
        )
    return nodes


def print_summary(env: Dict[str, str], rows: List[Dict[str, Any]]) -> None:
    print("配置检查通过。")
    print("入站协议：VLESS + Reality")
    print(f"代理 IP 版本：{env['PROXY_IP_VERSION']}（DNS strategy: {dns_strategy(env)}）")
    print(f"443 出口：direct，端口：{env['DIRECT_PORT']}")
    print(f"Reality server_name：{env['REALITY_SERVER_NAME']}")
    print(f"Reality handshake：{env['REALITY_HANDSHAKE_SERVER']}:{env['REALITY_HANDSHAKE_PORT']}")
    if custom_domain_enabled(env):
        print(f"自有域名：{env['CUSTOM_DOMAIN']}（Cloudflare zone: {env['CLOUDFLARE_ZONE_NAME']}；DNS 灰云；Nginx fallback {env['NGINX_FALLBACK_HOST']}:{env['NGINX_FALLBACK_PORT']}）")
    print("将开放端口：" + ", ".join(str(p) for p in port_list(env, rows)))
    home_rows = [row for row in rows if row.get("source") == "home"]
    upstream_rows = [row for row in rows if row.get("source") == "upstream"]
    if home_rows:
        print("家宽映射：")
        for row in home_rows:
            print(f"  {row['tag']} {row['listen_port']} -> {row['type']}://{row['server']}:{row['server_port']}")
    if upstream_rows:
        print("上游节点映射：")
        for row in upstream_rows:
            print(f"  {row['tag']} {row['listen_port']} -> {row['type']}://{row['server']}:{row['server_port']}")
    if not home_rows and not upstream_rows:
        print("没有家宽代理或上游节点行；仅生成 443 入口。")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config-env", default=str(project_dir() / "config.env"))
    ap.add_argument("--check-only", action="store_true")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--print-ports", action="store_true")
    ap.add_argument("--print-nodes-json", action="store_true")
    ap.add_argument("--summary", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="only validate/write without printing the human summary")
    args = ap.parse_args()

    env_path = Path(args.config_env).resolve()
    base = env_path.parent if env_path.exists() else project_dir()
    env = defaults(parse_env_file(env_path))
    validate_env(env)
    rows = load_rows(env, base, allow_empty=True)

    if args.print_ports:
        for p in port_list(env, rows):
            print(p)
        return 0

    if args.print_nodes_json:
        print(json.dumps(node_list(env, rows), ensure_ascii=False))
        return 0

    if args.check_only:
        if not args.quiet:
            print_summary(env, rows)
        return 0

    if args.summary:
        print_summary(env, rows)
        return 0

    if args.write:
        if os.environ.get("RRB_DRY_RUN") == "true":
            print("DRY-RUN: 将生成 VLESS+Reality sing-box 配置，但不会写入文件。")
            print_summary(env, rows)
            return 0
        uuid = read_vless_uuid(env)
        private_key = read_reality_private_key(env)
        short_id = read_reality_short_id(env)
        config = build_config(env, rows, uuid, private_key, short_id)
        config_path = Path(env["SINGBOX_CONFIG_PATH"])
        config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = config_path.with_name(config_path.name + ".tmp")
        tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(config_path)
        os.chmod(config_path, 0o600)
        if not args.quiet:
            print(f"已生成 {config_path}")
            print_summary(env, rows)
        return 0

    ap.error("请指定 --check-only、--write、--print-ports、--print-nodes-json 或 --summary")
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
