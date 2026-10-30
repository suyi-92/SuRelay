"""Exercise the shell handoff and SSH confirmation without touching host services."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

if os.name == "posix":
    import pty

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "Requires POSIX Bash")
class RedeployInstallFlowTest(unittest.TestCase):
    def setUp(self):
        self.functions = (ROOT / "install.sh").read_text().rsplit('\nmain "$@"', 1)[0]
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def shell(self, body, **env):
        return subprocess.run(["bash", "--noprofile", "--norc", "-c", self.functions + "\n" + body,
                               "test", str(self.root)], capture_output=True, text=True, timeout=10,
                              env={**os.environ, "RRB_INSTALL_LOG_FILE": str(self.root / "install.log"), **env})

    def test_redeploy_cli_is_exclusive_and_never_starts_another_mode(self):
        for args in ("--reset --redeploy", "--redeploy --reset", "--redeploy -6",
                     "--security-only --redeploy", "--redeploy --update"):
            result = self.shell("parse_args " + args)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("不能", result.stderr)
        result = self.shell('parse_args --redeploy; printf "%s" "$INSTALL_MODE"')
        self.assertEqual(result.stdout, "redeploy")

    def test_removed_handoff_resumes_full_deployment_and_other_modes_are_rejected(self):
        body = '''
python3() { printf removed; }
resume_redeploy "$1"
printf '%s\\n' "$RRB_REDEPLOY" "$RRB_RESET_INSTALL" "$RRB_SECURITY_ONLY" "$RRB_REDEPLOY_HANDOFF"
'''
        result = self.shell(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["true", "true", "false", "/root/surelay-backups/redeploy-handoff.json"])
        result = self.shell("INSTALL_MODE=security-only\n" + body)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("sr install", result.stderr)

    def test_missing_or_finalized_handoff_leaves_normal_install_untouched(self):
        for state in ("none", "finalized"):
            result = self.shell('python3() { printf ' + state + '; }; resume_redeploy "$1"; '
                                'printf "%s" "${RRB_REDEPLOY:-unset}"', RRB_REDEPLOY="")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "unset")

    def test_new_key_confirmation_defaults_to_pause_and_is_required_before_final_cleanup(self):
        (self.root / "scripts").mkdir()
        (self.root / "config.env").write_text('SERVER_IP_IPV4=192.0.2.10\nSSH_PORT=52322\n')
        for filename, phase in (("02-setup-root-ssh.sh", "keys"), ("03-ssh-hardening-phase1.sh", "stage")):
            (self.root / "scripts" / filename).write_text('printf "' + phase + '\\n" >>"$RRB_PROJECT_DIR/phases"\n')
        for answer, final in ((b"\n", False), (b"n\n", False), (b"y\n", True)):
            with self.subTest(answer=answer):
                (self.root / "phases").unlink(missing_ok=True)
                master, slave = pty.openpty()
                try:
                    os.write(master, answer)
                    result = self.shell('''
INPUT_TTY="$TEST_TTY"
RUN_PHASES=true
run_install_phase() { printf '%s\\n' "$1" >>"$RRB_TEST_PHASES"; }
python3() { :; }
run_install_flow "$1"
''', TEST_TTY=os.ttyname(slave), RRB_REDEPLOY="true", RRB_RESET_INSTALL="true",
                                        RRB_REDEPLOY_HANDOFF=str(self.root / "handoff.json"),
                                        RRB_TEST_PHASES=str(self.root / "phases"), SSH_CONNECTION="")
                    prompt = os.read(master, 65536).decode()
                finally:
                    os.close(master)
                    os.close(slave)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("[y/N]", prompt)
                self.assertIn("本次新公钥", prompt)
                phases = (self.root / "phases").read_text().splitlines()
                expected = ["preflight", "keys", "stage"]
                if final:
                    expected += ["ssh-final", "fail2ban", "singbox", "firewall", "validate"]
                else:
                    self.assertIn("sr install", result.stderr)
                self.assertEqual(phases, expected)


if __name__ == "__main__":
    unittest.main()
