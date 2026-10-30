from __future__ import annotations

import base64
import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GEN = load("protocol_gen", "08-generate-singbox-config.py")
OUTPUT = load("protocol_output", "11-output-nodes.py")
UUID = "11111111-2222-4333-8444-555555555555"


class ProtocolTest(unittest.TestCase):
    def env(self, **updates):
        return GEN.defaults({"SERVER_IP_IPV4": "192.0.2.10", "ENABLE_CUSTOM_DOMAIN": "true",
                             "CUSTOM_DOMAIN": "edge.example.com", "LE_EMAIL": "admin@example.com", **updates})

    def all_protocols(self, **updates):
        return self.env(ENABLE_HY2="true", ENABLE_SOCKS5="true", ENABLE_HTTP="true",
                        ENABLE_SHADOWSOCKS="true", **updates)

    def nodes(self, env, rows=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {"VLESS_UUID_PATH": UUID, "REALITY_PUBLIC_KEY_PATH": "public-key",
                     "REALITY_SHORT_ID_PATH": "abcd"}
            for key, value in paths.items():
                path = root / key
                path.write_text(value, encoding="utf-8")
                env[key] = str(path)
            return OUTPUT.build_client_nodes(env, rows or [], GEN)

    def test_legacy_defaults_keep_only_reality(self):
        env = self.env()
        GEN.validate_env(env)
        config = GEN.build_config(env, [], UUID, "private-key", "abcd")
        self.assertEqual([item["type"] for item in config["inbounds"]], ["vless"])
        self.assertEqual(GEN.transport_ports(env, []), [(443, "tcp")])

    def test_all_inbounds_authenticate_and_match_exported_credentials(self):
        env = self.all_protocols()
        GEN.validate_env(env)
        config = GEN.build_config(env, [], UUID, "private-key", "abcd")
        inbounds = {item["type"]: item for item in config["inbounds"]}
        self.assertEqual(set(inbounds), {"vless", "hysteria2", "socks", "shadowsocks", "http"})
        nodes = {item["type"]: item for item in self.nodes(env)}
        for protocol, inbound_type in (("hysteria2", "hysteria2"), ("socks5", "socks"), ("http", "http")):
            self.assertEqual(nodes[protocol]["password"], inbounds[inbound_type]["users"][0]["password"])
        self.assertEqual(nodes["ss"]["password"], inbounds["shadowsocks"]["password"])
        self.assertEqual(inbounds["hysteria2"]["tls"]["certificate_path"],
                         "/etc/letsencrypt/live/edge.example.com/fullchain.pem")
        self.assertFalse(nodes["hysteria2"]["skip-cert-verify"])
        self.assertEqual(len(config["route"]["rules"]), 5)
        self.assertTrue(all(rule["outbound"] == "direct" for rule in config["route"]["rules"]))

    def test_credentials_are_stable_separate_and_explicit_values_win(self):
        env = self.all_protocols()
        first = GEN.protocol_password(env, "hysteria2", UUID)
        self.assertEqual(first, GEN.protocol_password(dict(env), "hysteria2", UUID))
        self.assertNotEqual(first, GEN.protocol_password(env, "ss", UUID))
        self.assertNotEqual(first, GEN.protocol_password(env, "hysteria2", "new-identity"))
        self.assertEqual(GEN.protocol_password(env, "socks5", UUID), GEN.protocol_password(env, "http", UUID))
        self.assertEqual(GEN.protocol_password({**env, "HY2_PASSWORD": "explicit"}, "hysteria2", UUID), "explicit")

    def test_transport_catalog_tracks_udp_and_socks_tcp(self):
        env = self.all_protocols()
        self.assertEqual(GEN.transport_ports(env, []),
                         [(443, "tcp"), (51080, "tcp"), (54443, "udp"), (58080, "tcp"),
                          (58388, "tcp"), (58388, "udp")])
        no_udp = self.all_protocols(CLIENT_UDP="false")
        self.assertNotIn((58388, "udp"), GEN.transport_ports(no_udp, []))
        self.assertEqual(next(item for item in GEN.build_config(no_udp, [], UUID, "key", "abcd")["inbounds"]
                              if item["type"] == "shadowsocks")["network"], "tcp")
        self.assertFalse(next(item for item in self.nodes(env) if item["type"] == "socks5")["udp"])

    def test_service_and_protocol_port_collisions_are_rejected(self):
        for updates in ({"SOCKS5_PORT": "22"}, {"HTTP_PORT": "51080"}, {"HY2_PORT": "443"},
                        {"SHADOWSOCKS_PORT": "8443"}, {"HTTP_PORT": "51040"}):
            with self.subTest(updates=updates), self.assertRaises(GEN.ConfigError):
                GEN.validate_env(self.all_protocols(**updates))

    def test_hy2_requires_certificate_and_explicit_insecure_choice(self):
        env = GEN.defaults({"ENABLE_HY2": "true"})
        with self.assertRaisesRegex(GEN.ConfigError, "HY2_CERT_PATH"):
            GEN.validate_env(env)
        env.update(HY2_CERT_PATH="/private/cert.pem", HY2_KEY_PATH="/private/key.pem", HY2_SERVER_NAME="edge.example.com")
        GEN.validate_env(env)
        self.assertFalse(GEN.as_bool(env, "HY2_INSECURE"))
        env["HY2_INSECURE"] = "true"
        with contextlib.redirect_stderr(io.StringIO()) as warning:
            GEN.validate_env(env)
        self.assertIn("跳过证书校验", warning.getvalue())

    def test_uri_encoding_preserves_passwords_ipv6_and_tls_policy(self):
        env = self.all_protocols(PROXY_PASSWORD="p:/@? #&密钥", SHADOWSOCKS_PASSWORD="ss:/@? #&密钥",
                                 HY2_PASSWORD="hy:/@? #&密钥", SERVER_IP_IPV6="2001:db8::1", ENABLE_IPV6_LISTEN="true")
        nodes = self.nodes(env)
        self.assertEqual(len(nodes), 10)
        for node in nodes:
            uri = OUTPUT.node_to_uri(node)
            parsed = urlsplit(uri)
            self.assertEqual(parsed.hostname, node["server"])
            self.assertEqual(parsed.port, node["port"])
            self.assertEqual(unquote(parsed.fragment), node["name"])
            if node["type"] in {"socks5", "http"}:
                self.assertEqual(unquote(parsed.password), env["PROXY_PASSWORD"])
            elif node["type"] == "ss":
                data = parsed.username
                self.assertEqual(base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode(),
                                 f"{env['SHADOWSOCKS_METHOD']}:{env['SHADOWSOCKS_PASSWORD']}")
            elif node["type"] == "hysteria2":
                self.assertEqual(unquote(parsed.username), env["HY2_PASSWORD"])
                self.assertEqual(parse_qs(parsed.query)["insecure"], ["0"])

    def test_client_formats_include_every_enabled_protocol(self):
        env = self.all_protocols()
        nodes = self.nodes(env)
        yaml = OUTPUT.build_clash_yaml(env, nodes)
        for protocol in ("vless", "hysteria2", "socks5", "ss", "http"):
            self.assertIn(f"    type: {protocol}", yaml)
        client = OUTPUT.build_singbox_client_config(env, nodes)
        self.assertEqual({item["type"] for item in client["outbounds"]},
                         {"selector", "vless", "hysteria2", "socks", "shadowsocks", "http"})
        hysteria = next(item for item in client["outbounds"] if item["type"] == "hysteria2")
        self.assertFalse(hysteria["tls"]["insecure"])

    def test_empty_proxy_user_and_unknown_shadowsocks_method_are_rejected(self):
        for updates in ({"PROXY_USERNAME": ""}, {"PROXY_USERNAME": "a:b"}, {"SHADOWSOCKS_METHOD": "none"}):
            with self.subTest(updates=updates), self.assertRaises(GEN.ConfigError):
                GEN.validate_env(self.all_protocols(**updates))

    def test_auto_assigned_relay_ports_skip_extra_protocols(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "home-proxies.csv").write_text(
                "tag,type,server,server_port,username,password,network,listen_port\n"
                "home,socks5,proxy.example.com,1080,user,password,tcp,\n", encoding="utf-8")
            env = self.all_protocols(SOCKS5_PORT="51043")
            self.assertEqual(GEN.load_rows(env, root)[0]["listen_port"], 51044)

    def test_export_names_are_unique_across_protocols_ipv6_and_reserved_names(self):
        env = self.all_protocols(SERVER_ALIAS="edge", SERVER_IP_IPV6="2001:db8::1", ENABLE_IPV6_LISTEN="true")
        tags = ["edge-SS", "edge-SS-2", "edge-IPv6", "PROXY", "DIRECT"]
        rows = [{"tag": tag, "listen_port": 52000 + index, "source": "home"} for index, tag in enumerate(tags)]
        nodes = self.nodes(env, rows)
        names = [node["name"] for node in nodes]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(set(names).isdisjoint(OUTPUT.RESERVED_NODE_NAMES))
        self.assertEqual(names, [node["name"] for node in self.nodes(env, rows)])
        # Keep primary legacy labels even when an IPv6 copy or new protocol
        # would otherwise take the same name.
        primary_vless = [node["name"] for node in nodes if node["type"] == "vless" and node["server"] == "edge.example.com"]
        self.assertEqual(primary_vless[:4], ["edge", "edge-SS", "edge-SS-2", "edge-IPv6"])
        ss = [node for node in nodes if node["type"] == "ss"]
        self.assertNotIn(ss[0]["name"], tags)
        yaml = OUTPUT.build_clash_yaml(env, nodes)
        self.assertEqual(sum(line.startswith("  - name:") for line in yaml.splitlines()), len(nodes) + 1)
        outbounds = OUTPUT.build_singbox_client_config(env, nodes)["outbounds"]
        self.assertEqual(len(outbounds), len({item["tag"] for item in outbounds}))
        self.assertEqual([unquote(urlsplit(OUTPUT.node_to_uri(node)).fragment) for node in nodes], names)

    def test_high_direct_port_does_not_produce_warning(self):
        with contextlib.redirect_stderr(io.StringIO()) as warning:
            GEN.validate_env(self.env(DIRECT_PORT="53210"))
        self.assertEqual(warning.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
