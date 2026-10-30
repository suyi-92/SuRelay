from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
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

    def test_first_install_routes_detected_address_to_grouped_wizard(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = self.run_bash('''
public_ipv4() { printf '192.0.2.10'; }
python3() { printf '%s\\n' "$@" >"$TEST_ROOT/wizard-args"; }
configure_install "$TEST_ROOT"
''', root, TEST_ROOT=str(root))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / "wizard-args").read_text().splitlines(), [
                str(root / "scripts/surelay_install.py"), "--project-dir", str(root), "--ipv4", "192.0.2.10",
            ])
            self.assertTrue((root / "home-proxies.csv").exists())
            self.assertTrue((root / "upstream-nodes.txt").exists())

    def test_install_flow_preserves_configured_ssh_port_when_session_differs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config.env").write_text(
                'SERVER_IP_IPV4="217.217.78.142"\nSSH_PORT="24087"\n'
            )
            master, slave = pty.openpty()
            try:
                os.write(master, b"\n")
                result = self.run_bash('''
INPUT_TTY="$TEST_TTY"
run_install_phase() { :; }
python3() { :; }
run_install_flow "$1"
''', root, TEST_TTY=os.ttyname(slave), RRB_RUN_PHASES="true",
                                    SSH_CONNECTION="112.193.179.58 47504 217.217.78.142 22")
                prompt = os.read(master, 65536).decode("utf-8")
            finally:
                os.close(master)
                os.close(slave)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ssh -p 24087 ", result.stdout)
            self.assertIn("当前 SSH 会话使用端口 22", result.stderr)
            self.assertIn("验证命令将按配置端口执行", result.stderr)
            self.assertEqual(
                subprocess.check_output(
                    ["bash", "-c", 'source "$1"; printf "%s" "$SSH_PORT"', "_", str(root / "config.env")],
                    text=True,
                ),
                "24087",
            )
            self.assertIn("[Y/n]", prompt)

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

    def test_maintenance_lock_blocks_a_concurrent_install(self):
        if not shutil.which("flock"):
            self.skipTest("flock is required")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            holder = subprocess.Popen(
                ["bash", "--noprofile", "--norc", "-c",
                 self.functions + '\nacquire_maintenance_lock; : >"$1/held"; sleep 30',
                 "holder", str(root)],
                env={**os.environ, "RRB_MAINTENANCE_LOCK_FILE": str(root / "surelay.lock")},
            )
            try:
                deadline = time.monotonic() + 5
                while not (root / "held").exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue((root / "held").exists(), "lock holder did not start")
                result = self.run_bash(
                    'acquire_maintenance_lock', root,
                    RRB_MAINTENANCE_LOCK_FILE=str(root / "surelay.lock"),
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("另一个 SuRelay 安装或管理会话正在运行", result.stderr)
            finally:
                holder.terminate()
                holder.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
