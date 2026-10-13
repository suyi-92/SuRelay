"""SuRelay operations, compatible with existing state paths and node identities."""
from __future__ import annotations
from contextlib import nullcontext
import csv
import importlib.util
import io
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

from surelay_terminal import current as current_terminal, notice

ROOT = Path(__file__).resolve().parents[1]


class Error(Exception):
    pass


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


GEN = module("surelay_generator", "08-generate-singbox-config.py")


def run(args, *, capture=False, check=True, env=None, input=None, interactive=False):
    args = [str(x) for x in args]
    environment = {**os.environ, "LC_ALL": "C.UTF-8", **(env or {})}
    window = current_terminal()
    if window and not capture and not interactive:
        result = window.run(args, environment, input)
    else:
        with window.external() if window and interactive else nullcontext():
            result = subprocess.run(
                args, text=True, encoding="utf-8", errors="replace",
                stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.PIPE if capture else None,
                env=environment, input=input,
            )
    if check and result.returncode:
        detail = (result.stderr or "").strip()[-1600:] if capture else ""
        if window and window.quiet and capture:
            window.append((result.stdout or "") + (result.stderr or ""))
            detail = ""
        raise Error(f"{Path(str(args[0])).name} 执行失败（{result.returncode}）。{detail}")
    return result.stdout.strip() if capture and check else result


def atomic(path, data, mode=0o600):
    path = Path(path)
    if path.is_symlink():
        raise Error(f"拒绝覆盖符号链接：{path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".surelay-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.chmod(temporary, mode)
            stream.write(data.encode() if isinstance(data, str) else data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def env_text(text, updates):
    """Replace complete shell assignments, including multiline quoted public keys."""
    remaining, output = dict(updates), []
    lines = iter(text.splitlines(keepends=True))
    for line in lines:
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_]\w*)=(.*)", line)
        if not match:
            output.append(line)
            continue
        block, value = line, match[2]
        while True:
            try:
                shlex.split(value, comments=True)
                break
            except ValueError:
                extra = next(lines, None)
                if extra is None:
                    raise Error("config.env 存在未闭合引号，停止修改。")
                block += extra
                value += "\n" + extra
        key = match[1]
        if key in updates:
            if key in remaining:
                output.append(f"{key}={shlex.quote(str(remaining.pop(key)))}\n")
        else:
            output.append(block)
    if output and not output[-1].endswith("\n"):
        output[-1] += "\n"
    output.extend(f"{key}={shlex.quote(str(value))}\n" for key, value in remaining.items())
    return "".join(output)


def port(value):
    if not re.fullmatch(r"[0-9]{1,5}", str(value)) or not 1 <= int(value) <= 65535:
        raise Error("端口必须为 1–65535。")
    return str(int(value))


def csv_text(fields, rows):
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows({key: row.get(key, "") for key in fields} for row in rows)
    return stream.getvalue()


class Snapshot:
    """Private copies survive interruption; no arbitrary archive extraction."""
    def __init__(self, paths, root=None):
        root = Path(root or "/root/surelay-backups")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory = Path(tempfile.mkdtemp(prefix="surelay-", dir=root))
        self.items = []
        for index, path in enumerate(dict.fromkeys(Path(p) for p in paths)):
            if path.is_symlink():
                raise Error(f"备份目标是符号链接，请先检查：{path}")
            item = {"path": str(path), "exists": path.exists(), "file": str(index)}
            if path.exists():
                if not path.is_file():
                    raise Error(f"备份目标不是普通文件：{path}")
                item["mode"] = path.stat().st_mode & 0o777
                atomic(self.directory / str(index), path.read_bytes())
            self.items.append(item)
        atomic(self.directory / "manifest.json", json.dumps(self.items, ensure_ascii=False))

    def restore(self):
        for item in self.items:
            path = Path(item["path"])
            if item["exists"]:
                atomic(path, (self.directory / item["file"]).read_bytes(), item["mode"])
            elif path.exists():
                if path.is_symlink():
                    raise Error(f"恢复时发现符号链接：{path}")
                path.unlink()


