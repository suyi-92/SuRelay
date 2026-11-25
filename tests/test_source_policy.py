"""Source restrictions: pure configuration checks, no network or host changes."""
from __future__ import annotations

import importlib.util
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    value = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(value)
    return value


policy = load_module("source_policy_test", "source_policy.py")


class SourcePolicyTest(unittest.TestCase):
    def env(self, **updates):
        return {
            "DIRECT_PORT": "55443", "SSH_PORT": "52322",
            "ENABLE_CUSTOM_DOMAIN": "true", "ENABLE_SUBSCRIPTION_SERVER": "true",
            "SUBSCRIPTION_PORT": "", "SUBSCRIPTION_INTERNAL_PORT": "52440",
            "SUBSCRIPTION_LISTEN_PORT": "52440", "NGINX_FALLBACK_PORT": "8443",
            "ENABLE_HTTP": "false", "HTTP_PORT": "58080",
            "ENABLE_SOCKS5": "false", "SOCKS5_PORT": "51080",
            "ENABLE_IPV6_LISTEN": "false", "SERVER_IP_IPV4": "192.0.2.10",
            "SERVER_IP_IPV6": "", "ENABLE_UFW": "true", "SURELAY_SECURITY_ONLY": "false",
            "PROXY_SOURCE_ALLOWLIST": "", "WEB_SOURCE_ALLOWLIST": "",
            "TCP_PUBLIC_PORT_MAP": "", **updates,
        }

    @staticmethod
    def entries(plan):
        return {entry["port"]: entry["sources"] for entry in plan["entries"]}

    def test_allowlist_canonicalizes_ip_cidr_and_duplicates(self):
        actual = policy.parse_allowlist("192.0.2.1, 192.0.2.0/24 192.0.2.1 2001:db8::1", "test")
        self.assertEqual(actual, ["192.0.2.0/24", "2001:db8::1/128"])

    def test_allowlist_rejects_hostnames_invalid_text_and_whole_internet(self):
        for value in ("example.com", "192.0.2.1;echo bad", "192.0.2.300", "0.0.0.0/0", "::/0",
                      "0.0.0.0/1,128.0.0.0/1", "::/1 8000::/1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                policy.parse_allowlist(value, "PROXY_SOURCE_ALLOWLIST")
        self.assertEqual(policy.parse_allowlist("", "test"), [])

    def test_default_high_port_without_plaintext_entries_needs_no_allowlist(self):
        env = self.env(ENABLE_UFW="false")
        policy.validate_source_policy(env)
        self.assertEqual(policy.source_policy_plan(env)["entries"], [])

    def test_http_and_socks_need_proxy_allowlist_on_every_port(self):
        for protocol, port_key in (("HTTP", "HTTP_PORT"), ("SOCKS5", "SOCKS5_PORT")):
            for port in ("80", "51080", "65535"):
                env = self.env(**{"ENABLE_" + protocol: "true", port_key: port,
                                  "WEB_SOURCE_ALLOWLIST": "192.0.2.10"})
                with self.subTest(protocol=protocol, port=port):
                    with self.assertRaises(ValueError):
                        policy.validate_source_policy(env)
                    env["PROXY_SOURCE_ALLOWLIST"] = "192.0.2.0/24"
                    policy.validate_source_policy(env)
                    self.assertEqual(self.entries(policy.source_policy_plan(env))[int(port)], ["192.0.2.0/24"])

    def test_restricted_web_ports_require_sources_in_both_domain_modes(self):
        for port in ("80", "443", "8080", "8443"):
            for custom in ("true", "false"):
                env = self.env(DIRECT_PORT=port, ENABLE_CUSTOM_DOMAIN=custom,
                               PROXY_SOURCE_ALLOWLIST="192.0.2.0/24")
                with self.subTest(port=port, custom=custom):
                    with self.assertRaises(ValueError):
                        policy.validate_source_policy(env)
                    env["WEB_SOURCE_ALLOWLIST"] = "198.51.100.10"
                    self.assertEqual(self.entries(policy.source_policy_plan(env))[int(port)], ["198.51.100.10/32"])

    def test_public_subscription_is_restricted_but_internal_nginx_and_subscription_are_not(self):
        self.assertEqual(policy.source_policy_plan(self.env(SUBSCRIPTION_LISTEN_PORT="8443"))["entries"], [])
        for custom in ("true", "false"):
            env = self.env(ENABLE_CUSTOM_DOMAIN=custom, SUBSCRIPTION_PORT="8080",
                           SUBSCRIPTION_LISTEN_PORT="8080")
            with self.subTest(custom=custom), self.assertRaises(ValueError):
                policy.validate_source_policy(env)
            env["WEB_SOURCE_ALLOWLIST"] = "192.0.2.10"
            self.assertEqual(self.entries(policy.source_policy_plan(env))[8080], ["192.0.2.10/32"])

    def test_every_reality_row_is_checked_even_when_direct_entry_is_high(self):
        for source in ("home", "upstream"):
            rows = [{"tag": "fixture", "source": source, "listen_port": 443}]
            with self.subTest(source=source), self.assertRaises(ValueError):
                policy.validate_source_policy(self.env(), rows)
            env = self.env(WEB_SOURCE_ALLOWLIST="192.0.2.0/24")
            self.assertEqual(self.entries(policy.source_policy_plan(env, rows))[443], ["192.0.2.0/24"])

    def test_declared_nat_public_web_port_restricts_local_port(self):
        env = self.env(TCP_PUBLIC_PORT_MAP="55443:443,55443:60000")
        with self.assertRaises(ValueError):
            policy.validate_source_policy(env)
        env["WEB_SOURCE_ALLOWLIST"] = "192.0.2.10"
        self.assertEqual(self.entries(policy.source_policy_plan(env))[55443], ["192.0.2.10/32"])
        self.assertNotIn(443, self.entries(policy.source_policy_plan(env)))

    def test_invalid_nat_mapping_cannot_silently_disable_restrictions(self):
        for value in ("55443", "55443:0", "55443:65536", "site:443", "55443:443;true"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                policy.validate_source_policy(self.env(TCP_PUBLIC_PORT_MAP=value))

    def test_restricted_services_cannot_turn_firewall_off(self):
        for extra in ({"ENABLE_HTTP": "true", "PROXY_SOURCE_ALLOWLIST": "192.0.2.10"},
                      {"DIRECT_PORT": "443", "WEB_SOURCE_ALLOWLIST": "192.0.2.10"}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                policy.validate_source_policy(self.env(ENABLE_UFW="false", **extra))

    def test_ipv4_only_allowlist_keeps_ipv6_requirement_explicit(self):
        plan = policy.source_policy_plan(self.env(ENABLE_SOCKS5="true", ENABLE_IPV6_LISTEN="true",
                                                  PROXY_SOURCE_ALLOWLIST="192.0.2.10"))
        self.assertTrue(plan["ipv6"])
        self.assertEqual(self.entries(plan)[51080], ["192.0.2.10/32"])

    def test_actual_json_not_env_switches_decides_which_old_listeners_need_protection(self):
        env = self.env()
        config = {"inbounds": [{"type": "http", "listen": "0.0.0.0", "listen_port": 58080},
                                {"type": "vless", "listen": "::", "listen_port": 443,
                                 "tls": {"reality": {"enabled": True}}}]}
        with self.assertRaises(ValueError):
            policy.source_policy_plan(env, config=config)
        env.update(PROXY_SOURCE_ALLOWLIST="192.0.2.10", WEB_SOURCE_ALLOWLIST="198.51.100.10")
        plan = policy.source_policy_plan(env, config=config)
        self.assertTrue(plan["ipv6"])
        self.assertEqual(self.entries(plan), {443: ["198.51.100.10/32"], 58080: ["192.0.2.10/32"]})

    def test_loopback_only_plaintext_listener_does_not_claim_public_exposure(self):
        config = {"inbounds": [{"type": "mixed", "listen": "127.0.0.1", "listen_port": 8080}]}
        self.assertEqual(policy.source_policy_plan(self.env(), config=config)["entries"], [])
        config["inbounds"][0]["listen"] = "0.0.0.0"
        with self.assertRaises(ValueError):
            policy.source_policy_plan(self.env(), config=config)

    def test_public_ipv6_subscription_requires_ipv6_guard_without_ipv6_proxy_listener(self):
        env = self.env(SUBSCRIPTION_PORT="8080", SUBSCRIPTION_LISTEN_PORT="8080",
                       SERVER_IP_IPV6="2001:db8::10", WEB_SOURCE_ALLOWLIST="192.0.2.10")
        plan = policy.source_policy_plan(env)
        self.assertTrue(plan["ipv6"])
        self.assertEqual(self.entries(plan), {8080: ["192.0.2.10/32"]})

    def test_nat_mapping_to_unknown_listener_is_rejected_after_rows_are_known(self):
        with self.assertRaises(ValueError):
            policy.validate_source_policy(self.env(TCP_PUBLIC_PORT_MAP="60000:443"), [])
        env = self.env(TCP_PUBLIC_PORT_MAP="60000:443", WEB_SOURCE_ALLOWLIST="192.0.2.10")
        rows = [{"listen_port": 60000}]
        self.assertEqual(self.entries(policy.source_policy_plan(env, rows)), {60000: ["192.0.2.10/32"]})

    def test_ssh_public_mapping_does_not_make_ssh_a_web_service(self):
        plan = policy.source_policy_plan(self.env(TCP_PUBLIC_PORT_MAP="52322:443"))
        self.assertEqual(plan["entries"], [])

    def test_guard_payload_contains_policy_not_proxy_or_cloudflare_secrets(self):
        env = self.env(SINGBOX_CONFIG_PATH="/etc/sing-box/config.json", PROXY_PASSWORD="fixture-private",
                       CLOUDFLARE_API_TOKEN="fixture-cf-secret", VLESS_UUID_PATH="/etc/surelay/uuid.txt")
        payload = policy.guard_payload(env)
        encoded = json.dumps(payload)
        self.assertNotIn("fixture-private", encoded)
        self.assertNotIn("fixture-cf-secret", encoded)
        self.assertNotIn("VLESS_UUID_PATH", encoded)
        self.assertEqual(payload["singbox_config"], "/etc/sing-box/config.json")


class GeneratorSourcePolicyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gen = load_module("source_generator_test", "08-generate-singbox-config.py")

    def test_missing_source_restrictions_are_rejected_by_both_config_apis_before_dns(self):
        gen = self.gen
        for updates in ({"DIRECT_PORT": "443"},
                        {"DIRECT_PORT": "55443", "ENABLE_HTTP": "true"},
                        {"DIRECT_PORT": "55443", "ENABLE_SOCKS5": "true"}):
            env = gen.defaults({"SERVER_ALIAS": "fixture", "SERVER_IP_IPV4": "192.0.2.10", **updates})
            with self.subTest(updates=updates), patch.object(gen.socket, "getaddrinfo", side_effect=AssertionError("unexpected DNS")):
                with self.assertRaises(gen.ConfigError):
                    gen.validate_env(env)
                with self.assertRaises(gen.ConfigError):
                    gen.build_config(env, [], "test-uuid", "private-key", "abcd")

    def test_runtime_check_reads_active_firewall_and_refuses_inactive_policy(self):
        gen = self.gen
        config = {"inbounds": [{"type": "vless", "listen": "0.0.0.0", "listen_port": 443,
                                "tls": {"reality": {"enabled": True}}}]}
        result = subprocess.CompletedProcess(["ufw", "status"], 0, "Status: inactive\n", "")
        with tempfile.TemporaryDirectory() as directory:
            env = gen.defaults({"DIRECT_PORT": "443", "WEB_SOURCE_ALLOWLIST": "192.0.2.10",
                                "RRB_STATE_DIR": directory})
            payload = gen.SOURCE_POLICY.guard_payload(env)
            (Path(directory) / "source-guard.json").write_text(json.dumps(payload), encoding="utf-8")
            with patch("subprocess.run", return_value=result) as invoke:
                with self.assertRaises(gen.ConfigError):
                    gen.check_source_firewall(env, config)
        self.assertEqual(invoke.call_args.args[0], ["ufw", "status"])

    def test_unrestricted_high_port_start_does_not_require_firewall_tools(self):
        gen = self.gen
        env = gen.defaults({"DIRECT_PORT": "55443", "ENABLE_UFW": "false"})
        with patch("subprocess.run", side_effect=AssertionError("unexpected firewall command")):
            gen.check_source_firewall(env)

    def test_firewall_plan_cli_emits_only_policy_without_dns_or_node_credentials(self):
        gen = self.gen
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.env"
            (Path(directory) / "home-proxies.csv").write_text(
                "tag,type,server,server_port,username,password,network,listen_port\n", encoding="utf-8")
            config.write_text('DIRECT_PORT="55443"\nENABLE_HTTP="true"\n'
                              'PROXY_SOURCE_ALLOWLIST="192.0.2.10"\nPROXY_PASSWORD="fixture-secret"\n', encoding="utf-8")
            stream = io.StringIO()
            with patch.object(sys, "argv", ["generator", "--config-env", str(config), "--print-source-policy"]), \
                    patch.object(gen.socket, "getaddrinfo", side_effect=AssertionError("unexpected DNS")), \
                    patch.object(gen, "read_vless_uuid", side_effect=AssertionError("unexpected credentials")), \
                    contextlib.redirect_stdout(stream):
                self.assertEqual(gen.main(), 0)
            self.assertNotIn("fixture-secret", stream.getvalue())
            self.assertEqual(json.loads(stream.getvalue())["entries"], [{"port": 58080, "sources": ["192.0.2.10/32"]}])


if __name__ == "__main__":
    unittest.main()
