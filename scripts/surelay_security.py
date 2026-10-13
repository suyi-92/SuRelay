"""Explicit, reversible VPS security actions used by the SuRelay terminal menu."""
from __future__ import annotations
import glob
import ipaddress
import json
import os
import re
import shlex
import shutil
import socket
import tempfile
from pathlib import Path

from surelay_core import Error, Snapshot, atomic, port, run

SSH_BEGIN = "# BEGIN SuRelay SSH settings"
SSH_END = "# END SuRelay SSH settings"


def ssh_block(text, values):
    pattern = re.compile(r"(?ms)^# BEGIN SuRelay SSH settings\n(.*?)^# END SuRelay SSH settings\n?")
    matches = list(pattern.finditer(text))
    if len(matches) > 1 or (SSH_BEGIN in text and not matches):
        raise Error("SuRelay SSH 设置块损坏，请先检查 sshd_config。")
    settings = {}
    if matches:
        for line in matches[0][1].splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            key, _, value = line.partition(" ")
            settings.setdefault(key, []).append(value)
    settings.update(values)
    body = pattern.sub("", text)
    lines = [SSH_BEGIN]
    for key, entries in settings.items():
        lines.extend(f"{key} {entry}" for entry in entries)
    return "\n".join([*lines, SSH_END, ""]) + body


def ssh_files(path, seen=None):
    """Follow actual Include directives; refuse cycles and non-global Includes."""
    seen = set() if seen is None else seen
    path = Path(path)
    if path in seen:
        return []
    seen.add(path)
    files = [path]
    matched = False
    for line in path.read_text().splitlines():
        parts = shlex.split(line, comments=True)
        if not parts:
            continue
        if parts[0].lower() == "match":
            matched = True
        if parts[0].lower() == "include":
            if matched:
                raise Error("SSH 的 Match 块内有 Include，请先手工整理再修改端口。")
            for name in parts[1:]:
                pattern = name if name.startswith("/") else str(Path("/etc/ssh") / name)
                for included in sorted(glob.glob(pattern)):
                    files.extend(ssh_files(Path(included), seen))
    return files


def firewall_rule(ports, protocol, source, family, note):
    pieces = ports.replace("-", ":").split(":")
    if len(pieces) not in {1, 2}:
        raise Error("端口格式为 8080 或 8000:8100。")
    normalized = [port(p) for p in pieces]
    if len(normalized) == 2 and int(normalized[0]) > int(normalized[1]):
        raise Error("端口范围起点不能大于终点。")
    if protocol not in {"tcp", "udp", "both"} or family not in {"4", "6", "both"}:
        raise Error("协议或地址族无效。")
    if not re.fullmatch(r"[\w .-]{1,40}", note):
        raise Error("备注限 1–40 个字母、数字、汉字、空格、点、横线或下划线。")
    sources = []
    if source in {"", "any"}:
        sources = ["0.0.0.0/0", "::/0"] if family == "both" else ["0.0.0.0/0" if family == "4" else "::/0"]
    else:
        network = ipaddress.ip_network(source, strict=False)
        if family != "both" and str(network.version) != family:
            raise Error("来源地址与地址族不匹配。")
        sources = [str(network)]
    return {"port": ":".join(normalized), "protocol": protocol, "sources": sources, "note": note}


def rule_args(rule):
    protocols = ["tcp", "udp"] if rule["protocol"] == "both" else [rule["protocol"]]
    return [["allow", "proto", proto, "from", source, "to", "any", "port", rule["port"],
             "comment", "SuRelay custom " + rule["note"]] for source in rule["sources"] for proto in protocols]


