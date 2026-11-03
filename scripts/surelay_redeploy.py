#!/usr/bin/env python3
"""Keep SSH usable while a fresh SuRelay deployment replaces its old identity."""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import re
import shlex
import sys

from surelay_core import Error, GEN, Manager, Snapshot, atomic, port, run


def key_identity(line):
    """Compare key material, independently of comments or authorized_keys options."""
    try:
        parts = shlex.split(line, comments=True)
    except ValueError:
        return None
    for index, value in enumerate(parts[:-1]):
        if re.fullmatch(r"(?:ssh-(?:ed25519|rsa)|ecdsa-sha2-nistp[0-9]+|sk-ssh-ed25519@openssh.com|sk-ecdsa-sha2-nistp256@openssh.com)", value):
            try:
                base64.b64decode(parts[index + 1], validate=True)
            except (ValueError, TypeError):
                return None
            return value + " " + parts[index + 1]
    return None


def config_key_lines(env):
    return [line.strip() for name in ("ADMIN_PUBKEY", "ADMIN_PUBKEYS")
            for line in env.get(name, "").splitlines() if line.strip() and not line.lstrip().startswith("#")]


def ledger_path(manager):
    return Path(manager.env["RRB_STATE_DIR"]) / "ssh-managed-keys.json"


def read_ledger(path):
    path = Path(path)
    if path.is_symlink():
        raise Error(f"公钥归属记录不能为符号链接：{path}")
    if not path.exists():
        return set()
    data = json.loads(path.read_text())
    values = data.get("keys") if isinstance(data, dict) else data
    if not isinstance(values, list) or any(not isinstance(value, str) or not key_identity(value) for value in values):
        raise Error("SSH 公钥归属记录格式无效，停止以保留登录身份。")
    return {key_identity(value) for value in values}


def record_managed_keys(manager, before, after):
    previous = {value for line in before if (value := key_identity(line))}
    current = {value for line in after if (value := key_identity(line))}
    owned = (read_ledger(ledger_path(manager)) | (current - previous)) & current
    atomic(ledger_path(manager), json.dumps({"version": 1, "keys": sorted(owned)}, indent=2) + "\n")


def read_handoff(path, project, *, optional=False):
    path = Path(path)
    if any(parent.is_symlink() for parent in path.absolute().parents):
        raise Error("SSH 交接文件的父目录不能为符号链接。")
    if not path.exists() and not path.is_symlink():
        if optional:
            return None
        raise Error("找不到强重置 SSH 交接文件，未继续修改登录配置。")
    if (path.is_symlink() or not path.is_file()
            or (os.name == "posix" and (path.stat().st_mode & 0o077 or path.stat().st_uid != os.geteuid()))):
        raise Error("SSH 交接文件必须是权限 600 的普通文件。")
    try:
        data = json.loads(path.read_text())
    except (ValueError, OSError) as exc:
        raise Error("SSH 交接文件损坏，请使用原强重置备份恢复。") from exc
    if not isinstance(data, dict) or data.get("format", data.get("version")) != 1 or data.get("mode") != "redeploy":
        raise Error("SSH 交接文件格式无效，请使用原强重置备份恢复。")
    if not isinstance(data.get("project"), str) or Path(data["project"]).resolve() != Path(project).resolve():
        raise Error("SSH 交接文件不属于当前项目，未继续安装。")
    if not isinstance(data.get("status"), str) or data["status"] not in {"removed", "finalized"}:
        raise Error(f"强重置状态为 {data.get('status', '未知')}，请先检查恢复资料：{data.get('backup_dir', '原强重置备份')}")
    if (not isinstance(data.get("old_config"), dict)
            or any(not isinstance(value, str) for value in data.get("old_config", {}).values())
            or not isinstance(data.get("old_authorized_keys"), list)
            or any(not isinstance(value, str) for value in data["old_authorized_keys"])
            or not isinstance(data.get("old_managed_keys", []), list)
            or any(not isinstance(value, str) for value in data.get("old_managed_keys", []))
            or not isinstance(data.get("authorized_keys_path"), str)
            or not Path(data["authorized_keys_path"]).is_absolute()
            or not isinstance(data.get("pending_firewall"), list)
            or not isinstance(data.get("ssh_ports"), list)):
        raise Error(f"SSH 交接记录不完整，未继续安装；恢复资料：{data.get('backup_dir', '原强重置备份')}")
    return data


