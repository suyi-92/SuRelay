#!/usr/bin/env python3
"""Keep the current bootstrap SSH policy ahead of distribution/provider settings."""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


BEGIN = b"# BEGIN surelay SSH policy"
END = b"# END surelay SSH policy"
LEGACY_BEGIN = b"# BEGIN reality-relay-bootstrap SSH policy"
LEGACY_END = b"# END reality-relay-bootstrap SSH policy"
BOM = b"\xef\xbb\xbf"


def remove_policy(config: bytes) -> bytes:
    bom = BOM if config.startswith(BOM) else b""
    body = config[len(bom):]
    lines = []
    inside = False
    seen = False
    for line in body.splitlines(keepends=True):
        marker = line.rstrip(b"\r\n")
        if marker in (BEGIN, LEGACY_BEGIN):
            if inside or seen:
                raise ValueError("SSH policy block is duplicated or nested; inspect sshd_config before retrying")
            inside = seen = True
        elif marker in (END, LEGACY_END):
            if not inside:
                raise ValueError("SSH policy block has an unmatched end marker")
            inside = False
        elif not inside:
            lines.append(line)
    if inside:
        raise ValueError("SSH policy block is incomplete; inspect sshd_config before retrying")
    return bom + b"".join(lines)


def apply_policy(config: bytes, policy: bytes) -> bytes:
    remaining = remove_policy(config)
    policy_lines = policy.splitlines()
    if not policy.strip() or any(m in policy_lines for m in (BEGIN, END, LEGACY_BEGIN, LEGACY_END)):
        raise ValueError("SSH policy must be nonempty and must not contain managed block markers")
    bom = BOM if remaining.startswith(BOM) else b""
    body = remaining[len(bom):]
    match = re.search(rb"\r\n|\n|\r", body)
    newline = match.group() if match else b"\n"
    block = newline.join([BEGIN, *policy_lines, END]) + newline
    return bom + block + body


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--apply", action="store_true", help="Read the current phase policy from stdin")
    action.add_argument("--remove", action="store_true", help="Remove only the managed policy block")
    args = parser.parse_args()
    try:
        original = args.config.read_bytes()
        result = apply_policy(original, sys.stdin.buffer.read()) if args.apply else remove_policy(original)
        if result != original:
            # Keep the existing inode, permissions and all bytes outside the owned block.
            args.config.write_bytes(result)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
