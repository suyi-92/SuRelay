"""Port selection and staged SSH migration without changing host services."""
from __future__ import annotations

import contextlib
import errno
import io
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import surelay_core as core
import surelay_security as security
import sshd_policy


class RandomPortTest(unittest.TestCase):
    def test_ipv4_only_host_can_select_ports_when_ipv6_bind_is_unavailable(self):
        original_socket = socket.socket
        def create(family, kind):
            if family == socket.AF_INET:
                return original_socket(family, kind)
            disabled = Mock()
            disabled.bind.side_effect = OSError(errno.EADDRNOTAVAIL, "IPv6 disabled")
            return disabled
        with patch.object(core.socket, "has_ipv6", True), \
             patch.object(core.socket, "socket", side_effect=create):
            selected = core.random_high_ports(3)
        self.assertEqual(len(set(selected)), 3)

    def test_ports_are_unique_high_and_avoid_reserved_and_bound_transports(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp, \
             socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            available = core.random_high_ports(2)
            tcp.bind(("0.0.0.0", available[0]))
            udp.bind(("0.0.0.0", available[1]))
            occupied = {tcp.getsockname()[1], udp.getsockname()[1]}
            # Make occupied ports the first candidates, proving both transports
            # are probed rather than relying on chance in a large port range.
            def order(candidates):
                candidates.sort(key=lambda value: (value not in occupied, value))
            with patch.object(core.secrets.SystemRandom, "shuffle", side_effect=order):
                chosen = core.random_high_ports(12, reserved=(49152, "49153"))
        self.assertEqual(len(chosen), len(set(chosen)))
        self.assertTrue(all(49152 <= value <= 65535 for value in chosen))
        self.assertFalse(set(chosen).intersection(occupied | {49152, 49153}))
        self.assertEqual(core.random_high_ports(0), [])

    def test_udp_listener_uses_kernel_udp_state_and_exact_port(self):
        text = "UNCONN 0 0 0.0.0.0:54443 0.0.0.0:*\n"
        with patch.object(core, "run", return_value=subprocess.CompletedProcess([], 0, text)) as run:
            self.assertTrue(core.udp_listener(54443, socket.AF_INET))
            self.assertFalse(core.udp_listener(4443, socket.AF_INET))
        self.assertIn("-lun", run.call_args.args[0])


class StagedSshPortTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.ssh = self.root / "sshd_config"
        self.ssh.write_text("Port 22\nPasswordAuthentication no\nAuthenticationMethods publickey\n")
        self.manager = Mock()
        self.manager.env = core.GEN.defaults({"SSH_PORT": "52322", "SERVER_IP_IPV4": "192.0.2.1"})
        self.manager.config = self.root / "config.env"
        self.manager.config.write_text("SSH_PORT=52322\n")
        self.manager.rows.return_value = []
        self.manager.alpine.return_value = True
        self.manager.active.return_value = False
        self.sec = security.Security(self.manager)
        self.sec.ssh_config = self.ssh
        self.sec.socket_conf = self.root / "socket.conf"
        def effective():
            return "\n".join(line.lower() for line in self.ssh.read_text().splitlines()
                             if line.lower().startswith("port "))
        self.sec.effective = effective
        self.sec.verify_ssh_listener = Mock()
        self.sec.set_ssh_ports = lambda ports, active: core.atomic(
            self.ssh, security.ssh_block(self.ssh.read_text(), {"Port": ports}))
        self.sec._cleanup_owned_ssh_rules = Mock()
        snapshot = core.Snapshot
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(security, "Snapshot", side_effect=lambda paths: snapshot(
            [path for path in paths if Path(path).is_relative_to(self.root)], self.root / "backups")))
        self.stack.enter_context(patch.object(security.shutil, "which", return_value="fixture"))
        self.run = self.stack.enter_context(patch.object(security, "run", return_value=""))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def test_stage_keeps_old_listener_and_authentication_until_final_confirmation(self):
        confirm = Mock()
        self.sec.change_ssh_port("52322", confirm, stage=True)
        self.assertEqual(set(self.sec.effective().splitlines()), {"port 22", "port 52322"})
        self.assertIn("PasswordAuthentication no", self.ssh.read_text())
        self.assertIn("AuthenticationMethods publickey", self.ssh.read_text())
        confirm.assert_not_called()
        self.manager.update.assert_not_called()
        self.sec._cleanup_owned_ssh_rules.assert_not_called()
        self.sec.change_ssh_port("52322", confirm)
        confirm.assert_called_once_with("52322")
        self.assertEqual(self.sec.effective(), "port 52322")
        self.sec._cleanup_owned_ssh_rules.assert_called_once()

    def test_disabled_ufw_is_not_changed_during_stage_final_or_existing_port_sync(self):
        self.manager.env["ENABLE_UFW"] = "false"
        self.sec.change_ssh_port("52322", Mock(), stage=True)
        self.sec.change_ssh_port("52322", Mock())
        self.sec._sync_existing_ssh_port("52322", "22")
        security.Security._cleanup_owned_ssh_rules(self.sec, ["22"], "52322")
        self.assertFalse(any(call.args[0][0] == "ufw" for call in self.run.call_args_list))

    def test_socket_activation_preserves_port_absent_from_sshd_until_confirmation(self):
        self.manager.alpine.return_value = False
        self.manager.active.return_value = True
        self.manager.env["ENABLE_FAIL2BAN"] = "false"
        self.sec.set_ssh_ports = lambda ports, active: security.Security.set_ssh_ports(self.sec, ports, active)
        self.sec.ssh_service = Mock(return_value="ssh")
        def command(args, **kwargs):
            if args[:3] == ["systemctl", "show", "ssh.socket"]:
                return "0.0.0.0:50396 (Stream) [::]:50396 (Stream)"
            return ""
        self.run.side_effect = command
        confirm = Mock()
        self.sec.change_ssh_port("52322", confirm, stage=True)
        staged = self.sec.socket_conf.read_text()
        self.assertIn("ListenStream=50396\n", staged)
        self.assertIn("ListenStream=22\n", staged)
        self.assertIn("ListenStream=52322\n", staged)
        confirm.assert_not_called()
        self.sec.change_ssh_port("52322", confirm)
        confirm.assert_called_once_with("52322")
        self.assertEqual(self.sec.socket_conf.read_text(), "[Socket]\nListenStream=\nListenStream=52322\n")

    def test_unrecognized_socket_metadata_stops_before_mutation(self):
        self.manager.alpine.return_value = False
        self.manager.active.return_value = True
        original = self.ssh.read_bytes()
        for metadata in ("", "/run/ssh.sock (Stream)", "[::]:50396 (Datagram)"):
            with self.subTest(metadata=metadata):
                self.run.return_value = metadata
                with self.assertRaises(core.Error):
                    self.sec.change_ssh_port("52322", Mock(), stage=True)
                self.assertEqual(self.ssh.read_bytes(), original)
                self.assertFalse(self.sec.socket_conf.exists())
                self.manager.service.assert_not_called()

    def test_hy2_and_static_page_ports_are_protected_from_ssh_reuse(self):
        self.manager.env.update({"ENABLE_HY2": "true", "ENABLE_CUSTOM_DOMAIN": "true",
                                 "CUSTOM_DOMAIN": "example.com", "HY2_PORT": "54443"})
        for port in ("54443", self.manager.env["NGINX_FALLBACK_PORT"]):
            with self.subTest(port=port), self.assertRaises(core.Error):
                self.sec.change_ssh_port(port, Mock())

    def test_port_update_does_not_promote_old_password_override_above_final_policy(self):
        old_settings = security.ssh_block("# provider\n", {
            "Port": ["22"], "PasswordAuthentication": ["yes"], "AuthenticationMethods": ["any"]})
        final = b"PermitRootLogin prohibit-password\nPubkeyAuthentication yes\nPasswordAuthentication no\nAuthenticationMethods publickey\n"
        self.ssh.write_bytes(sshd_policy.apply_policy(old_settings.encode(), final))
        self.sec.reload_ssh = Mock()
        # Exercise the production writer, bypassing the test's service stub.
        security.Security.set_ssh_ports(self.sec, ["52322"], False)
        updated = self.ssh.read_text()
        self.assertTrue(updated.startswith(sshd_policy.BEGIN.decode()))
        self.assertNotIn("PasswordAuthentication yes", updated)
        self.assertNotIn("AuthenticationMethods any", updated)
        self.assertIn("Port 52322", updated)


if __name__ == "__main__":
    unittest.main()
