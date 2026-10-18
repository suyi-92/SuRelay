from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from importlib.util import module_from_spec, spec_from_file_location


ROOT = Path(__file__).resolve().parents[1]
GEN_SPEC = spec_from_file_location("surelay_generator_security_only", ROOT / "scripts/08-generate-singbox-config.py")
GEN = module_from_spec(GEN_SPEC)
assert GEN_SPEC.loader is not None
GEN_SPEC.loader.exec_module(GEN)


@unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "Requires POSIX Bash")
class SecurityOnlyInstallTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.functions = (ROOT / "install.sh").read_text(encoding="utf-8").replace("\r\n", "\n").rsplit("\nmain \"$@\"", 1)[0]

    def run_bash(self, body: str, root: Path):
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-c", self.functions + "\n" + body, "test", str(root)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            env={**os.environ, "TEST_ROOT": str(root)},
        )

    def test_cli_mode_skips_node_phases(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config.env").write_text(
                'SERVER_IP_IPV4="192.0.2.10"\nSSH_PORT="22"\n'
                'SURELAY_SECURITY_ONLY="true"\n',
                encoding="utf-8",
            )
            result = self.run_bash(
                '''
INSTALL_MODE=security-only
RUN_PHASES=true
read_yes_no() { return 0; }
run_install_phase() { printf '%s\\n' "$1" >>"$TEST_ROOT/phases"; }
run_install_flow "$TEST_ROOT"
''',
                root,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / "phases").read_text().splitlines(), [
                "preflight", "ssh-phase1", "ssh-final", "fail2ban", "firewall", "validate",
            ])
            self.assertNotIn("singbox", (root / "phases").read_text())
            self.assertIn("未安装 sing-box", result.stdout)

    def test_first_security_only_config_does_not_prompt_for_node_or_domain_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = self.run_bash(
                '''
INSTALL_MODE=security-only
public_ipv4() { printf '192.0.2.10'; }
read_default() {
  printf '%s\\n' "$1" >>"$TEST_ROOT/prompts"
  printf '%s' "$2"
}
collect_admin_pubkeys() { printf 'ssh-ed25519 AAAA test' >"$1"; }
configure_install "$TEST_ROOT"
cat "$TEST_ROOT/config.env"
''',
                root,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / "prompts").read_text().splitlines(), ["SERVER_IP_IPv4", "SSH_PORT"])
            config = (root / "config.env").read_text()
            self.assertIn("SURELAY_SECURITY_ONLY=true", config)
            self.assertIn("ENABLE_SUBSCRIPTION_SERVER=false", config)
            self.assertIn("CLOUDFLARE_API_TOKEN=''", config)
            self.assertFalse((root / "home-proxies.csv").exists())
            self.assertFalse((root / "upstream-nodes.txt").exists())



class SecurityOnlyGeneratorTest(unittest.TestCase):
    def test_allows_ssh_port_equal_to_unused_direct_port(self):
        env = GEN.defaults({
            "SURELAY_SECURITY_ONLY": "true",
            "SSH_PORT": "443",
            "DIRECT_PORT": "443",
            "ENABLE_SUBSCRIPTION_SERVER": "false",
            "ENABLE_CUSTOM_DOMAIN": "false",
        })
        GEN.validate_env(env)


if __name__ == "__main__":
    unittest.main()
