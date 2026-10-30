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
        self.assertIn("域名：edge.example.com", labels)
        self.assertIn("证书邮箱", labels)
        self.assertIn("已设置（隐藏）", labels)
        self.assertNotIn("fixture-secret", labels + wizard.summary())
        labels = "\n".join(label for label, _ in wizard.protocol_fields())
        for name in ("HY2", "SOCKS5", "Shadowsocks", "HTTP"):
            self.assertIn(name, labels)

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
