from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "Requires POSIX Bash")
class PlatformTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.os_release = self.root / "os-release"
        self.config = self.root / "config.env"
        self.config.write_text('SERVER_ALIAS="test"\nSERVER_IP="192.0.2.10"\n')

    def run_shell(self, body, *, distro="alpine", version="3.19.9", dry_run=False):
        self.os_release.write_text(f'ID={distro}\nVERSION_ID={version}\nPRETTY_NAME="test {distro}"\n')
        script = 'source "$RRB_PROJECT_DIR/scripts/00-lib.sh"\nLOG_FILE="$TEST_ROOT/phase.log"\n'
        script += 'require_root() { :; }\n' + body
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-c", script],
            cwd=ROOT, capture_output=True, text=True, timeout=15,
            env={**os.environ, "TEST_ROOT": str(self.root), "RRB_PROJECT_DIR": str(ROOT),
                 "RRB_CONFIG_FILE": str(self.config), "RRB_OS_RELEASE_FILE": str(self.os_release),
                 "RRB_DRY_RUN": str(dry_run).lower(), "RRB_VERBOSE": "false"},
        )

    def assert_ok(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_alpine_version_gate_is_deliberately_bounded(self):
        for version, accepted in [("3.19", True), ("3.19.9", True), ("3.18.9", False), ("3.20", False), ("edge", False)]:
            with self.subTest(version=version):
                result = self.run_shell("require_supported_os", version=version)
                self.assertEqual(result.returncode == 0, accepted, result.stderr)

    def test_auto_defaults_follow_platform_without_rewriting_config(self):
        original = self.config.read_bytes()
        for distro, method, confdir in [("alpine", "release", "http.d"), ("debian", "apt", "sites-available"), ("ubuntu", "apt", "sites-available")]:
            with self.subTest(distro=distro):
                result = self.run_shell('load_config; printf "%s\\n" "$INSTALL_SINGBOX_METHOD" "$NGINX_FALLBACK_CONF"', distro=distro)
                self.assert_ok(result)
                self.assertEqual(result.stdout.splitlines(), [method, f"/etc/nginx/{confdir}/reality-relay-bootstrap.conf"])
                self.assertEqual(self.config.read_bytes(), original)

    def test_alpine_rejects_apt_and_233boy_before_deployment(self):
        for setting in ['INSTALL_SINGBOX_METHOD="apt"', 'INSTALL_SINGBOX_METHOD="233boy"', 'USE_233BOY_INSTALLER="true"']:
            with self.subTest(setting=setting):
                self.config.write_text(f'SERVER_ALIAS="test"\n{setting}\n')
                result = self.run_shell('load_config; printf UNEXPECTED_SUCCESS')
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("UNEXPECTED_SUCCESS", result.stdout)
                self.assertIn("Alpine", result.stderr)

    def test_apk_maps_service_packages_and_does_not_consume_terminal_input(self):
        result = self.run_shell(r'''
apk() {
  printf '%s\n' "$*" >"$TEST_ROOT/calls"
  if read -r line; then return 99; fi
}
pkg_install openssh-server ufw fail2ban nginx python3 <<<'USER INPUT'
''')
        self.assert_ok(result)
        self.assertEqual((self.root / "calls").read_text().strip(),
                         "add --no-cache --wait 60 openssh openssh-server-common-openrc shadow ufw ufw-openrc ip6tables fail2ban fail2ban-openrc nginx nginx-openrc python3")

    def test_apk_failure_stops_and_dry_run_never_installs(self):
        body = 'apk() { printf CALLED >"$TEST_ROOT/calls"; return 23; }; pkg_install curl; printf FINISHED'
        result = self.run_shell(body)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("FINISHED", result.stdout)
        (self.root / "calls").unlink()
        result = self.run_shell(body, dry_run=True)
        self.assert_ok(result)
        self.assertFalse((self.root / "calls").exists())

    def test_service_lifecycle_dispatch_and_reload_fallback(self):
        for distro in ("alpine", "debian"):
            with self.subTest(distro=distro):
                result = self.run_shell(r'''
rc-service() { printf 'rc-service %s\n' "$*" >>"$TEST_ROOT/calls"; [[ "$2" != reload ]]; }
rc-update() { printf 'rc-update %s\n' "$*" >>"$TEST_ROOT/calls"; }
systemctl() { printf 'systemctl %s\n' "$*" >>"$TEST_ROOT/calls"; [[ "$1" != reload ]]; }
: >"$TEST_ROOT/calls"
service_enable demo
service_reload demo
service_active demo
service_disable demo
service_daemon_reload
''', distro=distro)
                self.assert_ok(result)
                calls = (self.root / "calls").read_text().splitlines()
                expected = (["rc-update add demo default", "rc-service demo reload", "rc-service demo restart",
                             "rc-service demo status", "rc-service demo status", "rc-service demo stop", "rc-update del demo default"]
                            if distro == "alpine" else
                            ["systemctl enable demo", "systemctl reload demo", "systemctl restart demo",
                             "systemctl is-active --quiet demo", "systemctl disable --now demo", "systemctl daemon-reload"])
                self.assertEqual(calls, expected)

    def test_service_wait_handles_reload_race_but_remains_bounded(self):
        result = self.run_shell(r'''
count=0
sleep() { :; }
service_active() { count=$((count + 1)); (( count >= 3 )); }
service_wait_active sshd
printf '%s\n' "$count"
count=0
service_active() { count=$((count + 1)); return 1; }
if service_wait_active sshd; then exit 90; fi
printf '%s\n' "$count"
''')
        self.assert_ok(result)
        self.assertEqual(result.stdout.splitlines(), ["3", "5"])

    def test_bad_release_checksum_stops_before_extraction_or_replacement(self):
        result = self.run_shell(r'''
export TMPDIR="$TEST_ROOT"
curl() {
  printf '%s\n' "$*" >"$TEST_ROOT/download"
  printf 'invalid archive' >"${*: -1}"
}
tar() { touch "$TEST_ROOT/extracted"; return 99; }
install_singbox_release
''')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("校验失败", result.stderr)
        self.assertIn("-musl.tar.gz", (self.root / "download").read_text())
        self.assertFalse((self.root / "extracted").exists())
        self.assertEqual(list(self.root.glob("tmp.*")), [])

    def test_openrc_arguments_survive_spaces_quotes_and_shell_metacharacters(self):
        arguments = ["run", "-c", "/tmp/a b's $(touch SHOULD_NOT_EXIST);config.json"]
        args = " ".join(shlex.quote(value) for value in arguments)
        result = self.run_shell(r'''
backup_path() { :; }
run() { :; }
# Capture only conf.d data, without touching host /etc.
write_root_file() {
  if [[ "$1" == /etc/conf.d/* ]]; then cat >"$TEST_ROOT/conf"; else cat >"$TEST_ROOT/init"; fi
}
install_openrc_service demo /usr/local/bin/sing-box /etc/sing-box '' ''' + args)
        self.assert_ok(result)
        result = subprocess.run(
            ["sh", "-c", '. "$1"; eval "set -- $command_args"; printf "%s\\n" "$@"', "test", str(self.root / "conf")],
            cwd=self.root, text=True, capture_output=True,
        )
        self.assert_ok(result)
        self.assertEqual(result.stdout.splitlines(), arguments)
        self.assertFalse((self.root / "SHOULD_NOT_EXIST").exists())
        self.assertEqual((self.root / "init").read_bytes(), (ROOT / "templates/rrb-service.openrc").read_bytes())

    def test_ssh_log_check_requires_actual_delivery(self):
        body = r'''
FAIL2BAN_SSH_LOG="$TEST_ROOT/ssh.log"
service_active() { return 0; }
sleep() { :; }
logger() { printf '%s\n' "${*: -1}" >>"$FAIL2BAN_SSH_LOG"; }
prepare_ssh_log
'''
        self.assert_ok(self.run_shell(body))
        (self.root / "ssh.log").write_text("")
        result = self.run_shell(body.replace('printf \'%s\\n\' "${*: -1}" >>"$FAIL2BAN_SSH_LOG"', ':'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("authpriv", result.stderr)

    def test_shared_phase_backup_preserves_first_file_and_directory_snapshot(self):
        result = self.run_shell(r'''
BACKUP_DIR="$TEST_ROOT/backup"
mkdir -p "$TEST_ROOT/source-dir"
printf original >"$TEST_ROOT/source-file"
printf original >"$TEST_ROOT/source-dir/item"
backup_path "$TEST_ROOT/source-file"
backup_path "$TEST_ROOT/source-dir"
printf changed >"$TEST_ROOT/source-file"
printf changed >"$TEST_ROOT/source-dir/item"
backup_path "$TEST_ROOT/source-file"
backup_path "$TEST_ROOT/source-dir"
cat "$BACKUP_DIR/${TEST_ROOT#/}/source-file"
cat "$BACKUP_DIR/${TEST_ROOT#/}/source-dir/item"
test ! -d "$BACKUP_DIR/${TEST_ROOT#/}/source-dir/source-dir"
''')
        self.assert_ok(result)
        self.assertEqual(result.stdout, "originaloriginal")

    def test_entry_installs_missing_python_even_when_git_is_present(self):
        installer = (ROOT / "install.sh").read_text().rsplit('\nmain "$@"', 1)[0]
        script = installer + r'''
command() {
  if [[ "$*" == '-v python3' ]]; then return 1; fi
  builtin command "$@"
}
apk() { printf '%s\n' "$*" >"$TEST_ROOT/calls"; }
ensure_entry_dependencies
'''
        result = self.run_shell(script)
        self.assert_ok(result)
        self.assertIn("python3", (self.root / "calls").read_text())

    def test_all_shell_entrypoints_parse(self):
        for path in [*ROOT.glob("*.sh"), *(ROOT / "scripts").glob("*.sh")]:
            with self.subTest(path=path.name):
                result = subprocess.run(["bash", "-n", str(path)], text=True, capture_output=True)
                self.assert_ok(result)


if __name__ == "__main__":
    unittest.main()
