from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "scripts" / "08-generate-singbox-config.py"
spec = importlib.util.spec_from_file_location("vless_upstream_gen", GENERATOR)
assert spec is not None and spec.loader is not None
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)

UUID = "11111111-1111-1111-1111-111111111111"
PLAIN_URL = f"vless://{UUID}@example.com:443?encryption=none&security=none&type=tcp"
REALITY_URL = (
    f"vless://{UUID}@example.com:443?encryption=none&security=reality"
    "&flow=xtls-rprx-vision&type=tcp&sni=www.microsoft.com"
    "&pbk=test-public-key&fp=chrome&sid=aabbccdd"
)


class VlessUpstreamTest(unittest.TestCase):
    def test_reality_upstream_relays_tcp_udp_and_preserves_options(self):
        row = gen.parse_vless_upstream(
            "reality", 51043, REALITY_URL + "&alpn=h2%2Chttp%2F1.1&insecure=1", 2
        )
        outbound = row["outbound"]

        self.assertEqual("vless", outbound["type"])
        # type=tcp selects the transport; omitted network enables TCP + UDP payloads.
        self.assertNotIn("network", outbound)
        self.assertNotIn("packet_encoding", outbound)  # Keep sing-box's default XUDP.
        self.assertEqual("out-reality", outbound["tag"])
        self.assertEqual(UUID, outbound["uuid"])
        self.assertEqual("dns-cloudflare", outbound["domain_resolver"])
        self.assertEqual("xtls-rprx-vision", outbound["flow"])
        self.assertEqual(
            {
                "enabled": True,
                "server_name": "www.microsoft.com",
                "reality": {
                    "enabled": True,
                    "public_key": "test-public-key",
                    "short_id": "aabbccdd",
                },
                "utls": {"enabled": True, "fingerprint": "chrome"},
                "alpn": ["h2", "http/1.1"],
                "insecure": True,
            },
            outbound["tls"],
        )

    def test_plain_upstream_relays_tcp_udp(self):
        row = gen.parse_vless_upstream("plain", 51044, PLAIN_URL, 3)
        outbound = row["outbound"]

        self.assertEqual("vless", outbound["type"])
        self.assertNotIn("network", outbound)
        self.assertNotIn("packet_encoding", outbound)
        self.assertNotIn("tls", outbound)
        self.assertNotIn("flow", outbound)
        self.assertEqual(("plain", 51044, "upstream"), (row["tag"], row["listen_port"], row["source"]))

    def test_tcp_transport_defaults_alias_and_server_addresses(self):
        for url in (PLAIN_URL, REALITY_URL):
            for authority, host in (
                ("example.com", "example.com"),
                ("192.0.2.10", "192.0.2.10"),
                ("[2001:db8::10]", "2001:db8::10"),
            ):
                for transport in ("&type=tcp", "&transport=tcp", ""):
                    with self.subTest(url=url, host=host, transport=transport):
                        node_url = url.replace("example.com", authority).replace("&type=tcp", transport)
                        outbound = gen.parse_vless_upstream("relay", 51043, node_url, 2)["outbound"]

                        self.assertEqual(host, outbound["server"])
                        self.assertEqual(443, outbound["server_port"])
                        self.assertNotIn("network", outbound)
                        self.assertNotIn("transport", outbound)  # Native TCP needs no V2Ray transport.

    def test_unsupported_transports_are_rejected(self):
        for url in (PLAIN_URL, REALITY_URL):
            for key in ("type", "transport"):
                for transport in ("ws", "grpc", "udp"):
                    with self.subTest(url=url, key=key, transport=transport):
                        node_url = url.replace("type=tcp", f"{key}={transport}")
                        with self.assertRaisesRegex(gen.ConfigError, "只支持 type=tcp"):
                            gen.parse_vless_upstream("relay", 51043, node_url, 2)

    def test_existing_file_formats_keep_tcp_udp_and_port_allocation(self):
        formats = {
            "csv": f"tag,node_url,listen_port\nreality,{REALITY_URL},\nplain,{PLAIN_URL},51049\n",
            "legacy_csv": f"tag,listen_port,node_url\nreality,,{REALITY_URL}\nplain,51049,{PLAIN_URL}\n",
            "raw_urls": f"{REALITY_URL}#reality\n{PLAIN_URL}#plain\n",
        }
        env = gen.defaults({})
        for name, content in formats.items():
            with self.subTest(format=name), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                (base / "home-proxies.csv").write_text(
                    "tag,listen_port,type,server,server_port,username,password,network\n", encoding="utf-8"
                )
                (base / "upstream-nodes.txt").write_text(content, encoding="utf-8")
                rows = gen.load_rows(env, base)
                ports = [51043, 51044 if name == "raw_urls" else 51049]

                self.assertEqual(["reality", "plain"], [row["tag"] for row in rows])
                self.assertEqual(ports, [row["listen_port"] for row in rows])
                self.assertEqual([443, *ports], gen.port_list(env, rows))
                for row in rows:
                    self.assertNotIn("network", row["outbound"])

    def test_cli_writes_tcp_udp_outbounds_and_fixed_routes(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "home-proxies.csv").write_text(
                "tag,listen_port,type,server,server_port,username,password,network\n", encoding="utf-8"
            )
            (base / "upstream-nodes.txt").write_text(
                f"{REALITY_URL}#reality\n{PLAIN_URL}#plain\n", encoding="utf-8"
            )
            for filename, value in (
                ("uuid.txt", UUID), ("private.key", "test-private-key"), ("short-id.txt", "aabbccdd")
            ):
                (base / filename).write_text(value, encoding="utf-8")
            settings = {
                "VLESS_UUID_PATH": str(base / "uuid.txt"),
                "REALITY_PRIVATE_KEY_PATH": str(base / "private.key"),
                "REALITY_SHORT_ID_PATH": str(base / "short-id.txt"),
                "SINGBOX_CONFIG_PATH": str(base / "config.json"),
            }
            env_path = base / "config.env"
            env_path.write_text(
                "".join(f"{key}='{value}'\n" for key, value in settings.items()), encoding="utf-8"
            )
            process_env = dict(os.environ, RRB_DRY_RUN="false")
            result = subprocess.run(
                [sys.executable, str(GENERATOR), "--config-env", str(env_path), "--write", "--quiet"],
                capture_output=True, text=True, encoding="utf-8", env=process_env,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            config = json.loads((base / "config.json").read_text(encoding="utf-8"))
            vless_outbounds = [item for item in config["outbounds"] if item["type"] == "vless"]
            self.assertEqual(["out-reality", "out-plain"], [item["tag"] for item in vless_outbounds])
            for outbound in vless_outbounds:
                self.assertNotIn("network", outbound)
            self.assertEqual("xtls-rprx-vision", vless_outbounds[0]["flow"])
            self.assertTrue(vless_outbounds[0]["tls"]["reality"]["enabled"])
            self.assertEqual(
                [
                    {"inbound": "vless-direct-443", "action": "route", "outbound": "direct"},
                    {"inbound": "vless-reality", "action": "route", "outbound": "out-reality"},
                    {"inbound": "vless-plain", "action": "route", "outbound": "out-plain"},
                ],
                config["route"]["rules"],
            )
            self.assertEqual([443, 51043, 51044], [item["listen_port"] for item in config["inbounds"]])


if __name__ == "__main__":
    unittest.main()