class Manager:
    def __init__(self, root=ROOT):
        self.root = Path(root).resolve()
        self.config = self.root / "config.env"
        self.env = {}
        if self.config.exists():
            self.reload()

    def reload(self):
        self.env = GEN.defaults(GEN.parse_env_file(self.config)) if self.config.exists() else {}
        return self.env

    def path(self, key):
        return GEN.resolve_path(self.env, key, self.root)

    def update(self, values):
        atomic(self.config, env_text(self.config.read_text(), values))
        self.reload()

    def phase(self, name, **extra):
        run(["bash", self.root / "bootstrap.sh", "--phase", name], env={
            "RRB_SHOW_RESULT": "false", "RRB_SKIP_PACKAGE_INSTALL": "true",
            "RRB_VERBOSE": "true", **extra,
        })

    def alpine(self):
        return bool(re.search(r"(?m)^ID=['\"]?alpine['\"]?$", Path("/etc/os-release").read_text()))

    def service(self, name, action, check=True):
        args = ["rc-service", name, action] if self.alpine() else ["systemctl", action, name]
        return run(args, check=check)

    def active(self, name):
        args = ["rc-service", name, "status"] if self.alpine() else ["systemctl", "is-active", "--quiet", name]
        return run(args, capture=True, check=False).returncode == 0

    def rows(self):
        return GEN.load_rows(self.env, self.root, allow_empty=True)

    def check(self):
        GEN.validate_env(self.env)
        rows = self.rows()
        reserved = {int(self.env["SSH_PORT"])}
        if self.env["ENABLE_SUBSCRIPTION_SERVER"] == "true":
            reserved.add(int(self.env["SUBSCRIPTION_LISTEN_PORT"]))
        if self.env["ENABLE_CUSTOM_DOMAIN"] == "true":
            reserved.add(int(self.env["NGINX_FALLBACK_PORT"]))
        if reserved.intersection(GEN.port_list(self.env, rows)):
            raise Error("代理入口端口与 SSH、订阅或域名内部端口冲突。")
        return rows

    def output(self, *args):
        run(["python3", self.root / "scripts/11-output-nodes.py",
             "--config-env", self.config, "--generator", self.root / "scripts/08-generate-singbox-config.py", *args])

    def owned_paths(self):
        keys = ["CSV_PATH", "UPSTREAM_NODES_PATH", "SINGBOX_CONFIG_PATH",
                "VLESS_UUID_PATH", "REALITY_PRIVATE_KEY_PATH", "REALITY_PUBLIC_KEY_PATH", "REALITY_SHORT_ID_PATH"]
        return [self.config, *(self.path(k) for k in keys)]

    def core_binary(self):
        candidates = ([Path("/usr/local/bin/sing-box")] if self.alpine() else [])
        candidates += [Path("/etc/sing-box/bin/sing-box")]
        located = shutil.which("sing-box")
        if located:
            candidates.append(Path(located))
        for candidate in candidates:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        raise Error("找不到 sing-box 内核，请先完成安装。")

    def nginx_path(self):
        value = self.env.get("NGINX_FALLBACK_CONF", "auto")
        if value != "auto":
            return Path(value)
        directory = "http.d" if self.alpine() else "sites-available"
        current = Path(f"/etc/nginx/{directory}/surelay.conf")
        legacy = current.with_name("reality-relay-bootstrap.conf")
        return legacy if legacy.is_file() and not current.exists() else current

    def check_listeners(self, rows):
        families = []
        if self.env.get("SERVER_IP_IPV4"):
            families.append((socket.AF_INET, "127.0.0.1"))
        if self.env["ENABLE_IPV6_LISTEN"] == "true":
            families.append((socket.AF_INET6, "::1"))
        for p in GEN.port_list(self.env, rows):
            for family, host in families:
                for attempt in range(15):
                    try:
                        with socket.socket(family, socket.SOCK_STREAM) as sock:
                            sock.settimeout(1)
                            sock.connect((host, p))
                        break
                    except OSError:
                        if attempt == 14:
                            raise Error(f"本机 {host}:{p} 监听验证失败。")
                        time.sleep(1)

    def apply(self, updates=None, files=None, domain=False):
        """Commit locally after validation; publication failures remain retryable."""
        updates, files = updates or {}, files or {}
        old = dict(self.env)
        paths = self.owned_paths() + list(files)
        paths += [Path("/etc/ufw/user.rules"), Path("/etc/ufw/user6.rules"), Path("/etc/default/ufw")]
        if domain:
            paths += [self.nginx_path(), self.path("CLOUDFLARE_API_TOKEN_FILE")]
        enabled_site = Path("/etc/nginx/http.d" if self.alpine() else "/etc/nginx/sites-enabled") / self.nginx_path().name
        site_existed = enabled_site.exists() or enabled_site.is_symlink()
        backup = Snapshot(paths)
        restarted = firewall_changed = domain_changed = False
        print(f"变更前备份：{backup.directory}")
        try:
            if updates:
                self.update(updates)
            for path, content in files.items():
                atomic(path, content)
            if self.env["RESET_PROXY_KEYS"] != "false":
                raise Error('请先将 RESET_PROXY_KEYS 设为 false；日常维护保留节点身份。')
            rows = self.check()
            config = GEN.build_config(self.env, rows, GEN.read_vless_uuid(self.env),
                                      GEN.read_reality_private_key(self.env), GEN.read_reality_short_id(self.env))
            target = self.path("SINGBOX_CONFIG_PATH")
            candidate = target.with_name(".surelay-candidate.json")
            try:
                atomic(candidate, json.dumps(config, ensure_ascii=False, indent=2) + "\n")
                run([self.core_binary(), "check", "-c", candidate], capture=True)
                if domain and self.env["ENABLE_CUSTOM_DOMAIN"] == "true":
                    domain_changed = True
                    self.phase("custom-domain", RRB_SKIP_PACKAGE_INSTALL="false")
                atomic(target, candidate.read_bytes())
            finally:
                if candidate.exists():
                    candidate.unlink()
            restarted = True
            self.service("sing-box", "restart")
            self.check_listeners(rows)
            if self.env["ENABLE_UFW"] == "true":
                firewall_changed = True
                self.phase("firewall")
        except BaseException:
            backup.restore()
            self.reload()
            if firewall_changed and shutil.which("ufw"):
                run(["ufw", "reload"], check=False)
            if restarted:
                self.service("sing-box", "restart", check=False)
            if domain_changed:
                if not site_existed and enabled_site.is_symlink() and enabled_site.resolve() == self.nginx_path().resolve():
                    enabled_site.unlink()
                self.service("nginx", "reload", check=False)
            notice(f"本地变更失败，已恢复配置；备份：{backup.directory}")
            raise
        print("本地配置已应用，节点身份已保留。")
        try:
            state = Path(self.env["RRB_STATE_DIR"])
            for key, name in (("CSV_PATH", "home-proxies.csv"), ("UPSTREAM_NODES_PATH", "upstream-nodes.txt")):
                if self.path(key).exists():
                    atomic(state / name, self.path(key).read_bytes())
            self.phase("subscription")
            if self.env["ENABLE_CUSTOM_DOMAIN"] == "true":
                self.phase("cloudflare-dns")
            if old.get("SERVER_IP_IPV6") and old.get("SERVER_IP_IPV6") != self.env.get("SERVER_IP_IPV6"):
                self.remove_old_dns(old, ["AAAA"])
            self.output("--skip-node-files", "--quiet", "--print-summary")
        except (Error, OSError, SystemExit) as exc:
            raise Error(f"本地配置已生效，但发布未全部完成：{exc}。请在菜单重试订阅刷新或 DNS 同步。") from exc
        except (KeyboardInterrupt, EOFError):
            notice("本地配置已生效；发布已取消，可在菜单重试订阅刷新或 DNS 同步。")
            raise

    def remove_old_dns(self, old, types):
        if old.get("ENABLE_CUSTOM_DOMAIN") != "true":
            return
        cf = module("surelay_cf", "14-setup-cloudflare-dns.py")
        token = cf.read_cloudflare_token(old)
        zone = cf.find_zone(token, old["CLOUDFLARE_ZONE_NAME"])
        if not zone:
            raise Error("找不到原域名 Zone，无法清理旧 DNS。")
        for kind in types:
            address = old.get("SERVER_IP_IPV4" if kind == "A" else "SERVER_IP_IPV6", "")
            for record in cf.list_records(token, zone["id"], kind, old["CUSTOM_DOMAIN"]):
                if address and record.get("content") == address and record.get("comment") == old["SERVER_ALIAS"]:
                    cf.cf_request(token, "DELETE", f"/zones/{zone['id']}/dns_records/{record['id']}")
                    print(f"已删除原配置管理的 {kind} 记录：{old['CUSTOM_DOMAIN']}")
                else:
                    notice(f"保留未能确认归属的 {kind} 记录：{record.get('content')}；请在 Cloudflare 检查。")

    def exit_files(self):
        """Pin generated tags/ports before edits so deleting rows cannot renumber others."""
        rows = self.rows()
        home = [r for r in rows if r["source"] == "home"]
        upstream = []
        path = self.path("UPSTREAM_NODES_PATH")
        if not path.exists():
            path = Path(self.env["RRB_STATE_DIR"]) / "upstream-nodes.txt"
        if path.exists():
            lines = list(GEN.iter_csv_lines(path))
            if lines:
                if GEN.REQUIRED_UPSTREAM_COLUMNS <= set(next(csv.reader([lines[0]]))):
                    urls = [r["node_url"] for r in csv.DictReader(lines) if r.get("node_url")]
                else:
                    urls = [line.strip() for line in lines if line.strip()]
                generated = [r for r in rows if r["source"] == "upstream"]
                if len(urls) != len(generated):
                    raise Error("上游源文件与解析结果不一致，请检查出口文件。")
                upstream = [dict(tag=r["tag"], listen_port=r["listen_port"], node_url=url)
                            for r, url in zip(generated, urls)]
        return home, upstream

    def save_exits(self, home, upstream):
        self.apply(files={
            self.path("CSV_PATH"): csv_text(
                ["tag", "type", "server", "server_port", "username", "password", "network", "listen_port"], home),
            self.path("UPSTREAM_NODES_PATH"): csv_text(["tag", "node_url", "listen_port"], upstream),
        })

    def backup(self):
        backup = Snapshot(self.owned_paths())
        roles = [str(i) if item["exists"] else None for i, item in enumerate(backup.items)]
        for index, name in ((1, "home-proxies.csv"), (2, "upstream-nodes.txt")):
            fallback = Path(self.env["RRB_STATE_DIR"]) / name
            if roles[index] is None and fallback.is_file():
                atomic(backup.directory / str(index), fallback.read_bytes())
                roles[index] = str(index)
        atomic(backup.directory / "node-backup.json", json.dumps({
            "format": 1, "roles": roles
        }))
        print(f"节点配置与身份备份：{backup.directory}")
        return backup.directory

    def restore_nodes(self, directory):
        directory = Path(directory).resolve()
        data = json.loads((directory / "node-backup.json").read_text())
        paths = self.owned_paths()
        roles = data.get("roles", [])
        if data.get("format") != 1 or len(roles) != len(paths):
            raise Error("不是兼容的 SuRelay 节点备份。")
        files = {}
        for index, (path, name) in enumerate(zip(paths, roles)):
            if name is None:
                continue
            if name != str(index) or (directory / name).is_symlink():
                raise Error("备份索引异常。")
            files[path] = (directory / name).read_bytes()
        if self.config not in files:
            raise Error("备份缺少 config.env。")
        previous = GEN.parse_env_file(directory / roles[0])
        protected = lambda k: (k.startswith(("SSH_", "ADMIN_", "SFTP_", "INITIAL_", "RRB_", "FAIL2BAN_"))
                               or k.endswith(("_PATH", "_DIR", "_FILE", "_CONF", "_ROOT"))
                               or k in {"ENABLE_UFW", "ENABLE_FAIL2BAN", "ENABLE_SFTP_USER"})
        updates = {k: v for k, v in previous.items() if not protected(k)}
        updates["RESET_PROXY_KEYS"] = "false"
        del files[self.config]
        files.pop(self.path("SINGBOX_CONFIG_PATH"), None)
        self.apply(updates, files, domain=True)

    def update_code(self):
        if run(["git", "-C", self.root, "status", "--porcelain"], capture=True):
            raise Error("项目有未提交修改；保留现场，停止更新。")
        branch = run(["git", "-C", self.root, "branch", "--show-current"], capture=True)
        if branch != "main":
            raise Error("自动更新要求 main 分支。")
        run(["git", "-C", self.root, "fetch", "origin", "main"])
        if run(["git", "-C", self.root, "rev-list", "origin/main..HEAD"], capture=True):
            raise Error("存在本地独有提交，停止自动更新。")
        run(["git", "-C", self.root, "merge", "--ff-only", "origin/main"])
        self.migrate()
        print("SuRelay 已更新；重新执行 sr 使用新版菜单。")

    def migrate(self, destination=Path("/opt/SuRelay"), legacy=Path("/opt/reality-relay-bootstrap"),
                launcher=Path("/usr/local/bin/sr")):
        """Move only the checkout; keep running services, credentials and identity in place."""
        destination, legacy, launcher = Path(destination), Path(legacy), Path(launcher)
        old_root = self.root
        move = old_root == legacy
        if move and (destination.exists() or destination.is_symlink()):
            raise Error(f"迁移目标已存在：{destination}；请先核实两份安装，未覆盖任何目录。")
        if move and not (old_root / ".git").is_dir():
            raise Error("迁移目录要求独立 Git 克隆；自定义工作树请在原路径更新。")
        remote = run(["git", "-C", old_root, "remote", "get-url", "origin"], capture=True)
        replacements = {
            "https://github.com/suyi-92/reality-relay-bootstrap.git": "https://github.com/suyi-92/SuRelay.git",
            "https://github.com/suyi-92/reality-relay-bootstrap": "https://github.com/suyi-92/SuRelay.git",
            "git@github.com:suyi-92/reality-relay-bootstrap.git": "git@github.com:suyi-92/SuRelay.git",
        }
        backup = Snapshot([self.config, launcher])
        moved = False
        try:
            if move:
                old_root.rename(destination)
                moved = True
                old_root.symlink_to(destination, target_is_directory=True)
                self.root, self.config = destination, destination / "config.env"
                if self.config.exists():
                    # Absolute project inputs follow the checkout; runtime paths stay untouched.
                    raw = GEN.parse_env_file(self.config)
                    paths = {k: str(destination) + v[len(str(old_root)):] for k, v in raw.items()
                             if k.endswith(("_PATH", "_DIR", "_FILE", "_CONF", "_ROOT"))
                             and v.startswith(str(old_root) + "/")}
                    if paths:
                        self.update(paths)
            if remote in replacements:
                run(["git", "-C", self.root, "remote", "set-url", "origin", replacements[remote]])
            install_command(self.root, launcher)
        except BaseException:
            if moved:
                if old_root.is_symlink():
                    old_root.unlink()
                destination.rename(old_root)
            self.root, self.config = old_root, old_root / "config.env"
            if remote in replacements:
                run(["git", "-C", old_root, "remote", "set-url", "origin", remote], check=False)
            backup.restore()
            self.reload()
            raise
        print(f"SuRelay 目录：{self.root}；迁移备份：{backup.directory}")
        print("原有配置、节点身份和订阅已保留。" if move else "已完成命令与仓库地址更新。")


def install_command(root=ROOT, target=Path("/usr/local/bin/sr")):
    target = Path(target)
    entry = Path(root).resolve() / "surelay.sh"
    if not entry.is_file():
        raise Error(f"缺少管理入口：{entry}")
    if target.exists() or target.is_symlink():
        if target.is_symlink():
            if target.resolve() != entry:
                raise Error(f"{target} 已被其他程序占用。")
            target.unlink()
        elif "# SuRelay managed command" not in target.read_text(errors="replace"):
            raise Error(f"{target} 已被其他程序占用。")
    atomic(target, "#!/bin/sh\n# SuRelay managed command\nexec bash " + shlex.quote(str(entry)) + ' "$@"\n', 0o755)
    print(f"已安装管理命令：{target}")
