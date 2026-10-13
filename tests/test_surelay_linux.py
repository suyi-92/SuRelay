"""Real OpenSSH parser/key integration in an isolated filesystem (no SSH daemon)."""
import os
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import surelay_core as core
import surelay_security as security


@unittest.skipUnless(os.name == "posix" and shutil.which("sing-box"), "Requires sing-box binary")
class CoreIntegrationTest(unittest.TestCase):
    def test_generated_ipv4_and_dual_stack_configs_pass_real_core_check(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pair = subprocess.run(["sing-box", "generate", "reality-keypair"], text=True, capture_output=True, check=True).stdout
            private = next(line.split(": ", 1)[1] for line in pair.splitlines() if line.lower().startswith("private"))
            env = core.GEN.defaults({"SERVER_IP_IPV4": "192.0.2.1", "SERVER_ALIAS": "fixture"})
            for enabled in ("false", "true"):
                env.update({"ENABLE_IPV6_LISTEN": enabled, "SERVER_IP_IPV6": "2001:db8::1" if enabled == "true" else ""})
                config = core.GEN.build_config(env, [], "11111111-2222-4333-8444-555555555555", private, "abcd")
                path = root / "config.json"
                path.write_text(json.dumps(config))
                result = subprocess.run(["sing-box", "check", "-c", str(path)], text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)


@unittest.skipUnless(os.name == "posix" and Path("/usr/sbin/sshd").exists() and Path("/run/sshd").exists(),
                     "Requires isolated Linux OpenSSH fixture")
class SSHIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.key = self.root / "host"
        subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(self.key)], capture_output=True, check=True)
        self.config = self.root / "sshd_config"
        self.config.write_text(f"HostKey {self.key}\nPort 22221\nPasswordAuthentication no\n"
                               "PermitRootLogin prohibit-password\nAuthorizedKeysFile .ssh/authorized_keys\n")
        self.wrapper = self.root / "sshd"
        self.wrapper.write_text(f'#!/bin/sh\nexec /usr/sbin/sshd -f "{self.config}" "$@"\n')
        self.wrapper.chmod(0o700)
        self.manager = Mock()
        self.manager.alpine.return_value = True
        self.manager.config = self.root / "config.env"
        self.manager.config.write_text('SSH_PORT=22221\n')
        self.manager.env = {"DIRECT_PORT": "443", "SUBSCRIPTION_LISTEN_PORT": "51040", "ENABLE_FAIL2BAN": "false"}
        self.manager.rows.return_value = []
        self.sec = security.Security(self.manager)
        self.sec.ssh_config = self.config
        self.sec.socket_conf = self.root / "socket.conf"
        self.sec.sshd = lambda: str(self.wrapper)
        self.snapshot_type = core.Snapshot
        self.snap = patch.object(security, "Snapshot", side_effect=lambda paths:
                                self.snapshot_type([p for p in paths if Path(p).is_relative_to(self.root)], self.root / "backups"))
        self.snap.start()
        self.addCleanup(self.snap.stop)

    def test_password_toggle_is_valid_and_effective(self):
        self.sec.password(True, lambda: None)
        self.assertIn("passwordauthentication yes", self.sec.effective())
        self.assertIn("permitrootlogin yes", self.sec.effective())
        with patch.object(self.sec, "keys", return_value=(None, ["key"], [0])):
            self.sec.password(False, lambda: None)
        self.assertIn("passwordauthentication no", self.sec.effective())
        self.assertIn("authenticationmethods publickey", self.sec.effective())

    def test_failed_reload_restores_exact_config(self):
        original = self.config.read_bytes()
        self.manager.service.side_effect = [core.Error("reload failure"), None]
        with self.assertRaises(core.Error):
            self.sec.password(True, lambda: None)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertIn("passwordauthentication no", self.sec.effective())

    def test_port_migration_cancel_restores_includes(self):
        include = self.root / "provider.conf"
        include.write_text("Port 22222\n")
        self.config.write_text(self.config.read_text() + f"Include {include}\n")
        original = self.config.read_bytes(), include.read_bytes()
        actual_run = security.run
        def execute(args, **kwargs):
            if args[0] == "ss":
                return ""
            return actual_run(args, **kwargs)
        def cancel(new):
            effective = self.sec.effective()
            self.assertIn("port 22221", effective)
            self.assertIn("port 22222", effective)
            self.assertIn("port 22223", effective)
            raise core.Error("cancel")
        with patch.object(security, "run", side_effect=execute), \
             patch.object(security.shutil, "which", return_value=None), \
             patch.object(self.sec, "verify_ssh_listener"):
            with self.assertRaisesRegex(core.Error, "cancel"):
                self.sec.change_ssh_port("22223", cancel)
        self.assertEqual((self.config.read_bytes(), include.read_bytes()), original)
        self.manager.update.assert_not_called()

    def test_port_migration_commits_after_confirmation(self):
        actual_run = security.run
        def execute(args, **kwargs):
            if args[0] == "ss":
                return ""
            return actual_run(args, **kwargs)
        with patch.object(security, "run", side_effect=execute), \
             patch.object(security.shutil, "which", return_value=None), \
             patch.object(self.sec, "verify_ssh_listener"):
            self.sec.change_ssh_port("22223", lambda new: self.assertIn("port 22221", self.sec.effective()))
        effective = self.sec.effective()
        self.assertIn("port 22223", effective)
        self.assertNotIn("port 22221", effective)
        self.manager.update.assert_called_once_with({"SSH_PORT": "22223"})

    def test_real_key_fingerprints_and_last_key_protection(self):
        authorized = self.root / "authorized_keys"
        self.sec.key_path = lambda: authorized
        key = self.key.with_suffix(".pub").read_text().strip()
        self.assertIn("ED25519", self.sec.validate_key(key))
        with self.assertRaises(core.Error):
            self.sec.validate_key("ssh-ed25519 invalid")
        self.sec.write_keys([key])
        self.assertEqual(authorized.read_text(), key + "\n")
        with self.assertRaises(core.Error):
            self.sec.write_keys([])


if __name__ == "__main__":
    unittest.main()
