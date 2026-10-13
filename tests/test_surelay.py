"""Management regression tests. No live host service/firewall changes."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import surelay_core as core
import surelay_security as security
import surelay


class ManagementTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config.env"
        self.state = self.root / "state"
        self.state.mkdir()
        self.config.write_text(core.env_text("", {
            "SERVER_ALIAS": "fixture", "SERVER_IP_IPV4": "192.0.2.1",
            "SINGBOX_CONFIG_PATH": str(self.root / "sing-box.json"),
            "RRB_STATE_DIR": str(self.state), "ENABLE_UFW": "false",
        }))
        (self.root / "home-proxies.csv").write_text("tag,type,server,server_port,username,password,network,listen_port\n")
        (self.root / "upstream-nodes.txt").write_text("tag,node_url,listen_port\n")
        for name, value in (("vless-uuid.txt", "11111111-2222-4333-8444-555555555555"),
                            ("reality-private.key", "a" * 43), ("reality-public.key", "b" * 43),
                            ("reality-short-id.txt", "abcd")):
            (self.state / name).write_text(value)
        (self.root / "sing-box.json").write_text('{"original":true}')
        self.m = core.Manager(self.root)

    def local_snapshot(self, paths):
        # Production snapshot paths are confined to the fixture for this test.
        selected = [p for p in paths if Path(p).is_relative_to(self.root)]
        return self.snapshot_class(selected, self.root / "backups")

    def mock_system(self):
        self.snapshot_class = core.Snapshot
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(core, "Snapshot", side_effect=self.local_snapshot))
        stack.enter_context(patch.object(core, "run", return_value=""))
        stack.enter_context(patch.object(core.shutil, "which", return_value=None))
        for name in ("service", "check_listeners", "phase", "output", "core_binary"):
            stack.enter_context(patch.object(self.m, name))
        return stack

    def test_shell_values_and_multiline_keys_roundtrip_without_execution(self):
        text = "ADMIN_PUBKEY='key1\nkey2'\nSERVER_ALIAS=old\nSERVER_ALIAS=duplicate\n"
        marker = self.root / "should-not-exist"
        value = "it's $(touch " + str(marker) + ") " + chr(96) + "echo unsafe" + chr(96)
        rendered = core.env_text(text, {"SERVER_ALIAS": value})
        self.assertIn("'key1\nkey2'", rendered)
        self.assertEqual(rendered.count("SERVER_ALIAS="), 1)
        self.config.write_text(rendered)
        self.assertEqual(core.GEN.parse_env_file(self.config)["SERVER_ALIAS"], value)
        if os.name == "posix" and shutil.which("bash"):
            result = subprocess.run(["bash", "-c", 'source "$1"; printf "%s" "$SERVER_ALIAS"', "_", str(self.config)],
                                    capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout, value)
        self.assertFalse(marker.exists())

    def test_unclosed_config_is_rejected(self):
        with self.assertRaises(core.Error):
            core.env_text("ADMIN_PUBKEY='broken\n", {"SERVER_ALIAS": "new"})

    def test_invalid_port_is_rejected(self):
        for value in ("0", "65536", "22;echo", "-1"):
            with self.assertRaises(core.Error):
                core.port(value)

    def test_plain_confirmation_uses_menu_choices_with_safe_destructive_default(self):
        with patch.object(surelay, "current_terminal", return_value=None), \
             contextlib.redirect_stdout(io.StringIO()), patch("builtins.input", return_value=""):
            surelay.confirm("保存修改")
            with self.assertRaises(surelay.Cancelled):
                surelay.confirm("删除出口", danger=True)
        with patch.object(surelay, "current_terminal", return_value=None), \
             contextlib.redirect_stdout(io.StringIO()), patch("builtins.input", return_value="1"):
            surelay.confirm("删除出口", danger=True)

    def test_routine_maintenance_runs_without_a_second_confirmation(self):
        panel = surelay.Panel(self.m)
        with patch.object(surelay, "confirm", side_effect=AssertionError("unexpected confirmation")), \
             patch.object(self.m, "update_code") as update, \
             patch.object(self.m, "backup") as backup, \
             patch.object(self.m, "phase") as phase, \
             patch.object(self.m, "service") as service, \
             patch.object(surelay, "run") as run:
            with self.assertRaises(SystemExit) as stopped:
                panel.update_code()
            self.assertEqual(stopped.exception.code, 0)
            update.assert_called_once_with()
            panel.update_core()
            backup.assert_called_once_with()
            self.m.env["ENABLE_CUSTOM_DOMAIN"] = "true"
            self.m.env["CUSTOM_DOMAIN"] = "edge.example.com"
            panel.certificate()
            phase.assert_called_once_with("custom-domain", RRB_SKIP_PACKAGE_INSTALL="false")
            with patch.object(surelay, "ask", return_value="1"):
                for action in ("start", "stop", "restart"):
                    panel.service_action(action)
                    service.assert_called_with("sing-box", action)
            with patch.object(surelay, "ask", return_value="192.0.2.5"):
                panel.unban()
                run.assert_called_with(["fail2ban-client", "set", "sshd", "unbanip", "192.0.2.5"])

    def test_name_edit_saves_once_without_confirmation_and_skips_unchanged_value(self):
        panel = surelay.Panel(self.m)
        with patch.object(surelay, "confirm", side_effect=AssertionError("unexpected confirmation")), \
             patch.object(self.m, "apply") as apply:
            with patch.object(surelay, "ask", return_value="new-name"):
                panel.setting("SERVER_ALIAS", "节点名称")
            apply.assert_called_once_with({"SERVER_ALIAS": "new-name"}, domain=False)
            apply.reset_mock()
            with patch.object(surelay, "ask", return_value="fixture"):
                panel.setting("SERVER_ALIAS", "节点名称")
            apply.assert_not_called()

    def test_panel_links_are_read_only_and_contain_only_client_endpoints(self):
        from urllib.parse import urlsplit
        from surelay_terminal import Terminal
        self.m.update({"ENABLE_IPV6_LISTEN": "true", "SERVER_IP_IPV6": "2001:db8::10",
                       "ENABLE_CUSTOM_DOMAIN": "true", "CUSTOM_DOMAIN": "edge.example.com",
                       "DIRECT_PORT": "24037", "SUBSCRIPTION_BASE_URL": "https://edge.example.com:24037"})
        (self.root / "home-proxies.csv").write_text(
            "tag,type,server,server_port,username,password,network,listen_port\n"
            "home-01,socks5,private-upstream.example,1080,user,private-password,tcp,51043\n")
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        window = Terminal()
        panel = surelay.Panel(self.m)
        with patch.object(surelay, "current_terminal", return_value=window):
            panel.node_links()
            self.assertEqual([name for name, _, _ in window.links],
                             ["fixture", "fixture-IPv6", "home-01", "home-01-IPv6"])
            urls = [url for _, _, url in window.links]
            self.assertEqual([urlsplit(url).hostname for url in urls],
                             ["edge.example.com", "2001:db8::10"] * 2)
            self.assertEqual([urlsplit(url).port for url in urls], [24037, 24037, 51043, 51043])
            self.assertTrue(all(not any(c.isspace() for c in url) for url in urls))
            self.assertNotIn("private-", window.buffer)
            panel.subscription_link()
            self.assertEqual(window.buffer, "https://edge.example.com:24037/sub/11111111-2222-4333-8444-555555555555?target=ClashMeta")
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_panel_uses_available_subscription_formats_without_client_specific_menus(self):
        from surelay_terminal import Terminal
        output = surelay.module("surelay_output", "11-output-nodes.py")
        window = Terminal()
        formats = [(f"Client-{i}", f"https://example.com/sub/fixture?target=Format{i}") for i in range(12)]
        with patch.object(surelay, "module", return_value=output), \
             patch.object(output, "subscription_links", return_value=formats), \
             patch.object(surelay, "current_terminal", return_value=window):
            surelay.Panel(self.m).subscription_link()
        self.assertEqual(window.link_kind, "订阅")
        self.assertEqual([url for _, _, url in window.links], [url for _, url in formats])
        self.assertEqual([fields for _, fields, _ in window.links], [[("适用客户端", label)] for label, _ in formats])

    def test_launcher_handles_spaces_and_refuses_other_program(self):
        project = self.root / "folder with spaces"
        project.mkdir()
        (project / "surelay.sh").write_text('printf "%s" "$1"\n')
        target = self.root / "sr"
        core.install_command(project, target)
        if os.name == "posix":
            result = subprocess.run(["sh", str(target), "hello world"], capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout, "hello world")
        target.write_text("#!/bin/sh\n# another program\n")
        with self.assertRaises(core.Error):
            core.install_command(project, target)

    def test_apply_failure_restores_config_and_identity(self):
        originals = {p: p.read_bytes() for p in self.m.owned_paths() if p.exists()}
        self.mock_system()
        self.m.check_listeners.side_effect = core.Error("listener failed")
        with self.assertRaises(core.Error):
            self.m.apply({"SERVER_ALIAS": "new"})
        self.assertEqual({p: p.read_bytes() for p in originals}, originals)
        self.m.phase.assert_not_called()

    def test_invalid_candidate_never_replaces_live_config(self):
        self.mock_system()
        before = (self.root / "sing-box.json").read_bytes()
        with patch.object(core, "run", side_effect=core.Error("invalid candidate")):
            with self.assertRaises(core.Error):
                self.m.apply({"SERVER_ALIAS": "new"})
        self.assertEqual((self.root / "sing-box.json").read_bytes(), before)
        self.assertEqual(self.m.env["SERVER_ALIAS"], "fixture")
        self.assertFalse((self.root / ".surelay-candidate.json").exists())

    def test_publication_failure_keeps_valid_local_change(self):
        self.mock_system()
        self.m.phase.side_effect = core.Error("subscription unavailable")
        with self.assertRaisesRegex(core.Error, "本地配置已生效"):
            self.m.apply({"SERVER_ALIAS": "new"})
        self.assertEqual(self.m.env["SERVER_ALIAS"], "new")
        self.assertNotIn("original", (self.root / "sing-box.json").read_text())

    def test_apply_does_not_run_install_or_rotate_keys(self):
        identities = {p: p.read_bytes() for p in self.state.iterdir()}
        self.mock_system()
        self.m.apply({"SERVER_ALIAS": "new"})
        self.assertEqual({p: p.read_bytes() for p in identities}, identities)
        self.assertEqual([c.args[0] for c in self.m.phase.call_args_list], ["subscription"])

    def test_exit_delete_preserves_generated_port(self):
        url = "vless://11111111-2222-4333-8444-555555555555@example.com:443?security=none&type=tcp"
        (self.root / "upstream-nodes.txt").write_text(url + "#one\n" + url + "#two\n")
        home, upstream = self.m.exit_files()
        self.assertEqual([r["listen_port"] for r in upstream], [51043, 51044])
        with patch.object(self.m, "apply") as apply:
            self.m.save_exits(home, upstream[1:])
        saved = apply.call_args.kwargs["files"][self.root / "upstream-nodes.txt"]
        self.assertIn("51044", saved)
        self.assertNotIn("51043", saved)

    def test_restore_preserves_host_paths_and_ssh_policy(self):
        self.mock_system()
        directory = self.m.backup()
        saved = core.env_text((directory / "0").read_text(), {
            "SERVER_ALIAS": "restored", "SSH_PORT": "9999", "SINGBOX_CONFIG_PATH": "/etc/shadow",
            "ENABLE_FAIL2BAN": "false", "RRB_STATE_DIR": "/etc",
        })
        (directory / "0").write_text(saved)
        with patch.object(self.m, "apply") as apply:
            self.m.restore_nodes(directory)
        updates, files = apply.call_args.args
        self.assertEqual(updates["SERVER_ALIAS"], "restored")
        for key in ("SSH_PORT", "SINGBOX_CONFIG_PATH", "RRB_STATE_DIR", "ENABLE_FAIL2BAN"):
            self.assertNotIn(key, updates)
        self.assertNotIn(Path("/etc/shadow"), files)

    def test_restore_rejects_traversal_index(self):
        self.mock_system()
        directory = self.m.backup()
        data = json.loads((directory / "node-backup.json").read_text())
        data["roles"][0] = "../../config.env"
        (directory / "node-backup.json").write_text(json.dumps(data))
        with self.assertRaises(core.Error):
            self.m.restore_nodes(directory)

    def test_firewall_explicit_families_and_protocols(self):
        rule = security.firewall_rule("8000-8010", "both", "any", "both", "app")
        args = security.rule_args(rule)
        self.assertEqual(len(args), 4)
        self.assertTrue(all("8000:8010" in a for a in args))
        self.assertTrue(any("::/0" in a for a in args))
        with self.assertRaises(core.Error):
            security.firewall_rule("22", "tcp", "::1", "4", "app")

    def test_custom_firewall_cannot_adopt_or_delete_required_rule(self):
        secure = security.Security(self.m)
        for value in ("22", "443", "51040", "1:1000"):
            with self.subTest(value=value):
                rule = security.firewall_rule(value, "tcp", "any", "4", "app")
                with self.assertRaisesRegex(core.Error, "所需 TCP"):
                    secure.edit_rule(rule)
                with patch.object(secure, "custom_rules", return_value=(self.root / "rules.json", [rule])):
                    with self.assertRaisesRegex(core.Error, "所需 TCP"):
                        secure.edit_rule(index=0)

    def test_custom_firewall_cannot_take_over_another_app(self):
        secure = security.Security(self.m)
        rule = security.firewall_rule("8080", "tcp", "any", "4", "app")
        with patch.object(security, "run", return_value="[ 1] 8080/tcp ALLOW IN Anywhere # another-app"):
            with self.assertRaisesRegex(core.Error, "已有规则"):
                secure.edit_rule(rule)
        with patch.object(security, "run", side_effect=["Status: inactive", "ufw allow 8080/tcp comment 'another app'"]):
            with self.assertRaisesRegex(core.Error, "已有规则"):
                secure.edit_rule(rule)

    def test_managed_ssh_block_preserves_other_policy(self):
        original = "# provider\nPort 22\nMatch User sftp\n    ForceCommand internal-sftp\n"
        first = security.ssh_block(original, {"Port": ["22", "2222"], "PasswordAuthentication": ["no"]})
        second = security.ssh_block(first, {"Port": ["2222"]})
        self.assertEqual(second.count(security.SSH_BEGIN), 1)
        self.assertIn(original, second)
        self.assertIn("PasswordAuthentication no", second)

    def test_ssh_port_comments_do_not_become_directives(self):
        text = security.ssh_block("PubkeyAuthentication yes\n", {"Port": ["22"]})
        text = text.replace("Port 22", "# SuRelay saved port: Port 22")
        result = security.ssh_block(text, {"Port": ["2222"]})
        self.assertNotIn("# SuRelay saved port:", result)

    def test_last_key_cannot_be_removed(self):
        secure = security.Security(self.m)
        with patch.object(secure, "key_path", return_value=self.root / "authorized_keys"):
            with self.assertRaisesRegex(core.Error, "最后一把"):
                secure.write_keys([])

    def test_new_key_preserved_when_replacement_verification_cancelled(self):
        panel = surelay.Panel(self.m)
        with patch.object(panel, "key_selection", return_value=(["old"], 0)), \
             patch.object(surelay, "ask", return_value="new"), \
             patch.object(panel.s, "validate_key", return_value="fingerprint"), \
             patch.object(panel.s, "write_keys") as write, \
             patch.object(panel, "confirm_login", side_effect=core.Error("cancelled")):
            with self.assertRaises(core.Error):
                panel.replace_key()
        write.assert_called_once_with(["old", "new"])

    def test_all_menus_can_return_without_mutations(self):
        panel = surelay.Panel(self.m)
        with patch.object(surelay, "ask", return_value="0"), \
             patch.object(panel, "status"), patch.object(core, "run") as external, \
             contextlib.redirect_stdout(io.StringIO()):
            for method in ("main", "nodes", "exits", "ipv6", "domain", "services", "security",
                           "keys", "ssh_login", "firewall", "fail2ban", "advanced"):
                getattr(panel, method)()
        external.assert_not_called()

    def test_update_refuses_dirty_tree_before_network(self):
        with patch.object(core, "run", return_value=" M changed") as command:
            with self.assertRaises(core.Error):
                self.m.update_code()
        self.assertEqual(command.call_count, 1)

    def test_ssh_migration_cancel_restores_files_and_does_not_save_port(self):
        secure = security.Security(self.m)
        secure.ssh_config = self.root / "sshd_config"
        secure.ssh_config.write_text("Port 22\nPasswordAuthentication no\n")
        secure.socket_conf = self.root / "socket.conf"
        original = secure.ssh_config.read_bytes()
        self.snapshot_class = core.Snapshot
        def cancelled(value):
            self.assertIn("Port 22\nPort 2222", secure.ssh_config.read_text())
            raise core.Error("cancelled")
        with patch.object(security, "Snapshot", side_effect=self.local_snapshot), \
             patch.object(security, "run", return_value=""), \
             patch.object(security.shutil, "which", return_value=None), \
             patch.object(self.m, "alpine", return_value=True), \
             patch.object(self.m, "service"), patch.object(self.m, "update") as update, \
             patch.object(secure, "effective", side_effect=["port 22", "port 22\nport 2222"]), \
             patch.object(secure, "verify_ssh_listener"):
            with self.assertRaisesRegex(core.Error, "cancelled"):
                secure.change_ssh_port("2222", cancelled)
        self.assertEqual(secure.ssh_config.read_bytes(), original)
        update.assert_not_called()

    def test_ssh_migration_saves_only_after_new_login_confirmation(self):
        secure = security.Security(self.m)
        secure.ssh_config = self.root / "sshd_config"
        secure.ssh_config.write_text("Port 22\n")
        secure.socket_conf = self.root / "socket.conf"
        self.m.env["ENABLE_FAIL2BAN"] = "false"
        self.snapshot_class = core.Snapshot
        with patch.object(security, "Snapshot", side_effect=self.local_snapshot), \
             patch.object(security, "run", return_value=""), \
             patch.object(security.shutil, "which", return_value=None), \
             patch.object(self.m, "alpine", return_value=True), patch.object(self.m, "service"), \
             patch.object(self.m, "update") as update, \
             patch.object(secure, "effective", side_effect=["port 22", "port 22\nport 2222"]), \
             patch.object(secure, "verify_ssh_listener"):
            secure.change_ssh_port("2222", lambda value: update.assert_not_called())
        update.assert_called_once_with({"SSH_PORT": "2222"})
        self.assertIn("Port 2222\n", secure.ssh_config.read_text())
        self.assertNotIn("\nPort 22\n", secure.ssh_config.read_text())


if __name__ == "__main__":
    unittest.main()
