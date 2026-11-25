from __future__ import annotations

import copy
import ipaddress
import unittest

from tests.test_vless_upstream import PLAIN_URL, REALITY_URL, UUID, gen


class IPFamilyRoutingTest(unittest.TestCase):
    def env(self, **updates):
        return gen.defaults({
            "SERVER_ALIAS": "edge",
            "SERVER_IP_IPV4": "192.0.2.10",
            "SERVER_IP_IPV6": "2001:db8::10",
            "ENABLE_IPV6_LISTEN": "true",
            "ENABLE_HY2": "true",
            "ENABLE_SOCKS5": "true",
            "ENABLE_SHADOWSOCKS": "true",
            "ENABLE_HTTP": "true",
            "HY2_CERT_PATH": "/tmp/surelay-test-cert.pem",
            "HY2_KEY_PATH": "/tmp/surelay-test-key.pem",
            "HY2_SERVER_NAME": "edge.example.com",
            "REALITY_HANDSHAKE_SERVER": "127.0.0.1",
            "WEB_SOURCE_ALLOWLIST": "192.0.2.0/24",
            "PROXY_SOURCE_ALLOWLIST": "192.0.2.0/24 2001:db8::/32",
            **updates,
        })

    def rows(self):
        home = [
            {"tag": "home", "listen_port": 52001, "source": "home", "type": "socks5",
             "server": "proxy.example.com", "server_port": 1080, "username": "user",
             "password": "password", "network": "tcp"},
            {"tag": "web", "listen_port": 52002, "source": "home", "type": "http",
             "server": "192.0.2.20", "server_port": 8080, "username": "user",
             "password": "password", "network": "tcp"},
        ]
        return [*home, gen.parse_vless_upstream("plain", 52003, PLAIN_URL, 2),
                gen.parse_vless_upstream("reality", 52004, REALITY_URL, 3)]

    def build(self, env=None, rows=None):
        return gen.build_config(env or self.env(), rows if rows is not None else self.rows(),
                                UUID, "private-key", "aabbccdd")

    def test_dns_transports_use_numeric_addresses_of_each_family_and_separate_cache(self):
        dns = gen.build_dns_config(self.env(), cache_capacity=512)
        servers = {item["tag"]: item for item in dns["servers"]}
        self.assertEqual(len(servers), 4)
        self.assertEqual(dns["cache_capacity"], 512)
        self.assertTrue(dns["independent_cache"])
        addresses = {item["server"] for item in servers.values()}
        self.assertEqual(addresses, {"1.1.1.1", "8.8.8.8", "2606:4700:4700::1111",
                                     "2001:4860:4860::8888"})
        for family, version in (("ipv4", 4), ("ipv6", 6)):
            with self.subTest(family=family):
                resolver = servers[gen.dns_server_tag(family)]
                self.assertEqual(ipaddress.ip_address(resolver["server"]).version, version)
        for server in servers.values():
            with self.subTest(tag=server["tag"]):
                self.assertEqual(server["type"], "https")
                self.assertEqual(server["server_port"], 443)
                self.assertEqual(server["path"], "/dns-query")
                self.assertFalse(server.get("tls", {}).get("insecure", False))

    def test_default_resolver_preserves_configured_policy_independently_of_listener(self):
        for policy in ("ipv4", "ipv6", "dual"):
            for listen in ("false", "true"):
                with self.subTest(policy=policy, listen=listen):
                    env = self.env(PROXY_IP_VERSION=policy, ENABLE_IPV6_LISTEN=listen)
                    family = "ipv6" if policy == "ipv6" else "ipv4"
                    expected = {"server": gen.dns_server_tag(family),
                                "strategy": gen.dns_strategy(env)}
                    self.assertEqual(gen.default_domain_resolver(env), expected)
                    dns = gen.build_dns_config(env)
                    self.assertEqual(dns["final"], expected["server"])
                    self.assertEqual(dns["strategy"], expected["strategy"])
                    self.assertEqual(self.build(env)["route"]["default_domain_resolver"], expected)

    def test_family_outbounds_copy_nested_options_without_mutating_input(self):
        original = gen.build_home_outbound(self.rows()[-1])
        snapshot = copy.deepcopy(original)
        ipv4 = gen.family_outbound(original, "ipv4")
        ipv6 = gen.family_outbound(original, "ipv6")
        self.assertEqual(ipv4["tag"], original["tag"])
        self.assertEqual(ipv6["tag"], original["tag"] + "/ipv6")
        for family, outbound in (("ipv4", ipv4), ("ipv6", ipv6)):
            with self.subTest(family=family):
                self.assertEqual(outbound["domain_resolver"], {
                    "server": gen.dns_server_tag(family), "strategy": "prefer_" + family})
                self.assertEqual(outbound["tls"], original["tls"])
        ipv6["tls"]["reality"]["short_id"] = "ffff"
        self.assertEqual(original, snapshot)
        self.assertEqual(ipv4["tls"], snapshot["tls"])

    def test_all_protocols_and_upstreams_keep_endpoints_and_separate_routes(self):
        env = self.env()
        rows = self.rows()
        original_env, original_rows = copy.deepcopy(env), copy.deepcopy(rows)
        config = self.build(env, rows)
        self.assertEqual(env, original_env)
        self.assertEqual(rows, original_rows)
        inbounds = config["inbounds"]
        tags = [item["tag"] for item in inbounds]
        self.assertEqual(len(tags), 9)
        self.assertEqual({item["type"] for item in inbounds},
                         {"vless", "hysteria2", "socks", "shadowsocks", "http"})
        self.assertTrue(all(item["listen"] == "::" for item in inbounds))
        rules = config["route"]["rules"]
        self.assertEqual(len(rules), 2 + 2 * len(inbounds))
        self.assertTrue(all(rule["action"] == "resolve" for rule in rules[:2]))
        for family, cidr in (("ipv4", "0.0.0.0/0"), ("ipv6", "::/0")):
            with self.subTest(resolve_family=family):
                resolve = next(rule for rule in rules[:2] if rule["source_ip_cidr"] == [cidr])
                self.assertEqual(set(resolve["inbound"]), set(tags))
                self.assertEqual(resolve["server"], gen.dns_server_tag(family))
                self.assertEqual(resolve["strategy"], "prefer_" + family)
        expected_ports = {"vless-direct-443": int(env["DIRECT_PORT"]),
                          **{f"vless-{row['tag']}": row["listen_port"] for row in rows},
                          **{f"{endpoint['type']}-direct": endpoint["port"]
                             for endpoint in gen.protocol_endpoints(env)}}
        self.assertEqual({item["tag"]: item["listen_port"] for item in inbounds}, expected_ports)
        outbounds = {item["tag"]: item for item in config["outbounds"]}
        upstream_tags = {f"vless-{row['tag']}": f"out-{row['tag']}" for row in rows}
        for index, inbound in enumerate(inbounds):
            original_outbound = upstream_tags.get(inbound["tag"], "direct")
            ipv6_rule, ipv4_rule = rules[2 + 2 * index:4 + 2 * index]
            with self.subTest(inbound=inbound["tag"]):
                for family, cidr, rule, outbound_tag in (
                    ("ipv6", "::/0", ipv6_rule, original_outbound + "/ipv6"),
                    ("ipv4", "0.0.0.0/0", ipv4_rule, original_outbound),
                ):
                    self.assertEqual(rule, {"inbound": inbound["tag"], "source_ip_cidr": [cidr],
                                            "action": "route", "outbound": outbound_tag})
                    self.assertEqual(outbounds[outbound_tag]["domain_resolver"], {
                        "server": gen.dns_server_tag(family), "strategy": "prefer_" + family})
                self.assertNotIn("ip_version", ipv6_rule)
                self.assertNotIn("ip_cidr", ipv6_rule)
        for row in rows:
            expected = gen.build_home_outbound(row)
            for family in ("ipv4", "ipv6"):
                tag = expected["tag"] + ("/ipv6" if family == "ipv6" else "")
                actual = outbounds[tag]
                self.assertEqual({key: value for key, value in actual.items()
                                  if key not in {"tag", "domain_resolver"}},
                                 {key: value for key, value in expected.items()
                                  if key not in {"tag", "domain_resolver"}})

    def test_single_stack_keeps_original_outbound_tags_and_one_resolve_group(self):
        env = self.env(ENABLE_IPV6_LISTEN="false")
        self.assertEqual(gen.connection_families(env), ["ipv4"])
        config = self.build(env)
        rules = config["route"]["rules"]
        self.assertEqual(len(rules), len(config["inbounds"]) + 1)
        self.assertEqual(rules[0]["action"], "resolve")
        self.assertTrue(all(item["listen"] == "0.0.0.0" for item in config["inbounds"]))
        self.assertTrue(all("/ipv6" not in item["tag"] for item in config["outbounds"]))
        self.assertEqual({rule["inbound"] for rule in rules[1:]},
                         {item["tag"] for item in config["inbounds"]})
        self.assertTrue(all(rule["source_ip_cidr"] == ["0.0.0.0/0"] for rule in rules[1:]))

    def test_enabling_ipv6_preserves_all_inbound_credentials_and_protocol_options(self):
        ipv4 = self.build(self.env(ENABLE_IPV6_LISTEN="false"))
        dual = self.build(self.env(ENABLE_IPV6_LISTEN="true"))
        self.assertEqual(
            [{key: value for key, value in item.items() if key != "listen"}
             for item in ipv4["inbounds"]],
            [{key: value for key, value in item.items() if key != "listen"}
             for item in dual["inbounds"]],
        )
        for policy in ("ipv4", "ipv6", "dual"):
            with self.subTest(policy=policy):
                config = self.build(self.env(PROXY_IP_VERSION=policy))
                for family, cidr in (("ipv4", "0.0.0.0/0"), ("ipv6", "::/0")):
                    rule = next(item for item in config["route"]["rules"]
                                if item["action"] == "resolve" and item["source_ip_cidr"] == [cidr])
                    self.assertEqual(rule["server"], gen.dns_server_tag(family))
                    self.assertEqual(rule["strategy"], "prefer_" + family)

    def test_user_tags_ending_in_ipv6_cannot_collide_with_generated_tags(self):
        first = gen.parse_vless_upstream("foo", 52011, REALITY_URL, 2)
        second = gen.parse_vless_upstream("foo-ipv6", 52012, PLAIN_URL, 3)
        config = self.build(rows=[first, second])
        tags = [item["tag"] for item in config["outbounds"]]
        self.assertEqual(len(tags), len(set(tags)))
        self.assertTrue({"out-foo", "out-foo/ipv6", "out-foo-ipv6", "out-foo-ipv6/ipv6"}.issubset(tags))
        self.assertEqual(gen.connection_families(self.env()), ["ipv4", "ipv6"])


if __name__ == "__main__":
    unittest.main()
