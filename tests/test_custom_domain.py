from __future__ import annotations

import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
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
nodes = load_module("nodes", ROOT / "scripts" / "11-output-nodes.py")
cf_dns = load_module("cf_dns", ROOT / "scripts" / "14-setup-cloudflare-dns.py")
GIT_BASH = Path("C:/Program Files/Git/bin/bash.exe")
BASH = str(GIT_BASH) if os.name == "nt" and GIT_BASH.is_file() else shutil.which("bash") if os.name != "nt" else None


class CustomDomainConfigTest(unittest.TestCase):
    def custom_domain_env(self, **overrides: str):
        env = {
            "SERVER_ALIAS": "edge",
            "SERVER_IP_IPV4": "203.0.113.10",
            "ENABLE_CUSTOM_DOMAIN": "true",
            "CUSTOM_DOMAIN": "edge.example.com",
            "LE_EMAIL": "admin@example.com",
            "WEB_SOURCE_ALLOWLIST": "192.0.2.0/24",
        }
        env.update(overrides)
        return gen.defaults(env)

    def test_custom_domain_defaults_reality_fallback_to_local_nginx(self):
        env = self.custom_domain_env()

        gen.validate_env(env)

        self.assertEqual(env["REALITY_SERVER_NAME"], "edge.example.com")
        self.assertEqual(env["REALITY_HANDSHAKE_SERVER"], "127.0.0.1")
        self.assertEqual(env["REALITY_HANDSHAKE_PORT"], "8443")
        self.assertEqual(env["NGINX_FALLBACK_ROOT"], "/var/www/surelay-fallback")
        self.assertEqual(env["SUBSCRIPTION_PORT"], "")
        self.assertEqual(env["SUBSCRIPTION_INTERNAL_PORT"], "51040")
        self.assertEqual(env["SUBSCRIPTION_LISTEN_PORT"], "51040")
        self.assertEqual(env["SUBSCRIPTION_BASE_URL"], "https://edge.example.com")

    def test_custom_domain_can_enable_public_subscription_port(self):
        env = self.custom_domain_env(SUBSCRIPTION_PORT="51041")

        gen.validate_env(env)

        self.assertEqual(env["SUBSCRIPTION_PORT"], "51041")
        self.assertEqual(env["SUBSCRIPTION_LISTEN_PORT"], "51041")

    def test_non_custom_domain_keeps_default_public_subscription_port(self):
        env = gen.defaults({
            "SERVER_ALIAS": "edge",
            "SERVER_IP_IPV4": "203.0.113.10",
            "ENABLE_CUSTOM_DOMAIN": "false",
            "WEB_SOURCE_ALLOWLIST": "192.0.2.0/24",
            "SUBSCRIPTION_PORT": "",
        })

        gen.validate_env(env)

        self.assertEqual(env["SUBSCRIPTION_PORT"], "51040")
        self.assertEqual(env["SUBSCRIPTION_LISTEN_PORT"], "51040")

    def test_fallback_site_writes_static_page_assets(self):
        script = (ROOT / "scripts" / "15-setup-custom-domain.sh").read_text(encoding="utf-8")

        self.assertIn('install -d -m 0755 -o root -g root "$NGINX_FALLBACK_ROOT"', script)
        self.assertIn("<title>Digital Notes</title>", script)
        self.assertIn("This site hosts lightweight static pages and public notes.", script)
        self.assertIn("$NGINX_FALLBACK_ROOT/robots.txt", script)
        self.assertIn("$NGINX_FALLBACK_ROOT/favicon.ico", script)
        self.assertIn("curl --noproxy '*' -k -fsS --resolve", script)
        self.assertIn("fallback homepage mismatch", script)
        self.assertIn("$base_url/healthz", script)

    @unittest.skipUnless(BASH, "Requires background Bash")
    def test_generated_renewal_hook_checks_actual_reality_before_restart(self):
        script = (ROOT / "scripts" / "15-setup-custom-domain.sh").read_text(encoding="utf-8")
        start = script.index("write_renew_hook() {")
        function = script[start:script.index("\nreload_nginx()", start)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # This path also verifies shell quoting in the generated hook.
            config = root / "config 'notes' $audit.json"
            hook, service_log = root / "renew-hook.sh", root / "services.log"
            environment = {**os.environ, "HOOK_OUTPUT": hook.as_posix(), "SERVICE_LOG": service_log.as_posix(),
                           "PYTHONIOENCODING": "utf-8"}
            generate = ('set -e\nrun() { :; }\nwrite_root_file() { cat > "$HOOK_OUTPUT"; }\n'
                        f'python3() {{ {shlex.quote(Path(sys.executable).as_posix())} "$@"; }}\n'
                        f'SCRIPT_DIR={shlex.quote((ROOT / "scripts").as_posix())}\n'
                        'is_alpine() { return 1; }\nSINGBOX_CONFIG_PATH="$1"\nRRB_STATE_DIR="$2"\n'
                        + function + '\nwrite_renew_hook\n')
            result = subprocess.run([BASH, "--noprofile", "--norc", "-c", generate, "generate", config.as_posix(), root.as_posix()],
                                    env=environment, capture_output=True, text=True, encoding="utf-8", timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            runner = ('python3() { if [[ "$1" == /usr/local/lib/surelay/source_guard.py ]]; then '
                      'printf "source guard check\\n" >> "$SERVICE_LOG"; return "$SOURCE_CHECK_STATUS"; fi; '
                      f'{shlex.quote(Path(sys.executable).as_posix())} "$@"; }}\n'
                      'systemctl() { printf "%s\\n" "$*" >> "$SERVICE_LOG"; }\n'
                      'rc-service() { printf "%s\\n" "$*" >> "$SERVICE_LOG"; }\n'
                      'source "$1"\n')

            def inbound(server="127.0.0.1", port=52843):
                return {"tls": {"reality": {"enabled": True, "handshake": {
                    "server": server, "server_port": port}}}}

            for target, source_status, accepted in ((inbound(), 0, True), (inbound("192.0.2.30", 443), 0, True),
                                     (inbound("104.16.0.1"), 0, False), (inbound("www.microsoft.com"), 0, False),
                                     (inbound("64:ff9b::6810:1"), 0, False), (inbound(), 42, False)):
                with self.subTest(target=target, source_status=source_status):
                    config.write_text(json.dumps({"inbounds": [inbound(), target]}), encoding="utf-8")
                    service_log.write_text("", encoding="utf-8")
                    result = subprocess.run([BASH, "--noprofile", "--norc", "-c", runner, "renew", hook.as_posix()],
                                            env={**environment, "SOURCE_CHECK_STATUS": str(source_status)},
                                            capture_output=True, text=True, encoding="utf-8", timeout=15)
                    self.assertEqual(result.returncode == 0, accepted, result.stderr)
                    calls = service_log.read_text(encoding="utf-8")
                    restarted = "restart sing-box" in calls or "sing-box restart" in calls
                    self.assertEqual(restarted, accepted)
                    self.assertTrue("reload nginx" in calls or "nginx reload" in calls)

    def test_subscription_scripts_use_derived_listen_and_public_ports(self):
        subscription_script = (ROOT / "scripts" / "13-setup-subscription.sh").read_text(encoding="utf-8")
        nginx_script = (ROOT / "scripts" / "15-setup-custom-domain.sh").read_text(encoding="utf-8")
        firewall_script = (ROOT / "scripts" / "09-apply-firewall.sh").read_text(encoding="utf-8")

        self.assertIn("--port $SUBSCRIPTION_LISTEN_PORT", subscription_script)
        self.assertIn("subscription_public_enabled", subscription_script)
        self.assertIn('custom_domain_enabled && sub_ipv4="true"', subscription_script)
        self.assertIn("127.0.0.1:$SUBSCRIPTION_LISTEN_PORT", nginx_script)
        self.assertIn("subscription_public_enabled", firewall_script)

    def test_custom_domain_nat_port_updates_listener_nodes_and_subscription(self):
        env = self.custom_domain_env(DIRECT_PORT="24037")
        gen.validate_env(env)
        config = gen.build_config(env, [], "test-uuid", "test-key", "abcd")
        inbound = config["inbounds"][0]
        self.assertEqual(inbound["listen_port"], 24037)
        self.assertEqual(inbound["tls"]["reality"]["handshake"], {"server": "127.0.0.1", "server_port": 8443})
        self.assertEqual(gen.port_list(env, []), [24037])
        self.assertEqual(gen.node_list(env, [])[0]["port"], 24037)
        self.assertEqual(nodes.subscription_url(env, "test-token"), "https://edge.example.com:24037/sub/test-token?target=ClashMeta")
        self.assertEqual(env["SSH_PORT"], "22")
        self.assertEqual(env["SUBSCRIPTION_PORT"], "")
        self.assertEqual(env["SUBSCRIPTION_LISTEN_PORT"], "51040")

    def test_nat_port_preserves_explicit_subscription_override(self):
        env = self.custom_domain_env(DIRECT_PORT="24037", SUBSCRIPTION_BASE_URL="https://sub.example.com")
        gen.validate_env(env)
        self.assertEqual(nodes.subscription_url(env, "token"), "https://sub.example.com/sub/token?target=ClashMeta")

    def test_custom_domain_still_rejects_fallback_port_collision(self):
        for port in ("22", "51040", "24037"):
            with self.subTest(port=port), self.assertRaises(SystemExit):
                gen.validate_env(self.custom_domain_env(DIRECT_PORT="24037", NGINX_FALLBACK_PORT=port))

    def test_nat_listener_cannot_take_ssh_or_subscription_port(self):
        for port in ("22", "51040"):
            with self.subTest(port=port), self.assertRaises(SystemExit):
                gen.validate_env(self.custom_domain_env(DIRECT_PORT=port))

    def test_reality_inbound_uses_custom_domain_and_local_handshake(self):
        env = self.custom_domain_env()
        inbound = gen.build_vless_reality_inbound(
            "vless-direct-443",
            443,
            "11111111-1111-1111-1111-111111111111",
            "private-key",
            "aabbccdd",
            env,
        )

        self.assertEqual(inbound["tls"]["server_name"], "edge.example.com")
        self.assertEqual(inbound["tls"]["reality"]["handshake"]["server"], "127.0.0.1")
        self.assertEqual(inbound["tls"]["reality"]["handshake"]["server_port"], 8443)

    def test_node_output_prefers_custom_domain_over_ip_overrides(self):
        env = self.custom_domain_env()

        self.assertEqual(nodes.get_server_ip(env, "203.0.113.10"), "edge.example.com")

    def test_cloudflare_zone_name_is_inferred_from_custom_domain(self):
        env = self.custom_domain_env(CUSTOM_DOMAIN="edge.example.com")

        self.assertEqual(env["CLOUDFLARE_ZONE_NAME"], "example.com")

    def test_cloudflare_desired_records_include_ipv4_and_ipv6(self):
        env = self.custom_domain_env(SERVER_IP_IPV6="2001:db8::10")

        self.assertEqual(cf_dns.desired_records(env), [("A", "203.0.113.10"), ("AAAA", "2001:db8::10")])

    def test_cloudflare_record_payload_is_dns_only(self):
        env = self.custom_domain_env()

        payload = cf_dns.record_payload(env, "A", "203.0.113.10")

        self.assertEqual(payload["name"], "edge.example.com")
        self.assertEqual(payload["type"], "A")
        self.assertEqual(payload["content"], "203.0.113.10")
        self.assertIs(payload["proxied"], False)
        self.assertEqual(payload["ttl"], 1)
        self.assertEqual(payload["comment"], "edge")


if __name__ == "__main__":
    unittest.main()
