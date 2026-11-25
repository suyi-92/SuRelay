#!/usr/bin/env python3
# Managed by surelay source guard.
"""Apply and verify source ACLs before UFW's ordinary INPUT rules.

Only SURELAY_SOURCE is rebuilt. iptables-restore --noflush keeps other chains,
including UFW and SSH policy, intact. RETURN for an allowed source deliberately
continues through UFW instead of granting access around its other restrictions.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List


CHAIN = "SURELAY_SOURCE"
SOURCE_MARKER = "surelay-source-v1"
WAIT_SECONDS = "5"
INSTALLED_HELPER = Path("/usr/local/lib/surelay/source_firewall.py")
UFW_AFTER_HOOK = Path("/etc/ufw/after.init")
HOOK_MARKER = "# SuRelay source ACL after.init wrapper v1"
ORIGINAL_BEGIN = "# BEGIN SURELAY ORIGINAL AFTER.INIT"
ORIGINAL_END = "# END SURELAY ORIGINAL AFTER.INIT"


class FirewallError(RuntimeError):
    pass


def normalize_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(plan, dict) or type(plan.get("version")) is not int or plan["version"] != 1:
        raise FirewallError("来源白名单计划版本不受支持。")
    if not isinstance(plan.get("ipv6"), bool) or not isinstance(plan.get("entries"), list):
        raise FirewallError("来源白名单计划格式不正确。")
    entries = []
    ports = set()
    for entry in plan["entries"]:
        if not isinstance(entry, dict):
            raise FirewallError("来源白名单入口格式不正确。")
        port = entry.get("port")
        sources = entry.get("sources")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535 or port in ports:
            raise FirewallError("来源白名单包含无效或重复的 TCP 端口。")
        if not isinstance(sources, list) or not sources:
            raise FirewallError(f"TCP/{port} 缺少来源 IP 白名单。")
        networks = []
        for source in sources:
            if not isinstance(source, str):
                raise FirewallError(f"TCP/{port} 的来源地址必须是 IP/CIDR 字符串。")
            try:
                network = ipaddress.ip_network(source, strict=False)
            except (TypeError, ValueError) as exc:
                raise FirewallError(f"TCP/{port} 的来源地址不是有效 IP/CIDR。") from exc
            if network.prefixlen == 0:
                raise FirewallError("来源白名单不能包含全网 /0。")
            if isinstance(network, ipaddress.IPv6Network) and network.network_address.ipv4_mapped is not None:
                raise FirewallError("来源白名单请使用原生 IPv4 或 IPv6，不能使用 IPv4 映射 IPv6。")
            if network not in networks:
                networks.append(network)
        canonical = []
        for version in (4, 6):
            collapsed = list(ipaddress.collapse_addresses(n for n in networks if n.version == version))
            if any(n.prefixlen == 0 for n in collapsed):
                raise FirewallError("来源白名单合并后覆盖全网，不能用于受限入口。")
            canonical.extend(str(n) for n in collapsed)
        entries.append({"port": port, "sources": canonical})
        ports.add(port)
    return {"version": 1, "ipv6": plan["ipv6"], "entries": sorted(entries, key=lambda e: e["port"])}


def expected_rules(plan: Dict[str, Any], version: int = 4) -> List[List[str]]:
    if version not in (4, 6):
        raise FirewallError("来源白名单只支持 IPv4/IPv6。")
    plan = normalize_plan(plan)
    rules = [["-A", CHAIN, "-i", "lo", "-m", "comment", "--comment", SOURCE_MARKER, "-j", "RETURN"]]
    for entry in plan["entries"]:
        for source in entry["sources"]:
            if ipaddress.ip_network(source).version == version:
                rules.append(["-A", CHAIN, "-s", source, "-p", "tcp", "-m", "tcp",
                              "--dport", str(entry["port"]), "-j", "RETURN"])
        # No source of this family means deny-all for this port, not allow-all.
        rules.append(["-A", CHAIN, "-p", "tcp", "-m", "tcp", "--dport", str(entry["port"]), "-j", "DROP"])
    rules.append(["-A", CHAIN, "-j", "RETURN"])
    return rules


def parse_rules(text: str) -> List[List[str]]:
    rules = []
    for line in text.splitlines():
        words = shlex.split(line)
        if not words or words[0] != "-A":
            continue
        for index, word in enumerate(words[:-1]):
            if word in {"-s", "-d", "--source", "--destination"}:
                try:
                    words[index + 1] = str(ipaddress.ip_network(words[index + 1], strict=False))
                except ValueError as exc:
                    raise FirewallError("内核防火墙返回了无法识别的地址规则。") from exc
        rules.append(words)
    return rules


def run_command(args: List[str], *, input=None, check=True):
    try:
        result = subprocess.run(args, input=input, text=True, capture_output=True,
                                encoding="utf-8", timeout=30,
                                env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FirewallError(f"无法执行来源白名单检查：{args[0]}：{exc}") from exc
    if check and result.returncode:
        raise FirewallError(f"来源白名单命令失败：{shlex.join(args)}：{result.stderr.strip()}")
    return result


def _command_result(runner, args, *, input=None):
    result = runner(args, input=input, check=True)
    # Also reject test/custom runners that do not implement check=True.
    if result.returncode:
        raise FirewallError(f"来源白名单命令失败：{shlex.join(args)}：{(result.stderr or '').strip()}")
    return result


def _families(plan):
    return (4, 6) if plan["ipv6"] else (4,)


def _binary(version):
    return "ip6tables" if version == 6 else "iptables"


def _check_family(plan, version, runner):
    binary = _binary(version)
    input_rules = parse_rules(_command_result(runner, [binary, "-w", WAIT_SECONDS, "-S", "INPUT"]).stdout)
    if not input_rules or input_rules[0] != ["-A", "INPUT", "-j", CHAIN]:
        raise FirewallError(f"IPv{version} 来源白名单没有位于 INPUT 首条；拒绝启用受限服务。")
    rules = parse_rules(_command_result(runner, [binary, "-w", WAIT_SECONDS, "-S", CHAIN]).stdout)
    if rules != expected_rules(plan, version):
        raise FirewallError(f"IPv{version} 来源白名单的允许/拒绝规则不完整或顺序不正确；请重新应用防火墙。")


def _owned_chain(version, runner):
    binary = _binary(version)
    args = [binary, "-w", WAIT_SECONDS, "-S", CHAIN]
    result = runner(args, input=None, check=False)
    if result.returncode:
        error = (result.stderr or "").lower()
        if "no chain" in error or "does not exist" in error:
            return None
        raise FirewallError(f"无法核查 IPv{version} 来源链归属：{(result.stderr or '').strip()}")
    rules = parse_rules(result.stdout)
    owner = ["-A", CHAIN, "-i", "lo", "-m", "comment", "--comment", SOURCE_MARKER, "-j", "RETURN"]
    if not rules or rules[0] != owner:
        raise FirewallError(f"IPv{version} 同名 {CHAIN} 链没有 SuRelay 归属标记；未覆盖或删除。")
    return rules


def check(plan: Dict[str, Any], runner=run_command) -> None:
    plan = normalize_plan(plan)
    if not plan["entries"]:
        return
    status = _command_result(runner, ["ufw", "status"]).stdout
    if "Status: active" not in [line.strip() for line in status.splitlines()]:
        raise FirewallError("受限入口要求 UFW 处于 active 状态；请先应用防火墙。")
    for version in _families(plan):
        _check_family(plan, version, runner)


def restore_text(plan: Dict[str, Any], version: int) -> str:
    # Declaring only this user chain replaces it atomically with --noflush.
    # https://man7.org/linux/man-pages/man8/iptables-restore.8.html
    return "\n".join(["*filter", f":{CHAIN} - [0:0]",
                      *(shlex.join(rule) for rule in expected_rules(plan, version)), "COMMIT", ""])


def restore(plan: Dict[str, Any], runner=run_command) -> None:
    """Restore kernel ACLs from an UFW hook without querying UFW's own lock."""
    plan = normalize_plan(plan)
    for version in _families(plan):
        binary = _binary(version)
        _owned_chain(version, runner)
        _command_result(runner, [binary + "-restore", "--wait", WAIT_SECONDS, "--noflush"],
                        input=restore_text(plan, version))
        current = parse_rules(_command_result(runner, [binary, "-w", WAIT_SECONDS, "-S", "INPUT"]).stdout)
        if not current or current[0] != ["-A", "INPUT", "-j", CHAIN]:
            _command_result(runner, [binary, "-w", WAIT_SECONDS, "-I", "INPUT", "1", "-j", CHAIN])
        _check_family(plan, version, runner)


