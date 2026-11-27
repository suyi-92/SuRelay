from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


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
nginx_safety = load_module("nginx_safety", ROOT / "scripts" / "nginx_safety.py")
GIT_BASH = Path("C:/Program Files/Git/bin/bash.exe")
BASH = str(GIT_BASH) if os.name == "nt" and GIT_BASH.is_file() else shutil.which("bash") if os.name != "nt" else None


class CustomDomainConfigTest(unittest.TestCase):
    def test_only_verified_debian_default_is_disabled_and_backed_up(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nginx, backup = root / "nginx", root / "backup"
            available = nginx / "sites-available/default"
            available.parent.mkdir(parents=True)
            available.write_text("factory Debian default\n")
            enabled = nginx / "sites-enabled/default"
            enabled.parent.mkdir()
            try:
                enabled.symlink_to(available)
            except OSError as exc:
                self.skipTest(f"requires symlinks: {exc}")
            other = enabled.with_name("user-site.conf")
            other.write_text("user configuration\n")
            digest = hashlib.md5(available.read_bytes()).hexdigest()
            def package(*args, **kwargs):
                return subprocess.CompletedProcess(args, 0, stdout=f" {available} {digest}\n")
            self.assertEqual(nginx_safety.disable_defaults(backup, nginx, runner=package), [str(enabled)])
            self.assertFalse(enabled.is_symlink())
            saved = backup / "nginx-defaults/sites-enabled/default"
            self.assertTrue(saved.is_symlink())
            self.assertEqual(os.readlink(saved), str(available))
            self.assertEqual(available.read_text(), "factory Debian default\n")
            self.assertEqual(other.read_text(), "user configuration\n")
            self.assertEqual(nginx_safety.disable_defaults(backup, nginx, runner=package), [])

    def test_customized_default_and_foreign_symlink_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            available, enabled = root / "nginx/sites-available/default", root / "nginx/sites-enabled/default"
            available.parent.mkdir(parents=True)
            enabled.parent.mkdir(parents=True)
            available.write_text("custom site\n")
            enabled.write_text("custom site\n")
            def package(*args, **kwargs):
                return subprocess.CompletedProcess(args, 0, stdout=f" {available} {'0' * 32}\n")
            with self.assertRaises(nginx_safety.NginxSafetyError):
                nginx_safety.disable_defaults(root / "backup", root / "nginx", runner=package)
            self.assertEqual(enabled.read_text(), "custom site\n")

    def test_alpine_factory_404_default_is_removed_but_custom_site_is_not(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            default = root / "nginx/http.d/default.conf"
            default.parent.mkdir(parents=True)
            default.write_text("# shipped default\n" + nginx_safety.ALPINE_DEFAULT)
            nginx_safety.disable_defaults(root / "backup", root / "nginx")
            self.assertFalse(default.exists())
            self.assertTrue((root / "backup/nginx-defaults/http.d/default.conf").is_file())
            default.write_text('server { listen 80; return 200 "my site"; }')
            with self.assertRaises(nginx_safety.NginxSafetyError):
                nginx_safety.disable_defaults(root / "another-backup", root / "nginx")
            self.assertIn("my site", default.read_text())

    def test_default_backup_cannot_be_inside_a_live_include_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nginx = root / "nginx"
            with self.assertRaises(nginx_safety.NginxSafetyError):
                nginx_safety.disable_defaults(nginx / "sites-enabled/backup", nginx)

    @unittest.skipUnless(BASH, "Requires background Bash")
    def test_nginx_package_guard_restores_existing_policy_even_when_install_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = root / "policy-rc.d"
            original = b"#!/bin/sh\nexit 42\n"
            policy.write_bytes(original)
            policy.chmod(0o751)
            mode = policy.stat().st_mode
            def packages(command, **kwargs):
                for service, expected in (("nginx", 101), ("nginx.service", 101), ("sshd", 42)):
                    result = subprocess.run([BASH, str(policy), service, "start"], capture_output=True, timeout=10)
                    self.assertEqual(result.returncode, expected)
                quiet = subprocess.run([BASH, str(policy), "--quiet", "nginx", "start"], capture_output=True, timeout=10)
                self.assertEqual(quiet.returncode, 101)
                return subprocess.CompletedProcess(command, 9)
            self.assertEqual(nginx_safety.guarded_install(["mock-package-install"], policy_path=policy, runner=packages), 9)
            self.assertEqual(policy.read_bytes(), original)
            self.assertEqual(policy.stat().st_mode, mode)
            self.assertEqual([p.name for p in root.iterdir()], ["policy-rc.d"])

    def test_nginx_package_guard_preparation_failure_never_leaves_active_policy(self):
        original_write = Path.write_text
        original_chmod = Path.chmod
        for failure in ("write", "chmod"):
            for existing in (False, True):
                with self.subTest(failure=failure, existing=existing), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    policy = root / "policy-rc.d"
                    original = b"#!/bin/sh\nexit 42\n"
                    if existing:
                        policy.write_bytes(original)
                    def write(path, text, *args, **kwargs):
                        if failure == "write" and path.name == "policy-rc.d.wrapper":
                            original_write(path, "partial", encoding="utf-8")
                            raise OSError("simulated partial write failure")
                        return original_write(path, text, *args, **kwargs)
                    def chmod(path, *args, **kwargs):
                        if failure == "chmod" and path.name == "policy-rc.d.wrapper":
                            raise OSError("simulated chmod failure")
                        return original_chmod(path, *args, **kwargs)
                    with patch.object(Path, "write_text", write), patch.object(Path, "chmod", chmod):
                        with self.assertRaises(OSError):
                            nginx_safety.guarded_install(["must-not-run"], policy_path=policy,
                                runner=lambda *args, **kwargs: self.fail("package command ran"))
                    if existing:
                        self.assertEqual(policy.read_bytes(), original)
                        self.assertEqual([p.name for p in root.iterdir()], ["policy-rc.d"])
                    else:
                        self.assertFalse(policy.exists())
                        self.assertEqual(list(root.iterdir()), [])

    def test_nginx_package_guard_preserves_concurrent_policy_and_original_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = root / "policy-rc.d"
            original = b"#!/bin/sh\nexit 42\n"
            replacement = b"#!/bin/sh\n# concurrently replaced\nexit 99\n"
            policy.write_bytes(original)
            def packages(command, **kwargs):
                policy.write_bytes(replacement)
                return subprocess.CompletedProcess(command, 0)
            with self.assertRaises(nginx_safety.NginxSafetyError):
                nginx_safety.guarded_install(["mock-package-install"], policy_path=policy, runner=packages)
            self.assertEqual(policy.read_bytes(), replacement)
            saved = [p / "policy-rc.d.original" for p in root.iterdir() if p.is_dir()]
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0].read_bytes(), original)

    @unittest.skipUnless(BASH, "Requires background Bash")
    def test_certificate_only_does_not_install_or_start_nginx(self):
        script = (ROOT / "scripts/15-setup-custom-domain.sh").read_text(encoding="utf-8")
        preparation = script[script.index("disable_nginx_defaults() {"):script.index("\ninstall_certbot")]
        harness = r'''
set -e
RRB_CERTIFICATES_ONLY=true
SCRIPT_DIR=/fixture/scripts
RRB_PROJECT_DIR=/fixture
BACKUP_DIR=/fixture/backup
init_backup_dir() { :; }
is_dry_run() { return 1; }
is_alpine() { return 1; }
log() { :; }
service_active() { return 1; }
service_reload() { printf 'unexpected nginx reload\n'; exit 98; }
pkg_install() { printf 'packages %s\n' "$*"; }
python3() { printf '{"disabled": []}\n'; }
die() { printf '%s\n' "$*" >&2; exit 99; }
'''
        result = subprocess.run([BASH, "--noprofile", "--norc", "-c", harness + preparation],
                                capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "packages ca-certificates curl openssl")

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
