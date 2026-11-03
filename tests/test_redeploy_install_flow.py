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

    def shell(self, body, *, allow_main=False, **env):
        # Main-flow tests replace every system operation; bypass only its root guard.
        functions = self.functions.replace('${EUID:-$(id -u)}', '0') if allow_main else self.functions
        return subprocess.run(["bash", "--noprofile", "--norc", "-c", functions + "\n" + body,
                               "test", str(self.root)], capture_output=True, text=True, timeout=10,
                              env={**os.environ, "RRB_INSTALL_LOG_FILE": str(self.root / "install.log"), **env})

    def main_flow(self, state, *, configured=False):
        scripts = self.root / "scripts"
        scripts.mkdir(exist_ok=True)
        (scripts / "00-lib.sh").write_text('require_supported_os() { :; }\n')
        (self.root / "surelay.sh").write_text('printf "launcher\\n" >>"$TEST_ROOT/calls"\n')
        (self.root / "handoff-state").write_text(state)
        (self.root / "calls").unlink(missing_ok=True)
        config = self.root / "config.env"
        if configured:
            config.write_text('SSH_PORT=52322\n# keep resumed configuration\n')
        else:
            config.unlink(missing_ok=True)
        return self.shell('''
acquire_maintenance_lock() { :; }
init_input_tty() { :; }
ensure_entry_dependencies() { :; }
ensure_project() { printf '%s' "$TEST_ROOT"; }
python3() {
  case "$1" in
    */surelay_redeploy.py)
      state="$(cat "$TEST_ROOT/handoff-state")"
      case "$state" in none|removed|finalized) printf '%s' "$state" ;; *) return 1 ;; esac
      ;;
    */17-reset-install.py)
      printf 'reset\\n' >>"$TEST_ROOT/calls"
      [[ "$(cat "$TEST_ROOT/handoff-state")" != removed ]] || return 91
      printf removed >"$TEST_ROOT/handoff-state"
      ;;
    *) return 92 ;;
  esac
}
configure_install() {
  if [[ -f "$1/config.env" ]]; then step=continue; else step=wizard; fi
  printf '%s %s %s\\n' "$step" "$RRB_REDEPLOY" "$RRB_RESET_INSTALL" >>"$TEST_ROOT/calls"
}
run_install_flow() { printf 'flow %s\\n' "$RRB_REDEPLOY" >>"$TEST_ROOT/calls"; }
main --redeploy
''', allow_main=True, TEST_ROOT=str(self.root), RRB_REDEPLOY="", RRB_RESET_INSTALL="")

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

    def test_redeploy_entry_resumes_removed_handoff_without_repeating_cleanup(self):
        for configured in (False, True):
            with self.subTest(configured=configured):
                result = self.main_flow("removed", configured=configured)
                self.assertEqual(result.returncode, 0, result.stderr)
                step = "continue" if configured else "wizard"
                self.assertEqual((self.root / "calls").read_text().splitlines(),
                                 ["launcher", step + " true true", "flow true"])
                self.assertEqual((self.root / "handoff-state").read_text(), "removed")
                if configured:
                    self.assertEqual((self.root / "config.env").read_text(),
                                     'SSH_PORT=52322\n# keep resumed configuration\n')

    def test_redeploy_entry_still_cleans_a_new_or_finalized_deployment_once(self):
        for state in ("none", "finalized"):
            with self.subTest(state=state):
                result = self.main_flow(state)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((self.root / "calls").read_text().splitlines(),
                                 ["launcher", "reset", "wizard true true", "flow true"])

    def test_redeploy_entry_rejects_invalid_handoff_before_any_changes(self):
        for state in ("prepared", "failed", "unknown", "invalid"):
            with self.subTest(state=state):
                result = self.main_flow(state, configured=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("交接记录未通过检查", result.stderr)
                self.assertFalse((self.root / "calls").exists())
                self.assertEqual((self.root / "handoff-state").read_text(), state)
                self.assertEqual((self.root / "config.env").read_text(),
                                 'SSH_PORT=52322\n# keep resumed configuration\n')

    def test_new_key_confirmation_defaults_to_pause_and_is_required_before_final_cleanup(self):
        (self.root / "scripts").mkdir()
        (self.root / "config.env").write_text('SERVER_IP_IPV4=192.0.2.10\nSSH_PORT=52322\n')
        for filename, phase in (("02-setup-root-ssh.sh", "keys"), ("03-ssh-hardening-phase1.sh", "stage")):
            (self.root / "scripts" / filename).write_text(
                'printf "' + phase + '\\n" >>"$RRB_PROJECT_DIR/phases"\n'
                'printf "SSH_STEP_NOISE_' + phase + '\\n"\n'
                'printf "SSH_STEP_STDERR_' + phase + '\\n" >&2\n'
                'printf "%s" "$RRB_INSTALL_FLOW" >"$RRB_PROJECT_DIR/flow-marker"\n')
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
                self.assertIn("本次填写的公钥", prompt)
                self.assertEqual(result.stdout.count("ssh -p "), 1)
                self.assertNotIn("SSH_STEP_NOISE", result.stdout + result.stderr)
                self.assertNotIn("SSH_STEP_STDERR", result.stdout + result.stderr)
                self.assertIn("SSH_STEP_NOISE_keys", (self.root / "install.log").read_text())
                self.assertIn("SSH_STEP_STDERR_stage", (self.root / "install.log").read_text())
                self.assertEqual((self.root / "flow-marker").read_text(), "true")
                phases = (self.root / "phases").read_text().splitlines()
                expected = ["preflight", "keys", "stage"]
                if final:
                    expected += ["ssh-final", "fail2ban", "singbox", "firewall", "validate"]
                else:
                    self.assertIn("sr install", result.stderr)
                self.assertEqual(phases, expected)


if __name__ == "__main__":
    unittest.main()
