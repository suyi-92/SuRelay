from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "Requires Bash on Linux/POSIX")
class AptInstallTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.library = (ROOT / "scripts/00-lib.sh").read_text(encoding="utf-8")

    def run_install(self, scenario, *, timeout="2", dry_run=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tmp").mkdir()
            (root / "phase.log").write_text("OLD RUN: historical package and SSH output\n")
            script = self.library + r'''
LOG_FILE="$TEST_ROOT/phase.log"
require_root() { :; }
sleep() {
  printf '%s\n' "$1" >>"$TEST_ROOT/sleeps"
  SECONDS=$((SECONDS + $1))
}
apt-get() {
  printf '%s|%s|%s\n' "$LC_ALL" "$DEBIAN_FRONTEND" "$*" >>"$TEST_ROOT/calls"
  while [[ "${1:-}" == -o ]]; do shift 2; done
  local operation="$1" counter="$TEST_ROOT/count-$1" count=0
  if [[ "$TEST_SCENARIO" == stdin && "$operation" == install ]]; then
    if IFS= read -r terminal_input; then
      printf 'Unexpected terminal input: %s\n' "$terminal_input" >&2
      return 90
    fi
  fi
  if [[ "$TEST_SCENARIO" == live-log && "$operation" == install ]]; then
    printf 'INSTALL_IN_PROGRESS\n'
    for ((attempt=0; attempt<100; attempt++)); do
      if grep -q '^INSTALL_IN_PROGRESS$' "$LOG_FILE"; then
        return 0
      fi
      command sleep 0.01
    done
    printf 'Progress was not visible before the command returned\n' >&2
    return 91
  fi
  [[ ! -f "$counter" ]] || read -r count <"$counter"
  count=$((count + 1))
  printf '%s\n' "$count" >"$counter"
  if [[ "$TEST_SCENARIO" == update-lock && "$operation" == update && "$count" == 1 ]]; then
    printf 'E: Could not get lock /var/lib/apt/lists/lock - open (11: Resource temporarily unavailable)\n' >&2
    return 100
  fi
  if [[ "$operation" == install ]]; then
    case "$TEST_SCENARIO" in
      install-lock|lock-then-package-error|permanent-lock)
        if [[ "$count" == 1 || "$TEST_SCENARIO" == permanent-lock ]]; then
          printf 'E: Could not get lock /var/lib/dpkg/lock-frontend. It is held by process 6338 (unattended-upgr)\n' >&2
          printf 'E: Unable to acquire the dpkg frontend lock (/var/lib/dpkg/lock-frontend), is another process using it?\n' >&2
          return 100
        fi
        ;;
    esac
    case "$TEST_SCENARIO" in
      package-error|lock-then-package-error)
        printf 'E: Unable to locate package missing-package\n' >&2
        return 100
        ;;
      permission-error)
        printf 'E: Could not open lock file /var/lib/dpkg/lock-frontend - open (13: Permission denied)\n' >&2
        printf 'E: Unable to acquire the dpkg frontend lock (/var/lib/dpkg/lock-frontend), are you root?\n' >&2
        return 100
        ;;
    esac
  elif [[ "$TEST_SCENARIO" == network-error ]]; then
    printf 'E: Failed to fetch https://example.invalid/Packages connection failed\n' >&2
    return 100
  fi
  printf 'CURRENT %s SUCCESS\n' "$operation"
}
trap 'printf outer-cleanup >"$TEST_ROOT/outer-cleanup"' EXIT
apt_install ca-certificates curl
printf 'INSTALL_COMPLETE\n'
'''
            result = subprocess.run(
                ["bash", "--noprofile", "--norc", "-c", script],
                capture_output=True, text=True, encoding="utf-8", timeout=10,
                input="USER_SHELL_COMMAND\n",
                env={
                    **os.environ, "TEST_ROOT": str(root), "TMPDIR": str(root / "tmp"),
                    "RRB_PROJECT_DIR": str(ROOT),
                    "TEST_SCENARIO": scenario, "RRB_APT_LOCK_TIMEOUT": timeout,
                    "RRB_DRY_RUN": str(dry_run).lower(), "RRB_VERBOSE": "false",
                },
            )
            calls = (root / "calls").read_text().splitlines() if (root / "calls").exists() else []
            sleeps = (root / "sleeps").read_text().splitlines() if (root / "sleeps").exists() else []
            log = (root / "phase.log").read_text()
            self.assertEqual(list((root / "tmp").iterdir()), [], "Temporary command logs must be removed")
            self.assertEqual((root / "outer-cleanup").read_text(), "outer-cleanup")
            self.assertNotIn("OLD RUN", result.stdout + result.stderr)
            self.assertIn("OLD RUN", log, "Historical logs must be preserved")
            return result, calls, sleeps, log

    def test_success_is_quiet_and_both_commands_use_noninteractive_english_output(self):
        result, calls, sleeps, log = self.run_install("success")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "INSTALL_COMPLETE\n")
        self.assertEqual(result.stderr, "")
        self.assertEqual(calls, [
            "C|noninteractive|-o DPkg::Lock::Timeout=0 -o Dpkg::Options::=--force-confold update -y",
            "C|noninteractive|-o DPkg::Lock::Timeout=0 -o Dpkg::Options::=--force-confold install -y ca-certificates curl",
        ])
        self.assertEqual(sleeps, [])
        self.assertIn("CURRENT install SUCCESS", log)

    def test_apt_does_not_consume_input_from_the_install_terminal(self):
        result, _, _, log = self.run_install("stdin")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("INSTALL_COMPLETE", result.stdout)
        self.assertNotIn("USER_SHELL_COMMAND", log)

    def test_apt_output_is_logged_while_the_command_is_still_running(self):
        result, _, _, log = self.run_install("live-log")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("INSTALL_IN_PROGRESS", log)
        self.assertNotIn("INSTALL_IN_PROGRESS", result.stdout)

    @unittest.skipUnless(
        all(shutil.which(tool) for tool in ("apt-get", "dpkg-deb", "fakeroot")),
        "Requires APT, dpkg-deb and fakeroot for an isolated local package install",
    )
    def test_real_dpkg_keeps_preexisting_unowned_config_without_prompting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            system = root / "system"
            package = root / "package"
            (package / "DEBIAN").mkdir(parents=True)
            (package / "DEBIAN/control").write_text(
                "Package: rrb-conffile-fixture\nVersion: 1.0\nArchitecture: all\n"
                "Maintainer: Test <test@example.invalid>\nDescription: Local conffile regression fixture\n"
            )
            (package / "DEBIAN/conffiles").write_text("/etc/sing-box/config.json\n")
            (package / "etc/sing-box").mkdir(parents=True)
            packaged_config = '{"source": "package default"}\n'
            (package / "etc/sing-box/config.json").write_text(packaged_config)
            deb = root / "fixture.deb"
            build = subprocess.run(
                ["dpkg-deb", "--build", "--root-owner-group", str(package), str(deb)],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(build.returncode, 0, build.stderr)
            for path in (
                "etc/apt/apt.conf.d", "etc/sing-box", "var/lib/dpkg", "var/lib/apt/lists/partial",
                "var/cache/apt/archives/partial", "var/log/apt",
            ):
                (system / path).mkdir(parents=True)
            (system / "var/lib/dpkg/status").touch()
            current_config = '{"source": "existing deployment"}\n'
            config = system / "etc/sing-box/config.json"
            config.write_text(current_config)
            apt_config = root / "apt.conf"
            # All package state, logs and installation paths stay in this temporary
            # root. No sources or maintainer scripts are provided; nothing downloads.
            apt_config.write_text(
                f'Dir "{system}";\n'
                f'Dir::State::status "{system}/var/lib/dpkg/status";\n'
                'Dir::Etc::sourcelist "-";\nDir::Etc::sourceparts "-";\n'
                f'DPkg::Options {{ "--root={system}"; }};\n'
                'DPkg::Use-Pty "0";\nAPT::Sandbox::User "root";\n'
            )
            result = subprocess.run(
                ["fakeroot", "bash", "--noprofile", "--norc", "-c", self.library + r'''
LOG_FILE="$TEST_ROOT/phase.log"
apt_get install -y "$TEST_DEB"
'''],
                capture_output=True, text=True, encoding="utf-8", input="", timeout=20,
                env={
                    **os.environ, "APT_CONFIG": str(apt_config), "TEST_ROOT": str(root),
                    "TEST_DEB": str(deb), "RRB_PROJECT_DIR": str(ROOT), "RRB_VERBOSE": "false",
                },
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(config.read_text(), current_config)
            self.assertEqual(config.with_suffix(".json.dpkg-dist").read_text(), packaged_config)
            self.assertIn("Status: install ok installed", (system / "var/lib/dpkg/status").read_text())

    def test_dpkg_and_legacy_lists_lock_are_retried_then_continue(self):
        for scenario in ("install-lock", "update-lock"):
            with self.subTest(scenario=scenario):
                result, calls, sleeps, _ = self.run_install(scenario)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(calls), 3)
                self.assertEqual(sleeps, ["2"])
                self.assertEqual(result.stderr.count("等待释放后继续"), 1)
                self.assertIn("INSTALL_COMPLETE", result.stdout)

    def test_persistent_lock_stops_at_deadline_without_running_remaining_steps(self):
        result, calls, sleeps, _ = self.run_install("permanent-lock")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, ["2"])
        self.assertIn("APT 锁等待超时（2 秒）", result.stderr)
        self.assertIn("unattended-upgr", result.stderr)
        self.assertNotIn("INSTALL_COMPLETE", result.stdout)

    def test_zero_timeout_does_not_wait(self):
        result, calls, sleeps, _ = self.run_install("permanent-lock", timeout="0")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(sleeps, [])

    def test_package_network_and_permission_errors_are_not_retried(self):
        for scenario, count in (("package-error", 2), ("network-error", 1), ("permission-error", 2)):
            with self.subTest(scenario=scenario):
                result, calls, sleeps, _ = self.run_install(scenario)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(len(calls), count)
                self.assertEqual(sleeps, [])
                self.assertNotIn("等待释放后继续", result.stderr)
                self.assertNotIn("CURRENT update SUCCESS", result.stderr)

    def test_package_error_after_lock_release_is_not_misreported_as_timeout(self):
        result, calls, sleeps, log = self.run_install("lock-then-package-error")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, ["2"])
        self.assertIn("Unable to locate package", result.stderr)
        self.assertNotIn("超时", result.stderr)
        self.assertNotIn("Could not get lock", result.stderr)
        self.assertIn("Could not get lock", log)

    def test_invalid_timeout_stops_before_calling_apt(self):
        result, calls, sleeps, _ = self.run_install("success", timeout="bad")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, [])
        self.assertEqual(sleeps, [])
        self.assertIn("RRB_APT_LOCK_TIMEOUT", result.stderr)

    def test_dry_run_never_calls_apt(self):
        result, calls, sleeps, log = self.run_install("success", dry_run=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [])
        self.assertEqual(sleeps, [])
        self.assertIn("DRY-RUN: apt-get update", log)


if __name__ == "__main__":
    unittest.main()
