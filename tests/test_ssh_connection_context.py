"""SSH effective-policy queries and resumed key paths use complete connection contexts."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import surelay_core as core
import surelay_redeploy as redeploy
import surelay_security as security


class SshConnectionContextTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = Mock(root=self.root / "project")
        self.manager.env = core.GEN.defaults({"SSH_PORT": "52322", "RRB_STATE_DIR": str(self.root / "state")})
        self.sec = security.Security(self.manager)
        self.sec.sshd = Mock(return_value="fixture-sshd")

    def test_policy_uses_current_connection_and_can_check_target_port_explicitly(self):
        connection = "2001:db8::1 45123 2001:db8::2 50396"
        with patch.dict(os.environ, SSH_CONNECTION=connection), patch.object(security, "run", return_value="") as run:
            self.sec.effective()
            self.assertEqual(run.call_args.args[0][-1],
                             "user=root,host=localhost,addr=2001:db8::1,laddr=2001:db8::2,lport=50396")
            self.sec.effective(local_port="52322")
            self.assertEqual(run.call_args.args[0][-1],
                             "user=root,host=localhost,addr=2001:db8::1,laddr=2001:db8::2,lport=52322")

    def test_absent_or_invalid_connection_falls_back_to_configured_port(self):
        for connection in ("", " ", "192.0.2.1", "192.0.2.1 invalid 192.0.2.2 50396",
                           "192.0.2.1 45000 192.0.2.2 0", "invalid 45000 192.0.2.2 50396"):
            with self.subTest(connection=connection), patch.dict(os.environ, SSH_CONNECTION=connection), \
                    patch.object(security, "run", return_value="") as run:
                self.sec.effective()
                self.assertEqual(run.call_args.args[0][-1],
                                 "user=root,host=localhost,addr=127.0.0.1,laddr=127.0.0.1,lport=52322")

    @unittest.skipUnless(os.name == "posix", "Requires the POSIX root account")
    def test_resumed_new_port_uses_validated_persistent_key_path_and_rejects_custom_paths(self):
        persistent = self.root / ".ssh/authorized_keys"
        handoff = {"format": 1, "mode": "redeploy", "status": "removed", "project": str(self.manager.root),
                   "old_config": {}, "old_authorized_keys": [], "old_managed_keys": [],
                   "authorized_keys_path": str(persistent), "pending_firewall": [], "ssh_ports": [50396]}
        record = self.root / "handoff.json"
        record.write_text(json.dumps(handoff))
        record.chmod(0o600)
        handoff = redeploy.read_handoff(record, self.manager.root)
        verification = self.sec.m.env["RRB_STATE_DIR"] + "/redeploy-authorized-keys"
        with patch("pwd.getpwnam", return_value=SimpleNamespace(pw_dir=str(self.root))), \
                patch.object(self.sec, "effective", return_value=f"authorizedkeysfile {verification}"):
            self.assertEqual(self.sec.key_path(redeploy_handoff=handoff), persistent)
            with self.assertRaises(core.Error):
                self.sec.key_path()
            for change in ({"status": "finalized"}, {"project": str(self.root / "other")},
                           {"authorized_keys_path": str(self.root / "arbitrary")}):
                with self.subTest(change=change), self.assertRaises(core.Error):
                    self.sec.key_path(redeploy_handoff={**handoff, **change})
            self.sec.effective.return_value = "authorizedkeysfile /srv/custom-authorized-keys"
            with self.assertRaises(core.Error):
                self.sec.key_path(redeploy_handoff=handoff)

    @unittest.skipUnless(os.name == "posix" and shutil.which("sshd") and shutil.which("ssh-keygen"),
                         "Requires real OpenSSH tools")
    def test_real_sshd_matches_current_and_target_local_ports(self):
        host_key = self.root / "host-key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(host_key)], check=True)
        persistent = self.root / ".ssh/authorized_keys"
        verification = Path(self.manager.env["RRB_STATE_DIR"]) / "redeploy-authorized-keys"
        config = self.root / "sshd_config"
        config.write_text(f"HostKey {host_key}\nPort 50396\nPort 52322\nPasswordAuthentication yes\n"
                          f"AuthorizedKeysFile {persistent}\nMatch LocalPort 52322\n"
                          f"    PasswordAuthentication no\n    AuthorizedKeysFile {verification}\nMatch all\n")
        binary = shutil.which("sshd")
        wrapper = self.root / "sshd"
        wrapper.write_text(f"#!/bin/sh\nexec {binary} -f '{config}' \"$@\"\n")
        wrapper.chmod(0o755)
        self.sec.sshd.return_value = str(wrapper)
        with patch.dict(os.environ, SSH_CONNECTION="192.0.2.1 45123 192.0.2.2 50396"):
            self.assertIn("passwordauthentication yes", self.sec.effective().splitlines())
            self.assertIn("passwordauthentication no", self.sec.effective(local_port="52322").splitlines())
        handoff = {"format": 1, "mode": "redeploy", "status": "removed", "project": str(self.manager.root),
                   "old_config": {"SSH_PORT": "50396"}, "old_authorized_keys": [], "old_managed_keys": [],
                   "authorized_keys_path": str(persistent), "pending_firewall": [], "ssh_ports": [50396]}
        record = self.root / "handoff.json"
        record.write_text(json.dumps(handoff))
        record.chmod(0o600)
        self.manager.env["ADMIN_PUBKEY"] = host_key.with_suffix(".pub").read_text().strip()
        with patch.dict(os.environ, SSH_CONNECTION="192.0.2.1 45123 192.0.2.2 52322"), \
                patch("pwd.getpwnam", return_value=SimpleNamespace(pw_dir=str(self.root))):
            self.assertIn(f"authorizedkeysfile {verification}", self.sec.effective().splitlines())
            operation = redeploy.Redeployer(self.manager, record, security=self.sec, backup_root=self.root / "backups")
            operation.stage_keys()
            self.assertEqual(persistent.read_text(), verification.read_text())
            self.assertEqual(persistent.read_text().strip(), self.manager.env["ADMIN_PUBKEY"])


if __name__ == "__main__":
    unittest.main()