class Security:
    def __init__(self, manager):
        self.m = manager
        self.ssh_config = Path("/etc/ssh/sshd_config")
        self.socket_conf = Path("/etc/systemd/system/ssh.socket.d/surelay.conf")
        legacy_socket = self.socket_conf.with_name("reality-relay-bootstrap.conf")
        if legacy_socket.exists() and not self.socket_conf.exists():
            self.socket_conf = legacy_socket

    def sshd(self):
        return shutil.which("sshd") or "/usr/sbin/sshd"

    def effective(self):
        address = os.environ.get("SSH_CONNECTION", "127.0.0.1").split()[0]
        return run([self.sshd(), "-T", "-C", f"user=root,host=localhost,addr={address}"], capture=True)

    def ssh_service(self):
        if self.m.alpine():
            return "sshd"
        return "ssh" if run(["systemctl", "show", "ssh", "-p", "LoadState", "--value"], capture=True) == "loaded" else "sshd"

    def reload_ssh(self):
        run([self.sshd(), "-t"], capture=True)
        self.m.service(self.ssh_service(), "reload")

    def key_path(self):
        import pwd
        home = Path(pwd.getpwnam("root").pw_dir)
        expected = {".ssh/authorized_keys", str(home / ".ssh/authorized_keys"), "%h/.ssh/authorized_keys"}
        line = next((s for s in self.effective().splitlines() if s.startswith("authorizedkeysfile ")), "")
        if not line or line.split()[1] not in expected:
            raise Error("root 使用了自定义 AuthorizedKeysFile；请先检查有效 SSH 配置。")
        return home / ".ssh/authorized_keys"

    def validate_key(self, text):
        if "\n" in text or "\r" in text or not text.strip():
            raise Error("每把公钥必须是一整行。")
        with tempfile.TemporaryDirectory(prefix="surelay-key-") as folder:
            path = Path(folder) / "key"
            atomic(path, text + "\n")
            return run(["ssh-keygen", "-l", "-f", path], capture=True)

    def keys(self):
        path = self.key_path()
        lines = path.read_text().splitlines() if path.exists() else []
        return path, lines, [i for i, line in enumerate(lines) if line.strip() and not line.lstrip().startswith("#")]

    def write_keys(self, lines):
        path = self.key_path()
        active = [line for line in lines if line.strip() and not line.lstrip().startswith("#")]
        # Keep at least one valid key even when password access is enabled.
        if not active:
            raise Error("不能删除最后一把 SSH 公钥。")
        for line in active:
            self.validate_key(line)
        path.parent.mkdir(mode=0o700, exist_ok=True)
        os.chmod(path.parent, 0o700)
        backup = Snapshot([path, self.m.config])
        try:
            atomic(path, "\n".join(lines) + "\n")
            # Existing install falls back to authorized_keys. Removed keys cannot reappear.
            self.m.update({"ADMIN_PUBKEY": "", "ADMIN_PUBKEYS": ""})
        except BaseException:
            backup.restore()
            self.m.reload()
            raise
        print(f"公钥已保存；备份：{backup.directory}")

    def password(self, enabled, confirm_login):
        if not enabled:
            self.keys()
            if not self.keys()[2]:
                raise Error("没有可用 SSH 公钥。")
            confirm_login()
        backup = Snapshot([self.ssh_config])
        try:
            values = {
                "PermitRootLogin": ["yes" if enabled else "prohibit-password"],
                "PubkeyAuthentication": ["yes"], "PasswordAuthentication": ["yes" if enabled else "no"],
                "AuthenticationMethods": ["any" if enabled else "publickey"],
                "PermitEmptyPasswords": ["no"],
            }
            atomic(self.ssh_config, ssh_block(self.ssh_config.read_text(), values), 0o600)
            self.reload_ssh()
            effective = self.effective()
            if f"passwordauthentication {'yes' if enabled else 'no'}" not in effective:
                raise Error("有效 SSH 策略未达到目标状态。")
        except BaseException:
            backup.restore()
            self.reload_ssh()
            raise
        print("SSH 策略已应用。开启密码认证不会创建密码或解锁 root；重跑完整加固会再次关闭密码认证。")

    def change_ssh_port(self, new, confirm_login):
        new = port(new)
        current = self.effective()
        old_ports = [port(line.split()[1]) for line in current.splitlines() if line.startswith("port ")]
        if new in old_ports and len(old_ports) == 1:
            print("SSH 已使用该端口。")
            return
        reserved = {str(self.m.env["DIRECT_PORT"]), str(self.m.env["SUBSCRIPTION_LISTEN_PORT"]),
                    *(str(row["listen_port"]) for row in self.m.rows())}
        if new in reserved:
            raise Error("新 SSH 端口与代理或订阅端口冲突。")
        listeners = run(["ss", "-H", "-ltn", "sport", "=", new], capture=True)
        if listeners and new not in old_ports:
            raise Error("新 SSH 端口已被占用。")
        files = ssh_files(self.ssh_config)
        for path in files:
            for line in path.read_text().splitlines():
                # ListenAddress with an explicit port overrides the Port list.
                match = re.match(r"^\s*ListenAddress\s+(\S+)", line, re.I)
                if match and (re.search(r"\]:[0-9]+$", match[1]) or re.match(r"[^:]+:[0-9]+$", match[1])):
                    raise Error("ListenAddress 固定了独立端口，请先整理该配置再迁移 SSH 端口。")
        socket_active = not self.m.alpine() and self.m.active("ssh.socket")
        paths = [*files, self.m.config, self.socket_conf,
                 Path("/etc/ufw/user.rules"), Path("/etc/ufw/user6.rules"),
                 Path("/etc/fail2ban/jail.d/zz-surelay.local")]
        backup = Snapshot(paths)
        print(f"SSH 迁移备份：{backup.directory}。验证前保留旧端口。")
        try:
            # Port is multi-valued in OpenSSH; neutralize global provider definitions.
            for path in files:
                lines, in_match = [], False
                for line in path.read_text().splitlines(keepends=True):
                    if re.match(r"^\s*Match\s", line, re.I):
                        in_match = True
                    if not in_match and re.match(r"^\s*Port\s", line, re.I):
                        line = "# SuRelay saved port: " + line
                    lines.append(line)
                atomic(path, "".join(lines), path.stat().st_mode & 0o777)
            both = list(dict.fromkeys([*old_ports, new]))
            self.set_ssh_ports(both, socket_active)
            effective_ports = {line.split()[1] for line in self.effective().splitlines() if line.startswith("port ")}
            if set(both) != effective_ports:
                raise Error("存在额外端口配置，已停止迁移。")
            if shutil.which("ufw"):
                run(["ufw", "allow", f"{new}/tcp", "comment", "surelay SSH current port"])
            self.verify_ssh_listener(new)
            confirm_login(new)
            self.set_ssh_ports([new], socket_active)
            self.verify_ssh_listener(new)
            self.m.update({"SSH_PORT": new})
            if self.m.env["ENABLE_FAIL2BAN"] == "true":
                self.fail2ban_settings({"port": new})
        except BaseException:
            backup.restore()
            self.m.reload()
            if socket_active:
                run(["systemctl", "daemon-reload"], check=False)
                self.m.service("ssh.socket", "restart", check=False)
                self.m.service(self.ssh_service(), "restart", check=False)
            self.reload_ssh()
            if shutil.which("fail2ban-client"):
                run(["fail2ban-client", "reload"], check=False)
            if shutil.which("ufw"):
                run(["ufw", "reload"], check=False)
            print("SSH 迁移未完成，已恢复原配置。")
            raise
        # Delete only owned rules for obsolete SSH ports after the new login was verified.
        if shutil.which("ufw"):
            status = run(["ufw", "status", "numbered"], capture=True)
            numbers = []
            for line in status.splitlines():
                match = re.match(r"\[\s*(\d+)\]\s+(\d+)/tcp", line)
                if match and match[2] in old_ports and match[2] != new and any(
                        name in line for name in ("surelay SSH", "reality-relay-bootstrap SSH")):
                    numbers.append(int(match[1]))
            for number in sorted(numbers, reverse=True):
                run(["ufw", "--force", "delete", str(number)])
        print(f"SSH 端口已改为 {new}；其他软件或手工配置的旧规则未删除。")

    def set_ssh_ports(self, ports, socket_active):
        atomic(self.ssh_config, ssh_block(self.ssh_config.read_text(), {"Port": ports}), 0o600)
        run([self.sshd(), "-t"], capture=True)
        if socket_active:
            atomic(self.socket_conf, "[Socket]\nListenStream=\n" + "".join(f"ListenStream={p}\n" for p in ports), 0o644)
            run(["systemctl", "daemon-reload"])
            self.m.service("ssh.socket", "restart")
            self.m.service(self.ssh_service(), "restart")
        else:
            self.reload_ssh()

    def verify_ssh_listener(self, value):
        import time
        for attempt in range(10):
            result = run(["ss", "-H", "-ltn", "sport", "=", value], capture=True)
            if result:
                return
            time.sleep(1)
        raise Error(f"新 SSH 端口 {value} 没有监听。")

    def custom_rules(self):
        path = Path(self.m.env["RRB_STATE_DIR"]) / "surelay-firewall.json"
        return path, json.loads(path.read_text()) if path.exists() else []

    def edit_rule(self, rule=None, index=None):
        path, rules = self.custom_rules()
        old = rules[index] if index is not None else None
        required = {int(self.m.env["SSH_PORT"]), int(self.m.env["DIRECT_PORT"]),
                    *(int(row["listen_port"]) for row in self.m.rows())}
        if self.m.env.get("SUBSCRIPTION_LISTEN_PORT"):
            required.add(int(self.m.env["SUBSCRIPTION_LISTEN_PORT"]))
        connection = os.environ.get("SSH_CONNECTION", "").split()
        if len(connection) == 4:
            required.add(int(port(connection[3])))
        for selected in (rule, old):
            if not selected or selected["protocol"] == "udp":
                continue
            numbers = [int(n) for n in selected["port"].split(":")]
            if any(numbers[0] <= value <= numbers[-1] for value in required):
                raise Error("规则覆盖 SSH、代理或订阅所需 TCP 端口，请通过对应设置调整。")
        if rule:
            if any(r["note"] == rule["note"] for i, r in enumerate(rules) if i != index):
                raise Error("请使用未重复的自定义规则备注。")
            # UFW replaces the comment of an existing equivalent rule. Refuse
            # overlap rather than accidentally taking ownership of another app's rule.
            status = run(["ufw", "status", "numbered"], capture=True)
            # status hides rules when UFW is inactive; show added still lists them.
            added = run(["ufw", "show", "added"], capture=True)
            for line in added.splitlines():
                fields = shlex.split(line)
                if not fields or fields[0] != "ufw":
                    continue
                candidate = fields[fields.index("port") + 1] if "port" in fields else next(
                    (field for field in fields[2:] if re.fullmatch(r"\d+(?::\d+)?(?:/(?:tcp|udp))?", field)), "")
                if not candidate:
                    continue
                if "/" not in candidate and "proto" in fields:
                    candidate += "/" + fields[fields.index("proto") + 1]
                comment = fields[fields.index("comment") + 1] if "comment" in fields else ""
                status += f"\n[0] {candidate} ALLOW IN Anywhere # {comment}"
            lower, *upper = [int(n) for n in rule["port"].split(":")]
            upper = upper[0] if upper else lower
            for line in status.splitlines():
                match = re.match(r"\[\s*\d+\]\s+(\d+)(?::(\d+))?(?:/(tcp|udp))?\b", line)
                if not match:
                    continue
                if old and line.rstrip().endswith("# SuRelay custom " + old["note"]):
                    continue
                lo, hi = int(match[1]), int(match[2] or match[1])
                if lo <= upper and lower <= hi and (not match[3] or rule["protocol"] in {"both", match[3]}):
                    raise Error("已有规则覆盖该端口；为保留其归属，请查看规则或修改对应的自定义规则。")
        backup = Snapshot([path, Path("/etc/ufw/user.rules"), Path("/etc/ufw/user6.rules")])
        try:
            if rule:
                if any(":" in source for source in rule["sources"]):
                    defaults = Path("/etc/default/ufw").read_text()
                    if not re.search(r"(?m)^IPV6=yes\s*$", defaults):
                        raise Error("UFW 尚未启用 IPv6 规则支持，请先启用 IPv6 支持或选择 IPv4。")
                for args in rule_args(rule):
                    run(["ufw", *args])
            if index is not None:
                # Add replacement first; shared rules are never removed.
                others = [r for i, r in enumerate(rules) if i != index] + ([rule] if rule else [])
                for args in rule_args(old):
                    if not any(args[:-2] == other_args[:-2] for other in others for other_args in rule_args(other)):
                        run(["ufw", "--force", "delete", *args])
                del rules[index]
            if rule:
                rules.append(rule)
            atomic(path, json.dumps(rules, ensure_ascii=False, indent=2))
        except BaseException:
            backup.restore()
            run(["ufw", "reload"], check=False)
            raise
        print("本机防火墙规则已更新；云安全组和服务监听请分别检查。")

    def enable_firewall(self):
        # Existing installer opens configured SSH; additionally preserve actual session port.
        connection = os.environ.get("SSH_CONNECTION", "").split()
        if len(connection) == 4 and connection[3] != self.m.env["SSH_PORT"]:
            raise Error("当前 SSH 会话端口与配置不同，请先同步 SSH 端口设置再启用防火墙。")
        effective_ports = {line.split()[1] for line in self.effective().splitlines() if line.startswith("port ")}
        if self.m.env["SSH_PORT"] not in effective_ports:
            raise Error("配置中的 SSH 端口尚未生效，请先核对有效 SSH 配置。")
        backup = Snapshot([self.m.config])
        try:
            self.m.update({"ENABLE_UFW": "true"})
            self.m.phase("firewall", RRB_SKIP_PACKAGE_INSTALL="false")
        except BaseException:
            backup.restore()
            self.m.reload()
            raise

    def fail2ban_settings(self, values):
        allowed = {"enabled", "ignoreip", "bantime", "findtime", "maxretry", "port"}
        if set(values) - allowed:
            raise Error("不支持的 fail2ban 参数。")
        for key, value in values.items():
            if key == "enabled" and value not in {"true", "false"}:
                raise Error("enabled 值无效。")
            if key in {"bantime", "findtime", "maxretry"} and (not str(value).isdigit() or int(value) < 1):
                raise Error("封禁时间、窗口和次数必须为正整数（时间单位为秒）。")
            if key == "port":
                port(value)
            if key == "ignoreip":
                for network in value.split():
                    ipaddress.ip_network(network, strict=False)
        path = Path("/etc/fail2ban/jail.d/zz-surelay.local")
        settings = {}
        if path.exists():
            for line in path.read_text().splitlines():
                if "=" in line:
                    key, value = line.split("=", 1)
                    settings[key.strip()] = value.strip()
        settings.update(values)
        backup = Snapshot([path, self.m.config])
        try:
            atomic(path, "# Managed by SuRelay.\n[sshd]\n" +
                   "".join(f"{key} = {value}\n" for key, value in settings.items()), 0o600)
            run(["fail2ban-client", "-t"], capture=True)
            run(["fail2ban-client", "reload"], capture=True)
            if "enabled" in values:
                self.m.update({"ENABLE_FAIL2BAN": values["enabled"]})
        except BaseException:
            backup.restore()
            self.m.reload()
            run(["fail2ban-client", "reload"], check=False)
            raise
        print("SSH 登录防护设置已保存。")
