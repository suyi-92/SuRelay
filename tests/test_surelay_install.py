"""Installation draft navigation and persisted deployment contracts; no host changes."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

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
        with patch.object(wizard, "choose", side_effect=[0, 4]), patch("surelay_install.ask", return_value="my-zone.example.com"):
            wizard.group("域名", lambda: [wizard.field("CLOUDFLARE_ZONE_NAME", "Zone", validate=install.domain),
                                          *wizard.domain_fields()[1:4]])
        self.assertEqual(wizard.values["CLOUDFLARE_ZONE_NAME"], "my-zone.example.com")
        self.assertEqual(wizard.values["CLOUDFLARE_API_TOKEN"], "fixture-secret")


if __name__ == "__main__":
    unittest.main()
