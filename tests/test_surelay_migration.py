"""Legacy upgrade and uninstall regressions, confined to temporary filesystems."""
import contextlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import surelay_core as core
import sshd_policy
from tests import test_reset_install as fixtures
reset = fixtures.reset


@unittest.skipUnless(os.name == "posix" and shutil.which("git"), "Requires POSIX Git")
class MigrationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.legacy = self.root / "reality-relay-bootstrap"
        self.legacy.mkdir()
        self.target = self.root / "SuRelay"
        self.launcher = self.root / "bin/sr"
        subprocess.run(["git", "init", "-q", self.legacy], check=True)
        subprocess.run(["git", "-C", self.legacy, "remote", "add", "origin",
                        "https://github.com/suyi-92/reality-relay-bootstrap.git"], check=True)
        (self.legacy / "surelay.sh").write_text('printf "fixture\\n"\n')
        (self.legacy / "config.env").write_text(core.env_text("", {
            "SERVER_ALIAS": "existing", "SERVER_IP_IPV4": "192.0.2.1",
            "RRB_STATE_DIR": "/etc/reality-relay-bootstrap",
            "SUBSCRIPTION_DIR": "/etc/reality-relay-bootstrap/subscription",
            "CSV_PATH": str(self.legacy / "home-proxies.csv"), "CLOUDFLARE_API_TOKEN": "fixture-token",
        }))
        (self.legacy / "home-proxies.csv").write_bytes(b"keep exits exactly\n")
        (self.legacy / "local-note.txt").write_text("keep local edits")
        core.install_command(self.legacy, self.launcher)
        self.m = core.Manager(self.legacy)
        snapshot_type = core.Snapshot
        self.snapshot = patch.object(core, "Snapshot", side_effect=lambda paths:
                                     snapshot_type(paths, self.root / "backups"))
        self.snapshot.start()
        self.addCleanup(self.snapshot.stop)

    def migrate(self):
        self.m.migrate(self.target, self.legacy, self.launcher)

    def test_migration_preserves_runtime_paths_inputs_and_shell_entry(self):
        self.migrate()
        self.assertTrue(self.legacy.is_symlink())
        self.assertEqual(self.legacy.resolve(), self.target)
        self.assertEqual((self.target / "home-proxies.csv").read_bytes(), b"keep exits exactly\n")
        self.assertEqual((self.target / "local-note.txt").read_text(), "keep local edits")
        env = self.m.reload()
        self.assertEqual(env["CSV_PATH"], str(self.target / "home-proxies.csv"))
        self.assertEqual(env["VLESS_UUID_PATH"], "/etc/reality-relay-bootstrap/vless-uuid.txt")
        self.assertEqual(env["SUBSCRIPTION_DIR"], "/etc/reality-relay-bootstrap/subscription")
        self.assertEqual(env["CLOUDFLARE_API_TOKEN"], "fixture-token")
        self.assertEqual(core.run(["git", "-C", self.target, "remote", "get-url", "origin"], capture=True),
                         "https://github.com/suyi-92/SuRelay.git")
        self.assertEqual(core.run([self.launcher], capture=True), "fixture")
        self.migrate()  # Repeated update must not move or overwrite the checkout.
        self.assertEqual(core.run([self.launcher], capture=True), "fixture")

    def test_existing_destination_is_never_overwritten(self):
        self.target.mkdir()
        (self.target / "note").write_text("other deployment")
        with self.assertRaisesRegex(core.Error, "迁移目标已存在"):
            self.migrate()
        self.assertFalse(self.legacy.is_symlink())
        self.assertEqual((self.target / "note").read_text(), "other deployment")

    def test_launcher_failure_rolls_back_move_config_and_remote(self):
        config = self.m.config.read_bytes()
        launcher = self.launcher.read_bytes()
        with patch.object(core, "install_command", side_effect=OSError("write failed")):
            with self.assertRaises(OSError):
                self.migrate()
        self.assertFalse(self.target.exists())
        self.assertFalse(self.legacy.is_symlink())
        self.assertEqual(self.m.config.read_bytes(), config)
        self.assertEqual(self.launcher.read_bytes(), launcher)
        self.assertIn("reality-relay-bootstrap.git", core.run(
            ["git", "-C", self.legacy, "remote", "get-url", "origin"], capture=True))


