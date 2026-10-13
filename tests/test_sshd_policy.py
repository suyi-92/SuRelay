from __future__ import annotations

import importlib.util
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts/sshd_policy.py"
SPEC = importlib.util.spec_from_file_location("sshd_policy", HELPER)
POLICY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(POLICY)
PHASE1 = b"PubkeyAuthentication yes\n"
FINAL = b"PermitRootLogin prohibit-password\nPubkeyAuthentication yes\nPasswordAuthentication no\nAuthenticationMethods publickey\nAllowUsers root\n"
SSHD = shutil.which("sshd") or ("/usr/sbin/sshd" if Path("/usr/sbin/sshd").is_file() else None)


class SshdPolicyTextTest(unittest.TestCase):
    def test_policy_is_first_and_preserves_all_other_bytes(self):
        original = b"# provider\nPermitRootLogin yes\nInclude /etc/ssh/sshd_config.d/*.conf\n# non-UTF8: \xff\xfe\n"
        updated = POLICY.apply_policy(original, FINAL)
        self.assertTrue(updated.startswith(POLICY.BEGIN + b"\nPermitRootLogin prohibit-password\n"))
        self.assertEqual(POLICY.remove_policy(updated), original)

    def test_rerunning_and_switching_phases_replaces_one_block(self):
        original = b"PasswordAuthentication yes\n"
        phase1 = POLICY.apply_policy(original, PHASE1)
        final = POLICY.apply_policy(phase1, FINAL)
        self.assertEqual(POLICY.apply_policy(final, FINAL), final)
        self.assertEqual(final.count(POLICY.BEGIN), 1)
        self.assertEqual(POLICY.apply_policy(final, PHASE1), phase1)

    def test_newlines_bom_and_missing_final_newline_survive_round_trip(self):
        for original in [b"", b"# comment", b"# comment\r\nPermitRootLogin yes\r\n", POLICY.BOM + b"# comment\r\n"]:
            with self.subTest(original=original):
                updated = POLICY.apply_policy(original, FINAL)
                self.assertEqual(POLICY.remove_policy(updated), original)
                self.assertEqual(POLICY.apply_policy(updated, FINAL), updated)
                if b"\r\n" in original:
                    self.assertNotIn(b"\n", updated.replace(b"\r\n", b""))

    def test_malformed_or_duplicate_blocks_are_rejected(self):
        complete = POLICY.BEGIN + b"\n" + PHASE1 + POLICY.END + b"\n"
        for original in [POLICY.BEGIN + b"\n", POLICY.END + b"\n", complete * 2, POLICY.BEGIN + b"\n" + complete]:
            with self.subTest(original=original), self.assertRaises(ValueError):
                POLICY.apply_policy(original, FINAL)

    def test_empty_or_marker_containing_policy_is_rejected(self):
        for policy in [b"", b"\n \n", POLICY.BEGIN + b"\n", POLICY.END + b"\n"]:
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                POLICY.apply_policy(b"# original\n", policy)

    def test_cli_preserves_permissions_and_does_not_rewrite_unchanged_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "sshd_config"
            config.write_bytes(b"# provider\n")
            config.chmod(0o600)
            command = [sys.executable, str(HELPER), "--config", str(config), "--apply"]
            subprocess.run(command, input=PHASE1, check=True)
            os.utime(config, ns=(1_000_000_000, 1_000_000_000))
            before = config.stat()
            subprocess.run(command, input=PHASE1, check=True)
            self.assertEqual(config.stat().st_mtime_ns, before.st_mtime_ns)
            self.assertEqual(config.stat().st_mode, before.st_mode)

    def test_cli_leaves_malformed_file_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "sshd_config"
            original = POLICY.BEGIN + b"\nPasswordAuthentication yes\n"
            config.write_bytes(original)
            result = subprocess.run([sys.executable, str(HELPER), "--config", str(config), "--apply"], input=FINAL, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(config.read_bytes(), original)


@unittest.skipUnless(os.name == "posix" and SSHD and shutil.which("ssh-keygen"), "Requires real OpenSSH server tools")
class SshdPolicyIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.host_directory = tempfile.TemporaryDirectory()
        cls.host_key = Path(cls.host_directory.name) / "host-key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(cls.host_key)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.host_directory.cleanup()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.ssh_dir = self.root / "ssh"
        self.dropins = self.ssh_dir / "sshd_config.d"
        self.dropins.mkdir(parents=True)
        self.config = self.ssh_dir / "sshd_config"
        self.original = f"# provider image\nInclude {self.dropins}/*.conf\nPasswordAuthentication yes\nPubkeyAuthentication no\n".encode()
        self.config.write_bytes(self.original)
        self.provider = self.dropins / "00-mofang.conf"
        self.provider_bytes = b"PermitRootLogin yes\nPasswordAuthentication yes\n"
        self.provider.write_bytes(self.provider_bytes)
        self.root_home = self.root / "root-home"
        (self.root_home / ".ssh").mkdir(parents=True)
        (self.root_home / ".ssh/authorized_keys").write_bytes(self.host_key.with_suffix(".pub").read_bytes())
        self.reloads = self.root / "reloads.txt"
        self.backups = []
        self.sshd_wrapper = self.root / "sshd"
        self.sshd_wrapper.write_text(f"#!/bin/sh\nexec {shlex.quote(SSHD)} -f {shlex.quote(str(self.config))} -h {shlex.quote(str(self.host_key))} \"$@\"\n")
        self.sshd_wrapper.chmod(0o755)

    def effective(self, user="root"):
        result = subprocess.run([str(self.sshd_wrapper), "-T", "-C", f"user={user},host=localhost,addr=127.0.0.1"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        values = {}
        for line in result.stdout.splitlines():
            if " " in line:
                key, value = line.split(" ", 1)
                values[key] = f"{values[key]} {value}" if key in values else value
        return values

    def run_phase(self, script_name, *, confirmed=True, sftp=False, restore=None, dry_run=False):
        library = (ROOT / "scripts/00-lib.sh").read_text(encoding="utf-8")
        phase = (ROOT / "scripts" / script_name).read_text(encoding="utf-8")
        phase = "\n".join(line for line in phase.splitlines() if not line.startswith('source "'))
        # Redirect every system write to a temporary fixture. sshd -t/-T remain real;
        # reload is recorded so failed validation can be proved to stop beforehand.
        for source, target in [("/etc/ssh", self.ssh_dir), ("/etc/sing-box", self.root / "sing-box"), ("/etc/ufw", self.root / "ufw")]:
            library = library.replace(source, str(target))
            phase = phase.replace(source, str(target))
        phase = phase.replace("/tmp/surelay-sshd-test.err", str(self.root / "sshd-test.err"))
        backup = self.root / "backups" / f"backup-{len(self.backups):03d}-{script_name}"
        self.backups.append(backup)
        variables = {
            "LOG_FILE": str(self.root / "phase.log"),
            "BACKUP_ROOT": str(self.root / "backups"),
            "BACKUP_DIR": str(backup),
            "RRB_DRY_RUN": str(dry_run).lower(),
            "RRB_VERBOSE": "true",
            "CONFIRM_ROOT_KEY_LOGIN": "yes" if confirmed else "",
            "CONFIRM_ROLLBACK": "yes",
            "ENABLE_SFTP_USER": str(sftp).lower(),
            "SFTP_USER": "nobody" if sftp else "sftpuser",
            "ADMIN_USER": "root",
            "SSH_PORT": "22",
            "SERVER_IP_IPV4": "192.0.2.10",
            "SERVER_IP_IPV6": "",
            "SERVER_IP": "192.0.2.10",
            "ROLLBACK_BACKUP_DIR": str(restore) if restore else "",
            "RESTORE_FULL_SSH_BACKUP": "yes" if restore else "no",
        }
        setup = "\n".join(f"{name}={shlex.quote(value)}" for name, value in variables.items())
        setup += f"""
require_root() {{ :; }}
load_config() {{ :; }}
sshd_bin() {{ printf '%s\\n' {shlex.quote(str(self.sshd_wrapper))}; }}
reload_ssh() {{ printf '%s\\n' "$PHASE_NAME" >> {shlex.quote(str(self.reloads))}; }}
getent() {{
  if [[ "$1" == passwd && "$2" == root ]]; then
    printf 'root:x:0:0:root:%s:/bin/bash\\n' {shlex.quote(str(self.root_home))}
  else
    command getent "$@"
  fi
}}
"""
        command = f"set -Eeuo pipefail\nexport RRB_PROJECT_DIR={shlex.quote(str(ROOT))}\n" + library + "\n" + setup + "\n" + phase
        return subprocess.run(["bash"], input=command, text=True, encoding="utf-8", capture_output=True)

    def assert_final(self):
        effective = self.effective()
        self.assertIn(effective["permitrootlogin"], {"prohibit-password", "without-password"})
        self.assertEqual(effective["pubkeyauthentication"], "yes")
        self.assertEqual(effective["passwordauthentication"], "no")
        self.assertEqual(effective["authenticationmethods"], "publickey")
        self.assertIn("root", effective["allowusers"].split())

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.provider.read_bytes(), self.provider_bytes)

    def test_reproduces_provider_conflict_then_migrates_legacy_files(self):
        legacy_final = self.dropins / "00-surelay-hardening.conf"
        legacy_phase1 = self.dropins / "00-surelay-phase1.conf"
        legacy_final.write_bytes(FINAL)
        legacy_phase1.write_bytes(PHASE1)
        before = self.effective()
        self.assertEqual(before["permitrootlogin"], "yes")
        self.assertEqual(before["passwordauthentication"], "yes")
        self.assertEqual(before["authenticationmethods"], "publickey")
        self.assert_success(self.run_phase("04-ssh-hardening-final.sh"))
        self.assert_final()
        self.assertFalse(legacy_final.exists())
        self.assertFalse(legacy_phase1.exists())
        self.assertEqual((Path(str(self.backups[-1]) + str(legacy_final))).read_bytes(), FINAL)
        self.assertEqual(POLICY.remove_policy(self.config.read_bytes()), self.original)

    def test_phase1_final_rerun_phase1_and_rollback(self):
        self.assert_success(self.run_phase("03-ssh-hardening-phase1.sh"))
        phase1 = self.config.read_bytes()
        self.assertEqual(self.effective()["passwordauthentication"], "yes")
        self.assertEqual(self.effective()["pubkeyauthentication"], "yes")
        self.assert_success(self.run_phase("04-ssh-hardening-final.sh"))
        self.assert_final()
        final = self.config.read_bytes()
        self.assert_success(self.run_phase("04-ssh-hardening-final.sh"))
        self.assertEqual(self.config.read_bytes(), final)
        self.assert_success(self.run_phase("03-ssh-hardening-phase1.sh"))
        self.assertEqual(self.config.read_bytes(), phase1)
        self.assertEqual(self.effective()["permitrootlogin"], "yes")
        self.assertEqual(self.effective()["passwordauthentication"], "yes")
        self.assert_success(self.run_phase("12-rollback.sh"))
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_full_backup_restores_previous_phase(self):
        self.assert_success(self.run_phase("03-ssh-hardening-phase1.sh"))
        phase1 = self.config.read_bytes()
        self.assert_success(self.run_phase("04-ssh-hardening-final.sh"))
        before_final = self.backups[-1]
        self.assert_success(self.run_phase("12-rollback.sh", restore=before_final))
        self.assertEqual(self.config.read_bytes(), phase1)
        self.assertEqual(self.effective()["passwordauthentication"], "yes")

    def test_global_settings_before_include_and_earlier_vendor_filename(self):
        self.provider.rename(self.dropins / "000000-provider.conf")
        self.provider = self.dropins / "000000-provider.conf"
        self.original = b"PermitRootLogin yes\nPasswordAuthentication yes\n" + self.original
        self.config.write_bytes(self.original)
        self.assert_success(self.run_phase("04-ssh-hardening-final.sh"))
        self.assert_final()
        self.assertEqual(POLICY.remove_policy(self.config.read_bytes()), self.original)

    def test_match_conflict_still_stops_reload_with_actual_value(self):
        self.config.write_bytes(self.original + b"Match User root\n    PermitRootLogin yes\n")
        result = self.run_phase("04-ssh-hardening-final.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("实际：yes", result.stderr)
        self.assertFalse(self.reloads.exists())

    def test_confirmation_gate_still_stops_before_writing(self):
        result = self.run_phase("04-ssh-hardening-final.sh", confirmed=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertFalse(self.reloads.exists())

    def test_sftp_match_block_remains_compatible(self):
        self.assert_success(self.run_phase("04-ssh-hardening-final.sh", sftp=True))
        self.assert_final()
        self.assertEqual(self.effective(user="nobody")["forcecommand"], "internal-sftp -d /upload -u 0027")
        self.assertEqual(self.config.read_bytes().count(POLICY.BEGIN), 1)

    def test_dry_run_does_not_migrate_or_write_policy(self):
        legacy = self.dropins / "00-surelay-hardening.conf"
        legacy.write_bytes(FINAL)
        self.assert_success(self.run_phase("03-ssh-hardening-phase1.sh", dry_run=True))
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertEqual(legacy.read_bytes(), FINAL)


if __name__ == "__main__":
    unittest.main()
