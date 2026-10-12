from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]


class ClientOutputTest(unittest.TestCase):
    def generate(self, directory, **overrides):
        root = Path(directory)
        (root / "vless-uuid.txt").write_text("11111111-2222-4333-8444-555555555555\n")
        (root / "reality-public.key").write_text("test-public-key\n")
        (root / "reality-short-id.txt").write_text("abcd\n")
        (root / "home-proxies.csv").write_text(
            "tag,type,server,server_port,username,password,network,listen_port\n"
            "home-01,socks5,proxy.example.com,1080,user,private-upstream-password,tcp,51043\n"
        )
        (root / "upstream-nodes.txt").write_text(
            "tag,node_url,listen_port\n"
            "relay-01,vless://aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee@upstream.example.com:443?security=none&type=tcp,51044\n"
        )
        env = {
            "SERVER_ALIAS": "edge", "SERVER_IP_IPV4": "192.0.2.10", "SERVER_IP_IPV6": "2001:db8::10",
            "ENABLE_IPV6_LISTEN": "true", "PROXY_IP_VERSION": "ipv4",
            "ENABLE_CUSTOM_DOMAIN": "true", "CUSTOM_DOMAIN": "edge.example.com", "LE_EMAIL": "admin@example.com",
            "RRB_STATE_DIR": root.as_posix(), "CSV_PATH": (root / "home-proxies.csv").as_posix(),
            "UPSTREAM_NODES_PATH": (root / "upstream-nodes.txt").as_posix(),
        }
        env.update(overrides)
        config = root / "config.env"
        config.write_text("\n".join(f"{key}={json.dumps(value)}" for key, value in env.items()) + "\n")
        result = subprocess.run([
            sys.executable, "-B", str(ROOT / "scripts/11-output-nodes.py"),
            "--config-env", str(config), "--generator", str(ROOT / "scripts/08-generate-singbox-config.py"),
            "--subscription-out", str(root / "subscription.yaml"), "--skip-node-files", "--quiet", "--print-summary",
        ], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        yaml = (root / "subscription.yaml").read_text(encoding="utf-8")
        links = [urlsplit(line) for line in result.stdout.splitlines() if line.startswith("vless://")]
        yaml_servers = [json.loads(line.strip().split(": ", 1)[1]) for line in yaml.splitlines() if line.startswith("    server: ")]
        self.assertEqual([link.hostname for link in links], yaml_servers)
        self.assertNotIn("private-upstream-password", result.stdout + yaml)
        self.assertNotIn("upstream.example.com", result.stdout + yaml)
        return result.stdout, yaml, links

    def test_domain_subscription_and_local_links_include_ipv6_for_every_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            output, yaml, links = self.generate(directory)
            self.assertEqual([link.hostname for link in links], ["edge.example.com", "2001:db8::10"] * 3)
            self.assertEqual([link.port for link in links], [443, 443, 51043, 51043, 51044, 51044])
            self.assertEqual([unquote(link.fragment) for link in links], ["edge", "edge-IPv6", "home-01", "home-01-IPv6", "relay-01", "relay-01-IPv6"])
            self.assertTrue(all(parse_qs(link.query)["sni"] == ["edge.example.com"] for link in links))
            self.assertIn("ipv6: true", yaml)
            self.assertEqual(sum(line.startswith("https://") for line in output.splitlines()), 1)
            self.assertIn("https://edge.example.com/sub/11111111-2222-4333-8444-555555555555?target=ClashMeta", output)
            self.assertIn("IPv6 节点：已生成", output)

    def test_nat_custom_domain_port_matches_uri_yaml_and_subscription(self):
        with tempfile.TemporaryDirectory() as directory:
            output, yaml, links = self.generate(directory, DIRECT_PORT="24037", SERVER_IP_IPV6="", ENABLE_IPV6_LISTEN="false")
            self.assertEqual(links[0].port, 24037)
            self.assertEqual(links[0].hostname, "edge.example.com")
            self.assertIn("port: 24037", yaml)
            self.assertIn("https://edge.example.com:24037/sub/", output)
            self.assertNotIn("https://edge.example.com/sub/", output)
            self.assertEqual([link.port for link in links[1:]], [51043, 51044])

    def test_ipv4_only_keeps_names_and_does_not_advertise_ipv6(self):
        with tempfile.TemporaryDirectory() as directory:
            output, yaml, links = self.generate(directory, ENABLE_CUSTOM_DOMAIN="false", SERVER_IP_IPV6="", ENABLE_IPV6_LISTEN="false")
            self.assertEqual([link.hostname for link in links], ["192.0.2.10"] * 3)
            self.assertEqual([link.fragment for link in links], ["edge", "home-01", "relay-01"])
            self.assertIn("ipv6: false", yaml)
            self.assertIn("http://192.0.2.10:51040/sub/", output)
            self.assertIn("IPv6 节点：未配置公网地址", output)
            self.assertIn(f"cd {shlex.quote(directory)} && sudo bash install.sh -6", output)

    def test_ipv6_only_brackets_subscription_and_local_addresses(self):
        with tempfile.TemporaryDirectory() as directory:
            output, yaml, links = self.generate(directory, ENABLE_CUSTOM_DOMAIN="false", SERVER_IP_IPV4="")
            self.assertEqual([link.hostname for link in links], ["2001:db8::10"] * 3)
            self.assertTrue(all(link.fragment.endswith("-IPv6") for link in links))
            self.assertIn("http://[2001:db8::10]:51040/sub/", output)
            self.assertIn("ipv6: true", yaml)

    def test_disabled_listener_has_no_ipv6_backup_and_disabled_subscription_has_no_url(self):
        with tempfile.TemporaryDirectory() as directory:
            output, _, links = self.generate(directory, ENABLE_IPV6_LISTEN="false", ENABLE_SUBSCRIPTION_SERVER="false")
            self.assertEqual([link.hostname for link in links], ["edge.example.com"] * 3)
            self.assertFalse(any(line.startswith(("https://", "http://")) for line in output.splitlines()))
            self.assertIn("IPv6 节点：未启用监听", output)
            self.assertIn("sudo bash install.sh -6 2001:db8::10", output)

    def test_summary_colors_only_interactive_terminal_and_preserves_link_text(self):
        spec = importlib.util.spec_from_file_location("node_output", ROOT / "scripts/11-output-nodes.py")
        output_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(output_module)
        with tempfile.TemporaryDirectory() as directory:
            plain, _, _ = self.generate(directory)
            root = Path(directory)
            gen = output_module.load_generator(ROOT / "scripts/08-generate-singbox-config.py")
            env = gen.defaults(gen.parse_env_file(root / "config.env"))
            nodes = output_module.build_client_nodes(env, gen.load_rows(env, root, allow_empty=True), gen)
            for tty, term, no_color, colored in (
                (True, "xterm-256color", "", True),
                (False, "xterm-256color", "", False),
                (True, "dumb", "", False),
                (True, "xterm-256color", "1", False),
            ):
                with self.subTest(tty=tty, term=term, no_color=no_color):
                    stream = io.StringIO()
                    with patch("sys.stdout", stream), patch.object(stream, "isatty", return_value=tty), patch.dict(
                        os.environ, {"TERM": term, "NO_COLOR": no_color}
                    ):
                        output_module.print_client_summary(env, nodes, root)
                    rendered = stream.getvalue()
                    self.assertEqual(re.sub(r"\x1b\[[0-9;]*m", "", rendered), plain)
                    if colored:
                        self.assertIn("\033[36mhttps://", rendered)
                        self.assertIn("\033[32mvless://", rendered)
                    else:
                        self.assertNotIn("\033[", rendered)


if __name__ == "__main__":
    unittest.main()
