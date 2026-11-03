"""Fresh deployment SSH handoff; every system path/service is isolated or mocked."""
from __future__ import annotations

import base64
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import surelay_core as core
import surelay_redeploy as redeploy


def key(name):
    return "ssh-ed25519 " + base64.b64encode(name.encode()).decode() + " " + name


class HandoffTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.keys = self.root / "authorized_keys"
        self.old, self.manual, self.new = key("old-surelay"), key("manual"), key("fresh-surelay")
        self.keys.write_text(self.old + "\n" + self.manual + "\n")
        self.config = self.project / "config.env"
        self.config.write_text("# fresh config\n")
        self.ssh = self.root / "sshd_config"
        self.provider = b"# provider preserved\n# SuRelay saved port: Port 22\n"
        self.ssh.write_bytes(b"# BEGIN surelay SSH policy\nPermitRootLogin prohibit-password\n"
                             b"PasswordAuthentication no\nAuthenticationMethods publickey\n# END surelay SSH policy\n"
                             b"# BEGIN SuRelay SSH settings\nPort 50396\nClientAliveInterval 99\n"
                             b"# END SuRelay SSH settings\n" + self.provider)
        self.handoff = self.root / "handoff.json"
        self.data = {"format": 1, "mode": "redeploy", "project": str(self.project), "status": "removed",
                     "backup_dir": str(self.root / "reset-backup"), "ssh_ports": [50396], "current_ssh_port": 50396,
                     "old_authorized_keys": [self.old, self.manual], "authorized_keys_path": str(self.keys),
                     "old_config": {"SSH_PORT": "50396", "ADMIN_PUBKEY": self.old}, "old_managed_keys": [],
                     "pending_firewall": [["ufw", "--force", "delete", "allow", "50396/tcp", "comment", "surelay SSH current port"]]}
        self.save_handoff()
        self.manager = Mock()
        self.manager.root, self.manager.config = self.project, self.config
        self.manager.env = core.GEN.defaults({"RRB_STATE_DIR": str(self.root / "state"), "SSH_PORT": "52322",
                                              "ADMIN_PUBKEY": self.new, "SERVER_IP_IPV4": "192.0.2.1"})
        self.manager.alpine.return_value = True
        self.manager.active.return_value = False
        self.sec = Mock()
        self.sec.key_path.return_value = self.keys
        self.sec.ssh_config = self.ssh
        self.sec.socket_conf = self.root / "sockets" / "surelay.conf"
        self.sec.sshd.return_value = "fixture-sshd"
        self.sec.effective.return_value = "port 52322\npasswordauthentication no\nauthenticationmethods publickey\npermitrootlogin prohibit-password"
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.run = self.stack.enter_context(patch.object(redeploy, "run", return_value=""))
        snapshot = core.Snapshot
        self.stack.enter_context(patch.object(redeploy, "Snapshot", side_effect=lambda paths, _root=None: snapshot(
            [path for path in paths if Path(path).is_relative_to(self.root)], self.root / "backups")))
        self.output = io.StringIO()
        self.stack.enter_context(contextlib.redirect_stdout(self.output))

    def save_handoff(self):
        self.handoff.write_text(json.dumps(self.data))
        self.handoff.chmod(0o600)

    def operation(self):
        return redeploy.Redeployer(self.manager, self.handoff, security=self.sec)

    def test_stage_keeps_old_and_manual_keys_and_registers_only_added_key(self):
        self.operation().stage_keys()
        self.assertEqual(self.keys.read_text().splitlines(), [self.old, self.manual, self.new])
        self.assertEqual(redeploy.read_ledger(redeploy.ledger_path(self.manager)), {redeploy.key_identity(self.new)})
        self.assertIn("1 把无法归属", self.output.getvalue())
        self.sec.reload_ssh.assert_not_called()

    def test_final_clears_old_owned_identity_and_settings_but_keeps_manual_and_provider(self):
        self.operation().stage_keys()
        self.operation().finalize()
        self.assertEqual(self.keys.read_text().splitlines(), [self.manual, self.new])
        text = self.ssh.read_bytes()
        self.assertTrue(text.endswith(self.provider))
        self.assertNotIn(b"ClientAliveInterval 99", text)
        self.assertNotIn(b"Port 50396", text)
        self.assertIn(b"Port 52322", text)
        self.assertEqual(json.loads(self.handoff.read_text())["status"], "finalized")
        self.assertIn(self.data["pending_firewall"][0], [call.args[0] for call in self.run.call_args_list])

    def test_cleanup_failure_restores_both_keys_and_handoff_for_retry(self):
        def command(args, **kwargs):
            if args[:3] == ["ufw", "--force", "delete"]:
                raise core.Error("fixture firewall failure")
            return ""
        self.run.side_effect = command
        for selected in (self.new, self.old):
            with self.subTest(selected=selected):
                self.manager.env["ADMIN_PUBKEY"] = selected
                self.operation().stage_keys()
                keys_before, ssh_before = self.keys.read_bytes(), self.ssh.read_bytes()
                verification_before = self.operation().verification_keys().read_bytes()
                with self.assertRaises(core.Error):
                    self.operation().finalize()
                self.assertEqual(self.keys.read_bytes(), keys_before)
                self.assertEqual(self.ssh.read_bytes(), ssh_before)
                self.assertEqual(self.operation().verification_keys().read_bytes(), verification_before)
                self.assertEqual(json.loads(self.handoff.read_text())["status"], "removed")
        self.assertIn(["ufw", "reload"], [call.args[0] for call in self.run.call_args_list])

    def test_reused_owned_key_is_staged_without_duplicates_and_survives_finalize(self):
        original = self.keys.read_bytes()
        self.manager.env["ADMIN_PUBKEY"] = self.old
        self.operation().stage_keys()
        self.assertEqual(self.keys.read_bytes(), original)
        self.assertEqual(self.operation().verification_keys().read_text().splitlines(), [self.old])
        self.operation().finalize()
        self.assertEqual(self.keys.read_text().splitlines(), [self.manual, self.old])
        self.assertEqual(redeploy.read_ledger(redeploy.ledger_path(self.manager)), {redeploy.key_identity(self.old)})

    def test_reused_manual_key_survives_while_unselected_owned_key_is_removed(self):
        self.manager.env["ADMIN_PUBKEY"] = self.manual
        self.operation().stage_keys()
        self.operation().finalize()
        self.assertEqual(self.keys.read_text().splitlines(), [self.manual])
        self.assertEqual(redeploy.read_ledger(redeploy.ledger_path(self.manager)), {redeploy.key_identity(self.manual)})

    def test_multiline_selection_keeps_both_new_and_reused_owned_keys(self):
        self.config.write_text(core.env_text("", {"ADMIN_PUBKEY": self.new + "\n" + self.old,
                                                  "SSH_PORT": "52322", "RRB_STATE_DIR": str(self.root / "state")}))
        self.manager.env = core.GEN.defaults(core.GEN.parse_env_file(self.config))
        self.operation().stage_keys()
        self.assertEqual(self.operation().verification_keys().read_text().splitlines(), [self.new, self.old])
        self.operation().finalize()
        self.assertEqual(self.keys.read_text().splitlines(), [self.manual, self.new, self.old])
        self.assertEqual(redeploy.read_ledger(redeploy.ledger_path(self.manager)),
                         {redeploy.key_identity(self.new), redeploy.key_identity(self.old)})

    def test_reused_restricted_key_final_line_matches_explicit_verification_input(self):
        restricted = 'from="192.0.2.0/24",no-agent-forwarding ' + self.old
        selected = redeploy.key_identity(self.old) + " selected-again"
        self.keys.write_text(restricted + "\n" + self.manual + "\n")
        self.data["old_authorized_keys"] = [restricted, self.manual]
        self.save_handoff()
        self.manager.env["ADMIN_PUBKEY"] = selected
        before = self.keys.read_bytes()
        self.operation().stage_keys()
        self.assertEqual(self.keys.read_bytes(), before)
        self.assertEqual(self.operation().verification_keys().read_text().splitlines(), [selected])
        self.operation().finalize()
        self.assertEqual(self.keys.read_text().splitlines(), [self.manual, selected])

    def test_empty_selection_and_reused_port_are_rejected_before_writes(self):
        original = self.keys.read_bytes()
        for selected, selected_port in (("", "52322"), (self.old, "50396")):
            with self.subTest(selected=selected, port=selected_port):
                self.manager.env.update({"ADMIN_PUBKEY": selected, "SSH_PORT": selected_port})
                with self.assertRaises(core.Error):
                    self.operation().stage_keys()
        self.assertEqual(self.keys.read_bytes(), original)
        self.assertFalse(self.operation().verification_keys().exists())

    def test_new_port_verification_uses_only_explicitly_selected_existing_key(self):
        self.manager.env["ADMIN_PUBKEY"] = self.old
        self.operation().stage_keys()
        keyfile = self.operation().verification_keys()
        self.run.return_value = (f"authorizedkeysfile {keyfile}\npubkeyauthentication yes\n"
                                 "passwordauthentication no\nauthenticationmethods publickey\n")
        self.operation().restrict_new_port()
        self.assertEqual(keyfile.read_text().splitlines(), [self.old])
        self.assertIn(f"Match User root LocalPort 52322\n    AuthorizedKeysFile {keyfile}", self.ssh.read_text())
        self.assertIn("Port 50396", self.ssh.read_text())
        self.sec.verify_ssh_listener.assert_called_with("52322")

    def test_finalized_handoff_cannot_stage_or_clean_selected_existing_key_again(self):
        self.manager.env["ADMIN_PUBKEY"] = self.old
        self.operation().stage_keys()
        self.operation().finalize()
        before = self.keys.read_bytes()
        for action in ("stage_keys", "finalize"):
            with self.subTest(action=action), self.assertRaisesRegex(core.Error, "已完成"):
                getattr(self.operation(), action)()
        self.assertEqual(self.keys.read_bytes(), before)

    def test_socket_overrides_are_replaced_with_only_new_owned_listener(self):
        self.operation().stage_keys()
        self.manager.alpine.return_value = False
        self.manager.active.return_value = True
        self.sec.socket_conf.parent.mkdir()
        legacy = self.sec.socket_conf.with_name("reality-relay-bootstrap.conf")
        legacy.write_text("[Socket]\nListenStream=\nListenStream=50396\n")
        self.operation().finalize()
        self.assertFalse(legacy.exists())
        self.assertIn("ListenStream=52322", self.sec.socket_conf.read_text())
        self.assertNotIn("50396", self.sec.socket_conf.read_text())

    def test_resume_status_is_read_only_and_rejects_failed_or_wrong_project(self):
        before = self.handoff.read_bytes()
        self.assertEqual(redeploy.read_handoff(self.handoff, self.project)["status"], "removed")
        self.assertEqual(self.handoff.read_bytes(), before)
        for status in ("prepared", "failed", "unknown"):
            self.data["status"] = status
            self.save_handoff()
            with self.assertRaises(core.Error):
                redeploy.read_handoff(self.handoff, self.project)
        self.data["status"] = "removed"
        self.save_handoff()
        with self.assertRaises(core.Error):
            redeploy.read_handoff(self.handoff, self.root / "other-project")
        self.assertIsNone(redeploy.read_handoff(self.root / "missing", self.project, optional=True))

    def test_disabled_firewall_cannot_remove_old_rule_covering_new_ssh_port(self):
        self.operation().stage_keys()
        self.manager.env["ENABLE_UFW"] = "false"
        self.data["pending_firewall"] = [["ufw", "--force", "delete", "allow", "50000:60000/tcp", "comment", "SuRelay custom old-range"]]
        self.save_handoff()
        before = self.keys.read_bytes()
        with self.assertRaises(core.Error):
            self.operation().finalize()
        self.assertEqual(self.keys.read_bytes(), before)

    def test_pending_rules_support_all_reset_actions_and_unknown_ranges_are_conservative(self):
        for action in ("allow", "deny", "reject", "limit"):
            command = ["ufw", "--force", "delete", action, "52322/tcp", "comment", "SuRelay custom old"]
            self.assertEqual(redeploy.pending_rule(command), command)
        for value, expected in (("50000:53000/tcp", True), ("50000-53000/tcp", True),
                                ("22,52322/tcp", True), ("22,443/tcp", False), ("OpenSSH", True),
                                ("any", True), ("52322/udp", False)):
            command = ["ufw", "--force", "delete", "allow", value, "comment", "SuRelay custom old"]
            with self.subTest(value=value):
                self.assertEqual(redeploy.rule_covers_tcp(command, 52322), expected)

    @unittest.skipUnless(os.name == "posix", "Requires symlink and Unix uid metadata")
    def test_resume_rejects_symlink_parent_or_different_owner(self):
        link = self.root / "linked"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(core.Error):
            redeploy.read_handoff(link / self.handoff.name, self.project)
        with patch.object(redeploy.os, "geteuid", return_value=os.geteuid() + 1):
            with self.assertRaises(core.Error):
                redeploy.read_handoff(self.handoff, self.project)

    def test_resume_status_cli_does_not_require_configuration(self):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/surelay_redeploy.py"), "--resume-status",
                                 "--project-dir", str(self.project), "--handoff", str(self.handoff)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "removed")

    @unittest.skipUnless(os.name == "posix" and shutil.which("sshd") and shutil.which("ssh-keygen"), "Requires real OpenSSH tools")
    def test_finalized_configuration_passes_real_sshd_and_keeps_new_port_and_authentication(self):
        import sshd_policy
        host = self.root / "host-key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(host)], check=True)
        permissive = self.ssh.read_bytes().replace(b"PasswordAuthentication no", b"PasswordAuthentication yes").replace(
            b"AuthenticationMethods publickey", b"AuthenticationMethods any")
        self.ssh.write_bytes(permissive)
        self.ssh.write_bytes(f"HostKey {host}\nPidFile {self.root / 'sshd.pid'}\n".encode() + self.ssh.read_bytes())
        wrapper = self.root / "sshd"
        wrapper.write_text(f"#!/bin/sh\nexec {shutil.which('sshd')} -f '{self.ssh}' -h '{host}' \"$@\"\n")
        wrapper.chmod(0o755)
        self.sec.sshd.return_value = str(wrapper)
        def command(args, **kwargs):
            if args[0] == str(wrapper):
                return core.run(args, **kwargs)
            return ""
        self.run.side_effect = command
        self.sec.effective.side_effect = lambda: core.run([wrapper, "-T", "-C", "user=root,host=localhost,addr=127.0.0.1"], capture=True)
        self.operation().stage_keys()
        self.operation().restrict_new_port()
        new_context = core.run([wrapper, "-T", "-C", "user=root,host=localhost,addr=127.0.0.1,lport=52322"], capture=True)
        old_context = core.run([wrapper, "-T", "-C", "user=root,host=localhost,addr=127.0.0.1,lport=50396"], capture=True)
        verification = f"authorizedkeysfile {self.operation().verification_keys()}"
        self.assertIn(verification, new_context.splitlines())
        self.assertNotIn(verification, old_context.splitlines())
        self.assertIn("passwordauthentication no", new_context.splitlines())
        self.assertIn("authenticationmethods publickey", new_context.splitlines())
        self.assertIn("passwordauthentication yes", old_context.splitlines())
        final_policy = b"PermitRootLogin prohibit-password\nPubkeyAuthentication yes\nPasswordAuthentication no\nAuthenticationMethods publickey\n"
        self.ssh.write_bytes(sshd_policy.apply_policy(self.ssh.read_bytes(), final_policy))
        self.operation().finalize()
        effective = self.sec.effective().splitlines()
        self.assertIn("port 52322", effective)
        self.assertNotIn("port 50396", effective)
        self.assertIn("passwordauthentication no", effective)
        self.assertIn("authenticationmethods publickey", effective)
        self.assertEqual(self.keys.read_text().splitlines(), [self.manual, self.new])
        self.assertFalse(self.operation().verification_keys().exists())
        self.assertNotIn("redeploy SSH verification", self.ssh.read_text())


if __name__ == "__main__":
    unittest.main()
