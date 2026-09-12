from __future__ import annotations

import contextlib
import io
import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


gen = load_module("gen", ROOT / "scripts" / "08-generate-singbox-config.py")


class RoutingTest(unittest.TestCase):
    def upstream_row(self):
        return gen.parse_vless_upstream(
            "frontier",
            51043,
            "vless://11111111-1111-1111-1111-111111111111@frontier.example.com:443"
            "?security=reality&type=tcp&flow=xtls-rprx-vision"
            "&sni=www.microsoft.com&pbk=public-key",
            2,
        )

    def test_legacy_smart_mode_is_normalized_to_direct(self):
        with contextlib.redirect_stderr(io.StringIO()):
            env = gen.defaults(
                {
                    "SERVER_ALIAS": "edge",
                    "SERVER_IP_IPV4": "203.0.113.10",
                    "MODE_443": "smart",
                    "SMART_AI_HOME_TAG": "frontier",
                    "REJECT_CN_PRIVATE": "true",
                }
            )

        self.assertEqual("direct", env["MODE_443"])

    def test_generated_routes_only_contain_fixed_inbound_mappings(self):
        with contextlib.redirect_stderr(io.StringIO()):
            env = gen.defaults(
                {
                    "SERVER_ALIAS": "edge",
                    "SERVER_IP_IPV4": "203.0.113.10",
                    "MODE_443": "smart",
                    "SMART_AI_HOME_TAG": "frontier",
                    "REJECT_CN_PRIVATE": "true",
                }
            )
        config = gen.build_config(
            env,
            [self.upstream_row()],
            "22222222-2222-2222-2222-222222222222",
            "private-key",
            "aabbccdd",
        )
        rules = config["route"]["rules"]

        self.assertEqual(
            [
                {"inbound": "vless-direct-443", "action": "route", "outbound": "direct"},
                {"inbound": "vless-frontier", "action": "route", "outbound": "out-frontier"},
            ],
            rules,
        )
        self.assertNotIn("rule_set", config["route"])
        upstream_outbound = next(item for item in config["outbounds"] if item["tag"] == "out-frontier")
        self.assertNotIn("network", upstream_outbound)
        split_keys = {
            "domain",
            "domain_suffix",
            "domain_keyword",
            "ip_cidr",
            "ip_is_private",
            "protocol",
            "rule_set",
        }
        forbidden_actions = {"sniff", "hijack-dns", "reject"}
        split_rules = [
            rule
            for rule in rules
            if split_keys & set(rule) or rule.get("action") in forbidden_actions
        ]

        self.assertEqual([], split_rules)


if __name__ == "__main__":
    unittest.main()
