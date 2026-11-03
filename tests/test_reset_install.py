from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('reset_install', ROOT / 'scripts/17-reset-install.py')
reset = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reset)


@unittest.skipUnless(os.name == 'posix', 'Requires POSIX filesystem semantics')
class ResetInstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = Path('/opt/relay')
        self.calls = []
        self.active = {'sing-box', 'surelay-subscription', 'nginx', 'fail2ban'}
        self.rules = ["ufw allow 22/tcp comment 'surelay SSH current port'",
                      "ufw allow 443/tcp comment 'surelay sing-box VLESS+Reality 443'",
                      "ufw allow 51040/tcp comment 'surelay subscription 51040'",
                      "ufw allow 8080/tcp comment 'unrelated application'"]
        self.put('/etc/os-release', 'ID=debian\n')
        self.put('/opt/relay/config.env', 'SERVER_ALIAS=old\nSERVER_IP=192.0.2.10\n')
        self.put('/opt/relay/home-proxies.csv', 'old-list\n')
        self.put('/opt/relay/upstream-nodes.txt', 'old-upstream\n')
        self.put('/opt/relay/README.md', 'source code must survive\n')
        self.put('/etc/surelay/vless-uuid.txt', 'old-identity\n')
        self.put('/etc/surelay/reality-private.key', 'fake-test-private\n')
        self.put('/etc/surelay/subscription/all/clashmeta.yaml', 'old-subscription\n')
        self.put('/etc/surelay/user-note.txt', 'keep non-project data\n')
        self.put('/etc/sing-box/config.json', '{"inbounds": []}\n')
        self.put('/etc/systemd/system/surelay-subscription.service', 'Description=SuRelay subscription file server\n')
        self.put('/etc/nginx/sites-available/surelay.conf', '# Managed by surelay.\n')
        self.put('/etc/nginx/sites-enabled/other.conf', 'other site\n')
        (self.p('/etc/nginx/sites-enabled/surelay.conf')).symlink_to('../sites-available/surelay.conf')
        self.put('/etc/fail2ban/jail.d/sshd.local', '# Managed by surelay.\n')
        self.put('/etc/fail2ban/jail.d/other.local', 'other jail\n')
        self.put('/etc/ufw/ufw.conf', 'ENABLED=yes\n')
        self.put('/etc/ssh/sshd_config', 'working SSH config\n')
        self.put('/root/.ssh/authorized_keys', 'ssh-ed25519 test\n')

    def p(self, value):
        return self.root / str(value).lstrip('/')

    def put(self, value, content):
        p = self.p(value)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return p

    def command(self, args, check=True):
        self.calls.append(args)
        code, output = 0, ''
        if args[:3] == ['ufw', 'show', 'added']:
            output = '\n'.join(self.rules)
        elif args[:3] == ['ufw', '--force', 'delete']:
            original = next(rule for rule in self.rules if shlex.split(rule) == ['ufw', *args[3:]])
            self.rules.remove(original)
        elif args[0] == 'systemctl':
            if args[1] == 'show':
                output = 'loaded\n'
            elif args[1] == 'is-active':
                code = 0 if args[-1] in self.active else 3
            elif args[1] == 'disable':
                self.active.discard(args[-1])
        elif args[0] == 'rc-service':
            if args[2] == 'status':
                code = 0 if args[1] in self.active else 3
            elif args[2] == 'stop':
                self.active.discard(args[1])
        elif args[0] == 'rc-update':
            self.p(f'/etc/runlevels/{args[3]}/{args[2]}').unlink()
        return subprocess.CompletedProcess(args, code, output, '')

    def resetter(self):
        obj = reset.ResetDeployment(self.project, self.root, self.command)
        obj.collect()
        return obj

    def test_systemd_reset_archives_before_removal_and_preserves_shared_assets(self):
        obj = self.resetter()
        original = {p: self.p(p).read_bytes() for p in obj.paths if self.p(p).is_file()}
        obj.apply()
        self.assertTrue(obj.backup.is_dir())
        self.assertEqual(obj.backup.stat().st_mode & 0o777, 0o700)
        archive = obj.backup / 'files.tar.gz'
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        with tarfile.open(archive) as tar:
            for value, data in original.items():
                if tar.getmember(value.lstrip('/')).isfile():
                    self.assertEqual(tar.extractfile(value.lstrip('/')).read(), data)
        self.assertTrue(all(not self.p(p).exists() for p in obj.paths))
        for value in ('/etc/ssh/sshd_config', '/root/.ssh/authorized_keys', '/opt/relay/README.md',
                      '/etc/nginx/sites-enabled/other.conf', '/etc/fail2ban/jail.d/other.local',
                      '/etc/surelay/user-note.txt'):
            self.assertTrue(self.p(value).is_file(), value)
        self.assertEqual(len(self.rules), 2)
        self.assertIn('SSH', self.rules[0])
        self.assertIn('unrelated', self.rules[1])
        self.assertIn(['systemctl', 'disable', '--now', 'sing-box'], self.calls)
        self.assertIn(['systemctl', 'reload', 'nginx'], self.calls)
        self.assertEqual(json.loads((obj.backup / 'manifest.json').read_text())['status'], 'removed')

    def test_alpine_removes_owned_services_from_every_runlevel(self):
        self.put('/etc/os-release', 'ID=alpine\nVERSION_ID=3.19.9\n')
        self.put('/usr/local/bin/sing-box', 'musl fixture\n')
        for name in ('sing-box', 'surelay-subscription'):
            self.put(f'/etc/init.d/{name}', '# Managed by surelay.\n')
            self.put(f'/etc/conf.d/{name}', 'command=test\n')
            for level in ('default', 'custom'):
                p = self.p(f'/etc/runlevels/{level}/{name}')
                p.parent.mkdir(parents=True, exist_ok=True)
                p.symlink_to(f'../../init.d/{name}')
        obj = self.resetter()
        obj.apply()
        self.assertFalse(self.p('/usr/local/bin/sing-box').exists())
        self.assertFalse(any(self.p('/etc/runlevels').glob('*/*')))
        self.assertIn(['rc-service', 'sing-box', 'stop'], self.calls)
        self.assertFalse(any(c[0] == 'systemctl' for c in self.calls))

    def test_preview_has_no_writes_or_service_actions(self):
        obj = self.resetter()
        obj.describe()
        self.assertTrue(self.p('/opt/relay/config.env').exists())
        self.assertFalse(self.p('/root/surelay-backups').exists())
        self.assertEqual(self.calls, [['ufw', 'show', 'added']])

    def test_os_release_symlink_is_read_only_metadata(self):
        for distro in ('debian', 'ubuntu', 'alpine'):
            contents = f'ID="{distro}"\n'
            target = self.put('/usr/lib/os-release', contents)
            for destination in ('../usr/lib/os-release', target):
                self.p('/etc/os-release').unlink()
                self.p('/etc/os-release').symlink_to(destination)
                for uninstall in (False, True):
                    with self.subTest(distro=distro, target=destination, uninstall=uninstall):
                        self.calls.clear()
                        obj = reset.ResetDeployment(self.project, self.root, self.command, uninstall=uninstall)
                        obj.collect()
                        with redirect_stdout(io.StringIO()):
                            obj.describe()
                        self.assertEqual(obj.alpine, distro == 'alpine')
                        self.assertNotIn('/etc/os-release', obj.paths)
                        self.assertNotIn('/usr/lib/os-release', obj.paths)
                        self.assertTrue(self.p('/etc/os-release').is_symlink())
                        self.assertEqual(target.read_text(), contents)
                        self.assertTrue(self.p('/opt/relay/config.env').exists())
                        self.assertFalse(self.p('/root/surelay-backups').exists())
                        self.assertEqual(self.calls, [['ufw', 'show', 'added']])

    def test_confirmation_cancel_and_eof_do_not_create_backup(self):
        for reply in ('no\n', '\n', ''):
            obj = reset.ResetDeployment(self.project, self.root, self.command)
            with patch.object(reset, 'ResetDeployment', return_value=obj), \
                    patch('sys.argv', ['reset', '--project-dir', '/opt/relay', '--apply']), \
                    patch.object(os, 'geteuid', return_value=0), \
                    patch('builtins.open') as tty, redirect_stdout(io.StringIO()):
                tty.return_value.__enter__.return_value.readline.return_value = reply
                with self.assertRaisesRegex(reset.ResetError, '已取消'):
                    reset.main()
            self.assertIsNone(obj.backup)
            self.assertTrue(self.p('/opt/relay/config.env').exists())

    def test_real_terminal_confirmation_runs_reset(self):
        import pty
        master, slave = pty.openpty()
        try:
            obj = reset.ResetDeployment(self.project, self.root, self.command)
            os.write(master, b'RESET\n')
            with patch.object(reset, 'ResetDeployment', return_value=obj), \
                    patch('sys.argv', ['reset', '--project-dir', '/opt/relay', '--apply']), \
                    patch.object(os, 'geteuid', return_value=0), \
                    patch.dict(os.environ, {'RRB_INPUT_TTY': os.ttyname(slave)}), \
                    redirect_stdout(io.StringIO()):
                reset.main()
            self.assertFalse(self.p('/opt/relay/config.env').exists())
            self.assertTrue((obj.backup / 'files.tar.gz').exists())
        finally:
            os.close(master)
            os.close(slave)

    def test_backup_failure_prevents_stop_and_delete(self):
        obj = self.resetter()
        with patch.object(tarfile.TarFile, 'add', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                obj.apply()
        self.assertTrue(all(obj.exists(p) for p in obj.paths))
        self.assertFalse(any(c[0] in ('systemctl', 'rc-service') for c in self.calls))

    def test_stop_failure_keeps_all_files_and_backup(self):
        obj = self.resetter()
        previous = obj.runner
        def fail(args, check=True):
            if args[:2] == ['systemctl', 'disable']:
                raise reset.ResetError('stop failure')
            return previous(args, check)
        obj.runner = fail
        with self.assertRaisesRegex(reset.ResetError, 'stop failure'):
            obj.apply()
        self.assertTrue(all(obj.exists(p) for p in obj.paths))
        self.assertTrue((obj.backup / 'files.tar.gz').exists())

    def test_unsafe_custom_paths_fail_before_any_cleanup(self):
        for setting in ('RRB_STATE_DIR=/etc', 'CSV_PATH=/etc/passwd',
                        'SINGBOX_CONFIG_PATH=/etc/ssh/sshd_config', 'SUBSCRIPTION_DIR=/root',
                        'REALITY_PRIVATE_KEY_PATH=/etc/surelay/../passwd'):
            with self.subTest(setting=setting):
                self.put('/opt/relay/config.env', setting + '\n')
                with self.assertRaises(reset.ResetError):
                    self.resetter()
                self.assertTrue(self.p('/etc/sing-box/config.json').exists())
        self.assertFalse(self.calls)

    def test_symlink_state_parent_is_rejected(self):
        self.p('/etc/surelay').rename(self.p('/etc/other-data'))
        self.p('/etc/surelay').symlink_to('other-data')
        with self.assertRaisesRegex(reset.ResetError, '符号链接'):
            self.resetter()
        self.assertTrue(self.p('/etc/other-data/vless-uuid.txt').exists())

    def test_symlink_config_target_is_rejected(self):
        target = self.put('/srv/other-config.json', '{"keep": true}\n')
        self.p('/etc/sing-box/config.json').unlink()
        self.p('/etc/sing-box/config.json').symlink_to('../../srv/other-config.json')
        with self.assertRaisesRegex(reset.ResetError, '符号链接'):
            self.resetter()
        self.assertEqual(target.read_text(), '{"keep": true}\n')
        self.assertTrue(self.p('/opt/relay/config.env').exists())
        self.assertFalse(self.p('/root/surelay-backups').exists())
        self.assertFalse(self.calls)

    def test_unowned_nginx_configuration_stops_reset(self):
        self.put('/etc/nginx/sites-available/surelay.conf', '# manually owned\n')
        with self.assertRaisesRegex(reset.ResetError, '归属标记'):
            self.resetter()
        self.assertTrue(self.p('/opt/relay/config.env').exists())

    def certificate(self):
        self.put('/etc/surelay/cloudflare.ini', 'dns_cloudflare_api_token = fixture\n')
        self.put('/etc/letsencrypt/renewal/edge.example.test.conf',
                 'version = 4.0.0\n[renewalparams]\ndns_cloudflare_credentials = /etc/surelay/cloudflare.ini\n')
        self.put('/etc/letsencrypt/archive/edge.example.test/cert1.pem', 'test certificate\n')
        p = self.p('/etc/letsencrypt/live/edge.example.test/cert.pem')
        p.parent.mkdir(parents=True, exist_ok=True)
        p.symlink_to('../../archive/edge.example.test/cert1.pem')
        self.put('/etc/letsencrypt/renewal/unrelated.conf', 'version = 4.0.0\n[renewalparams]\nauthenticator = nginx\n')

    def test_removes_only_certificate_lineage_owned_by_project_credentials(self):
        self.certificate()
        obj = self.resetter()
        obj.apply()
        self.assertFalse(self.p('/etc/letsencrypt/live/edge.example.test').exists())
        self.assertFalse(self.p('/etc/letsencrypt/archive/edge.example.test').exists())
        self.assertTrue(self.p('/etc/letsencrypt/renewal/unrelated.conf').exists())

    def test_shared_certificate_reference_stops_reset(self):
        self.certificate()
        self.put('/etc/nginx/sites-enabled/other.conf', 'ssl_certificate /etc/letsencrypt/live/edge.example.test/cert.pem;\n')
        with self.assertRaisesRegex(reset.ResetError, '其他配置引用'):
            self.resetter()

    def test_shared_certificate_reference_through_symlink_stops_reset(self):
        self.certificate()
        self.put('/srv/other-site.conf', 'ssl_certificate /etc/letsencrypt/live/edge.example.test/cert.pem;\n')
        self.p('/etc/nginx/sites-enabled/other.conf').unlink()
        self.p('/etc/nginx/sites-enabled/other.conf').symlink_to('../../../srv/other-site.conf')
        with self.assertRaisesRegex(reset.ResetError, '其他配置引用'):
            self.resetter()

    def test_repeated_reset_handles_partial_install_and_missing_files(self):
        self.resetter().apply()
        self.calls.clear()
        obj = self.resetter()
        self.assertFalse(obj.paths)
        self.assertFalse(obj.services)
        obj.apply()
        self.assertTrue(self.p('/root/.ssh/authorized_keys').exists())

    def test_conflicting_modes_rejected_in_both_orders(self):
        for args in [('--reset', '-6'), ('-6', '--reset'), ('--reset', '--ipv6-address', '2001:db8::1')]:
            proc = subprocess.run(['bash', str(ROOT / 'install.sh'), *args], capture_output=True, text=True)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn('不能与 -6', proc.stderr)

    def test_reset_install_preserves_ssh_policy_until_confirmation(self):
        functions = (ROOT / 'install.sh').read_text().rsplit('\nmain "$@"', 1)[0]
        project = self.root / 'entry'
        (project / 'scripts').mkdir(parents=True)
        (project / 'config.env').write_text('SERVER_IP_IPV4=192.0.2.10\nSSH_PORT=2222\n')
        (project / 'scripts/02-setup-root-ssh.sh').write_text('printf "keys\\n" >>"$RRB_PROJECT_DIR/phases"\n')
        (project / 'scripts/03-ssh-hardening-phase1.sh').write_text(
            'test "$RRB_RESET_INSTALL" = true\nprintf "stage-ports\\n" >>"$RRB_PROJECT_DIR/phases"\n')
        # Fixed local filename, so no service script from the repository executes.
        script = functions + '''
INSTALL_MODE=reset
RUN_PHASES=true
phase_log="$1/phases"
run_install_phase() { printf '%s\\n' "$1" >>"$phase_log"; }
read_yes_no() { return 1; }
run_install_flow "$1"
'''
        result = subprocess.run(['bash', '-c', script, 'test', str(project)], capture_output=True, text=True,
                                env={**os.environ, 'SSH_CONNECTION': ''}, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((project / 'phases').read_text().splitlines(), ['preflight', 'keys', 'stage-ports'])
        self.assertIn('RRB_RESET_INSTALL=true', result.stderr)

    def redeployer(self, **kwargs):
        obj = reset.ResetDeployment(self.project, self.root, self.command, redeploy=True, **kwargs)
        obj.collect()
        return obj

    def test_redeploy_removes_custom_security_but_defers_every_rule_carrying_ssh(self):
        security = self.put('/etc/surelay/surelay-firewall.json', '{"old": "custom safety"}\n')
        ledger = self.put('/etc/surelay/ssh-managed-keys.json', '{"format": 1, "keys": ["ssh-ed25519 AAAA"]}\n')
        socket = self.put('/etc/systemd/system/ssh.socket.d/surelay.conf', '[Socket]\nListenStream=22\n')
        self.put('/etc/fail2ban/jail.d/zz-surelay.local', '# Managed by SuRelay.\nignoreip = 192.0.2.1\n')
        immediate = ["ufw allow 54443/udp comment 'surelay sing-box 54443/udp'",
                     "ufw allow proto udp from ::/0 to any port 60000:60010 comment 'SuRelay custom old-v6'",
                     "ufw allow proto tcp from 192.0.2.0/24 to any port 52000:52010 comment 'SuRelay custom old-range'"]
        deferred = ["ufw allow proto tcp from 2001:db8::/32 to any port 20:25 comment 'SuRelay custom ssh-range'",
                    "ufw allow 50396/tcp comment 'SuRelay custom current-session'",
                    "ufw allow 55000/tcp comment 'surelay SSH old socket'"]
        self.rules.extend([*immediate, *deferred])
        previous = self.command
        runtime_bans = ['192.0.2.9', '2001:db8::9']
        def metadata(args, check=True):
            if args[-1:] == ['-T']:
                return subprocess.CompletedProcess(args, 0, 'port 22\nport 55000\n', '')
            if args[:3] == ['ss', '-H', '-ltnp']:
                return subprocess.CompletedProcess(args, 0, 'LISTEN 0 128 [::]:55001 [::]:* users:(("sshd",pid=1,fd=3))\n', '')
            if args == ['fail2ban-client', 'get', 'sshd', 'banip']:
                return subprocess.CompletedProcess(args, 0, ' '.join(runtime_bans), '')
            if args[:4] == ['fail2ban-client', 'set', 'sshd', 'unbanip']:
                runtime_bans.remove(args[4])
            return previous(args, check)
        self.command = metadata
        with patch.dict(os.environ, {'SSH_CONNECTION': '192.0.2.5 40000 192.0.2.10 50396'}):
            obj = self.redeployer()
        self.assertEqual(obj.ssh_ports, {22, 50396, 55000, 55001})
        obj.apply()
        self.assertFalse(security.exists())
        self.assertFalse(ledger.exists())
        self.assertTrue(socket.is_file())
        self.assertTrue(self.p('/etc/ssh/sshd_config').is_file())
        self.assertEqual(self.p('/root/.ssh/authorized_keys').read_text(), 'ssh-ed25519 test\n')
        for rule in immediate:
            self.assertNotIn(rule, self.rules)
        for rule in deferred:
            self.assertIn(rule, self.rules)
        self.assertIn("ufw allow 22/tcp comment 'surelay SSH current port'", self.rules)
        self.assertIn("ufw allow 8080/tcp comment 'unrelated application'", self.rules)
        handoff = self.p('/root/surelay-backups/redeploy-handoff.json')
        self.assertEqual(handoff.stat().st_mode & 0o777, 0o600)
        record = json.loads(handoff.read_text())
        self.assertEqual(record['status'], 'removed')
        self.assertEqual(record['old_managed_keys'], ['ssh-ed25519 AAAA'])
        self.assertEqual(record['current_ssh_port'], 50396)
        self.assertEqual(record['old_authorized_keys'], ['ssh-ed25519 test'])
        self.assertIn('/etc/systemd/system/ssh.socket.d/surelay.conf', record['ssh_assets'])
        with tarfile.open(obj.backup / 'files.tar.gz') as archive:
            self.assertEqual(archive.extractfile('etc/surelay/surelay-firewall.json').read(), b'{"old": "custom safety"}\n')
            self.assertTrue(archive.getmember('etc/systemd/system/ssh.socket.d/surelay.conf').isfile())
        self.assertEqual(runtime_bans, [])
        self.assertIn(['fail2ban-client', 'set', 'sshd', 'unbanip', '192.0.2.9'], self.calls)
        self.assertIn(['fail2ban-client', 'set', 'sshd', 'unbanip', '2001:db8::9'], self.calls)
        self.assertFalse(any(args[:2] == ['fail2ban-client', 'unban'] for args in self.calls))

    def test_redeploy_preview_is_read_only_and_retains_unknown_public_keys(self):
        original = self.p('/root/.ssh/authorized_keys').read_bytes()
        obj = self.redeployer()
        with redirect_stdout(io.StringIO()) as output:
            obj.describe()
        self.assertIn('归属不明', output.getvalue())
        self.assertIsNone(obj.backup)
        self.assertFalse(self.p('/root/surelay-backups').exists())
        self.assertEqual(self.p('/root/.ssh/authorized_keys').read_bytes(), original)
        self.assertTrue(self.p('/opt/relay/config.env').is_file())
        self.assertFalse(any('delete' in args or 'disable' in args or 'restart' in args for args in self.calls))

    def test_redeploy_reads_multiline_and_legacy_ansi_quoted_key_sources_without_execution(self):
        for assignment in ("ADMIN_PUBKEY='ssh-ed25519 AAAA one\nssh-ed25519 BBBB two'\n",
                           "ADMIN_PUBKEY=$'ssh-ed25519 AAAA one\\nssh-ed25519 BBBB two'\n"):
            with self.subTest(assignment=assignment):
                self.put('/opt/relay/config.env', 'SERVER_ALIAS=old\nSSH_PORT=22\n' + assignment)
                obj = self.redeployer()
                self.assertEqual(obj.old_config['ADMIN_PUBKEY'].splitlines(),
                                 ['ssh-ed25519 AAAA one', 'ssh-ed25519 BBBB two'])

    def test_redeploy_consistently_backs_up_shared_fail2ban_db_without_removing_other_jails(self):
        database = self.p('/var/lib/fail2ban/fail2ban.sqlite3')
        database.parent.mkdir(parents=True)
        with sqlite3.connect(database) as connection:
            connection.execute('CREATE TABLE bans (jail TEXT, ip TEXT)')
            connection.execute("INSERT INTO bans VALUES ('other-jail', '192.0.2.8')")
            connection.execute("INSERT INTO bans VALUES ('sshd', '192.0.2.9')")
        obj = self.redeployer()
        obj.apply()
        with sqlite3.connect(obj.backup / 'fail2ban.sqlite3') as connection:
            self.assertEqual(connection.execute('SELECT * FROM bans').fetchall(),
                             [('other-jail', '192.0.2.8'), ('sshd', '192.0.2.9')])
        with sqlite3.connect(database) as connection:
            self.assertEqual(connection.execute('SELECT * FROM bans').fetchall(), [('other-jail', '192.0.2.8')])
        self.assertEqual((obj.backup / 'fail2ban.sqlite3').stat().st_mode & 0o777, 0o600)

    def test_redeploy_uses_configured_fail2ban_db_and_only_removes_sshd_bans_and_bips(self):
        database = self.p('/srv/security/shared-bans.sqlite3')
        database.parent.mkdir(parents=True)
        with sqlite3.connect(database) as connection:
            for table in ('bans', 'bips'):
                connection.execute(f'CREATE TABLE {table} (jail TEXT, ip TEXT)')
                connection.executemany(f'INSERT INTO {table} VALUES (?, ?)',
                                       [('sshd', '192.0.2.9'), ('other-jail', '192.0.2.8')])
        previous = self.command
        def settings(args, check=True):
            if args == ['fail2ban-client', 'get', 'dbfile']:
                return subprocess.CompletedProcess(args, 0,
                    'Current database file is:\n`- /srv/security/shared-bans.sqlite3\n', '')
            return previous(args, check)
        self.command = settings
        obj = self.redeployer()
        obj.apply()
        with sqlite3.connect(database) as connection:
            for table in ('bans', 'bips'):
                self.assertEqual(connection.execute(f'SELECT * FROM {table}').fetchall(),
                                 [('other-jail', '192.0.2.8')])
        self.assertTrue((obj.backup / 'fail2ban.sqlite3').is_file())

    def test_redeploy_accepts_plain_and_formatted_fail2ban_db_paths(self):
        database = '/srv/security/shared-bans.sqlite3'
        self.put(database, 'database selected during read-only collection\n')
        previous = self.command
        for output in (database + '\n', 'Current database file is:\n`- ' + database + '\n',
                       'Current database file is:\r\n`- ' + database + '\r\n'):
            with self.subTest(output=output), patch.object(self, 'command', side_effect=(
                    lambda args, check=True: subprocess.CompletedProcess(args, 0, output, '')
                    if args == ['fail2ban-client', 'get', 'dbfile'] else previous(args, check))):
                obj = self.redeployer()
                self.assertEqual(obj.fail2ban_database, Path(database))
                self.assertIsNone(obj.backup)
                self.assertFalse(self.p('/root/surelay-backups').exists())

    def test_redeploy_respects_disabled_plain_and_formatted_fail2ban_db(self):
        self.put('/var/lib/fail2ban/fail2ban.sqlite3', 'existing default database must not be selected\n')
        previous = self.command
        for output in ('None\n', 'Current database file is:\n`- None\n', 'Database currently disabled\n'):
            with self.subTest(output=output), patch.object(self, 'command', side_effect=(
                    lambda args, check=True: subprocess.CompletedProcess(args, 0, output, '')
                    if args == ['fail2ban-client', 'get', 'dbfile'] else previous(args, check))):
                obj = self.redeployer()
                self.assertIsNone(obj.fail2ban_database)
                self.assertIsNone(obj.backup)

    def test_redeploy_rejects_ambiguous_or_unsafe_fail2ban_db_output_before_cleanup(self):
        previous = self.command
        database = '/var/lib/fail2ban/fail2ban.sqlite3'
        invalid = ('Current database file is:',
                   'Current database file is:\n|- ' + database,
                   'Current database file is:\n`- ' + database + '\nextra output',
                   'Current database file is:\n`- ' + database + '\n`- /srv/other.sqlite3',
                   'Database currently disabled\n' + database,
                   'Database currently disabled but extra text',
                   database + '\n/srv/other.sqlite3',
                   'Current database file is:\n`- relative.sqlite3',
                   'Current database file is:\n`- /srv/../other.sqlite3',
                   'Current database file is:\n`- /srv/$(touch forbidden).sqlite3',
                   'Current database file is:\n`- /srv/`touch forbidden`.sqlite3')
        for output in invalid:
            with self.subTest(output=output), patch.object(self, 'command', side_effect=(
                    lambda args, check=True: subprocess.CompletedProcess(args, 0, output, '')
                    if args == ['fail2ban-client', 'get', 'dbfile'] else previous(args, check))):
                with self.assertRaises(reset.ResetError):
                    self.redeployer()
                self.assertTrue(self.p('/opt/relay/config.env').is_file())
                self.assertFalse(self.p('/root/surelay-backups').exists())
        self.assertFalse(any('delete' in args or 'disable' in args or 'restart' in args for args in self.calls))

    def test_redeploy_rejects_formatted_fail2ban_db_symlink(self):
        database = self.put('/srv/security/shared-bans.sqlite3', 'keep shared database\n')
        link = self.p('/var/lib/fail2ban/fail2ban.sqlite3')
        link.parent.mkdir(parents=True)
        link.symlink_to(database)
        previous = self.command
        def settings(args, check=True):
            if args == ['fail2ban-client', 'get', 'dbfile']:
                return subprocess.CompletedProcess(args, 0,
                    'Current database file is:\n`- /var/lib/fail2ban/fail2ban.sqlite3\n', '')
            return previous(args, check)
        self.command = settings
        with self.assertRaisesRegex(reset.ResetError, '符号链接'):
            self.redeployer()
        self.assertEqual(database.read_text(), 'keep shared database\n')
        self.assertFalse(self.p('/root/surelay-backups').exists())

    def test_redeploy_unbans_legacy_fail2ban_jail_without_global_unban(self):
        previous = self.command
        banned = ['192.0.2.9']
        def legacy(args, check=True):
            if args == ['fail2ban-client', 'get', 'sshd', 'banip']:
                return subprocess.CompletedProcess(args, 1, '', 'Invalid command')
            if args == ['fail2ban-client', 'status', 'sshd']:
                return subprocess.CompletedProcess(args, 0, 'Actions\nBanned IP list: ' + ' '.join(banned) + '\n', '')
            if args[:4] == ['fail2ban-client', 'set', 'sshd', 'unbanip']:
                banned.remove(args[4])
            return previous(args, check)
        self.command = legacy
        obj = self.redeployer()
        obj.apply()
        self.assertEqual(banned, [])
        self.assertFalse(any('--all' in args for args in self.calls))

    def test_redeploy_backup_failure_never_writes_handoff_or_cleans_old_deployment(self):
        obj = self.redeployer()
        with patch.object(tarfile.TarFile, 'add', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                obj.apply()
        self.assertFalse(self.p('/root/surelay-backups/redeploy-handoff.json').exists())
        self.assertTrue(self.p('/opt/relay/config.env').is_file())
        self.assertFalse(any('disable' in args or 'delete' in args for args in self.calls))

    def test_failed_redeploy_keeps_private_recovery_state_and_blocks_ordinary_reset(self):
        obj = self.redeployer()
        previous = obj.runner
        def failure(args, check=True):
            if args[:2] == ['systemctl', 'disable']:
                raise reset.ResetError('fixture stop failure')
            return previous(args, check)
        obj.runner = failure
        with self.assertRaisesRegex(reset.ResetError, 'fixture stop failure'):
            obj.apply()
        handoff = self.p('/root/surelay-backups/redeploy-handoff.json')
        self.assertEqual(json.loads(handoff.read_text())['status'], 'failed')
        self.assertTrue((obj.backup / 'files.tar.gz').is_file())
        self.assertTrue(self.p('/opt/relay/config.env').is_file())
        for uninstall in (False, True):
            with self.subTest(uninstall=uninstall), self.assertRaisesRegex(reset.ResetError, '尚未完成'):
                reset.ResetDeployment(self.project, self.root, self.command, uninstall=uninstall).collect()

    def test_redeploy_refuses_unfinished_corrupt_or_symlinked_handoff_before_any_changes(self):
        handoff = self.p('/root/surelay-backups/redeploy-handoff.json')
        handoff.parent.mkdir(parents=True)
        for contents in ('{broken', '{}', '{"format":1,"mode":"redeploy","status":"removed"}'):
            handoff.write_text(contents)
            with self.subTest(contents=contents), self.assertRaises(reset.ResetError):
                self.redeployer()
            self.assertTrue(self.p('/opt/relay/config.env').is_file())
        handoff.unlink()
        handoff.symlink_to('../../../srv/unrelated')
        with self.assertRaisesRegex(reset.ResetError, '符号链接'):
            self.redeployer()

    def test_only_finalized_valid_handoff_allows_a_new_reset(self):
        handoff = self.put('/root/surelay-backups/redeploy-handoff.json', json.dumps({
            'format': 1, 'mode': 'redeploy', 'status': 'finalized', 'project': '/opt/relay',
            'backup_dir': '/root/surelay-backups/redeploy-old'}))
        handoff.chmod(0o600)
        self.assertTrue(self.resetter().paths)

    def test_redeploy_defers_source_port_rules_and_bare_socket_ports(self):
        rules = ["ufw allow proto tcp from 192.0.2.5 port 40000 to any port 22 comment 'SuRelay custom source-destination'",
                 "ufw allow proto tcp from 192.0.2.5 port 40000 to any comment 'SuRelay custom source-only'",
                 "ufw allow 55001/tcp comment 'SuRelay custom socket-only'"]
        self.rules.extend(rules)
        previous = self.command
        def socket(args, check=True):
            if args == ['systemctl', 'show', 'ssh.socket', '--property=Listen', '--value']:
                return subprocess.CompletedProcess(args, 0, '55001 (Stream)', '')
            return previous(args, check)
        self.command = socket
        obj = self.redeployer()
        self.assertIn(55001, obj.ssh_ports)
        pending = [shlex.split(rule) for rule in rules]
        for rule in pending:
            self.assertIn(['ufw', '--force', 'delete', *rule[1:]], obj.pending_firewall)

    def test_redeploy_removes_owned_state_tls_but_keeps_external_certs_and_unknown_files(self):
        self.put('/opt/relay/config.env', 'HY2_CERT_PATH=/etc/surelay/hy2.pem\nHY2_KEY_PATH=/etc/surelay/hy2.key\n')
        self.put('/etc/surelay/hy2.pem', 'state certificate\n')
        self.put('/etc/surelay/hy2.key', 'state private key\n')
        obj = self.redeployer()
        obj.apply()
        self.assertFalse(self.p('/etc/surelay/hy2.pem').exists())
        self.assertFalse(self.p('/etc/surelay/hy2.key').exists())
        self.assertTrue(self.p('/etc/surelay/user-note.txt').exists())
        # A separate fixture starts without the unfinished redeploy record.
        self.p('/root/surelay-backups/redeploy-handoff.json').unlink()
        self.put('/opt/relay/config.env', 'HY2_CERT_PATH=/srv/shared/cert.pem\nHY2_KEY_PATH=/srv/shared/key.pem\n')
        self.put('/srv/shared/cert.pem', 'external certificate\n')
        self.put('/srv/shared/key.pem', 'external private key\n')
        obj = self.redeployer()
        self.assertEqual(set(obj.external_tls_preserved), {'/srv/shared/cert.pem', '/srv/shared/key.pem'})
        self.assertNotIn('/srv/shared/cert.pem', obj.paths)
        self.assertNotIn('/srv/shared/key.pem', obj.paths)

    def test_redeploy_refuses_state_hy2_cert_referenced_by_other_web_configuration(self):
        self.put('/opt/relay/config.env', 'HY2_CERT_PATH=/etc/surelay/hy2.pem\n')
        self.put('/etc/surelay/hy2.pem', 'shared certificate\n')
        self.put('/etc/nginx/sites-enabled/other.conf', 'ssl_certificate /etc/surelay/hy2.pem;\n')
        with self.assertRaisesRegex(reset.ResetError, '其他配置引用'):
            self.redeployer()
        self.assertFalse(self.p('/root/surelay-backups').exists())

    def test_redeploy_cancellation_preserves_all_old_files_and_never_creates_handoff(self):
        obj = reset.ResetDeployment(self.project, self.root, self.command, redeploy=True)
        with patch.object(reset, 'ResetDeployment', return_value=obj), \
                patch('sys.argv', ['reset', '--project-dir', '/opt/relay', '--redeploy', '--apply']), \
                patch.object(os, 'geteuid', return_value=0), \
                patch('builtins.open') as tty, redirect_stdout(io.StringIO()):
            tty.return_value.__enter__.return_value.readline.return_value = 'RESET\n'
            with self.assertRaisesRegex(reset.ResetError, '已取消'):
                reset.main()
        self.assertIsNone(obj.backup)
        self.assertTrue(self.p('/opt/relay/config.env').is_file())
        self.assertFalse(self.p('/root/surelay-backups/redeploy-handoff.json').exists())

    def test_redeploy_stops_on_unsupported_owned_firewall_shape_or_shared_certificate(self):
        self.rules.append("ufw route allow 9999/tcp comment 'SuRelay custom forwarded'")
        with self.assertRaisesRegex(reset.ResetError, '安全重放'):
            self.redeployer()
        self.rules.pop()
        self.certificate()
        self.put('/etc/nginx/sites-enabled/other.conf', 'ssl_certificate /etc/letsencrypt/live/edge.example.test/cert.pem;\n')
        with self.assertRaisesRegex(reset.ResetError, '其他配置引用'):
            self.redeployer()
        self.assertTrue(self.p('/opt/relay/config.env').is_file())


def shlex_join_ufw(args):
    return f"ufw {args[0]} {args[1]} comment '{args[3]}'"


if __name__ == '__main__':
    unittest.main()
