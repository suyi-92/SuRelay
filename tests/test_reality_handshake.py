"""Reality fallback safety, using only local data and mocked DNS answers."""
from __future__ import annotations

import copy
import importlib.util
import ipaddress
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("handshake_gen", ROOT / "scripts" / "08-generate-singbox-config.py")
gen = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(gen)


def dns_answers(*addresses):
    return [
        (socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM,
         socket.IPPROTO_TCP, "", (address, 443, 0, 0) if ":" in address else (address, 443))
        for address in addresses
    ]


class RealityHandshakeTest(unittest.TestCase):
    def env(self, **updates):
        return gen.defaults({"SERVER_ALIAS": "edge", "SERVER_IP_IPV4": "192.0.2.10",
                             "WEB_SOURCE_ALLOWLIST": "192.0.2.0/24", **updates})

    def custom_env(self, **updates):
        return self.env(ENABLE_CUSTOM_DOMAIN="true", CUSTOM_DOMAIN="edge.example.com",
                        LE_EMAIL="admin@example.com", **updates)

    def test_custom_domain_needs_no_dns_and_keeps_loopback(self):
        env = self.custom_env()
        with patch.object(gen.socket, "getaddrinfo", side_effect=AssertionError("unexpected DNS")):
            gen.validate_env(env)
            config = gen.build_config(env, [], "test-uuid", "private-key", "abcd")
        handshake = config["inbounds"][0]["tls"]["reality"]["handshake"]
        self.assertEqual(handshake, {"server": "127.0.0.1", "server_port": 8443})

    def test_custom_domain_rejects_nonloopback_fallback_even_when_fields_match(self):
        for host in ("www.cloudflare.com", "edge.example.com", "104.16.0.1", "0.0.0.0",
                     "localhost", "127.0.0.2", "::1"):
            with self.subTest(host=host):
                env = self.custom_env(NGINX_FALLBACK_HOST=host, REALITY_HANDSHAKE_SERVER=host)
                with self.assertRaises(gen.ConfigError):
                    gen.validate_env(env)
                # The direct generation API must uphold the boundary as well.
                with self.assertRaises(gen.ConfigError):
                    gen.build_config(env, [], "test-uuid", "private-key", "abcd")

    def test_literal_cloudflare_addresses_are_rejected_without_dns(self):
        # One representative from every official Cloudflare HTTP CIDR, sourced
        # independently from https://www.cloudflare.com/ips/ (2026-11-25).
        addresses = ("173.245.48.1", "103.21.244.1", "103.22.200.1", "103.31.4.1",
                     "141.101.64.1", "108.162.192.1", "190.93.240.1", "188.114.96.1",
                     "197.234.240.1", "198.41.128.1", "162.158.0.1", "104.16.0.1",
                     "104.24.0.1", "172.64.0.1", "131.0.72.1", "2400:cb00::1",
                     "2606:4700::1", "2803:f800::1", "2405:b500::1", "2405:8100::1",
                     "2a06:98c0::1", "2c0f:f248::1",
                     "::ffff:104.16.0.1")
        with patch.object(gen.socket, "getaddrinfo", side_effect=AssertionError("unexpected DNS")):
            for address in addresses:
                with self.subTest(address=address):
                    env = self.env(REALITY_HANDSHAKE_SERVER=address)
                    with self.assertRaises(gen.ConfigError):
                        gen.validate_env(env)
                    with self.assertRaises(gen.ConfigError):
                        gen.resolve_reality_handshake_server(env)

    def test_safe_literal_is_pinned_without_dns(self):
        for address in ("127.0.0.1", "192.0.2.30", "2001:db8::30"):
            with self.subTest(address=address), patch.object(
                    gen.socket, "getaddrinfo", side_effect=AssertionError("unexpected DNS")):
                selected = gen.resolve_reality_handshake_server(self.env(REALITY_HANDSHAKE_SERVER=address))
                self.assertEqual(ipaddress.ip_address(selected), ipaddress.ip_address(address))

    def test_external_domain_selects_pinned_address_by_outbound_family(self):
        answers = dns_answers("2001:db8::30", "192.0.2.30")
        for policy, expected in (("ipv4", "192.0.2.30"), ("ipv6", "2001:db8::30"),
                                 ("dual", "192.0.2.30")):
            for listen in ("false", "true"):
                with self.subTest(policy=policy, listen=listen), patch.object(
                        gen.socket, "getaddrinfo", return_value=answers) as resolver:
                    env = self.env(PROXY_IP_VERSION=policy, ENABLE_IPV6_LISTEN=listen)
                    self.assertEqual(gen.resolve_reality_handshake_server(env), expected)
                    resolver.assert_called_once_with("www.microsoft.com", 443, type=socket.SOCK_STREAM)

    def test_dual_stack_accepts_ipv6_only_origin(self):
        with patch.object(gen.socket, "getaddrinfo", return_value=dns_answers("2001:db8::30")):
            self.assertEqual(gen.resolve_reality_handshake_server(self.env(PROXY_IP_VERSION="dual")),
                             "2001:db8::30")

    def test_any_cloudflare_dns_answer_rejects_whole_target(self):
        # A neutral domain or a safe first result must not hide a CF endpoint,
        # including CF answers in the currently unused address family.
        for answers in (dns_answers("104.16.0.1"),
                        dns_answers("192.0.2.30", "104.16.0.1"),
                        dns_answers("192.0.2.30", "2606:4700::1"),
                        dns_answers("192.0.2.30", "::ffff:104.16.0.1")):
            with self.subTest(answers=answers), patch.object(gen.socket, "getaddrinfo", return_value=answers):
                with self.assertRaises(gen.ConfigError):
                    gen.resolve_reality_handshake_server(self.env(REALITY_HANDSHAKE_SERVER="innocent.example"))

    def test_ipv6_encodings_cannot_hide_cloudflare_or_local_nat64_targets(self):
        targets = (
            "64:ff9b::6810:1",  # Standard NAT64 mapping of 104.16.0.1.
            "2002:6810:1::1",  # 6to4 gateway 104.16.0.1.
            "2001:0:6810:1::3fff:fde1",  # Teredo server 104.16.0.1.
            "2001:0:c000:21e::97ef:fffe",  # Teredo client 104.16.0.1.
            "64:ff9b:1::c000:21e",  # Local-use NAT64 layout cannot be verified.
        )
        for address in targets:
            with self.subTest(address=address):
                with self.assertRaises(gen.ConfigError):
                    gen.validate_env(self.env(REALITY_HANDSHAKE_SERVER=address))
                with patch.object(gen.socket, "getaddrinfo", return_value=dns_answers(address)):
                    with self.assertRaises(gen.ConfigError):
                        gen.resolve_reality_handshake_server(self.env(PROXY_IP_VERSION="ipv6"))
                with patch.object(gen.socket, "getaddrinfo", side_effect=AssertionError("unexpected DNS")):
                    with self.assertRaises(gen.ConfigError):
                        gen.check_generated_reality_handshakes(self.stored_config(address), self.env())

    def test_safe_standard_nat64_target_keeps_its_numeric_ipv6_address(self):
        address = "64:ff9b::c000:21e"  # 192.0.2.30 through standard NAT64.
        env = self.env(PROXY_IP_VERSION="ipv6")
        with patch.object(gen.socket, "getaddrinfo", return_value=dns_answers(address)):
            config = gen.build_config(env, [], "test-uuid", "private-key", "abcd")
        self.assertEqual(config["inbounds"][0]["tls"]["reality"]["handshake"]["server"], address)
        with patch.object(gen.socket, "getaddrinfo", side_effect=AssertionError("unexpected DNS")):
            self.assertEqual(gen.resolve_reality_handshake_server(
                self.env(PROXY_IP_VERSION="ipv6", REALITY_HANDSHAKE_SERVER=address)), address)
            gen.check_generated_reality_handshakes(config, env)

    def test_dns_errors_empty_answers_and_family_mismatches_fail_closed(self):
        for result in (socket.gaierror("DNS unavailable"), [], dns_answers("2001:db8::30")):
            options = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
            with self.subTest(result=result), patch.object(gen.socket, "getaddrinfo", **options):
                with self.assertRaises(gen.ConfigError):
                    gen.resolve_reality_handshake_server(self.env(PROXY_IP_VERSION="ipv4"))
        with patch.object(gen.socket, "getaddrinfo", return_value=dns_answers("192.0.2.30")):
            with self.assertRaises(gen.ConfigError):
                gen.resolve_reality_handshake_server(self.env(PROXY_IP_VERSION="ipv6"))

    def test_all_reality_inbounds_share_one_resolution_and_keep_original_sni(self):
        env = self.env()
        original = copy.deepcopy(env)
        rows = [{"tag": "home", "listen_port": 52001, "source": "home", "type": "socks5",
                 "server": "proxy.example.com", "server_port": 1080, "username": "user",
                 "password": "password", "network": "tcp"}]
        with patch.object(gen.socket, "getaddrinfo", return_value=dns_answers("192.0.2.30")) as resolver:
            config = gen.build_config(env, rows, "test-uuid", "private-key", "abcd")
        resolver.assert_called_once()
        self.assertEqual(env, original)
        self.assertEqual(len(config["inbounds"]), 2)
        for inbound in config["inbounds"]:
            self.assertEqual(inbound["tls"]["reality"]["handshake"]["server"], "192.0.2.30")
            self.assertEqual(inbound["tls"]["server_name"], "www.microsoft.com")

    def test_direct_inbound_builder_also_rejects_cloudflare_target(self):
        with patch.object(gen.socket, "getaddrinfo", return_value=dns_answers("104.16.0.1")):
            with self.assertRaises(gen.ConfigError):
                gen.build_vless_reality_inbound("test", 443, "test-uuid", "private-key", "abcd", self.env())

    def test_cli_check_only_rejects_cf_dns_before_reading_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.env"
            config.write_text('SERVER_ALIAS="edge"\nSERVER_IP_IPV4="192.0.2.10"\nDIRECT_PORT="55443"\n'
                              'REALITY_HANDSHAKE_SERVER="innocent.example"\n', encoding="utf-8")
            with patch.object(sys, "argv", ["generator", "--config-env", str(config), "--check-only", "--quiet"]), \
                    patch.object(gen.socket, "getaddrinfo", return_value=dns_answers("104.16.0.1")), \
                    patch.object(gen, "read_vless_uuid", side_effect=AssertionError("unexpected secret read")):
                with self.assertRaises(gen.ConfigError):
                    gen.main()

    def test_cli_write_rejects_cf_dns_without_replacing_existing_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.env"
            generated = root / "singbox.json"
            original = b'{"previous": "working configuration"}\n'
            generated.write_bytes(original)
            config.write_text('SERVER_ALIAS="edge"\nSERVER_IP_IPV4="192.0.2.10"\nDIRECT_PORT="55443"\n'
                              'REALITY_HANDSHAKE_SERVER="innocent.example"\n'
                              f'SINGBOX_CONFIG_PATH="{generated.as_posix()}"\n', encoding="utf-8")
            with patch.object(sys, "argv", ["generator", "--config-env", str(config), "--write", "--quiet"]), \
                    patch.dict(os.environ, {"RRB_DRY_RUN": "false"}), \
                    patch.object(gen.socket, "getaddrinfo", return_value=dns_answers("104.16.0.1")), \
                    patch.object(gen, "read_vless_uuid", return_value="test-uuid"), \
                    patch.object(gen, "read_reality_private_key", return_value="private-key"), \
                    patch.object(gen, "read_reality_short_id", return_value="abcd"):
                with self.assertRaises(gen.ConfigError):
                    gen.main()
            self.assertEqual(generated.read_bytes(), original)
            self.assertFalse((root / "singbox.json.tmp").exists())

    def stored_config(self, server="127.0.0.1", port=8443):
        return {"inbounds": [{"type": "vless", "tag": "fixture", "listen_port": 55443, "tls": {
            "enabled": True, "reality": {"enabled": True, "handshake": {
                "server": server, "server_port": port}}}}]}

    def test_existing_config_checks_every_reality_inbound_without_dns(self):
        safe = self.stored_config("192.0.2.30")
        with patch.object(gen.socket, "getaddrinfo", side_effect=AssertionError("unexpected DNS")):
            gen.check_generated_reality_handshakes(safe, self.env())
            for target in ("104.16.0.1", "2606:4700::1", "::ffff:104.16.0.1", "innocent.example"):
                with self.subTest(target=target):
                    config = copy.deepcopy(safe)
                    config["inbounds"].append(self.stored_config(target)["inbounds"][0])
                    with self.assertRaises(gen.ConfigError):
                        gen.check_generated_reality_handshakes(config, self.env())

    def test_existing_custom_config_requires_exact_loopback_and_port(self):
        with patch.object(gen.socket, "getaddrinfo", side_effect=AssertionError("unexpected DNS")):
            gen.check_generated_reality_handshakes(self.stored_config(), self.custom_env())
            for server, port in (("192.0.2.30", 8443), ("localhost", 8443), ("127.0.0.1", 443)):
                with self.subTest(server=server, port=port), self.assertRaises(gen.ConfigError):
                    gen.check_generated_reality_handshakes(self.stored_config(server, port), self.custom_env())

    def test_audit_config_cli_does_not_load_rows_secrets_or_dns(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env_file, json_file = root / "config.env", root / "singbox.json"
            env_file.write_text('SERVER_ALIAS="edge"\nSERVER_IP_IPV4="192.0.2.10"\nDIRECT_PORT="55443"\n', encoding="utf-8")
            with patch.object(sys, "argv", ["generator", "--config-env", str(env_file),
                                             "--audit-config", str(json_file), "--quiet"]), \
                    patch.object(gen, "load_rows", side_effect=AssertionError("unexpected CSV read")), \
                    patch.object(gen, "read_vless_uuid", side_effect=AssertionError("unexpected secret read")), \
                    patch.object(gen.socket, "getaddrinfo", side_effect=AssertionError("unexpected DNS")):
                json_file.write_text(json.dumps(self.stored_config("192.0.2.30")), encoding="utf-8")
                self.assertEqual(gen.main(), 0)
                json_file.write_text(json.dumps(self.stored_config("104.16.0.1")), encoding="utf-8")
                with self.assertRaises(gen.ConfigError):
                    gen.main()


if __name__ == "__main__":
    unittest.main()
