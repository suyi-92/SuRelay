#!/usr/bin/env python3
"""Remove a deployment before reinstalling; never purge shared system packages."""
from __future__ import annotations

import argparse
import configparser
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile


class ResetError(Exception):
    pass


class ResetDeployment:
    # root/runner injection is for isolated tests only, never exposed by the CLI.
    def __init__(self, project: Path, root: Path = Path('/'), runner=None, uninstall=False,
                 redeploy=False, handoff_file=Path('/root/surelay-backups/redeploy-handoff.json')):
        self.root = root.resolve()
        self.project = project
        self.runner = runner or self.run
        self.paths = {}
        self.empty_dirs = set()
        self.services = []
        self.shared = []
        self.firewall = []
        self.backup = None
        self.uninstall = uninstall
        self.redeploy = redeploy
        if uninstall and redeploy:
            raise ResetError('强重新布置不能与卸载模式同时使用。')
        self.handoff_file = Path(handoff_file)
        self.handoff = None
        self.backup_extra = set()
        self.pending_firewall = []
        self.ssh_ports = set()
        self.ssh_assets = []
        self.old_managed_keys = []
        self.managed_sshd_jail = False

    def check_handoff(self):
        self.safe_path(self.handoff_file)
        if not self.exists(self.handoff_file):
            return
        info = self.path(self.handoff_file).stat()
        if info.st_mode & 0o077 or (self.root == Path('/') and info.st_uid != 0):
            raise ResetError('已有重新布置交接记录不是 root 私有文件，请先核实归属和权限。')
        try:
            value = json.loads(self.text(self.handoff_file))
        except (ValueError, OSError) as exc:
            raise ResetError('已有重新布置交接记录无法安全读取，请先核实恢复资料。') from exc
        if (not isinstance(value, dict) or value.get('format') != 1 or value.get('mode') != 'redeploy'
                or value.get('status') != 'finalized' or not isinstance(value.get('project'), str)
                or not Path(value['project']).is_absolute() or not isinstance(value.get('backup_dir'), str)
                or not Path(value['backup_dir']).is_absolute()):
            raise ResetError('上次引导式重新布置尚未完成；保留 SSH 交接记录，请继续原安装或先检查恢复资料。')

    @staticmethod
    def private_json(path, value):
        path = Path(path)
        if path.is_symlink():
            raise ResetError(f'拒绝覆盖交接记录链接：{path}')
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix='.surelay-handoff-', dir=path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                os.chmod(name, 0o600)
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def path(self, value):
        return self.root / str(value).lstrip('/')

    @staticmethod
    def run(args, check=True):
        result = subprocess.run(args, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=90,
                                env={**os.environ, 'LC_ALL': 'C'})
        if check and result.returncode:
            raise ResetError(f"命令失败：{shlex.join(args)}\n{result.stderr.strip()}")
        return result

    def exists(self, value):
        p = self.path(value)
        return p.exists() or p.is_symlink()

    def safe_path(self, value, *, link=False):
        p = Path(value)
        if not p.is_absolute() or '..' in p.parts or any(c in str(p) for c in '\n\r\x00$`'):
            raise ResetError(f'无法安全解析重置路径：{value}')
        # Never recursively follow parents redirected outside the named location.
        physical = self.path(p)
        for parent in physical.parents:
            if parent == self.root:
                break
            if parent.is_symlink():
                raise ResetError(f'重置路径的父目录是符号链接：{parent}')
        if physical.is_symlink() and not link:
            raise ResetError(f'重置目标是符号链接，请先核实：{value}')
        return p

    def add(self, value, *, directory=False, marker=None, link=False):
        p = self.safe_path(value, link=link)
        physical = self.path(p)
        if not self.exists(p):
            return
        if ((directory and not physical.is_dir()) or
                (not directory and not physical.is_file())) and not physical.is_symlink():
            raise ResetError(f'重置目标类型不符合预期：{p}')
        markers = (marker, marker.replace('surelay', 'reality-relay-bootstrap').replace('SuRelay', 'Reality Relay Bootstrap')) if marker else ()
        if markers and not any(m in physical.read_text(errors='replace') for m in markers):
            raise ResetError(f'配置没有项目归属标记，未卸载：{p}')
        if directory:
            # Only this fixed, project-specific tree is recursively removed.
            if str(p) not in ('/opt/surelay-certbot', '/opt/reality-relay-certbot'):
                raise ResetError(f'拒绝递归删除未知目录：{p}')
            for current, dirs, _ in os.walk(physical, followlinks=False):
                if os.path.ismount(current):
                    raise ResetError(f'目录内存在挂载点：{current}')
        self.paths[str(p)] = directory

    def text(self, value, *, link=False):
        self.safe_path(value, link=link)
        p = self.path(value)
        return p.read_text(errors='replace') if p.exists() else ''

    def config_path(self, env, key, default, *, parent=None):
        value = env.get(key) or default
        p = Path(value)
        if not p.is_absolute():
            p = self.project / p
        p = self.safe_path(p)
        if parent is not None and (p == parent or parent not in p.parents):
            raise ResetError(f'{key} 必须位于专用目录 {parent} 内，当前为 {p}；请先核实自定义路径。')
        return p

    def collect(self):
        self.check_handoff()
        spec = importlib.util.spec_from_file_location('reset_generator', Path(__file__).with_name('08-generate-singbox-config.py'))
        gen = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gen)
        config = self.project / 'config.env'
        self.safe_path(config)
        env = gen.parse_env_file(self.path(config)) if self.exists(config) else {}
        self.old_config = dict(env)
        self.old_config_text = self.text(config)
        # Reset does not validate deployment values: it also repairs failed installs.
        # Restrict custom directories to project-specific descendants so a typo
        # cannot designate /etc, /root, another application's config, or source code.
        default_state = '/etc/reality-relay-bootstrap' if self.exists('/etc/reality-relay-bootstrap') and not self.exists('/etc/surelay') else '/etc/surelay'
        state = self.config_path(env, 'RRB_STATE_DIR', default_state)
        if state.name not in ('surelay', 'reality-relay-bootstrap') and not state.name.startswith(('surelay-', 'reality-relay-bootstrap-')):
            raise ResetError(f'自定义 state 目录没有项目专用名称：{state}；请先核实并迁移。')
        self.empty_dirs.add(str(state))
        deployment = self.exists(config) or self.exists(state / 'vless-uuid.txt')
        for name in ('config.env', 'home-proxies.csv', 'upstream-nodes.txt',
                     'surelay-nodes.txt', 'surelay-clash.yaml',
                     'surelay-vless.txt'):
            self.add(self.project / name)
        for key, name in [('CSV_PATH', 'home-proxies.csv'), ('UPSTREAM_NODES_PATH', 'upstream-nodes.txt')]:
            path = self.config_path(env, key, str(self.project / name))
            if path not in (self.project / name, state / name):
                raise ResetError(f'{key} 指向外部输入 {path}；请先备份并移回项目目录后重置。')
            self.add(path)
        for key, name in [('VLESS_UUID_PATH', 'vless-uuid.txt'), ('REALITY_PRIVATE_KEY_PATH', 'reality-private.key'),
                          ('REALITY_PUBLIC_KEY_PATH', 'reality-public.key'), ('REALITY_SHORT_ID_PATH', 'reality-short-id.txt'),
                          ('CLOUDFLARE_API_TOKEN_FILE', 'cloudflare.ini')]:
            value = env.get(key, '')
            if value in (f'/etc/surelay/{name}', f'/etc/reality-relay-bootstrap/{name}'):
                value = ''
            self.add(self.config_path({key: value}, key, str(state / name), parent=state))
        self.add('/etc/fail2ban/jail.d/zz-surelay.local', marker='Managed by SuRelay.')
        if '/etc/fail2ban/jail.d/zz-surelay.local' in self.paths:
            self.shared.append('fail2ban')
        for name in ('home-proxies.csv', 'upstream-nodes.txt'):
            self.add(state / name)
        subscription = self.config_path(env, 'SUBSCRIPTION_DIR', str(state / 'subscription'), parent=state)
        for name in ('nodes.txt', 'clash.yaml', 'clashmeta.yaml', 'vless.txt'):
            self.add(subscription / 'all' / name)
        self.empty_dirs.update([str(subscription / 'all'), str(subscription)])

        singbox_config = self.config_path(env, 'SINGBOX_CONFIG_PATH', '/etc/sing-box/config.json')
        if str(singbox_config) != '/etc/sing-box/config.json':
            raise ResetError(f'重置暂不接管自定义 sing-box 配置路径：{singbox_config}')
        if deployment:
            self.add(singbox_config)
            self.services = ['surelay-subscription', 'sing-box']
            # A previous opt-in 233boy install takes precedence in singbox_bin().
            # Remove that binary and its unit override, so auto/APT can take over.
            legacy_binary = '/etc/sing-box/bin/sing-box'
            if self.exists(legacy_binary):
                self.add(legacy_binary)
                legacy_unit = '/etc/systemd/system/sing-box.service'
                if self.exists(legacy_unit):
                    self.add(legacy_unit, marker=legacy_binary)
                self.empty_dirs.add('/etc/sing-box/bin')
        for name in ('surelay-nodes.txt', 'surelay-clash.yaml', 'surelay-vless.txt',
                     'surelay-v6-nodes.txt', 'surelay-v6-clash.yaml', 'surelay-v6-vless.txt'):
            self.add(Path('/root') / name)
        self.add('/usr/local/lib/surelay/subscription-server.py')
        self.empty_dirs.add('/usr/local/lib/surelay')
        self.add('/etc/systemd/system/surelay-subscription.service', marker='SuRelay')
        if '/etc/systemd/system/surelay-subscription.service' in self.paths and 'surelay-subscription' not in self.services:
            self.services.append('surelay-subscription')
        for service in ('sing-box', 'surelay-subscription'):
            init = f'/etc/init.d/{service}'
            if 'Managed by surelay' in self.text(init):
                self.add(init)
                self.add(f'/etc/conf.d/{service}')
                if service == 'sing-box':
                    self.add('/usr/local/bin/sing-box')
                if service not in self.services:
                    self.services.append(service)
        jail = '/etc/fail2ban/jail.d/sshd.local'
        if 'Managed by surelay' in self.text(jail):
            self.add(jail)
            if 'fail2ban' not in self.shared:
                self.shared.append('fail2ban')

        nginx = env.get('NGINX_FALLBACK_CONF', 'auto')
        candidates = {Path(f'/etc/nginx/{d}/{name}.conf') for d in ('sites-available', 'http.d')
                      for name in ('surelay', 'reality-relay-bootstrap')}
        if nginx != 'auto':
            candidate = self.safe_path(nginx)
            if Path('/etc/nginx') not in candidate.parents:
                raise ResetError(f'Nginx 自定义配置不在 /etc/nginx 内：{candidate}')
            candidates.add(candidate)
        for conf in sorted(candidates):
            if not self.exists(conf):
                continue
            if self.path(conf).is_symlink():
                continue  # Added only when its owned target is found below.
            self.add(conf, marker='Managed by surelay')
            for folder in ('sites-enabled', 'http.d'):
                enabled = Path('/etc/nginx') / folder / conf.name
                if enabled == conf or not self.exists(enabled):
                    continue
                self.safe_path(enabled, link=True)
                if not self.path(enabled).is_symlink() or self.path(enabled).resolve() != self.path(conf).resolve():
                    raise ResetError(f'Nginx 启用文件不属于本项目：{enabled}')
                self.add(enabled, link=True)
            if 'nginx' not in self.shared:
                self.shared.append('nginx')
        for conf in candidates:
            if self.exists(conf) and str(conf) not in self.paths:
                raise ResetError(f'Nginx 站点链接未确认归属：{conf}')
        default_fallback = '/var/www/reality-fallback' if self.exists('/var/www/reality-fallback') and not self.exists('/var/www/surelay-fallback') else '/var/www/surelay-fallback'
        fallback = self.config_path(env, 'NGINX_FALLBACK_ROOT', default_fallback)
        if fallback.name not in ('surelay-fallback', 'reality-fallback'):
            raise ResetError(f'自定义 fallback 目录未确认归属：{fallback}')
        for name in ('index.html', 'robots.txt', 'favicon.ico'):
            self.add(fallback / name)
        self.empty_dirs.add(str(fallback))
        self.add('/etc/letsencrypt/renewal-hooks/deploy/surelay-nginx.sh')
        self.add('/etc/periodic/daily/surelay-certbot')
        self.collect_certificates()
        self.add('/opt/surelay-certbot', directory=True)
        self.collect_legacy()
        if self.uninstall:
            launcher = '/usr/local/bin/sr'
            if self.exists(launcher):
                physical = self.path(launcher)
                if physical.is_symlink():
                    if physical.resolve() == self.path(self.project / 'surelay.sh').resolve():
                        self.add(launcher, link=True)
                elif '# SuRelay managed command' in self.text(launcher) and str(self.project / 'surelay.sh') in self.text(launcher):
                    self.add(launcher)
        self.empty_dirs.add('/etc/sing-box')
        # Read-only distro metadata is commonly a link to /usr/lib/os-release.
        # It is never added to the removal plan; managed targets remain strict.
        self.alpine = bool(re.search(r'^ID=[\"\']?alpine[\"\']?$', self.text('/etc/os-release', link=True), re.M))
        if self.redeploy:
            self.collect_redeploy(state)
        self.collect_firewall(env)

    def collect_redeploy(self, state):
        """Keep the working SSH channel; record only evidence of managed identity."""
        self.external_tls_preserved = []
        for key in ('HY2_CERT_PATH', 'HY2_KEY_PATH'):
            value = self.old_config.get(key, '')
            if not value:
                continue
            path = Path(value)
            if path.is_absolute() and state in path.parents:
                self.add(path)
                self.check_shared_tls(str(path))
            elif value not in self.paths:
                self.external_tls_preserved.append(value)
        for name in ('surelay-firewall.json', 'ssh-managed-keys.json'):
            self.add(state / name)
        ledger = state / 'ssh-managed-keys.json'
        if self.exists(ledger):
            try:
                saved = json.loads(self.text(ledger))
                keys = saved.get('keys') if isinstance(saved, dict) else saved
                if not isinstance(keys, list) or any(not isinstance(key, str) for key in keys):
                    raise ValueError('invalid key ledger')
                self.old_managed_keys = list(dict.fromkeys(keys))
            except (ValueError, TypeError) as exc:
                raise ResetError('项目 SSH 公钥归属记录损坏，请先核实后重新布置。') from exc
        self.managed_sshd_jail = any(path in self.paths for path in (
            '/etc/fail2ban/jail.d/sshd.local', '/etc/fail2ban/jail.d/zz-surelay.local'))
        self.fail2ban_database = None
        if self.managed_sshd_jail:
            database = '/var/lib/fail2ban/fail2ban.sqlite3'
            try:
                result = self.runner(['fail2ban-client', 'get', 'dbfile'], check=False)
                configured_database = result.stdout.strip() if result.returncode == 0 else ''
                if configured_database.lower() == 'none':
                    database = ''
                elif configured_database:
                    database = configured_database
            except FileNotFoundError:
                pass  # Repair a partial install whose fail2ban executable is absent.
            if database:
                self.safe_path(database)
                if self.exists(database):
                    self.fail2ban_database = Path(database)
        self.ssh_assets = ['/etc/ssh/sshd_config', '/root/.ssh/authorized_keys']
        for folder in ('ssh.socket.d', 'sshd.socket.d'):
            directory = Path('/etc/systemd/system') / folder
            if self.exists(directory):
                self.safe_path(directory)
                self.backup_extra.add(str(directory))
                for name in ('surelay.conf', 'reality-relay-bootstrap.conf'):
                    if self.exists(directory / name):
                        self.safe_path(directory / name)
                        self.ssh_assets.append(str(directory / name))
        for name in ('00-surelay-phase1.conf', '00-surelay-hardening.conf',
                     '00-reality-relay-bootstrap-phase1.conf', '00-reality-relay-bootstrap-hardening.conf'):
            value = Path('/etc/ssh/sshd_config.d') / name
            if self.exists(value):
                self.safe_path(value)
                self.ssh_assets.append(str(value))
        self.safe_path('/root/.ssh/authorized_keys')
        self.old_authorized_keys = self.text('/root/.ssh/authorized_keys').splitlines()
        # Recover complete multiline key assignments without executing old shell
        # configuration. Legacy bash ANSI quoting is limited to its key fields.
        lines = iter(self.old_config_text.splitlines())
        for line in lines:
            match = re.match(r'^\s*(?:export\s+)?(ADMIN_PUBKEYS?)=(.*)$', line)
            if not match:
                continue
            value = match[2]
            while True:
                try:
                    tokens = shlex.split(value, comments=True)
                    break
                except ValueError:
                    extra = next(lines, None)
                    if extra is None:
                        raise ResetError('旧管理员公钥赋值引号不完整，无法确认身份来源。')
                    value += '\n' + extra
            if value.startswith("$'") and value.endswith("'"):
                decoded = value[2:-1]
                decoded = re.sub(r"\\([nrt\\'])", lambda m: {'n': '\n', 'r': '\r', 't': '\t',
                                  '\\': '\\', "'": "'"}[m[1]], decoded)
            elif len(tokens) <= 1:
                decoded = tokens[0] if tokens else ''
            else:
                raise ResetError('旧管理员公钥赋值无法安全解析，未开始重新布置。')
            self.old_config[match[1]] = decoded
        self.current_ssh_port = None
        connection = os.environ.get('SSH_CONNECTION', '').split()
        if len(connection) == 4 and connection[3].isdigit() and 1 <= int(connection[3]) <= 65535:
            self.current_ssh_port = int(connection[3])
            self.ssh_ports.add(self.current_ssh_port)
        configured = self.old_config.get('SSH_PORT', '22')
        if configured.isdigit() and 1 <= int(configured) <= 65535:
            self.ssh_ports.add(int(configured))
        binary = shutil.which('sshd') or '/usr/sbin/sshd'
        result = self.runner([binary, '-T'], check=False)
        for value in re.findall(r'(?m)^port\s+(\d+)\s*$', result.stdout):
            if 1 <= int(value) <= 65535:
                self.ssh_ports.add(int(value))
        if not self.alpine:
            socket = self.runner(['systemctl', 'show', 'ssh.socket', '--property=Listen', '--value'], check=False)
            for value in re.findall(r'(?:^|\s|:)(\d+)\s+\(Stream\)', socket.stdout):
                if 1 <= int(value) <= 65535:
                    self.ssh_ports.add(int(value))
        listeners = self.runner(['ss', '-H', '-ltnp'], check=False)
        for line in listeners.stdout.splitlines():
            if not re.search(r'\bsshd\b', line):
                continue
            columns = line.split()
            for column in columns[:5]:
                match = re.search(r':(\d+)$', column)
                if match and 1 <= int(match[1]) <= 65535:
                    self.ssh_ports.add(int(match[1]))
                    break
        if not self.ssh_ports:
            raise ResetError('无法确认当前 SSH 监听端口，停止重新布置以保留登录通道。')

    def check_shared_tls(self, value):
        for directory in ('/etc/nginx', '/etc/apache2', '/etc/httpd', '/etc/haproxy', '/etc/caddy'):
            for current, dirs, files in os.walk(self.path(directory), followlinks=False):
                if any((Path(current) / name).is_symlink() for name in dirs):
                    raise ResetError('Web 配置有目录链接，无法确认项目 HY2 证书独占。')
                for name in files:
                    path = Path(current) / name
                    logical = '/' + path.relative_to(self.root).as_posix()
                    if logical not in self.paths and path.is_file() and value in path.read_text(errors='replace'):
                        raise ResetError(f'项目 HY2 证书还被其他配置引用：{logical}；未开始重新布置。')

    @staticmethod
    def managed_rule(args):
        return (len(args) >= 5 and args[0] == 'ufw' and args[1] in {'allow', 'deny', 'limit', 'reject'}
                and args[-2] == 'comment' and args[-1].lower().startswith(('surelay ', 'reality-relay-bootstrap ')))

    def protects_ssh(self, args):
        """Defer broad/source-scoped SSH rules until a new key and port work."""
        body, comment = args[2:-2], args[-1].lower()
        protocol = body[body.index('proto') + 1] if 'proto' in body and body.index('proto') + 1 < len(body) else ''
        shorthand = re.fullmatch(r'([0-9,:-]+)/(tcp|udp)', body[0]) if body else None
        if shorthand:
            specification, protocol = shorthand.groups()
        elif 'port' in body:
            # A source port is not the destination SSH listener. Rules which
            # constrain only the source still cover every destination port.
            destination = body[body.index('to') + 1:] if 'to' in body else body
            position = destination.index('port') if 'port' in destination else -1
            specification = destination[position + 1] if 0 <= position < len(destination) - 1 else ''
        elif body and re.fullmatch(r'[0-9,:-]+', body[0]):
            specification = body[0]
        else:
            specification = ''  # Profile/any-port rules may carry this session.
        if protocol == 'udp':
            return False
        if 'ssh' in comment or not specification:
            return True
        for interval in specification.split(','):
            limits = re.split('[:-]', interval)
            if len(limits) not in (1, 2) or any(not part.isdigit() for part in limits):
                raise ResetError('项目 UFW 端口范围无法安全解析，未开始重新布置。')
            lower, upper = int(limits[0]), int(limits[-1])
            if not 1 <= lower <= upper <= 65535:
                raise ResetError('项目 UFW 端口范围不合法，未开始重新布置。')
            if any(lower <= value <= upper for value in self.ssh_ports):
                return True
        return False

    def collect_legacy(self):
        """Recognize exact old managed assets without renaming live services."""
        for folder in (self.project, Path('/root')):
            for suffix in ('nodes.txt', 'clash.yaml', 'vless.txt', 'v6-nodes.txt', 'v6-clash.yaml', 'v6-vless.txt'):
                self.add(folder / ('reality-relay-bootstrap-' + suffix))
        service = 'reality-relay-subscription'
        unit = f'/etc/systemd/system/{service}.service'
        if self.exists(unit):
            self.add(unit, marker='SuRelay')
            self.services.append(service)
        for name in ('sing-box', service):
            init = f'/etc/init.d/{name}'
            if 'Managed by reality-relay-bootstrap' in self.text(init):
                self.add(init)
                self.add(f'/etc/conf.d/{name}')
                if name == 'sing-box':
                    self.add('/usr/local/bin/sing-box')
                if name not in self.services:
                    self.services.append(name)
        jail = '/etc/fail2ban/jail.d/sshd.local'
        if 'Managed by reality-relay-bootstrap' in self.text(jail):
            self.add(jail)
            if 'fail2ban' not in self.shared:
                self.shared.append('fail2ban')
        self.add('/usr/local/lib/reality-relay-bootstrap/subscription-server.py')
        self.empty_dirs.add('/usr/local/lib/reality-relay-bootstrap')
        self.add('/etc/letsencrypt/renewal-hooks/deploy/reality-relay-bootstrap-nginx.sh')
        self.add('/etc/periodic/daily/reality-relay-certbot')
        self.add('/opt/reality-relay-certbot', directory=True)

    def collect_certificates(self):
        # Certbot lineages belong to this deployment only when their recorded
        # credentials point to a file in the removal plan. Refuse shared users.
        renewal = self.path('/etc/letsencrypt/renewal')
        self.safe_path('/etc/letsencrypt/renewal')
        for conf in renewal.glob('*.conf'):
            self.safe_path('/' + conf.relative_to(self.root).as_posix())
            parser = configparser.RawConfigParser()
            try:
                parser.read_string('[certificate]\n' + conf.read_text())
            except configparser.Error:
                raise ResetError(f'无法解析证书续期配置，未开始重置：{conf.name}') from None
            credential = parser.get('renewalparams', 'dns_cloudflare_credentials', fallback='')
            if credential not in self.paths:
                continue
            name = conf.stem
            if not re.fullmatch(r'[A-Za-z0-9._-]+', name):
                raise ResetError(f'证书名称不合法：{name}')
            live = f'/etc/letsencrypt/live/{name}'
            archive = f'/etc/letsencrypt/archive/{name}'
            for directory in ('/etc/nginx', '/etc/apache2', '/etc/httpd', '/etc/haproxy', '/etc/caddy'):
                for current, dirs, files in os.walk(self.path(directory), followlinks=False):
                    for dirname in dirs:
                        if (Path(current) / dirname).is_symlink():
                            raise ResetError(f'Web 配置存在目录链接，无法确认项目证书独占：{Path(current) / dirname}')
                    for filename in files:
                        p = Path(current) / filename
                        value = '/' + p.relative_to(self.root).as_posix()
                        if value in self.paths or not p.is_file():
                            continue
                        contents = p.read_text(errors='replace')
                        if live in contents or archive in contents:
                            raise ResetError(f'项目证书还被其他配置引用：{value}；未开始重置。')
            for directory in (live, archive):
                self.safe_path(directory)
                for p in self.path(directory).glob('*'):
                    value = '/' + p.relative_to(self.root).as_posix()
                    if p.is_symlink():
                        target = p.resolve()
                        if self.path(archive) not in target.parents:
                            raise ResetError(f'证书链接指向其他目录：{value}')
                    self.add(value, link=directory == live)
                self.empty_dirs.add(directory)
            self.add('/' + conf.relative_to(self.root).as_posix())

    def collect_firewall(self, env):
        if not self.exists('/etc/ufw/ufw.conf'):
            return
        rules = self.runner(['ufw', 'show', 'added']).stdout
        ports = {env.get('SSH_PORT', '22')}
        connection = os.environ.get('SSH_CONNECTION', '').split()
        if len(connection) == 4:
            ports.add(connection[3])
        for line in rules.splitlines():
            if self.redeploy:
                try:
                    args = shlex.split(line)
                except ValueError as exc:
                    raise ResetError('防火墙规则无法安全解析，未开始重新布置。') from exc
                if not self.managed_rule(args):
                    if args and args[-1].lower().startswith(('surelay ', 'reality-relay-bootstrap ')):
                        raise ResetError('发现无法安全重放的项目 UFW 规则，未开始重新布置。')
                    continue
                command = ['ufw', '--force', 'delete', *args[1:]]
                (self.pending_firewall if self.protects_ssh(args) else self.firewall).append(command)
                continue
            if 'surelay' not in line and 'reality-relay-bootstrap' not in line:
                continue
            args = shlex.split(line)
            if len(args) != 5 or args[:2] != ['ufw', 'allow'] or args[3] != 'comment' or not re.fullmatch(r'\d+/(?:tcp|udp)', args[2]):
                raise ResetError('发现无法自动识别的项目 UFW 规则，未开始重置。')
            if 'SSH' in args[4] or args[2].split('/')[0] in ports:
                continue
            if not args[4].startswith(('surelay sing-box ', 'surelay subscription ',
                                       'reality-relay-bootstrap sing-box ', 'reality-relay-bootstrap subscription ')):
                raise ResetError('发现未知项目防火墙注释，未开始重置。')
            self.firewall.append(['ufw', '--force', 'delete', *args[1:]])

    def service(self, name, action, check=True):
        if self.alpine:
            if action == 'exists':
                return self.exists(f'/etc/init.d/{name}')
            command = ['rc-service', name, 'status' if action == 'active' else action]
        else:
            if action == 'exists':
                return self.runner(['systemctl', 'show', name, '--property=LoadState', '--value']).stdout.strip() == 'loaded'
            command = ['systemctl', 'is-active', '--quiet', name] if action == 'active' else ['systemctl', action, name]
        result = self.runner(command, check=check and action != 'active')
        return result.returncode == 0

    def describe(self):
        print('开始引导式节点布置：清空旧节点、配置、密钥、域名凭据与 SR 安全属性，重新填写全部参数。' if self.redeploy else
              '卸载将停止节点与订阅，并删除本项目部署配置。' if self.uninstall else
              '重置安装将停止旧节点和订阅，清空配置/出口列表，并在重装时生成新密钥。')
        print('备份后删除以下项目文件：')
        for path in sorted(self.paths):
            print(f'  {path}')
        print('停用服务：' + (', '.join(self.services) or '无'))
        if self.redeploy:
            print(f'立即清理已确认 SR 防火墙规则：{len(self.firewall)} 条；新 SSH 验证后再清理：{len(self.pending_firewall)} 条。')
            print('暂时保留当前 SSH 配置和全部公钥以完成交接；新端口与新公钥验证后清除可确认归属的旧 SR 身份。')
            print('归属不明的手工公钥、无 SR 注释的防火墙规则、其他服务、Cloudflare DNS、云安全组和系统软件包保留。')
            if self.external_tls_preserved:
                print('保留用户自行管理的外部 HY2 证书/私钥：' + ', '.join(self.external_tls_preserved))
            print(f'私有交接记录：{self.handoff_file}；未完成交接时禁止再次重置。')
        else:
            print(f'清理代理/订阅 UFW 规则：{len(self.firewall)} 条；保留 SSH 规则。')
            print('保留 SSH 配置/公钥/账户、系统软件包、自定义端口规则、其他站点、Cloudflare DNS、代码目录及历史备份。')

    def snapshot(self):
        base = self.path('/root/surelay-backups')
        self.safe_path('/root/surelay-backups')
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        base.chmod(0o700)
        self.backup = Path(tempfile.mkdtemp(prefix='redeploy-' if self.redeploy else 'reset-', dir=base))
        extra = ['/etc/ssh', '/root/.ssh/authorized_keys', '/etc/ufw', '/etc/default/ufw', *self.backup_extra]
        paths = set(self.paths)
        for value in extra:
            if self.exists(value):
                self.safe_path(value)
                paths.add(value)
        archive = self.backup / 'files.tar.gz'
        with tarfile.open(archive, 'w:gz', dereference=False) as tar:
            for value in sorted(paths):
                tar.add(self.path(value), arcname=value.lstrip('/'))
        archive.chmod(0o600)
        # Verify readable archive and every regular file before the first stop/delete.
        with tarfile.open(archive, 'r:gz') as tar:
            for member in tar:
                if member.isfile():
                    with tar.extractfile(member) as saved, self.path(member.name).open('rb') as original:
                        def digest(stream):
                            h = hashlib.sha256()
                            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                                h.update(chunk)
                            return h.digest()
                        if digest(saved) != digest(original):
                            raise ResetError(f'备份核验不一致：{member.name}')
        if self.redeploy and self.fail2ban_database is not None:
            # SQLite's backup API includes committed WAL pages without stopping
            # unrelated jails or copying an inconsistent live database file.
            database = self.backup / 'fail2ban.sqlite3'
            with sqlite3.connect(f'file:{self.path(self.fail2ban_database)}?mode=ro', uri=True) as source:
                with sqlite3.connect(database) as target:
                    source.backup(target)
                    if target.execute('PRAGMA quick_check').fetchone() != ('ok',):
                        raise ResetError('fail2ban 封禁状态备份校验失败，未开始重新布置。')
            database.chmod(0o600)
        state = {name: {'active': self.service(name, 'active'), 'exists': self.service(name, 'exists')}
                 for name in dict.fromkeys(self.services + self.shared)}
        for name, details in state.items():
            if self.alpine:
                details['runlevels'] = [p.parent.name for p in self.path('/etc/runlevels').glob(f'*/{name}') if p.is_symlink()]
            else:
                details['enabled'] = self.runner(['systemctl', 'is-enabled', name], check=False).stdout.strip()
        (self.backup / 'manifest.json').write_text(json.dumps({
            'project': str(self.project), 'remove': self.paths, 'services': state,
            'firewall': self.firewall, 'status': 'backed-up',
        }, ensure_ascii=False, indent=2) + '\n')
        (self.backup / 'manifest.json').chmod(0o600)
        print(f'备份已核验：{self.backup}', flush=True)
        return state

    def write_handoff(self, status, error=''):
        self.safe_path(self.handoff_file)
        if self.handoff is None:
            self.handoff = {
                'format': 1, 'mode': 'redeploy', 'project': str(self.project),
                'backup_dir': '/' + self.backup.relative_to(self.root).as_posix(),
                'ssh_ports': sorted(self.ssh_ports), 'current_ssh_port': self.current_ssh_port,
                'pending_firewall': self.pending_firewall,
                'authorized_keys_path': '/root/.ssh/authorized_keys',
                'old_authorized_keys': self.old_authorized_keys,
                'old_managed_keys': self.old_managed_keys,
                'old_config': self.old_config, 'old_config_text': self.old_config_text,
                'ssh_assets': self.ssh_assets,
                'external_tls_preserved': self.external_tls_preserved,
                'fail2ban_backup': str(self.backup / 'fail2ban.sqlite3') if self.fail2ban_database is not None else '',
                'unknown_keys': '只有旧配置或归属账本中可确认的 SR 公钥才应在新公钥验证后移除；其他公钥保留。',
            }
        self.handoff['status'] = status
        if error:
            self.handoff['error'] = error
        self.private_json(self.backup / 'handoff.json', self.handoff)
        self.private_json(self.path(self.handoff_file), self.handoff)

    def clear_sshd_bans(self, state):
        if not self.redeploy or not self.managed_sshd_jail:
            return
        running = state.get('fail2ban', {}).get('active') and self.runner(
            ['fail2ban-client', 'status', 'sshd'], check=False).returncode == 0
        if running:
            for value in self.sshd_bans():
                # Enumerate explicitly: `set sshd unbanip --all` is not an all
                # selector in supported fail2ban versions, and global unban
                # commands would also clear unrelated service bans.
                self.runner(['fail2ban-client', 'set', 'sshd', 'unbanip', value])
        if self.fail2ban_database is not None:
            # Remove only this confirmed managed jail's history, including
            # expired/incremental tickets absent from the runtime list.
            with sqlite3.connect(f'file:{self.path(self.fail2ban_database)}?mode=rw', uri=True) as database:
                for table in ('bans', 'bips'):
                    columns = {row[1] for row in database.execute(f'PRAGMA table_info({table})')}
                    if not columns:
                        continue
                    if not {'jail', 'ip'} <= columns:
                        raise ResetError('fail2ban 封禁表结构无法安全确认，未清除共享数据库。')
                    database.execute(f'DELETE FROM {table} WHERE jail = ?', ('sshd',))
        if running and self.sshd_bans():
            raise ResetError('项目 sshd jail 仍有封禁记录，停止重新布置。')

    def sshd_bans(self):
        result = self.runner(['fail2ban-client', 'get', 'sshd', 'banip'], check=False)
        value = result.stdout.strip()
        if result.returncode:
            # fail2ban 0.10 does not expose `get <jail> banip`.
            value = self.runner(['fail2ban-client', 'status', 'sshd']).stdout
            match = re.search(r'Banned IP list:\s*([^\n]*)', value)
            if not match:
                raise ResetError('无法确认项目 sshd 封禁列表，停止重新布置。')
            value = match[1]
        ips = value.translate(str.maketrans({'[': ' ', ']': ' ', "'": ' ', '"': ' ', ',': ' '})).split()
        for item in ips:
            try:
                ipaddress.ip_network(item, strict=False)
            except ValueError as exc:
                raise ResetError('项目 sshd 封禁列表含未知格式，未继续清理。') from exc
        return list(dict.fromkeys(ips))

    def apply(self):
        state = self.snapshot()
        if self.redeploy:
            self.write_handoff('prepared')
        try:
            self.clear_sshd_bans(state)
            self.remove_deployment(state)
            if self.redeploy:
                self.write_handoff('removed')
        except BaseException as exc:
            if self.redeploy and self.handoff is not None:
                try:
                    self.write_handoff('failed', str(exc))
                except (ResetError, OSError):
                    print(f'交接状态未能更新，保留恢复资料：{self.backup}', flush=True)
            raise

    def remove_deployment(self, state):
        for name in self.services:
            if not state[name]['exists']:
                continue
            if self.alpine:
                if state[name]['active']:
                    self.service(name, 'stop')
                # rc-update del is not idempotent when absent; enumerate links.
                for runlevel in self.path('/etc/runlevels').glob('*'):
                    if (runlevel / name).is_symlink():
                        self.runner(['rc-update', 'del', name, runlevel.name])
            else:
                self.runner(['systemctl', 'disable', '--now', name])
            if self.service(name, 'active'):
                raise ResetError(f'服务仍在运行，停止清理：{name}')
        for command in self.firewall:
            self.runner(command)
        # Remove links before their targets, then only exact planned files/trees.
        for value in sorted(self.paths, key=lambda v: not self.path(v).is_symlink()):
            p = self.path(value)
            self.safe_path(value, link=p.is_symlink())
            if self.paths[value]:
                shutil.rmtree(p)
            else:
                p.unlink()
        for value in sorted(self.empty_dirs, key=len, reverse=True):
            p = self.path(value)
            if p.exists():
                self.safe_path(value)
                if not any(p.iterdir()):
                    p.rmdir()
                else:
                    print(f'保留目录中的非项目文件：{value}')
        if not self.alpine:
            self.runner(['systemctl', 'daemon-reload'])
        for name in self.shared:
            if state[name]['active']:
                if name == 'nginx':
                    self.runner(['nginx', '-t'])
                    self.service(name, 'reload')
                else:
                    self.service(name, 'restart')
        if any(self.exists(value) for value in self.paths):
            raise ResetError('重置文件核验失败；未继续安装。')
        manifest = self.backup / 'manifest.json'
        saved = json.loads(manifest.read_text())
        saved['status'] = 'removed'
        manifest.write_text(json.dumps(saved, ensure_ascii=False, indent=2) + '\n')
        print('旧 SR 部署已清除；SSH 交接尚未完成。请继续向导并验证新端口与新公钥。' if self.redeploy else
              'SuRelay 已卸载。SSH 登录配置、公钥和代码目录已保留。' if self.uninstall else
              '旧部署已卸载。接下来重新填写配置并执行安装。')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-dir', type=Path, required=True)
    parser.add_argument('--apply', action='store_true', help='显示清单并要求输入对应确认词后执行；默认只预览')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--uninstall', action='store_true', help='只卸载，不重新安装；确认词为 UNINSTALL')
    mode.add_argument('--redeploy', action='store_true', help='强重新布置，清除 SR 安全属性并交接 SSH；确认词 REDEPLOY')
    parser.add_argument('--handoff-file', type=Path, default=Path('/root/surelay-backups/redeploy-handoff.json'))
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ResetError('请以 root 运行重置安装。')
    reset = ResetDeployment(args.project_dir.resolve(), uninstall=args.uninstall,
                            redeploy=args.redeploy, handoff_file=args.handoff_file)
    word = 'REDEPLOY' if args.redeploy else 'UNINSTALL' if args.uninstall else 'RESET'
    try:
        reset.collect()
        reset.describe()
        if args.apply:
            tty_path = os.environ.get('RRB_INPUT_TTY', '/dev/tty')
            # A terminal is not seekable: Python's buffered r+ mode cannot open it.
            with open(tty_path, 'r') as tty_in:
                if not tty_in.isatty():
                    raise ResetError('重置确认需要交互式终端。')
                with open(tty_path, 'w') as tty_out:
                    label = '开始引导式节点布置' if args.redeploy else '卸载 SuRelay' if args.uninstall else '重置并重新安装'
                    tty_out.write(f'确认{label}，请输入大写 {word}（其他输入取消）：')
                    tty_out.flush()
                if tty_in.readline().strip() != word:
                    raise ResetError('已取消操作；旧部署保持不变。')
            reset.apply()
    except Exception:
        if reset.backup:
            print(f'重置未完成，已停止。恢复资料：{reset.backup}', flush=True)
        raise


if __name__ == '__main__':
    try:
        main()
    except (ResetError, OSError, sqlite3.Error, subprocess.TimeoutExpired) as exc:
        raise SystemExit(f'ERROR: {exc}')
