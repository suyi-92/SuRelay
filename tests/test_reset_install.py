from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
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
        self.active = {'sing-box', 'reality-relay-subscription', 'nginx', 'fail2ban'}
        self.rules = ["ufw allow 22/tcp comment 'reality-relay-bootstrap SSH current port'",
                      "ufw allow 443/tcp comment 'reality-relay-bootstrap sing-box VLESS+Reality 443'",
                      "ufw allow 51040/tcp comment 'reality-relay-bootstrap subscription 51040'",
                      "ufw allow 8080/tcp comment 'unrelated application'"]
        self.put('/etc/os-release', 'ID=debian\n')
        self.put('/opt/relay/config.env', 'SERVER_ALIAS=old\nSERVER_IP=192.0.2.10\n')
        self.put('/opt/relay/home-proxies.csv', 'old-list\n')
        self.put('/opt/relay/upstream-nodes.txt', 'old-upstream\n')
        self.put('/opt/relay/README.md', 'source code must survive\n')
        self.put('/etc/reality-relay-bootstrap/vless-uuid.txt', 'old-identity\n')
        self.put('/etc/reality-relay-bootstrap/reality-private.key', 'fake-test-private\n')
        self.put('/etc/reality-relay-bootstrap/subscription/all/clashmeta.yaml', 'old-subscription\n')
        self.put('/etc/reality-relay-bootstrap/user-note.txt', 'keep non-project data\n')
        self.put('/etc/sing-box/config.json', '{"inbounds": []}\n')
        self.put('/etc/systemd/system/reality-relay-subscription.service', 'Description=Reality Relay Bootstrap subscription file server\n')
        self.put('/etc/nginx/sites-available/reality-relay-bootstrap.conf', '# Managed by reality-relay-bootstrap.\n')
        self.put('/etc/nginx/sites-enabled/other.conf', 'other site\n')
        (self.p('/etc/nginx/sites-enabled/reality-relay-bootstrap.conf')).symlink_to('../sites-available/reality-relay-bootstrap.conf')
        self.put('/etc/fail2ban/jail.d/sshd.local', '# Managed by reality-relay-bootstrap.\n')
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
            rule = shlex_join_ufw(args[3:])
            self.rules.remove(rule)
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
                      '/etc/reality-relay-bootstrap/user-note.txt'):
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
        for name in ('sing-box', 'reality-relay-subscription'):
            self.put(f'/etc/init.d/{name}', '# Managed by reality-relay-bootstrap.\n')
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
        self.assertFalse(self.p('/root/reality-relay-bootstrap-backups').exists())
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
                        'REALITY_PRIVATE_KEY_PATH=/etc/reality-relay-bootstrap/../passwd'):
            with self.subTest(setting=setting):
                self.put('/opt/relay/config.env', setting + '\n')
                with self.assertRaises(reset.ResetError):
                    self.resetter()
                self.assertTrue(self.p('/etc/sing-box/config.json').exists())
        self.assertFalse(self.calls)

    def test_symlink_state_parent_is_rejected(self):
        self.p('/etc/reality-relay-bootstrap').rename(self.p('/etc/other-data'))
        self.p('/etc/reality-relay-bootstrap').symlink_to('other-data')
        with self.assertRaisesRegex(reset.ResetError, '符号链接'):
            self.resetter()
        self.assertTrue(self.p('/etc/other-data/vless-uuid.txt').exists())

    def test_unowned_nginx_configuration_stops_reset(self):
        self.put('/etc/nginx/sites-available/reality-relay-bootstrap.conf', '# manually owned\n')
        with self.assertRaisesRegex(reset.ResetError, '归属标记'):
            self.resetter()
        self.assertTrue(self.p('/opt/relay/config.env').exists())

    def certificate(self):
        self.put('/etc/reality-relay-bootstrap/cloudflare.ini', 'dns_cloudflare_api_token = fixture\n')
        self.put('/etc/letsencrypt/renewal/edge.example.test.conf',
                 'version = 4.0.0\n[renewalparams]\ndns_cloudflare_credentials = /etc/reality-relay-bootstrap/cloudflare.ini\n')
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
        self.assertEqual((project / 'phases').read_text().splitlines(), ['preflight', 'keys'])
        self.assertIn('RRB_RESET_INSTALL=true', result.stderr)


def shlex_join_ufw(args):
    return f"ufw {args[0]} {args[1]} comment '{args[3]}'"


if __name__ == '__main__':
    unittest.main()