def strip_settings(data):
    begin, end = b"# BEGIN SuRelay SSH settings", b"# END SuRelay SSH settings"
    lines, inside, seen = [], False, False
    for line in data.splitlines(keepends=True):
        marker = line.rstrip(b"\r\n")
        if marker == begin:
            if inside or seen:
                raise Error("旧 SSH settings 块重复或嵌套，停止交接。")
            inside = seen = True
        elif marker == end:
            if not inside:
                raise Error("旧 SSH settings 块不完整，停止交接。")
            inside = False
        elif not inside:
            lines.append(line)
    if inside:
        raise Error("旧 SSH settings 块不完整，停止交接。")
    return b"".join(lines)


def strip_verification(data):
    pattern = rb"(?ms)^# BEGIN SuRelay redeploy SSH verification\r?\n.*?^# END SuRelay redeploy SSH verification\r?\n?"
    found = list(re.finditer(pattern, data))
    if len(found) > 1 or (b"# BEGIN SuRelay redeploy SSH verification" in data and not found):
        raise Error("SSH 本次公钥验证块损坏，停止交接。")
    return re.sub(pattern, b"", data)


def pending_rule(command):
    if (not isinstance(command, list) or not all(isinstance(value, str) for value in command)
            or command[:3] != ["ufw", "--force", "delete"]
            or len(command) < 7 or command[3] not in {"allow", "deny", "reject", "limit"}
            or "comment" not in command):
        raise Error("旧防火墙交接规则格式不明，停止以保留 SSH。")
    index = command.index("comment")
    if index != len(command) - 2 or not command[-1].lower().startswith(("surelay ", "reality-relay-bootstrap ")):
        raise Error("旧防火墙规则缺少项目归属，未清理。")
    return command


def rule_covers_tcp(command, value):
    if "proto" in command and command[command.index("proto") + 1] == "udp":
        return False
    candidate = command[command.index("port") + 1] if "port" in command else command[4]
    if candidate.endswith("/udp"):
        return False
    candidate = candidate.removesuffix("/tcp")
    for part in candidate.split(","):
        match = re.fullmatch(r"([0-9]+)(?:[:-]([0-9]+))?", part)
        if not match:
            # Profiles, `any`, or future UFW syntax may cover the new SSH
            # listener. Disabled UFW management must not silently delete it.
            return True
        if int(match[1]) <= int(value) <= int(match[2] or match[1]):
            return True
    return False


