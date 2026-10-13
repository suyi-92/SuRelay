from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

if os.name == "posix":
    import pty


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "Requires Bash on Linux/POSIX")
class InstallFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.functions = (ROOT / "install.sh").read_text(encoding="utf-8").rsplit('\nmain "$@"', 1)[0]

    def run_bash(self, body, directory, **environment):
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-c", self.functions + "\n" + body, "test", str(directory)],
            capture_output=True, text=True, encoding="utf-8",
            env={**os.environ, "RRB_INSTALL_LOG_FILE": str(directory / "install.log"), **environment},
            timeout=10,
        )

    def test_reinstall_preserves_config_and_both_exit_lists_without_prompting(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            originals = {
                "config.env": b'# Custom settings\nRESET_PROXY_KEYS="false"\nDIRECT_PORT="8443"\n',
                "home-proxies.csv": b"home-01,socks5,proxy.example.com,1080,user,password,tcp,51043\n",
                "upstream-nodes.txt": b"relay-01,vless://kept,51044\n",
            }
            for name, content in originals.items():
                (root / name).write_bytes(content)
            result = self.run_bash('read_default() { exit 91; }; read_bool_value() { exit 92; }; configure_install "$1"', root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual({name: (root / name).read_bytes() for name in originals}, originals)

    def test_first_install_only_collects_basics_cloudflare_and_public_keys(self):
        for ipv6 in ("", "2001:db8::10"):
            with self.subTest(ipv6=ipv6), tempfile.TemporaryDirectory() as temporary:
                self.check_first_install(Path(temporary), ipv6)

    def check_first_install(self, root, ipv6):
        result = self.run_bash('''
prompt_log="$1/prompts"
public_ipv4() { printf '192.0.2.10'; }
public_ipv6() { printf 'Unexpected IPv6 probe' >&2; exit 99; }
read_default() {
  printf '%s\n' "$1" >>"$prompt_log"
  case "$1" in
    SERVER_ALIAS) printf 'relay' ;;
    CUSTOM_DOMAIN) printf 'edge.example.com' ;;
    LE_EMAIL) printf 'admin@example.com' ;;
    *) printf '%s' "$2" ;;
  esac
}
read_bool_value() { printf '%s\n' "$1" >>"$prompt_log"; printf true; }
read_secret_default() { printf '%s\n' "$1" >>"$prompt_log"; printf fake-cf-token; }
collect_admin_pubkeys() { printf 'ADMIN_PUBKEY\n' >>"$prompt_log"; printf 'ssh-ed25519 AAAA test\n' >"$1"; }
configure_install "$1"
source "$1/config.env"
printf '%s\n' "$DIRECT_PORT" "$NGINX_FALLBACK_PORT" "$RESET_PROXY_KEYS" "$ENABLE_SUBSCRIPTION_SERVER" "$SUBSCRIPTION_TARGET" "$CLIENT_UDP" "$ENABLE_IPV6_LISTEN" "$SERVER_IP_IPV6" >"$1/defaults"
''', root, TEST_IPV6=ipv6)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((root / "prompts").read_text().splitlines(), [
            "SERVER_ALIAS", "SERVER_IP_IPv4", "SSH_PORT",
            "ENABLE_CUSTOM_DOMAIN", "CUSTOM_DOMAIN", "CLOUDFLARE_ZONE_NAME", "LE_EMAIL",
            "CLOUDFLARE_API_TOKEN", "ADMIN_PUBKEY",
        ])
        self.assertEqual((root / "defaults").read_text().splitlines(), [
            "443", "8443", "false", "true", "ClashMeta", "true", "false", "",
        ])
        self.assertNotIn("fake-cf-token", result.stdout + result.stderr)
        self.assertEqual(len((root / "home-proxies.csv").read_text().splitlines()), 1)
        self.assertEqual(len((root / "upstream-nodes.txt").read_text().splitlines()), 1)

    def test_ssh_confirmation_gates_final_and_summary(self):
        for reply, confirmed in ((b"\n", True), (b"Y\n", True), (b"n\n", False), (b"\x04", False)):
            with self.subTest(reply=reply), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "config.env").write_text('SERVER_IP_IPV4="192.0.2.10"\nSSH_PORT="2222"\n')
                master, slave = pty.openpty()
                try:
                    os.write(master, reply)
                    result = self.run_bash('''
INPUT_TTY="$TEST_TTY"
phase_log="$1/phases"
run_install_phase() { printf '%s\n' "$1" >>"$phase_log"; }
python3() { printf 'FINAL_NODE_SUMMARY\n'; }
run_install_flow "$1"
''', root, TEST_TTY=os.ttyname(slave), RRB_RUN_PHASES="true", SSH_CONNECTION="")
                    prompt = os.read(master, 65536).decode("utf-8")
                finally:
                    os.close(master)
                    os.close(slave)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("[Y/n]", prompt)
                expected = ["preflight", "ssh-phase1"]
                if confirmed:
                    expected += ["ssh-final", "fail2ban", "singbox", "firewall", "validate"]
                self.assertEqual((root / "phases").read_text().splitlines(), expected)
                self.assertEqual(result.stdout.count("ssh -p "), 1)
                self.assertIn("-o BatchMode=yes", result.stdout)
                self.assertIn("root@192.0.2.10 whoami", result.stdout)
                self.assertEqual("FINAL_NODE_SUMMARY" in result.stdout, confirmed)
                self.assertIn("SSH 公钥登录验证", result.stdout)
                self.assertEqual("应用配置与验证" in result.stdout, confirmed)
                self.assertNotIn("\033[", result.stdout + result.stderr)

    def test_quiet_phase_keeps_warnings_and_failure_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "bootstrap.sh").write_text('printf "routine output\\n"\nprintf "WARN: fixture failure\\n" >&2\nexit 7\n')
            result = self.run_bash('cd "$1"; run_install_phase singbox', root, RRB_VERBOSE="false")
            self.assertEqual(result.returncode, 7)
            self.assertNotIn("routine output", result.stdout)
            self.assertIn("routine output", (root / "install.log").read_text())
            self.assertIn("WARN: fixture failure", result.stderr)
            self.assertIn("开始：安装 sing-box", result.stderr)
            self.assertIn("阶段失败：安装 sing-box", result.stderr)
            self.assertNotIn("完成：", result.stderr)

    def test_phase_reports_start_and_completion_without_exposing_quiet_output(self):
        for verbose in ("false", "true"):
            with self.subTest(verbose=verbose), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "bootstrap.sh").write_text('printf "routine output\\n"\n')
                result = self.run_bash('cd "$1"; run_install_phase singbox', root, RRB_VERBOSE=verbose)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("开始：安装 sing-box", result.stderr)
                self.assertIn("完成：安装 sing-box", result.stderr)
                self.assertNotIn("阶段失败", result.stderr)
                self.assertEqual("routine output" in result.stdout, verbose == "true")


if __name__ == "__main__":
    unittest.main()