@unittest.skipUnless(os.name == "posix", "Requires POSIX filesystem and terminal")
class LegacyUninstallTest(unittest.TestCase):
    setUp = fixtures.ResetInstallTest.setUp
    p = fixtures.ResetInstallTest.p
    put = fixtures.ResetInstallTest.put
    command = fixtures.ResetInstallTest.command
    resetter = fixtures.ResetInstallTest.resetter

    def test_uninstall_requires_its_own_confirmation_word(self):
        import pty
        for reply in (b'RESET\n', b'\n', b'UNINSTALL\n'):
            obj = reset.ResetDeployment(self.project, self.root, self.command, uninstall=True)
            master, slave = pty.openpty()
            try:
                os.write(master, reply)
                with patch.object(reset, 'ResetDeployment', return_value=obj), \
                     patch.object(sys, 'argv', ['reset', '--project-dir', str(self.project), '--uninstall', '--apply']), \
                     patch.object(os, 'geteuid', return_value=0), \
                     patch.dict(os.environ, {'RRB_INPUT_TTY': os.ttyname(slave)}), \
                     contextlib.redirect_stdout(io.StringIO()):
                    if reply == b'UNINSTALL\n':
                        reset.main()
                        self.assertFalse(self.p('/opt/relay/config.env').exists())
                    else:
                        with self.assertRaisesRegex(reset.ResetError, '已取消'):
                            reset.main()
                        self.assertTrue(self.p('/opt/relay/config.env').exists())
                        self.assertIsNone(obj.backup)
            finally:
                os.close(master)
                os.close(slave)

    def test_uninstall_removes_owned_launcher_without_reinstalling_or_touching_ssh(self):
        self.put('/usr/local/bin/sr', '#!/bin/sh\n# SuRelay managed command\nexec bash /opt/relay/surelay.sh "$@"\n')
        obj = reset.ResetDeployment(self.project, self.root, self.command, uninstall=True)
        obj.collect()
        original_keys = self.p('/root/.ssh/authorized_keys').read_bytes()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            obj.apply()
        self.assertFalse(self.p('/usr/local/bin/sr').exists())
        self.assertEqual(self.p('/root/.ssh/authorized_keys').read_bytes(), original_keys)
        self.assertTrue(self.p('/opt/relay/README.md').exists())
        self.assertNotIn('接下来重新填写', output.getvalue())

    def test_legacy_assets_and_rules_are_recognized(self):
        self.put('/opt/relay/config.env', 'RRB_STATE_DIR=/etc/reality-relay-bootstrap\nNGINX_FALLBACK_ROOT=/var/www/reality-fallback\n')
        self.put('/etc/reality-relay-bootstrap/vless-uuid.txt', 'legacy identity')
        self.put('/etc/systemd/system/reality-relay-subscription.service', 'Description=Reality Relay Bootstrap subscription file server\n')
        self.put('/etc/nginx/sites-available/reality-relay-bootstrap.conf', '# Managed by reality-relay-bootstrap.\n')
        self.put('/etc/fail2ban/jail.d/sshd.local', '# Managed by reality-relay-bootstrap.\n')
        self.active.add('reality-relay-subscription')
        self.rules.extend(["ufw allow 2222/tcp comment 'reality-relay-bootstrap SSH current port'",
                           "ufw allow 8443/tcp comment 'reality-relay-bootstrap sing-box VLESS+Reality 8443'"])
        obj = self.resetter()
        obj.apply()
        self.assertFalse(self.p('/etc/reality-relay-bootstrap/vless-uuid.txt').exists())
        self.assertNotIn('reality-relay-subscription', self.active)
        self.assertTrue(any('2222/tcp' in rule for rule in self.rules))
        self.assertFalse(any('8443/tcp' in rule for rule in self.rules))


class CompatibilityTest(unittest.TestCase):
    def test_ssh_policy_upgrade_replaces_legacy_block_without_duplicates(self):
        old = b'# BEGIN reality-relay-bootstrap SSH policy\nPasswordAuthentication yes\n# END reality-relay-bootstrap SSH policy\nPort 22\n'
        changed = sshd_policy.apply_policy(old, b'PasswordAuthentication no\n')
        self.assertNotIn(b'PasswordAuthentication yes', changed)
        self.assertEqual(changed.count(sshd_policy.BEGIN), 1)
        self.assertEqual(sshd_policy.remove_policy(changed), b'Port 22\n')

if __name__ == '__main__':
    unittest.main()
