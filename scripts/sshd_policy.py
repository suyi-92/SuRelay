#!/usr/bin/env python3
"""Keep the current bootstrap SSH policy ahead of distribution/provider settings."""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path


BEGIN = b"# BEGIN surelay SSH policy"
END = b"# END surelay SSH policy"
LEGACY_BEGIN = b"# BEGIN reality-relay-bootstrap SSH policy"
LEGACY_END = b"# END reality-relay-bootstrap SSH policy"
BOM = b"\xef\xbb\xbf"
SETTINGS_BEGIN = b"# BEGIN SuRelay SSH settings"
SETTINGS_END = b"# END SuRelay SSH settings"


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
    # OpenSSH accumulates AuthenticationMethods values, and `any` is illegal
    # alongside a later/earlier method. Supersede only our own old maintenance
    # overrides before installing the new policy; preserve provider directives.
    keys = {line.split()[0].lower() for line in policy_lines
            if line.strip() and not line.lstrip().startswith(b"#")} - {b"port"}
    lines, inside, seen = [], False, False
    for line in remaining.splitlines(keepends=True):
        marker = line.rstrip(b"\r\n")
        if marker == SETTINGS_BEGIN:
            if inside or seen:
                raise ValueError("SuRelay SSH settings block is duplicated or nested")
            inside = seen = True
        elif marker == SETTINGS_END:
            if not inside:
                raise ValueError("SuRelay SSH settings block has an unmatched end marker")
            inside = False
        elif inside and line.strip() and line.split()[0].lower() in keys:
            continue
        lines.append(line)
    if inside:
        raise ValueError("SuRelay SSH settings block is incomplete")
    remaining = b"".join(lines)
    bom = BOM if remaining.startswith(BOM) else b""
    body = remaining[len(bom):]
    match = re.search(rb"\r\n|\n|\r", body)
    newline = match.group() if match else b"\n"
    block = newline.join([BEGIN, *policy_lines, END]) + newline
    return bom + block + body


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--validate-sshd", help="Check a private candidate with this sshd before replacing config")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--apply", action="store_true", help="Read the current phase policy from stdin")
    action.add_argument("--remove", action="store_true", help="Remove only the managed policy block")
    args = parser.parse_args()
    try:
        original = args.config.read_bytes()
        result = apply_policy(original, sys.stdin.buffer.read()) if args.apply else remove_policy(original)
        if result != original:
            if args.validate_sshd:
                # Validate the full candidate before any live file mutation.
                with tempfile.NamedTemporaryFile(prefix=".surelay-sshd-", dir=args.config.parent) as candidate:
                    candidate.write(result)
                    candidate.flush()
                    validation = subprocess.run([args.validate_sshd, "-t", "-f", candidate.name],
                                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    if validation.returncode:
                        detail = validation.stderr.decode("utf-8", errors="replace").strip()
                        raise ValueError(f"SSH candidate validation failed; original config preserved: {detail}")
            # Keep the existing inode, permissions and all bytes outside the owned block.
            args.config.write_bytes(result)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
