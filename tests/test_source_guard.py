"""Installed pre-start guard behavior with all kernel checks mocked."""
from __future__ import annotations

import sys
import os
import contextlib
import io
import json
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import source_guard as guard
from tests import test_source_policy as policy_tests
from tests import test_source_firewall as firewall_tests


class SourceGuardTest(unittest.TestCase):
    def payload(self, **updates):
        env = policy_tests.SourcePolicyTest().env(SINGBOX_CONFIG_PATH="/etc/sing-box/config.json", **updates)
        return guard.source_policy.guard_payload(env)

    @staticmethod
    def inbound(kind="http", port=58080, listen="0.0.0.0"):
        return {"inbounds": [{"type": kind, "listen": listen, "listen_port": port}]}

    def test_old_public_proxy_cannot_hide_behind_disabled_env_switch(self):
        payload = self.payload(PROXY_SOURCE_ALLOWLIST="192.0.2.10")
        with patch.object(guard.source_firewall, "check") as check:
            with self.assertRaises(ValueError):
                guard.check(payload, config=self.inbound())
        check.assert_not_called()

    def test_changed_allowlist_cannot_start_using_stale_kernel_plan(self):
        payload = self.payload(ENABLE_HTTP="true", PROXY_SOURCE_ALLOWLIST="192.0.2.10")
        current = {**payload["env"], "PROXY_SOURCE_ALLOWLIST": "198.51.100.10"}
        with patch.object(guard.source_firewall, "check") as check:
            with self.assertRaises(ValueError):
                guard.check(payload, config=self.inbound(), env=current)
        check.assert_not_called()

    def test_new_ipv6_listener_needs_applied_ipv6_policy(self):
        payload = self.payload(ENABLE_HTTP="true", PROXY_SOURCE_ALLOWLIST="192.0.2.10")
        self.assertFalse(payload["plan"]["ipv6"])
        with patch.object(guard.source_firewall, "check") as check:
            with self.assertRaises(ValueError):
                guard.check(payload, config=self.inbound(listen="::"))
        check.assert_not_called()

    def test_start_rechecks_complete_applied_kernel_chain(self):
        payload = self.payload(ENABLE_HTTP="true", PROXY_SOURCE_ALLOWLIST="192.0.2.10",
                               DIRECT_PORT="443", WEB_SOURCE_ALLOWLIST="198.51.100.10")
        with patch.object(guard.source_firewall, "check") as check:
            guard.check(payload, config=self.inbound())
        check.assert_called_once_with(payload["plan"])
        self.assertEqual({entry["port"] for entry in check.call_args.args[0]["entries"]}, {443, 58080})

    def test_inactive_or_incomplete_actual_firewall_never_becomes_success(self):
        payload = self.payload(ENABLE_HTTP="true", PROXY_SOURCE_ALLOWLIST="192.0.2.10")
        with patch.object(guard.source_firewall, "check", side_effect=RuntimeError("missing active rules")):
            with self.assertRaises(RuntimeError):
                guard.check(payload, config=self.inbound())

    def test_subscription_port_change_is_rejected_before_start(self):
        payload = self.payload(SUBSCRIPTION_PORT="8080", SUBSCRIPTION_LISTEN_PORT="8080",
                               WEB_SOURCE_ALLOWLIST="192.0.2.10")
        payload["subscription"]["ready"] = True
        with patch.object(guard.source_firewall, "check") as check:
            with self.assertRaises(ValueError):
                guard.check(payload, "subscription", listen_port=443)
        check.assert_not_called()

    def test_subscription_requires_ready_and_actual_binding_families_match_refreshed_policy(self):
        private = self.payload(SUBSCRIPTION_INTERNAL_PORT="443", SUBSCRIPTION_LISTEN_PORT="443")
        private["subscription"]["ready"] = True
        public = self.payload(SUBSCRIPTION_PORT="443", SUBSCRIPTION_LISTEN_PORT="443",
                              WEB_SOURCE_ALLOWLIST="192.0.2.10")
        public["subscription"]["ready"] = True
        unprepared = self.payload(SUBSCRIPTION_PORT="443", SUBSCRIPTION_LISTEN_PORT="443",
                                  WEB_SOURCE_ALLOWLIST="192.0.2.10")
        cases = (
            (private, {"listen_port": 443, "bind": "public", "ipv4": True, "ipv6": False}),
            (public, {"listen_port": 443, "bind": "public", "ipv4": True, "ipv6": True}),
            (unprepared, {"listen_port": 443, "bind": "public", "ipv4": True, "ipv6": False}),
        )
        for payload, actual in cases:
            with self.subTest(binding=payload["subscription"], actual=actual), \
                    patch.object(guard.source_firewall, "check") as check:
                with self.assertRaises(ValueError):
                    guard.check(payload, "subscription", **actual)
                check.assert_not_called()
        # Same port, but a public old unit must not inherit a newly private plan.
        live_env = {**public["env"], "SUBSCRIPTION_PORT": "", "SUBSCRIPTION_INTERNAL_PORT": "443"}
        with patch.object(guard.source_firewall, "check") as check:
            with self.assertRaises(ValueError):
                guard.check(public, "subscription", listen_port=443, bind="public", ipv4=True, ipv6=False,
                            env=live_env)
            check.assert_not_called()

    def test_refreshed_public_private_subscription_metadata_and_native_cli_binding_gate(self):
        for public in (False, True):
            payload = self.payload(SUBSCRIPTION_PORT="443" if public else "",
                                   SUBSCRIPTION_INTERNAL_PORT="443", SUBSCRIPTION_LISTEN_PORT="443",
                                   WEB_SOURCE_ALLOWLIST="192.0.2.10" if public else "")
            payload["subscription"]["ready"] = True
            with self.subTest(public=public), patch.object(guard.source_firewall, "check") as check:
                guard.check(payload, "subscription", listen_port=443,
                            bind="public" if public else "localhost", ipv4=True, ipv6=False)
                if public:
                    check.assert_called_once_with(payload["plan"])
                else:
                    check.assert_not_called()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "guard.json"
            payload = self.payload(SUBSCRIPTION_INTERNAL_PORT="443", SUBSCRIPTION_LISTEN_PORT="443")
            payload["subscription"]["ready"] = True
            path.write_text(json.dumps(payload), encoding="utf-8")
            args = ["source_guard", "--plan", str(path), "--service", "subscription", "--port", "443",
                    "--ipv4", "true", "--ipv6", "false"]
            with patch.object(sys, "argv", args), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(guard.main(), 1, "old units without a captured bind must stay blocked")
            with patch.object(sys, "argv", args + ["--bind", "localhost"]):
                self.assertEqual(guard.main(), 0)

    def test_native_guard_execute_never_runs_after_rejection_and_preserves_exact_argv_on_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "guard.json"
            payload = self.payload(SUBSCRIPTION_INTERNAL_PORT="443", SUBSCRIPTION_LISTEN_PORT="443")
            command = ["/opt/core with space/sing-box", "run", "-c", "/etc/config notes%fixture.json"]
            args = ["source_guard", "--plan", str(path), "--service", "subscription", "--port", "443",
                    "--bind", "localhost", "--ipv4", "true", "--ipv6", "false", "--execute", *command]
            path.write_text(json.dumps(payload), encoding="utf-8")
            with patch.object(sys, "argv", args), patch.object(guard.os, "execv") as execute, \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(guard.main(), 1)
                execute.assert_not_called()
            payload["subscription"]["ready"] = True
            path.write_text(json.dumps(payload), encoding="utf-8")
            with patch.object(sys, "argv", args), patch.object(guard.os, "execv") as execute:
                self.assertEqual(guard.main(), 0)
                execute.assert_called_once_with(command[0], command)

    @unittest.skipUnless(firewall_tests.BASH, "Requires background Bash")
    def test_service_writers_pin_the_exact_checked_json_and_replace_legacy_config_arguments(self):
        script = (ROOT / "scripts/00-lib.sh").read_text(encoding="utf-8")
        start = script.index("write_source_systemd_guard() {")
        function = script[start:script.index("\nwrite_source_openrc_guard()", start)]
        with tempfile.TemporaryDirectory(prefix="source-unit-") as directory:
            root = Path(directory)
            config = (root / 'config notes%${NAME}fixture.json').as_posix()
            core_bin = (root / "core bin%${NAME}fixture/sing-box").as_posix()
            output = root / "source.conf"
            harness = ('set -euo pipefail\nRRB_STATE_DIR="$1"\nSINGBOX_CONFIG_PATH="$2"\nCORE_BIN="$3"\nUNIT_OUTPUT="$4"\n'
                       'ENABLE_UFW=true\nrun() { :; }\nservice_daemon_reload() { :; }\n'
                       'singbox_bin() { printf "%s\\n" "$CORE_BIN"; }\n'
                       'write_root_file() { cat > "$UNIT_OUTPUT"; }\n'
                       f'python3() {{ {shlex.quote(Path(sys.executable).as_posix())} "$@"; }}\n'
                       + function + f'\nwrite_source_systemd_guard {shlex.quote(root.name)} sing-box\n')
            driver = root / "writer.sh"
            driver.write_text(harness, encoding="utf-8")
            result = subprocess.run([firewall_tests.BASH, "--noprofile", "--norc", driver.as_posix(),
                                     root.as_posix(), config, core_bin, output.as_posix()],
                                    capture_output=True, text=True, encoding="utf-8", timeout=15,
                                    env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = output.read_text(encoding="utf-8").splitlines()
            commands = [line[len("ExecStart="):] for line in lines if line.startswith("ExecStart=")]
            self.assertTrue(commands, (lines, result.stderr))
            self.assertEqual(commands[0], "", "clear any inherited -C directory or old config first")
            self.assertEqual(len(commands), 2)
            actual = [arg.replace("%%", "%").replace("$$", "$") for arg in shlex.split(commands[1])]
            self.assertIn("StateDirectory=sing-box", lines)
            self.assertEqual(actual, [core_bin, "-D", "/var/lib/sing-box", "run", "-c", config])
            self.assertIn("%%", commands[1], "systemd percent specifiers must be escaped")
            self.assertIn("$${NAME}", commands[1], "systemd environment expansion must be escaped")

            # Execute the OpenRC writer's emitted Python against a private conf
            # fixture, never its /etc paths, and inspect the resulting shell args.
            openrc = script[script.index("write_source_openrc_guard() {"):script.index("\nmark_subscription_source_ready()")]
            begin = openrc.index("import re, shlex, sys\n")
            body = openrc[begin:openrc.index("\nPY", begin)]
            conf = root / "openrc.conf"
            conf.write_text("command_args='run -C /old/directory'\nrrb_check_config=/old/config.json\n", encoding="utf-8")
            result = subprocess.run([sys.executable, "-B", "-", conf.as_posix(), (root / "guard.json").as_posix(),
                                     "sing-box", "", "true", "unprepared", "false", "false", config],
                                    input=body, capture_output=True, text=True, encoding="utf-8", timeout=15,
                                    env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            self.assertEqual(result.returncode, 0, result.stderr)
            assignments = {key: shlex.split(value)[0] for key, value in (
                line.split("=", 1) for line in conf.read_text().splitlines() if "=" in line)}
            self.assertEqual(shlex.split(assignments["command_args"]), ["run", "-c", config])
            self.assertEqual(assignments["rrb_check_config"], config)

    def test_safe_unrestricted_start_does_not_require_kernel_tools(self):
        payload = self.payload()
        with patch.object(guard.source_firewall, "check") as check:
            guard.check(payload, config={"inbounds": []})
        check.assert_not_called()

    @unittest.skipUnless(firewall_tests.BASH, "Requires background Bash")
    def test_firewall_phase_stops_legacy_services_even_with_empty_new_plan_and_refreshes_subscription(self):
        script = (ROOT / "scripts/09-apply-firewall.sh").read_text(encoding="utf-8")
        start = script.index("source_policy_plan=")
        end = script.index('\nif [[ "$ENABLE_UFW"', start)
        lifecycle = script[start:end]
        harness = r'''
set -e
RRB_STATE_DIR="$1"
SCRIPT_DIR=/fixture/scripts
SINGBOX_CONFIG_PATH=/fixture/config.json
SURELAY_SECURITY_ONLY=false
SUBSCRIPTION_SERVICE=surelay-subscription
RRB_SOURCE_KEEP_STOPPED=false
is_dry_run() { return 1; }
service_active() { return 0; }
service_stop() { printf 'stop %s\n' "$1" >> "$RRB_STATE_DIR/events"; }
service_restart() { printf 'restart %s\n' "$1" >> "$RRB_STATE_DIR/events"; }
singbox_check() { printf 'check %s\n' "$1" >> "$RRB_STATE_DIR/events"; }
python3() {
  if [[ "$1" == -c ]]; then cat >/dev/null; printf 'false\n';
  else printf '{"version":1,"ipv6":false,"entries":[]}\n'; fi
}
bash() { printf 'subscription %s %s\n' "$RRB_SOURCE_POLICY_READY" "$1" >> "$RRB_STATE_DIR/events"; }
die() { printf '%s\n' "$*" >&2; exit 99; }
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = subprocess.run([firewall_tests.BASH, "--noprofile", "--norc", "-c",
                                     harness + lifecycle + "\nrestore_source_services\n", "firewall", root.as_posix()],
                                    capture_output=True, text=True, encoding="utf-8", timeout=15,
                                    env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / "events").read_text().splitlines(), [
                "stop sing-box", "stop surelay-subscription", "check /fixture/config.json",
                "restart sing-box", "subscription true /fixture/scripts/13-setup-subscription.sh"])


if __name__ == "__main__":
    unittest.main()
