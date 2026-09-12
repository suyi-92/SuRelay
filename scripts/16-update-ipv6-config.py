#!/usr/bin/env python3
"""Safely add an IPv6 listener address to an existing deployment config."""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
from pathlib import Path
from typing import Dict


CONFIG_UPDATES = {
    "ENABLE_IPV6_LISTEN": "true",
    "CLIENT_UDP": "true",
    "RESET_PROXY_KEYS": "false",
}


def normalize_ipv6(value: str) -> str:
    try:
        address = ipaddress.IPv6Address(value.strip().strip("[]"))
    except ValueError as exc:
        raise ValueError(f"IPv6 地址不合法：{value}（{exc}）") from exc
    if address.is_unspecified or address.is_loopback or address.is_multicast or address.is_link_local:
        raise ValueError(f"不能把 {address} 用作公网 VLESS 节点地址")
    return address.compressed


def render_value(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def update_config(path: Path, ipv6: str) -> Dict[str, str]:
    if not path.is_file():
        raise FileNotFoundError(f"找不到现有配置：{path}")

    normalized = normalize_ipv6(ipv6)
    updates = {"SERVER_IP_IPV6": normalized, **CONFIG_UPDATES}
    lines = path.read_text(encoding="utf-8").splitlines()
    found = set()
    patterns = {
        key: re.compile(rf"^(?P<prefix>\s*(?:export\s+)?{re.escape(key)}\s*=).*$")
        for key in updates
    }

    rendered = []
    for line in lines:
        replacement = None
        for key, pattern in patterns.items():
            match = pattern.match(line)
            if match:
                replacement = f"{match.group('prefix')}{render_value(updates[key])}"
                found.add(key)
                break
        rendered.append(replacement if replacement is not None else line)

    missing = [key for key in updates if key not in found]
    if missing:
        if rendered and rendered[-1].strip():
            rendered.append("")
        rendered.append("# Added by install.sh -6 IPv6 node maintenance mode.")
        rendered.extend(f"{key}={render_value(updates[key])}" for key in missing)

    text = "\n".join(rendered) + "\n"
    tmp = path.with_name(path.name + ".ipv6.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(path)
        os.chmod(path, 0o600)
    finally:
        if tmp.exists():
            tmp.unlink()
    return updates


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-env", type=Path, required=True)
    parser.add_argument("--address", required=True)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    try:
        if args.check_only:
            print(normalize_ipv6(args.address))
            return 0
        updates = update_config(args.config_env.resolve(), args.address)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(updates["SERVER_IP_IPV6"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
