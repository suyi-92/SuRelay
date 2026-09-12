from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[1]


def load_module(relative_path: str, module_name: str):
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class IPv6ConfigUpdateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.updater = load_module("scripts/16-update-ipv6-config.py", "ipv6_config_updater")

    def test_updates_only_ipv6_listener_and_client_safety_fields(self):
        original = """\
SERVER_IP_IPV4="192.0.2.10"
export SERVER_IP_IPV6=""
ENABLE_IPV6_LISTEN="false"
PROXY_IP_VERSION="ipv4"
CLIENT_UDP="false"
RESET_PROXY_KEYS="true"
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Path(tmpdir) / "config.env"
            config.write_text(original, encoding="utf-8")

            updates = self.updater.update_config(config, "[2001:0db8::10]")
            content = config.read_text(encoding="utf-8")

            self.assertEqual(updates["SERVER_IP_IPV6"], "2001:db8::10")
            self.assertIn('export SERVER_IP_IPV6="2001:db8::10"', content)
            self.assertIn('ENABLE_IPV6_LISTEN="true"', content)
            self.assertIn('CLIENT_UDP="true"', content)
            self.assertIn('RESET_PROXY_KEYS="false"', content)
            self.assertIn('PROXY_IP_VERSION="ipv4"', content)
            self.assertIn('SERVER_IP_IPV4="192.0.2.10"', content)

    def test_missing_fields_are_appended_once(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Path(tmpdir) / "config.env"
            config.write_text('SERVER_ALIAS="test"\n', encoding="utf-8")

            self.updater.update_config(config, "2001:db8::20")
            self.updater.update_config(config, "2001:db8::21")
            content = config.read_text(encoding="utf-8")

            for key in ["SERVER_IP_IPV6", "ENABLE_IPV6_LISTEN", "CLIENT_UDP", "RESET_PROXY_KEYS"]:
                self.assertEqual(content.count(f"{key}="), 1)
            self.assertIn('SERVER_IP_IPV6="2001:db8::21"', content)

    def test_rejects_non_ipv6_and_non_public_listener_classes(self):
        for value in ["192.0.2.1", "::", "::1", "fe80::1", "ff02::1"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.updater.normalize_ipv6(value)


class IPv6NodeOutputTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.output = load_module("scripts/11-output-nodes.py", "node_output")

    @staticmethod
    def node():
        return {
            "name": "relay-IPv6",
            "type": "vless",
            "server": "2001:db8::10",
            "port": 443,
            "uuid": "11111111-2222-4333-8444-555555555555",
            "flow": "xtls-rprx-vision",
            "tls": True,
            "servername": "www.microsoft.com",
            "client-fingerprint": "chrome",
            "reality-opts": {"public-key": "public-key", "short-id": "abcd"},
            "udp": True,
            "alpn": ["h2", "http/1.1"],
        }

    def test_vless_uri_uses_bracketed_ipv6_and_reality_fields(self):
        uri = self.output.node_to_vless_uri(self.node())
        split = urlsplit(uri)
        query = parse_qs(split.query)

        self.assertEqual(split.scheme, "vless")
        self.assertEqual(split.hostname, "2001:db8::10")
        self.assertEqual(split.port, 443)
        self.assertEqual(split.username, "11111111-2222-4333-8444-555555555555")
        self.assertEqual(query["security"], ["reality"])
        self.assertEqual(query["flow"], ["xtls-rprx-vision"])
        self.assertEqual(query["type"], ["tcp"])
        self.assertEqual(query["sni"], ["www.microsoft.com"])
        self.assertEqual(query["fp"], ["chrome"])
        self.assertEqual(query["pbk"], ["public-key"])
        self.assertEqual(query["sid"], ["abcd"])
        self.assertEqual(query["spx"], ["/"])

    def test_mihomo_v6_output_explicitly_enables_ipv6_udp_and_tcp_transport(self):
        env = {"CLASH_MIXED_PORT": "7890", "PROXY_IP_VERSION": "ipv4"}
        yaml = self.output.build_clash_yaml(env, [self.node()], clash_ipv6="true")

        self.assertIn("ipv6: true", yaml)
        self.assertIn("type: vless", yaml)
        self.assertIn('server: "2001:db8::10"', yaml)
        self.assertIn("network: tcp", yaml)
        self.assertIn("udp: true", yaml)

    def test_explicit_ipv6_override_wins_in_custom_domain_mode(self):
        env = {
            "ENABLE_CUSTOM_DOMAIN": "true",
            "CUSTOM_DOMAIN": "edge.example.com",
            "SERVER_IP": "192.0.2.10",
        }
        self.assertEqual(self.output.get_server_ip(env, "2001:db8::10"), "edge.example.com")
        self.assertEqual(
            self.output.get_server_ip(env, "2001:db8::10", force_server_override=True),
            "2001:db8::10",
        )
        self.assertEqual(self.output.get_server_ip(env), "edge.example.com")

    def test_cli_writes_v6_only_text_yaml_and_vless_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            uuid_path = root / "vless-uuid.txt"
            public_key_path = root / "reality-public.key"
            short_id_path = root / "reality-short-id.txt"
            home_csv = root / "home-proxies.csv"
            config = root / "config.env"
            nodes_out = root / "v6-nodes.txt"
            clash_out = root / "v6-clash.yaml"
            vless_out = root / "v6-vless.txt"

            uuid_path.write_text("11111111-2222-4333-8444-555555555555\n", encoding="utf-8")
            public_key_path.write_text("public-key\n", encoding="utf-8")
            short_id_path.write_text("abcd\n", encoding="utf-8")
            home_csv.write_text(
                "tag,type,server,server_port,username,password,network,listen_port\n",
                encoding="utf-8",
            )
            config.write_text(
                "\n".join(
                    [
                        'SERVER_ALIAS="relay"',
                        'SERVER_IP_IPV4="192.0.2.10"',
                        'SERVER_IP_IPV6="2001:db8::10"',
                        'ENABLE_SUBSCRIPTION_SERVER="false"',
                        'ENABLE_IPV6_LISTEN="true"',
                        'CLIENT_UDP="true"',
                        f'CSV_PATH="{home_csv.as_posix()}"',
                        f'RRB_STATE_DIR="{root.as_posix()}"',
                        'CLOUDFLARE_API_TOKEN_FILE="/tmp/test-cloudflare.ini"',
                        f'VLESS_UUID_PATH="{uuid_path.as_posix()}"',
                        f'REALITY_PUBLIC_KEY_PATH="{public_key_path.as_posix()}"',
                        f'REALITY_SHORT_ID_PATH="{short_id_path.as_posix()}"',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/11-output-nodes.py"),
                    "--config-env",
                    str(config),
                    "--generator",
                    str(ROOT / "scripts/08-generate-singbox-config.py"),
                    "--server-override",
                    "2001:db8::10",
                    "--force-server-override",
                    "--name-suffix=-IPv6",
                    "--clash-ipv6",
                    "true",
                    "--nodes-out",
                    str(nodes_out),
                    "--clash-out",
                    str(clash_out),
                    "--vless-out",
                    str(vless_out),
                    "--quiet",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("server=2001:db8::10", nodes_out.read_text(encoding="utf-8"))
            self.assertIn("udp=true", nodes_out.read_text(encoding="utf-8"))
            self.assertIn('server: "2001:db8::10"', clash_out.read_text(encoding="utf-8"))
            self.assertIn("ipv6: true", clash_out.read_text(encoding="utf-8"))
            self.assertTrue(vless_out.read_text(encoding="utf-8").startswith("vless://"))
            self.assertIn("@[2001:db8::10]:443?", vless_out.read_text(encoding="utf-8"))


class IPv6InstallerContractTest(unittest.TestCase):
    def test_one_click_short_option_uses_existing_deployment_flow(self):
        installer = (ROOT / "install.sh").read_text(encoding="utf-8")
        maintenance = (ROOT / "scripts/16-add-ipv6-node.sh").read_text(encoding="utf-8")

        self.assertIn("-6|--add-ipv6-node", installer)
        self.assertIn('INSTALL_MODE="ipv6-node"', installer)
        self.assertIn("16-add-ipv6-node.sh", installer)
        self.assertIn("--name-suffix=-IPv6", maintenance)
        self.assertIn("--force-server-override", maintenance)
        self.assertIn("--clash-ipv6 true", maintenance)
        self.assertIn('wait_for_local_tcp_family 6 "$port"', maintenance)
        self.assertIn('wait_for_local_tcp_family 4 "$port"', maintenance)


if __name__ == "__main__":
    unittest.main()