def apply(plan: Dict[str, Any], runner=run_command) -> None:
    plan = normalize_plan(plan)
    restore(plan, runner)
    check(plan, runner)


def _atomic(path: Path, content: bytes, mode: int) -> None:
    path = Path(path)
    if path.is_symlink():
        raise FirewallError(f"来源白名单持久化目标不能是符号链接：{path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(content)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def after_hook_text(original: str, helper_path: Path, plan_path: Path) -> str:
    """Keep an existing hook's exit/exec inside a child, then enforce the ACL."""
    if "\x00" in original:
        raise FirewallError("现有 UFW after.init 包含无效内容；未覆盖。")
    lines = original.splitlines(keepends=True)
    shebang = lines.pop(0).rstrip("\r\n") if lines and lines[0].startswith("#!") else "#!/bin/sh"
    if shebang not in {"#!/bin/sh", "#!/bin/bash", "#!/usr/bin/env sh", "#!/usr/bin/env bash"}:
        raise FirewallError("现有 UFW after.init 不是受支持的 sh/bash 脚本；未覆盖。")
    body = "".join(lines)
    if HOOK_MARKER in body:
        if not body.splitlines() or body.splitlines()[0] != HOOK_MARKER:
            raise FirewallError("UFW after.init 的 SuRelay 归属标记不在预期位置；未覆盖。")
        if body.count(ORIGINAL_BEGIN) != 1 or body.count(ORIGINAL_END) != 1:
            raise FirewallError("UFW after.init 的 SuRelay 标记损坏；未覆盖。")
        body = body.split(ORIGINAL_BEGIN + "\n", 1)[1].split(ORIGINAL_END, 1)[0]
    elif ORIGINAL_BEGIN in body or ORIGINAL_END in body:
        raise FirewallError("UFW after.init 含有不完整的 SuRelay 标记；未覆盖。")
    if body and not body.endswith("\n"):
        body += "\n"
    command = shlex.join(["/usr/bin/python3", str(helper_path), "--restore", str(plan_path)])
    return (f"{shebang}\n{HOOK_MARKER}\nset +e\n(\n{ORIGINAL_BEGIN}\n{body}"
            f"{ORIGINAL_END}\n)\nsurelay_after_status=$?\n"
            'case "${1:-}" in\n  start|stop|restart|reload|force-reload|flush-all)\n'
            f"    {command} || exit \"$?\"\n    ;;\nesac\n"
            'exit "$surelay_after_status"\n')


def persist(plan: Dict[str, Any], plan_path: Path, *, helper_path=INSTALLED_HELPER,
            hook_path=UFW_AFTER_HOOK) -> None:
    plan = normalize_plan(plan)
    plan_path, helper_path, hook_path = map(Path, (plan_path, helper_path, hook_path))
    if hook_path.is_symlink():
        raise FirewallError("UFW after.init 是符号链接；未覆盖，请先核实其归属。")
    original = hook_path.read_text(encoding="utf-8") if hook_path.exists() else "#!/bin/sh\n"
    hook = after_hook_text(original, helper_path, plan_path)
    _atomic(helper_path, Path(__file__).read_bytes(), 0o755)
    _atomic(plan_path, (json.dumps(plan, ensure_ascii=False, indent=2) + "\n").encode("utf-8"), 0o600)
    _atomic(hook_path, hook.encode("utf-8"), 0o755)


def remove_persistence(plan_path: Path, runner=run_command, *, helper_path=INSTALLED_HELPER,
                       hook_path=UFW_AFTER_HOOK) -> None:
    """Remove only SuRelay anchors and unwrap our hook after services stop.

    The caller owns deletion of the saved plan/helper after this succeeds.
    Keeping those files until then permits an uninstall/reset to be retried.
    """
    plan_path, helper_path, hook_path = map(Path, (plan_path, helper_path, hook_path))
    plan = normalize_plan(json.loads(plan_path.read_text(encoding="utf-8")))
    original = None
    if hook_path.is_symlink():
        raise FirewallError("UFW after.init 是符号链接；未修改，请先核实其归属。")
    if hook_path.exists():
        text = hook_path.read_text(encoding="utf-8")
        if HOOK_MARKER in text:
            lines = text.splitlines(keepends=True)
            shebang = lines[0].rstrip("\r\n")
            if len(lines) < 2 or lines[1].rstrip("\r\n") != HOOK_MARKER:
                raise FirewallError("UFW after.init 的 SuRelay 归属标记不在预期位置；未修改。")
            if text.count(ORIGINAL_BEGIN) != 1 or text.count(ORIGINAL_END) != 1:
                raise FirewallError("UFW after.init 的 SuRelay 标记损坏；未修改。")
            expected = ["/usr/bin/python3", str(helper_path), "--restore", str(plan_path)]
            if not any(shlex.split(line)[:4] == expected for line in lines if "--restore" in line):
                raise FirewallError("UFW after.init 不属于指定的来源白名单计划；未修改。")
            body = text.split(ORIGINAL_BEGIN + "\n", 1)[1].split(ORIGINAL_END, 1)[0]
            original = shebang + "\n" + body
        elif ORIGINAL_BEGIN in text or ORIGINAL_END in text:
            raise FirewallError("UFW after.init 含有不完整的 SuRelay 标记；未修改。")
    for version in (4, 6):
        binary = _binary(version)
        try:
            result = runner([binary, "-w", WAIT_SECONDS, "-S", "INPUT"], input=None, check=False)
        except FirewallError:
            if version == 6 and not plan["ipv6"]:
                continue
            raise
        if result.returncode:
            if version == 6 and not plan["ipv6"]:
                continue
            raise FirewallError(f"无法核查 IPv{version} 的 SuRelay 来源链；保留持久化配置。")
        owned = _owned_chain(version, runner)
        if owned is None:
            continue
        for rule in parse_rules(result.stdout):
            if len(rule) >= 4 and "-j" in rule and rule[rule.index("-j") + 1] == CHAIN:
                _command_result(runner, [binary, "-w", WAIT_SECONDS, "-D", *rule[1:]])
        _command_result(runner, [binary, "-w", WAIT_SECONDS, "-F", CHAIN])
        _command_result(runner, [binary, "-w", WAIT_SECONDS, "-X", CHAIN])
    if original is not None:
        _atomic(hook_path, original.encode("utf-8"), 0o755)


def main() -> int:
    parser = argparse.ArgumentParser(description="SuRelay source ACL kernel enforcement")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--apply", "--apply-plan", action="store_true")
    modes.add_argument("--check", action="store_true")
    modes.add_argument("--restore", type=Path)
    modes.add_argument("--remove-persistence", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--persist", type=Path)
    args = parser.parse_args()
    try:
        if args.remove_persistence is not None:
            if args.dry_run:
                print(f"DRY-RUN: remove SuRelay source ACL persistence for {args.remove_persistence}")
            else:
                remove_persistence(args.remove_persistence)
            return 0
        if args.restore is not None:
            plan = normalize_plan(json.loads(args.restore.read_text(encoding="utf-8")))
        else:
            plan = normalize_plan(json.load(sys.stdin))
        if args.dry_run:
            for version in _families(plan):
                print(f"DRY-RUN: IPv{version} INPUT 首条跳转 {CHAIN}")
                print(restore_text(plan, version), end="")
        elif args.apply:
            if args.persist is not None:
                persist(plan, args.persist)
            apply(plan)
        elif args.restore is not None:
            restore(plan)
        else:
            check(plan)
    except (FirewallError, OSError, ValueError, TypeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
