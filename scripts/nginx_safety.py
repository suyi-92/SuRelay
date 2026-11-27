#!/usr/bin/env python3
"""Disable only verified distribution defaults and guard package auto-start."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path


class NginxSafetyError(RuntimeError):
    pass


# https://github.com/alpinelinux/aports/blob/3.19-stable/main/nginx/default.conf
ALPINE_DEFAULT = """server {
    listen 80 default_server;
    listen [::]:80 default_server;
    location / { return 404; }
    location = /404.html { internal; }
}"""


def nginx_tokens(text):
    # Factory default files have no quoted # characters or user directives.
    text = re.sub(r"(?m)#.*$", "", text)
    return re.findall(r"[^\s{};]+|[{};]", text)


def debian_default_digest(path, runner=subprocess.run):
    result = runner(["dpkg-query", "-W", "-f=${Conffiles}\n", "nginx-common"],
                    capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise NginxSafetyError("无法读取 nginx-common 默认站点校验记录；未修改站点。")
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == str(path) and re.fullmatch(r"[0-9a-f]{32}", fields[1]):
            return fields[1]
    raise NginxSafetyError(f"没有找到发行版默认站点的校验记录：{path}")


def default_sites(nginx_root=Path("/etc/nginx"), *, runner=subprocess.run):
    """Inspect exactly the two known default nodes; never enumerate site trees."""
    root = Path(nginx_root).absolute()
    result = []
    loaded = root / "sites-enabled/default"
    packaged = root / "sites-available/default"
    if loaded.exists() or loaded.is_symlink():
        if loaded.is_symlink() and loaded.resolve() != packaged.resolve():
            raise NginxSafetyError(f"默认站点链接指向自定义文件，未修改：{loaded}")
        if packaged.is_symlink() or not loaded.is_file():
            raise NginxSafetyError(f"默认站点不是可核验的发行版文件，未修改：{loaded}")
        expected = debian_default_digest(packaged, runner)
        actual = hashlib.md5(loaded.read_bytes()).hexdigest()
        if actual != expected:
            raise NginxSafetyError(f"默认站点内容已自定义，未修改：{loaded}；请自行限制其公开访问。")
        result.append(loaded)
    alpine = root / "http.d/default.conf"
    if alpine.exists() or alpine.is_symlink():
        if alpine.is_symlink() or not alpine.is_file() or nginx_tokens(alpine.read_text(encoding="utf-8")) != nginx_tokens(ALPINE_DEFAULT):
            raise NginxSafetyError(f"默认站点内容已自定义，未修改：{alpine}；请自行限制其公开访问。")
        result.append(alpine)
    return result


def disable_defaults(backup_dir, nginx_root=Path("/etc/nginx"), *, dry_run=False, runner=subprocess.run):
    root = Path(nginx_root).absolute()
    backup = Path(backup_dir).resolve()
    for active in (root / "sites-enabled", root / "http.d"):
        active = active.resolve()
        if backup == active or active in backup.parents:
            raise NginxSafetyError("默认站点备份必须放在 Nginx 活跃加载目录之外。")
    paths = default_sites(root, runner=runner)
    if not dry_run:
        # Back up only confirmed default nodes, preserving symlinks verbatim.
        for path in paths:
            target = backup / "nginx-defaults" / path.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                same = (target.is_symlink() and path.is_symlink() and os.readlink(target) == os.readlink(path))
                same = same or (not target.is_symlink() and not path.is_symlink() and target.is_file() and target.read_bytes() == path.read_bytes())
                if not same:
                    raise NginxSafetyError(f"默认站点备份已存在且内容不同，未覆盖：{target}")
            else:
                shutil.copy2(path, target, follow_symlinks=False)
        for path in paths:
            path.unlink()
    return [str(path) for path in paths]


def guarded_install(command, *, policy_path=Path("/usr/sbin/policy-rc.d"), runner=subprocess.run):
    """Preserve existing service policy while denying only nginx package actions."""
    policy = Path(policy_path).absolute()
    work = Path(tempfile.mkdtemp(prefix=".surelay-nginx-policy-", dir=policy.parent))
    previous = work / "policy-rc.d.original"
    staged = work / "policy-rc.d.wrapper"
    existed = policy.exists() or policy.is_symlink()
    delegate = policy.resolve() if policy.is_symlink() else previous
    executable = os.access(policy, os.X_OK)
    wrapper = ("#!/bin/sh\n# Temporary SuRelay nginx package start guard.\n"
               'for surelay_service in "$@"; do\n'
               '  case "$surelay_service" in\n'
               '    --*) continue ;;\n'
               '    nginx|nginx.service|/etc/init.d/nginx) exit 101 ;;\n'
               '    *) break ;;\n'
               '  esac\n'
               'done\n'
               + (f"exec {shlex.quote(str(delegate))} \"$@\"\n" if existed and executable else "exit 0\n"))
    installed = False
    wrapper_identity = None
    old_term = signal.getsignal(signal.SIGTERM)
    def interrupted(signum, frame):
        raise SystemExit(128 + signum)
    try:
        signal.signal(signal.SIGTERM, interrupted)
        # Prepare fully before moving the original node. A write/chmod failure
        # can then leave neither a partial active hook nor a missing original.
        staged.write_text(wrapper, encoding="utf-8")
        staged.chmod(0o755)
        stat = staged.stat()
        wrapper_identity = (stat.st_dev, stat.st_ino)
        if existed:
            policy.rename(previous)
        # Exclusive creation avoids overwriting a concurrently created policy.
        os.link(staged, policy)
        installed = True
        staged.unlink()
        result = runner(command, check=False)
        return result.returncode
    finally:
        signal.signal(signal.SIGTERM, old_term)
        if staged.exists():
            staged.unlink()
        if installed and (policy.exists() or policy.is_symlink()):
            stat = policy.lstat()
            if (policy.is_symlink() or (stat.st_dev, stat.st_ino) != wrapper_identity
                    or policy.read_text(encoding="utf-8") != wrapper):
                raise NginxSafetyError(f"安装期间服务策略被其他进程改动，未覆盖；原策略备份（如存在）：{previous}")
            policy.unlink()
        if previous.exists() or previous.is_symlink():
            if policy.exists() or policy.is_symlink():
                raise NginxSafetyError(f"服务策略路径被其他进程占用，未覆盖；原策略备份：{previous}")
            previous.rename(policy)
        work.rmdir()


def main():
    parser = argparse.ArgumentParser()
    modes = parser.add_subparsers(dest="mode", required=True)
    disable = modes.add_parser("disable-default")
    disable.add_argument("--backup-dir", type=Path, required=True)
    disable.add_argument("--nginx-root", type=Path, default=Path("/etc/nginx"))
    disable.add_argument("--dry-run", action="store_true")
    install = modes.add_parser("guarded-install")
    install.add_argument("--dry-run", action="store_true")
    install.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        if args.mode == "disable-default":
            print(json.dumps({"disabled": disable_defaults(args.backup_dir, args.nginx_root, dry_run=args.dry_run),
                              "dry_run": args.dry_run}))
            return 0
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        if not command:
            raise NginxSafetyError("缺少要执行的软件包命令。")
        if args.dry_run:
            print("DRY-RUN: deny nginx auto-start while installing packages")
            return 0
        return guarded_install(command)
    except (OSError, ValueError, NginxSafetyError) as exc:
        print(f"Nginx 默认站点安全准备失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
