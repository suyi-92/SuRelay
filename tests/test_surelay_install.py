"""Installation draft navigation and persisted deployment contracts; no host changes."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import mock_open, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import surelay_install as install
from surelay_core import GEN


class InstallWizardTest(unittest.TestCase):
    def fresh_domain_draft(self, alias="AalTR-Verizon", zone="example.com"):
        values = install.initial_values("192.0.2.10")
        values.update(SERVER_ALIAS=alias, CLOUDFLARE_ZONE_NAME=zone, LE_EMAIL="admin@example.com",
                      CLOUDFLARE_API_TOKEN="fixture-secret")
        wizard = install.Wizard(values)
        wizard.keys_confirmed = True
        return wizard

    def test_domain_page_defaults_prefix_from_node_name_and_saves_complete_hostname(self):
        wizard = self.fresh_domain_draft()
        labels = [label for label, _ in wizard.domain_fields()]
        self.assertEqual(labels[1], "一级子域名：aaltr-verizon")
        self.assertEqual(labels[2], "域名：example.com")
        values = wizard.validate()
        self.assertEqual(values["CUSTOM_DOMAIN"], "aaltr-verizon.example.com")
        self.assertEqual(values["CLOUDFLARE_ZONE_NAME"], "example.com")
        self.assertEqual(values["REALITY_SERVER_NAME"], values["CUSTOM_DOMAIN"])
        self.assertIn(values["CUSTOM_DOMAIN"], values["SUBSCRIPTION_BASE_URL"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.example.env").write_bytes((ROOT / "config.example.env").read_bytes())
            install.save_config(root, values)
            saved = GEN.parse_env_file(root / "config.env")
            self.assertEqual(saved["CUSTOM_DOMAIN"], "aaltr-verizon.example.com")
            self.assertEqual(saved["CLOUDFLARE_ZONE_NAME"], "example.com")

    def test_automatic_prefix_follows_node_changes_even_when_page_is_not_reopened(self):
        wizard = self.fresh_domain_draft()
        wizard.domain_fields()
        with patch("surelay_install.ask", return_value="aaltr-verizon"):
            wizard.edit_subdomain()
        wizard.values["SERVER_ALIAS"] = "Next-Node"
        self.assertIn("next-node.example.com", wizard.summary())
        self.assertEqual(wizard.validate()["CUSTOM_DOMAIN"], "next-node.example.com")

    def test_custom_prefix_survives_node_rename_and_cf_toggle_and_recombines_new_zone(self):
        wizard = self.fresh_domain_draft()
        with patch("surelay_install.ask", return_value="Custom-Edge"):
            wizard.edit_subdomain()
        wizard.values["SERVER_ALIAS"] = "Different-Node"
        wizard.toggle("ENABLE_CUSTOM_DOMAIN")
        wizard.domain_fields()
        wizard.toggle("ENABLE_CUSTOM_DOMAIN")
        with patch("surelay_install.ask", return_value="Example.Co.UK"):
            wizard.edit("CLOUDFLARE_ZONE_NAME", "域名", validate=install.domain)
        values = wizard.validate()
        self.assertEqual(values["CUSTOM_DOMAIN"], "custom-edge.example.co.uk")
        self.assertEqual(values["CLOUDFLARE_ZONE_NAME"], "example.co.uk")
        self.assertIn("一级子域名：custom-edge", [label for label, _ in wizard.domain_fields()])

    def test_subdomain_validation_rejects_nested_and_invalid_labels(self):
        for value in ("", "a.b", "_node", "-node", "node-", "two words", "节点", "a" * 64):
            with self.subTest(value=value), self.assertRaisesRegex(install.Error, "一级子域名"):
                install.subdomain(value)
        self.assertEqual(install.subdomain("AalTR-Verizon"), "aaltr-verizon")
        self.assertEqual(install.subdomain("a" * 63), "a" * 63)
        wizard = self.fresh_domain_draft("节点 One")
        self.assertIn("一级子域名：节点 one", [label for label, _ in wizard.domain_fields()])
        with self.assertRaisesRegex(install.Error, "一级子域名"):
            wizard.validate()
        wizard = self.fresh_domain_draft("a" * 63, ".".join(["b" * 63] * 3))
        with self.assertRaisesRegex(install.Error, "完整域名"):
            wizard.validate()

    def test_cancelled_prefix_edit_keeps_automatic_suggestion_and_credentials(self):
        wizard = self.fresh_domain_draft()
        with patch("surelay_install.ask", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            wizard.edit_subdomain()
        wizard.values["SERVER_ALIAS"] = "Changed-Name"
        self.assertEqual(wizard.validate()["CUSTOM_DOMAIN"], "changed-name.example.com")
        self.assertEqual(wizard.values["CLOUDFLARE_API_TOKEN"], "fixture-secret")

    def test_protocol_only_keeps_existing_multilevel_domain_and_hy2_tls_values(self):
        values = self.fresh_domain_draft().values
        values.update(CUSTOM_DOMAIN="deep.edge.example.com", HY2_SERVER_NAME="tls.example.net",
                      HY2_CERT_PATH="/certs/custom.pem", HY2_KEY_PATH="/certs/custom.key")
        wizard = install.Wizard(values, protocol_only=True)
        wizard.values["SERVER_ALIAS"] = "Renamed"
        saved = wizard.validate()
        for key in ("CUSTOM_DOMAIN", "CLOUDFLARE_ZONE_NAME", "HY2_SERVER_NAME", "HY2_CERT_PATH", "HY2_KEY_PATH"):
            self.assertEqual(saved[key], values[key])

    def draft(self, security=False):
        values = install.initial_values("192.0.2.10", security)
        values.update(SERVER_ALIAS="relay", CUSTOM_DOMAIN="edge.example.com",
                      CLOUDFLARE_ZONE_NAME="example.com", LE_EMAIL="admin@example.com",
                      CLOUDFLARE_API_TOKEN="fixture-secret")
        wizard = install.Wizard(values, security_only=security)
        wizard.keys_confirmed = True
        return wizard

    def test_fresh_ports_are_distinct_high_ports_and_survive_back_navigation(self):
        wizard = self.draft()
        keys = ["SSH_PORT", "DIRECT_PORT", "SUBSCRIPTION_INTERNAL_PORT", "NGINX_FALLBACK_PORT",
                "HY2_PORT", "SOCKS5_PORT", "SHADOWSOCKS_PORT", "HTTP_PORT"]
        ports = [int(wizard.values[key]) for key in keys]
        self.assertEqual(len(set(ports)), len(ports))
        self.assertTrue(all(49152 <= value <= 65535 for value in ports))
        # Advance, return from ports, edit basics, advance to preview, jump back
        # to ports, then finish. No field should be discarded or regenerated.
        group_calls = []
        decisions = iter([True, False, True, True, True, True, True, True, True, True, True])
        def group(title, fields, **kwargs):
            group_calls.append(title)
            if len(group_calls) == 3:
                wizard.values["SERVER_ALIAS"] = "edited"
            return next(decisions)
        with patch.object(wizard, "group", side_effect=group), patch.object(wizard, "choose", side_effect=[2, 0]):
            result = wizard.run()
        self.assertEqual(result["SERVER_ALIAS"], "edited")
        self.assertEqual([int(result[key]) for key in keys], ports)
        self.assertEqual(group_calls[0], group_calls[2])
        self.assertIn("服务端口", group_calls[7])

    def test_default_cf_and_static_page_and_protocols_are_visible_in_groups(self):
        wizard = self.draft()
        self.assertEqual(wizard.values["ENABLE_CUSTOM_DOMAIN"], "true")
        labels = "\n".join(label for label, _ in wizard.domain_fields())
        self.assertIn("CF 域名与静态网页：开启", labels)
        self.assertIn("一级子域名：edge", labels)
        self.assertIn("域名：example.com", labels)
        self.assertIn("证书邮箱", labels)
        self.assertIn("已设置（隐藏）", labels)
        self.assertNotIn("fixture-secret", labels + wizard.summary())
        labels = "\n".join(label for label, _ in wizard.protocol_fields())
        for name in ("HY2", "SOCKS5", "Shadowsocks", "HTTP"):
            self.assertIn(name, labels)

    def test_plain_install_can_disable_cf_and_save_without_domain_or_credentials(self):
        wizard = install.Wizard(install.initial_values("192.0.2.10"))
        wizard.values["SERVER_ALIAS"] = "relay"
        # Reach CF, choose Off, reopen and confirm the current mode, then
        # finish installation without entering a domain, email or API token.
        answers = [">", ">", "1", "2", "1", "", ">", ">", "2", ">", "1"]
        with patch("surelay_install.current", return_value=None), \
                patch("surelay.current_terminal", return_value=None), \
                patch("builtins.input", side_effect=answers), contextlib.redirect_stdout(io.StringIO()):
            values = wizard.run()
        self.assertEqual(values["ENABLE_CUSTOM_DOMAIN"], "false")
        for key in ("CUSTOM_DOMAIN", "CLOUDFLARE_ZONE_NAME", "LE_EMAIL", "CLOUDFLARE_API_TOKEN"):
            self.assertEqual(values[key], "")
        self.assertIn("CF 域名与静态页：关闭", wizard.summary())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.example.env").write_bytes((ROOT / "config.example.env").read_bytes())
            install.save_config(root, values)
            saved = GEN.defaults(GEN.parse_env_file(root / "config.env"))
        GEN.validate_env(saved)
        self.assertEqual(saved["ENABLE_CUSTOM_DOMAIN"], "false")
        self.assertEqual(saved["REALITY_SERVER_NAME"], "www.microsoft.com")
        self.assertEqual(saved["REALITY_HANDSHAKE_SERVER"], "www.microsoft.com")
        self.assertEqual(saved["REALITY_HANDSHAKE_PORT"], "443")
        self.assertEqual(saved["SUBSCRIPTION_BASE_URL"], "")
        self.assertEqual(saved["SUBSCRIPTION_PORT"], saved["SUBSCRIPTION_INTERNAL_PORT"])
        self.assertEqual(saved["SUBSCRIPTION_LISTEN_PORT"], saved["SUBSCRIPTION_INTERNAL_PORT"])

    def test_domain_mode_cancel_and_reenable_preserve_entered_fields(self):
        wizard = self.draft()
        original = wizard.validate()
        for answer, enabled in (("0", "true"), ("2", "false"), ("0", "false"), ("1", "true")):
            with self.subTest(answer=answer, enabled=enabled), \
                    patch("surelay.current_terminal", return_value=None), \
                    patch("builtins.input", return_value=answer), contextlib.redirect_stdout(io.StringIO()):
                try:
                    wizard.domain_fields()[0][1]()
                except install.Cancelled:
                    self.assertEqual(answer, "0")
                self.assertEqual(wizard.values["ENABLE_CUSTOM_DOMAIN"], enabled)
                for key in ("CUSTOM_DOMAIN", "CLOUDFLARE_ZONE_NAME", "LE_EMAIL", "CLOUDFLARE_API_TOKEN"):
                    self.assertEqual(wizard.values[key], original[key])
        self.assertEqual(wizard.validate(), original)

    def test_validation_rejects_port_conflicts_and_missing_keys_without_writing(self):
        wizard = self.draft()
        wizard.keys_confirmed = False
        with self.assertRaisesRegex(install.Error, "公钥"):
            wizard.validate()
        wizard.keys_confirmed = True
        wizard.values["SSH_PORT"] = wizard.values["DIRECT_PORT"]
        with self.assertRaises(GEN.ConfigError):
            wizard.validate()

    def test_cancel_never_writes_config(self):
        wizard = self.draft()
        with patch.object(wizard, "group", return_value=False), self.assertRaises(install.Cancelled):
            wizard.run()

    def test_save_generates_correct_domain_routes_and_does_not_overwrite(self):
        wizard = self.draft()
        values = wizard.validate()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config.example.env").write_bytes((ROOT / "config.example.env").read_bytes())
            install.save_config(root, values)
            config = GEN.defaults(GEN.parse_env_file(root / "config.env"))
            self.assertEqual(config["REALITY_HANDSHAKE_PORT"], config["NGINX_FALLBACK_PORT"])
            self.assertEqual(config["SUBSCRIPTION_LISTEN_PORT"], config["SUBSCRIPTION_INTERNAL_PORT"])
            self.assertEqual(config["SUBSCRIPTION_PORT"], "")
            self.assertEqual(config["SUBSCRIPTION_BASE_URL"], "https://edge.example.com:" + config["DIRECT_PORT"])
            original = (root / "config.env").read_bytes()
            with self.assertRaises(install.Error):
                install.save_config(root, values)
            self.assertEqual((root / "config.env").read_bytes(), original)

    def test_security_only_draft_disables_node_and_domain_deployment(self):
        wizard = self.draft(True)
        values = wizard.validate()
        self.assertEqual(values["SURELAY_SECURITY_ONLY"], "true")
        self.assertEqual(values["ENABLE_SUBSCRIPTION_SERVER"], "false")
        self.assertEqual(values["ENABLE_CUSTOM_DOMAIN"], "false")
        for key, _, _ in install.PROTOCOLS:
            self.assertEqual(values["ENABLE_" + key], "false")

    def test_protocol_validation_can_be_fixed_without_discarding_draft(self):
        wizard = self.draft()
        wizard.protocol_only = True
        wizard.values.update(ENABLE_HTTP="true", HTTP_PORT=wizard.values["SSH_PORT"])
        calls = []
        def group(*args, **kwargs):
            calls.append(wizard.error)
            if len(calls) == 2:
                wizard.values["HTTP_PORT"] = install.port(wizard.values["SOCKS5_PORT"])
            return True
        with patch.object(wizard, "group", side_effect=group):
            values = wizard.run()
        self.assertEqual(len(calls), 2)
        self.assertIn("冲突", calls[1])
        self.assertEqual(values["ENABLE_HTTP"], "true")
        self.assertEqual(values["CLOUDFLARE_API_TOKEN"], "fixture-secret")

    def test_plain_group_keeps_related_values_and_can_reedit_without_losing_secret(self):
        wizard = self.draft()
        with patch.object(wizard, "choose", side_effect=[0, install.STEP_NEXT]), patch("surelay_install.ask", return_value="my-zone.example.com"):
            wizard.group("域名", lambda: [wizard.field("CLOUDFLARE_ZONE_NAME", "Zone", validate=install.domain),
                                          *wizard.domain_fields()[1:4]])
        self.assertEqual(wizard.values["CLOUDFLARE_ZONE_NAME"], "my-zone.example.com")
        self.assertEqual(wizard.values["CLOUDFLARE_API_TOKEN"], "fixture-secret")

    def test_plain_steps_accept_angle_brackets_and_direction_words(self):
        for value, expected in (("<", install.STEP_PREVIOUS), ("left", install.STEP_PREVIOUS),
                                (">", install.STEP_NEXT), ("right", install.STEP_NEXT)):
            with self.subTest(value=value), patch("surelay_install.current", return_value=None), \
                    patch("surelay_install.ask", return_value=value), contextlib.redirect_stdout(io.StringIO()) as output:
                actual = self.draft().choose("分组", [("域名", None)], steps=("上一步", "下一步"))
                self.assertEqual(actual, expected)
                self.assertIn("< 上一步    下一步 >", output.getvalue())
                self.assertNotIn("0)", output.getvalue())
                self.assertNotIn("2) 下一步", output.getvalue())

    def test_step_groups_have_no_numbered_navigation_and_keep_edits_on_return(self):
        wizard = self.draft()
        calls = []
        decisions = iter([0, install.STEP_PREVIOUS])
        def choose(title, actions, *args, **kwargs):
            calls.append(([label for label, _ in actions], kwargs))
            return next(decisions)
        with patch.object(wizard, "choose", side_effect=choose), patch("surelay_install.ask", return_value="edited"):
            self.assertFalse(wizard.group("基础", lambda: [wizard.field("SERVER_ALIAS", "名称")]))
        self.assertEqual(wizard.values["SERVER_ALIAS"], "edited")
        self.assertEqual(calls[0][0], ["名称：relay"])
        self.assertEqual(calls[1][0], ["名称：edited"])
        self.assertEqual(calls[0][1]["steps"], ("上一步", "下一步"))

    def test_protocol_apply_retains_explicit_action_instead_of_arrow_execution(self):
        wizard = self.draft()
        wizard.protocol_only = True
        with patch.object(wizard, "group", return_value=True) as group:
            wizard.run()
        self.assertFalse(group.call_args.kwargs["step_navigation"])
        self.assertEqual(group.call_args.kwargs["finish"], "校验并应用")

    def test_redeploy_requires_new_public_key_and_cannot_reuse_existing_keys(self):
        wizard = self.draft()
        wizard.redeploy = True
        self.assertEqual(len(wizard.key_fields()), 1)
        self.assertIn("新管理员公钥", wizard.key_fields()[0][0])
        with self.assertRaisesRegex(install.Error, "重新部署需录入新管理员公钥"):
            wizard.reuse_keys()
        with self.assertRaisesRegex(install.Error, "重新部署需录入新管理员公钥"):
            wizard.validate()
        from tests.test_admin_pubkey_input import KEYS
        with patch("surelay_install.ask", side_effect=[KEYS[0], ""]):
            wizard.add_keys()
        self.assertEqual(wizard.validate()["ADMIN_PUBKEY"], KEYS[0])
        normal = self.draft()
        normal.keys_confirmed = False
        normal.reuse_keys()
        self.assertTrue(normal.keys_confirmed)

    def test_redeploy_preview_rejects_old_identity_before_save_and_allows_correction(self):
        from tests.test_admin_pubkey_input import KEYS
        for conflict, message in (("key", "公钥必须与旧"), ("port", "不同于旧 SSH")):
            with self.subTest(conflict=conflict), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                wizard = self.draft()
                wizard.redeploy = True
                wizard.values["ADMIN_PUBKEY"] = KEYS[0] if conflict == "key" else KEYS[1]
                old_port = wizard.values["SSH_PORT"] if conflict == "port" else "22"
                handoff = root / "handoff.json"
                handoff.write_text(json.dumps({
                    "format": 1, "mode": "redeploy", "project": str(root), "status": "removed",
                    "old_authorized_keys": [KEYS[0]], "ssh_ports": [old_port],
                    "old_config": {"SSH_PORT": old_port, "ADMIN_PUBKEY": KEYS[0]},
                    "authorized_keys_path": str(root / "authorized_keys"), "pending_firewall": [],
                }), encoding="utf-8")
                handoff.chmod(0o600)
                before = handoff.read_bytes()
                wizard.validate_extra = install.redeploy_validator(root, handoff)
                previews = []
                def choose(title, actions, status, *args, **kwargs):
                    previews.append(status)
                    self.assertFalse((root / "config.env").exists())
                    if len(previews) == 2:
                        self.assertIn(message, status)
                        return 5 if conflict == "key" else 2
                    return 0
                def group(title, fields, **kwargs):
                    if len(previews) == 2:
                        if conflict == "key" and "管理员 SSH 公钥" in title:
                            wizard.values["ADMIN_PUBKEY"] = KEYS[1]
                        elif conflict == "port" and "服务端口" in title:
                            wizard.values["SSH_PORT"] = next(str(value) for value in range(49152, 65536)
                                                             if str(value) not in wizard.values.values())
                    return True
                with patch.object(wizard, "choose", side_effect=choose), \
                        patch.object(wizard, "group", side_effect=group), \
                        patch("surelay_security.run", return_value="fixture fingerprint") as command:
                    values = wizard.run()
                self.assertEqual(len(previews), 3)
                self.assertEqual(values["ADMIN_PUBKEY"], KEYS[1])
                self.assertNotEqual(values["SSH_PORT"], old_port)
                self.assertEqual(values["CLOUDFLARE_API_TOKEN"], "fixture-secret")
                self.assertFalse((root / "config.env").exists())
                self.assertEqual(handoff.read_bytes(), before)
                self.assertTrue(command.call_args_list)
                self.assertTrue(all(call.args[0][0] == "ssh-keygen" for call in command.call_args_list))

    def test_main_only_attaches_handoff_validation_in_redeploy_mode(self):
        for enabled in ("false", "true"):
            with self.subTest(redeploy=enabled), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                validator = lambda _: None
                with patch.dict(install.os.environ, {"RRB_REDEPLOY": enabled,
                                                      "RRB_REDEPLOY_HANDOFF": "fixture-handoff"}), \
                        patch.object(sys, "argv", ["surelay_install.py", "--project-dir", str(root)]), \
                        patch.object(install, "open", mock_open(), create=True), \
                        patch.object(install, "Terminal", return_value=contextlib.nullcontext()), \
                        patch.object(install, "Wizard") as wizard, \
                        patch.object(install, "save_config"), \
                        patch.object(install, "redeploy_validator", return_value=validator) as factory:
                    self.assertEqual(install.main(), 0)
                if enabled == "true":
                    factory.assert_called_once_with(root, "fixture-handoff")
                    self.assertIs(wizard.call_args.kwargs["validate_extra"], validator)
                else:
                    factory.assert_not_called()
                    self.assertIsNone(wizard.call_args.kwargs["validate_extra"])


if __name__ == "__main__":
    unittest.main()
