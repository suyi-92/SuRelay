#!/usr/bin/env python3
"""Remove a deployment before reinstalling; never purge shared system packages."""
from __future__ import annotations

import argparse
import configparser
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile


class ResetError(Exception):
    pass


class ResetDeployment:
    # root/runner injection is for isolated tests only, never exposed by the CLI.
    def __init__(self, project: Path, root: Path = Path('/'), runner=None):
        self.root = root.resolve()
        self.project = project
        self.runner = runner or self.run
        self.paths = {}
        self.empty_dirs = set()
        self.services = []
        self.shared = []
        self.firewall = []
        self.backup = None

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
        if marker and marker not in physical.read_text(errors='replace'):
            raise ResetError(f'配置没有项目归属标记，未卸载：{p}')
        if directory:
            # Only this fixed, project-specific tree is recursively removed.
            if str(p) != '/opt/reality-relay-certbot':
                raise ResetError(f'拒绝递归删除未知目录：{p}')
            for current, dirs, _ in os.walk(physical, followlinks=False):
                if os.path.ismount(current):
                    raise ResetError(f'目录内存在挂载点：{current}')
        self.paths[str(p)] = directory

    def text(self, value):
        self.safe_path(value)
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
        spec = importlib.util.spec_from_file_location('reset_generator', Path(__file__).with_name('08-generate-singbox-config.py'))
        gen = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gen)
        config = self.project / 'config.env'
        self.safe_path(config)
        env = gen.parse_env_file(self.path(config)) if self.exists(config) else {}
        # Reset does not validate deployment values: it also repairs failed installs.
        # Restrict custom directories to project-specific descendants so a typo
        # cannot designate /etc, /root, another application's config, or source code.
        state = self.config_path(env, 'RRB_STATE_DIR', '/etc/reality-relay-bootstrap')
        if state.name != 'reality-relay-bootstrap' and not state.name.startswith('reality-relay-bootstrap-'):
            raise ResetError(f'自定义 state 目录没有项目专用名称：{state}；请先核实并迁移。')
        self.empty_dirs.add(str(state))
        deployment = self.exists(config) or self.exists(state / 'vless-uuid.txt')
        for name in ('config.env', 'home-proxies.csv', 'upstream-nodes.txt',
                     'reality-relay-bootstrap-nodes.txt', 'reality-relay-bootstrap-clash.yaml',
                     'reality-relay-bootstrap-vless.txt'):
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
            if str(state) != '/etc/reality-relay-bootstrap' and value == f'/etc/reality-relay-bootstrap/{name}':
                value = ''
            self.add(self.config_path({key: value}, key, str(state / name), parent=state))
        self.add('/etc/fail2ban/jail.d/zz-surelay.local', marker='Managed by SuRelay.')
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
            self.services = ['reality-relay-subscription', 'sing-box']
            # A previous opt-in 233boy install takes precedence in singbox_bin().
            # Remove that binary and its unit override, so auto/APT can take over.
            legacy_binary = '/etc/sing-box/bin/sing-box'
            if self.exists(legacy_binary):
                self.add(legacy_binary)
                legacy_unit = '/etc/systemd/system/sing-box.service'
                if self.exists(legacy_unit):
                    self.add(legacy_unit, marker=legacy_binary)
                self.empty_dirs.add('/etc/sing-box/bin')
        for name in ('reality-relay-bootstrap-nodes.txt', 'reality-relay-bootstrap-clash.yaml', 'reality-relay-bootstrap-vless.txt',
                     'reality-relay-bootstrap-v6-nodes.txt', 'reality-relay-bootstrap-v6-clash.yaml', 'reality-relay-bootstrap-v6-vless.txt'):
            self.add(Path('/root') / name)
        self.add('/usr/local/lib/reality-relay-bootstrap/subscription-server.py')
        self.empty_dirs.add('/usr/local/lib/reality-relay-bootstrap')
        self.add('/etc/systemd/system/reality-relay-subscription.service', marker='Reality Relay Bootstrap')
        if '/etc/systemd/system/reality-relay-subscription.service' in self.paths and 'reality-relay-subscription' not in self.services:
            self.services.append('reality-relay-subscription')
        for service in ('sing-box', 'reality-relay-subscription'):
            init = f'/etc/init.d/{service}'
            if 'Managed by reality-relay-bootstrap' in self.text(init):
                self.add(init)
                self.add(f'/etc/conf.d/{service}')
                if service == 'sing-box':
                    self.add('/usr/local/bin/sing-box')
                if service not in self.services:
                    self.services.append(service)
        jail = '/etc/fail2ban/jail.d/sshd.local'
        if 'Managed by reality-relay-bootstrap' in self.text(jail):
            self.add(jail)
            self.shared.append('fail2ban')

        nginx = env.get('NGINX_FALLBACK_CONF', 'auto')
        candidates = {Path(f'/etc/nginx/{d}/reality-relay-bootstrap.conf') for d in ('sites-available', 'http.d')}
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
            self.add(conf, marker='Managed by reality-relay-bootstrap')
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
        fallback = self.config_path(env, 'NGINX_FALLBACK_ROOT', '/var/www/reality-fallback')
        if fallback.name != 'reality-fallback':
            raise ResetError(f'自定义 fallback 目录未确认归属：{fallback}')
        for name in ('index.html', 'robots.txt', 'favicon.ico'):
            self.add(fallback / name)
        self.empty_dirs.add(str(fallback))
        self.add('/etc/letsencrypt/renewal-hooks/deploy/reality-relay-bootstrap-nginx.sh')
        self.add('/etc/periodic/daily/reality-relay-certbot')
        self.collect_certificates()
        self.add('/opt/reality-relay-certbot', directory=True)
        self.empty_dirs.add('/etc/sing-box')
        self.alpine = bool(re.search(r'^ID=[\"\']?alpine[\"\']?$', self.text('/etc/os-release'), re.M))
        self.collect_firewall(env)

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
            if 'reality-relay-bootstrap' not in line:
                continue
            args = shlex.split(line)
            if len(args) != 5 or args[:2] != ['ufw', 'allow'] or args[3] != 'comment' or not re.fullmatch(r'\d+/tcp', args[2]):
                raise ResetError('发现无法自动识别的项目 UFW 规则，未开始重置。')
            if 'SSH' in args[4] or args[2].split('/')[0] in ports:
                continue
            if not args[4].startswith(('reality-relay-bootstrap sing-box ', 'reality-relay-bootstrap subscription ')):
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
        print('重置安装将停止旧节点和订阅，清空配置/出口列表，并在重装时生成新密钥。')
        print('备份后删除以下项目文件：')
        for path in sorted(self.paths):
            print(f'  {path}')
        print('停用服务：' + (', '.join(self.services) or '无'))
        print(f'清理代理/订阅 UFW 规则：{len(self.firewall)} 条；保留 SSH 规则。')
        print('保留系统软件包、SSH 配置/公钥/账户、其他证书、Cloudflare DNS、其他站点及历史备份。')

    def snapshot(self):
        base = self.path('/root/reality-relay-bootstrap-backups')
        self.safe_path('/root/reality-relay-bootstrap-backups')
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        base.chmod(0o700)
        self.backup = Path(tempfile.mkdtemp(prefix='reset-', dir=base))
        extra = ['/etc/ssh', '/root/.ssh/authorized_keys', '/etc/ufw', '/etc/default/ufw']
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

    def apply(self):
        state = self.snapshot()
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
        print('旧部署已卸载。接下来重新填写配置并执行安装。')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-dir', type=Path, required=True)
    parser.add_argument('--apply', action='store_true', help='显示清单并要求输入 RESET 后执行；默认只预览')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ResetError('请以 root 运行重置安装。')
    reset = ResetDeployment(args.project_dir.resolve())
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
                    tty_out.write('确认卸载旧部署并重新安装，请输入大写 RESET（其他输入取消）：')
                    tty_out.flush()
                if tty_in.readline().strip() != 'RESET':
                    raise ResetError('已取消重置；旧部署保持不变。')
            reset.apply()
    except Exception:
        if reset.backup:
            print(f'重置未完成，已停止。恢复资料：{reset.backup}', flush=True)
        raise


if __name__ == '__main__':
    try:
        main()
    except (ResetError, OSError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(f'ERROR: {exc}')