class Redeployer:
    def __init__(self, manager, handoff=None, *, security=None, backup_root=None):
        from surelay_security import Security
        self.m = manager
        self.sec = security or Security(manager)
        self.handoff_path = Path(handoff) if handoff else None
        self.handoff = read_handoff(self.handoff_path, manager.root) if handoff else None
        self.backup_root = backup_root

    def new_keys(self):
        lines = config_key_lines(self.m.env)
        if not lines and self.handoff:
            raise Error("重新部署必须明确录入至少一把管理员公钥，可使用现有公钥。")
        for line in lines:
            if not key_identity(line):
                raise Error("管理员公钥格式无效。")
            self.sec.validate_key(line)
        return lines

    def old_owned_keys(self):
        if not self.handoff:
            return set()
        values = self.handoff.get("old_managed_keys", []) + config_key_lines(self.handoff.get("old_config", {}))
        return {value for line in values if (value := key_identity(line))}

    def validate_fresh_identity(self, lines):
        if not self.handoff:
            return
        if self.handoff["status"] == "finalized":
            raise Error("本次强重置已完成，不能再次清理旧 SSH 身份。")
        old_ports = {str(value) for value in self.handoff.get("ssh_ports", [])}
        old_ports.update(str(value) for value in (self.handoff.get("current_ssh_port"), self.handoff.get("old_config", {}).get("SSH_PORT")) if value)
        if str(self.m.env["SSH_PORT"]) in old_ports:
            raise Error("强重置必须选择不同于旧 SSH 监听的端口。")

    def key_path(self):
        path = self.sec.key_path(redeploy_handoff=self.handoff) if self.handoff else self.sec.key_path()
        if self.handoff and Path(self.handoff["authorized_keys_path"]).resolve() != path.resolve():
            raise Error("当前 authorized_keys 与强重置备份路径不同，停止交接。")
        return path

    def verification_keys(self):
        return Path(self.m.env["RRB_STATE_DIR"]) / "redeploy-authorized-keys"

    def stage_keys(self):
        lines = self.new_keys()
        self.validate_fresh_identity(lines)
        if not lines:
            return
        path = self.key_path()
        before = path.read_text().splitlines() if path.exists() else []
        after, seen = before[:], {key_identity(line) for line in before}
        for line in lines:
            if key_identity(line) not in seen:
                after.append(line)
                seen.add(key_identity(line))
        backup = Snapshot([path, ledger_path(self.m), self.verification_keys()], self.backup_root)
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            atomic(path, "\n".join(after) + "\n")
            record_managed_keys(self.m, before, after)
            if self.handoff:
                self.verification_keys().parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.chmod(self.verification_keys().parent, 0o700)
                atomic(self.verification_keys(), "\n".join(lines) + "\n")
        except BaseException:
            backup.restore()
            raise
        print("本次录入的公钥已准备；原公钥保留到新端口登录验证完成。")
        if self.handoff:
            known = self.old_owned_keys()
            unknown = {value for line in self.handoff.get("old_authorized_keys", [])
                       if (value := key_identity(line)) and value not in known}
            if unknown:
                print(f"旧 authorized_keys 中有 {len(unknown)} 把无法归属到 SuRelay 的公钥，将按手工公钥保留。")

    def restrict_new_port(self):
        """Verify possession of the explicitly selected keys on the new port."""
        if not self.handoff:
            raise Error("本次公钥端口验证需要强重置交接记录。")
        lines = self.new_keys()
        self.validate_fresh_identity(lines)
        new_port = port(self.m.env["SSH_PORT"])
        keyfile = self.verification_keys()
        if not keyfile.is_file() or keyfile.is_symlink():
            raise Error("尚未准备本次公钥专用验证文件。")
        if {key_identity(line) for line in keyfile.read_text().splitlines()} != {key_identity(line) for line in lines}:
            raise Error("本次公钥验证文件与向导输入不同，停止交接。")
        backup = Snapshot([self.sec.ssh_config], self.backup_root)
        try:
            original = strip_verification(self.sec.ssh_config.read_bytes()).rstrip(b"\r\n") + b"\n"
            block = (f"\n# BEGIN SuRelay redeploy SSH verification\nMatch User root LocalPort {new_port}\n"
                     f"    AuthorizedKeysFile {keyfile}\n    PubkeyAuthentication yes\n"
                     "    PasswordAuthentication no\n    ChallengeResponseAuthentication no\n"
                     "    AuthenticationMethods publickey\n    PermitRootLogin prohibit-password\n"
                     "Match all\n# END SuRelay redeploy SSH verification\n").encode()
            atomic(self.sec.ssh_config, original + block, self.sec.ssh_config.stat().st_mode & 0o777)
            run([self.sec.sshd(), "-t"], capture=True)
            context = f"user=root,host=localhost,addr=127.0.0.1,lport={new_port}"
            effective = run([self.sec.sshd(), "-T", "-C", context], capture=True)
            values = effective.splitlines()
            if (f"authorizedkeysfile {keyfile}" not in values or "pubkeyauthentication yes" not in values
                    or "passwordauthentication no" not in values or "authenticationmethods publickey" not in values):
                raise Error("已有 Match 配置覆盖了本次公钥验证路径，未继续交接。")
            self.sec.reload_ssh()
            self.sec.verify_ssh_listener(new_port)
        except BaseException:
            backup.restore()
            self.sec.reload_ssh()
            raise
        print("新 SSH 端口只接受本次录入的公钥；旧端口保留原公钥以便恢复。")

    def finalize(self):
        from surelay_security import ssh_files
        if not self.handoff:
            raise Error("最终 SSH 交接需要强重置记录。")
        lines = self.new_keys()
        self.validate_fresh_identity(lines)
        path = self.key_path()
        current = path.read_text().splitlines()
        new_ids = {key_identity(line) for line in lines}
        if not new_ids.issubset({key_identity(line) for line in current}):
            raise Error("本次录入的公钥尚未写入 authorized_keys，拒绝移除旧身份。")
        new_port = port(self.m.env["SSH_PORT"])
        self.sec.verify_ssh_listener(new_port)
        files = ssh_files(self.sec.ssh_config)
        socket_paths = [self.sec.socket_conf.with_name(name) for name in ("surelay.conf", "reality-relay-bootstrap.conf")]
        for target in socket_paths:
            if target.is_symlink():
                raise Error("SSH socket 覆盖为符号链接，停止交接。")
        socket_active = not self.m.alpine() and self.m.active("ssh.socket")
        if socket_active:
            self.sec.socket_listener_ports()
        commands = [pending_rule(command) for command in self.handoff.get("pending_firewall", [])]
        if self.m.env.get("ENABLE_UFW") != "true" and any(rule_covers_tcp(command, new_port) for command in commands):
            raise Error("新 SSH 端口仍位于待清理的旧防火墙规则内；请先提供独立规则或开启 UFW 管理后重试。")
        paths = [*files, path, ledger_path(self.m), self.verification_keys(), *socket_paths, self.handoff_path,
                 Path("/etc/ufw/user.rules"), Path("/etc/ufw/user6.rules")]
        backup = Snapshot(paths, self.backup_root)
        firewall_changed = False
        try:
            for file in files:
                original = file.read_bytes()
                data = strip_verification(strip_settings(original))
                if file == self.sec.ssh_config:
                    data = f"# BEGIN SuRelay SSH settings\nPort {new_port}\n# END SuRelay SSH settings\n".encode() + data
                if data != original:
                    atomic(file, data, file.stat().st_mode & 0o777)
            old_owned = self.old_owned_keys()
            # The selected lines already proved login on the isolated new port.
            # Use those exact lines even when an old entry has different options.
            kept = [line for line in current if key_identity(line) not in (old_owned | new_ids)] + lines
            atomic(path, "\n".join(kept) + "\n")
            atomic(ledger_path(self.m), json.dumps({"version": 1, "keys": sorted(new_ids)}, indent=2) + "\n")
            if self.verification_keys().exists():
                self.verification_keys().unlink()
            for target in socket_paths:
                if target.exists():
                    target.unlink()
            run([self.sec.sshd(), "-t"], capture=True)
            if socket_active:
                canonical = self.sec.socket_conf.with_name("surelay.conf")
                atomic(canonical, f"# Managed by SuRelay.\n[Socket]\nListenStream=\nListenStream={new_port}\n", 0o644)
                run(["systemctl", "daemon-reload"])
                self.m.service("ssh.socket", "restart")
                self.m.service(self.sec.ssh_service(), "restart")
            else:
                self.sec.reload_ssh()
            self.sec.verify_ssh_listener(new_port)
            effective = self.sec.effective(local_port=new_port).splitlines()
            if ("passwordauthentication no" not in effective or "authenticationmethods publickey" not in effective
                    or not any(value in effective for value in ("permitrootlogin prohibit-password", "permitrootlogin without-password"))):
                raise Error("最终认证策略未达到公钥模式，停止旧身份清理。")
            if self.m.env.get("ENABLE_UFW") == "true" and commands:
                run(["ufw", "allow", f"{new_port}/tcp", "comment", "surelay SSH current port"])
            for command in commands:
                firewall_changed = True
                run(command)
            saved = {**self.handoff, "status": "finalized", "ssh_finalize_backup": str(backup.directory)}
            atomic(self.handoff_path, json.dumps(saved, ensure_ascii=False, indent=2) + "\n")
            self.handoff = saved
        except BaseException:
            backup.restore()
            if socket_active:
                run(["systemctl", "daemon-reload"], check=False)
                self.m.service("ssh.socket", "restart", check=False)
                self.m.service(self.sec.ssh_service(), "restart", check=False)
            else:
                self.sec.reload_ssh()
            if firewall_changed:
                run(["ufw", "reload"], check=False)
            print(f"SSH 交接未完成，已恢复到保留原登录入口的安全阶段；恢复资料：{backup.directory}")
            raise
        print(f"本次 SSH 公钥已接管，未被本次选用的旧 SuRelay 公钥及旧端口覆盖/规则已清理；备份：{backup.directory}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--resume-status", action="store_true")
    action.add_argument("--stage-keys", action="store_true")
    action.add_argument("--restrict-new-port", action="store_true")
    action.add_argument("--finalize", action="store_true")
    parser.add_argument("--project-dir", type=Path)
    parser.add_argument("--config-env", type=Path)
    parser.add_argument("--handoff", type=Path)
    args = parser.parse_args()
    if args.resume_status:
        if not args.project_dir or not args.handoff:
            parser.error("--resume-status requires --project-dir and --handoff")
        data = read_handoff(args.handoff, args.project_dir, optional=True)
        print(data["status"] if data else "none")
        return
    if not args.config_env:
        parser.error("--config-env is required")
    if os.geteuid() != 0:
        raise Error("请以 root 执行 SSH 交接。")
    manager = Manager(args.config_env.resolve().parent)
    manager.config = args.config_env.resolve()
    manager.reload()
    operation = Redeployer(manager, args.handoff)
    action = operation.stage_keys if args.stage_keys else operation.restrict_new_port if args.restrict_new_port else operation.finalize
    action()


if __name__ == "__main__":
    try:
        main()
    except (Error, GEN.ConfigError, OSError, ValueError) as exc:
        raise SystemExit(f"ERROR: {exc}")
