"""Kernel ACL contract with a fake command runner; never touches host firewall."""
from __future__ import annotations

import importlib.util
import ipaddress
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("source_firewall_test", ROOT / "scripts/source_firewall.py")
firewall = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(firewall)
GIT_BASH = Path("C:/Program Files/Git/bin/bash.exe")
BASH = str(GIT_BASH) if os.name == "nt" and GIT_BASH.is_file() else shutil.which("bash") if os.name != "nt" else None


def rule_text(rules):
    return "\n".join(shlex.join(rule) for rule in rules) + "\n"


class SourceFirewallTest(unittest.TestCase):
    def plan(self, **updates):
        return {"version": 1, "ipv6": True,
                "entries": [{"port": 58080, "sources": ["192.0.2.0/24"]}], **updates}

    def runner(self, plan, *, inputs=None, chains=None, active=True):
        inputs = inputs or {4: [["-A", "INPUT", "-j", "SURELAY_SOURCE"],
                               ["-A", "INPUT", "-j", "ACCEPT"]],
                            6: [["-A", "INPUT", "-j", "SURELAY_SOURCE"]]}
        chains = chains or {version: firewall.expected_rules(plan, version) for version in (4, 6)}
        calls = []

        def invoke(args, *, input=None, check=True):
            calls.append((list(args), input))
            name = Path(args[0]).name
            if name == "ufw":
                body = "Status: active\n" if active else "Status: inactive\n"
            elif name in {"iptables", "ip6tables"} and "-S" in args:
                version = 6 if name == "ip6tables" else 4
                chain = args[args.index("-S") + 1]
                body = rule_text(inputs[version] if chain == "INPUT" else chains[version])
            else:
                raise AssertionError(f"unexpected command: {args}")
            return subprocess.CompletedProcess(args, 0, body, "")

        return invoke, calls

    def test_guard_precedes_legacy_wide_allow_and_ipv6_has_explicit_denial(self):
        plan = self.plan()
        runner, calls = self.runner(plan)
        firewall.check(plan, runner=runner)
        self.assertTrue(any(Path(args[0]).name == "ip6tables" for args, _ in calls))
        ipv6 = firewall.expected_rules(plan, 6)
        self.assertTrue(any("--dport" in rule and "58080" in rule and rule[-1] == "DROP" for rule in ipv6))
        self.assertFalse(any("-s" in rule for rule in ipv6))

    def test_allowlist_permits_only_matching_sources_on_the_protected_port(self):
        rules = firewall.expected_rules(self.plan(), 4)

        def verdict(source, port, interface="eth0"):
            for rule in rules:
                if "-i" in rule and rule[rule.index("-i") + 1] != interface:
                    continue
                if "--dport" in rule and int(rule[rule.index("--dport") + 1]) != port:
                    continue
                if "-s" in rule and ipaddress.ip_address(source) not in ipaddress.ip_network(rule[rule.index("-s") + 1]):
                    continue
                return rule[rule.index("-j") + 1]
            return "RETURN"

        self.assertEqual(verdict("192.0.2.10", 58080), "RETURN")
        self.assertEqual(verdict("198.51.100.10", 58080), "DROP")
        self.assertEqual(verdict("198.51.100.10", 52322), "RETURN")
        self.assertEqual(verdict("127.0.0.1", 58080, "lo"), "RETURN")

    def test_late_jump_missing_deny_and_inactive_firewall_fail_closed(self):
        plan = self.plan()
        bad_inputs = {4: [["-A", "INPUT", "-j", "ACCEPT"], ["-A", "INPUT", "-j", "SURELAY_SOURCE"]],
                      6: [["-A", "INPUT", "-j", "SURELAY_SOURCE"]]}
        variants = [self.runner(plan, inputs=bad_inputs), self.runner(plan, active=False)]
        for version in (4, 6):
            chains = {family: firewall.expected_rules(plan, family) for family in (4, 6)}
            chains[version] = [rule for rule in chains[version] if rule[-1] != "DROP"]
            variants.append(self.runner(plan, chains=chains))
        for runner, _ in variants:
            with self.subTest(runner=runner), self.assertRaises((ValueError, RuntimeError, SystemExit)):
                firewall.check(plan, runner=runner)

    def test_empty_plan_check_needs_no_firewall_tools(self):
        def unexpected(*args, **kwargs):
            self.fail("empty policy must not require firewall tools")
        firewall.check(self.plan(entries=[]), runner=unexpected)

    def test_wildcard_or_malformed_plan_never_reaches_tools(self):
        for entry in ({"port": 58080, "sources": ["0.0.0.0/0"]},
                      {"port": 0, "sources": ["192.0.2.1"]},
                      {"port": 58080, "sources": []}):
            with self.subTest(entry=entry), self.assertRaises((ValueError, RuntimeError, SystemExit)):
                firewall.normalize_plan(self.plan(entries=[entry]))

    def test_apply_rebuilds_only_owned_chain_before_installing_first_input_jump(self):
        plan = self.plan()
        state = {version: {"INPUT": [["-A", "INPUT", "-j", "ACCEPT"]],
                           "SURELAY_SOURCE": None} for version in (4, 6)}
        calls = []

        def runner(args, *, input=None, check=True):
            calls.append((list(args), input))
            name = Path(args[0]).name
            version = 6 if name.startswith("ip6tables") else 4
            if name == "ufw":
                body = "Status: active\n"
            elif name.endswith("-restore"):
                self.assertIn("--noflush", args)
                self.assertNotIn(":INPUT", input)
                self.assertNotIn("-F INPUT", input)
                state[version]["SURELAY_SOURCE"] = firewall.parse_rules(input)
                body = ""
            elif "-I" in args:
                self.assertEqual(args[-4:], ["INPUT", "1", "-j", "SURELAY_SOURCE"])
                self.assertTrue(state[version]["SURELAY_SOURCE"], "install policy before linking it")
                state[version]["INPUT"].insert(0, ["-A", "INPUT", "-j", "SURELAY_SOURCE"])
                body = ""
            elif "-S" in args:
                rules = state[version][args[args.index("-S") + 1]]
                if rules is None:
                    return subprocess.CompletedProcess(args, 1, "", "No chain/target/match by that name.")
                body = rule_text(rules)
            else:
                self.fail(f"unexpected firewall operation: {args}")
            return subprocess.CompletedProcess(args, 0, body, "")

        firewall.apply(plan, runner=runner)
        for version in (4, 6):
            self.assertEqual(state[version]["INPUT"][0], ["-A", "INPUT", "-j", "SURELAY_SOURCE"])
            self.assertEqual(state[version]["INPUT"][1], ["-A", "INPUT", "-j", "ACCEPT"])
        self.assertEqual(sum(Path(args[0]).name.endswith("-restore") for args, _ in calls), 2)

    def test_same_named_unowned_chain_is_not_overwritten(self):
        plan = self.plan()
        calls = []

        def runner(args, *, input=None, check=True):
            calls.append(args)
            if "-S" in args and args[-1] == "SURELAY_SOURCE":
                return subprocess.CompletedProcess(args, 0, "-N SURELAY_SOURCE\n-A SURELAY_SOURCE -j RETURN\n", "")
            self.fail(f"unowned chain must be detected before mutation: {args}")

        with self.assertRaises(firewall.FirewallError):
            firewall.apply(plan, runner=runner)
        self.assertTrue(calls)
        self.assertFalse(any("-I" in args or Path(args[0]).name.endswith("-restore") for args in calls))

    def test_nft_missing_chain_is_verified_before_first_apply_for_both_families(self):
        plan = self.plan()
        state = {version: {"INPUT": [["-A", "INPUT", "-j", "ufw-before-input"]],
                           "SURELAY_SOURCE": None} for version in (4, 6)}
        inventories = set()

        def runner(args, *, input=None, check=True):
            name = Path(args[0]).name
            if name == "nft":
                family = args[-1]
                self.assertEqual(args, ["nft", "--json", "list", "chains", family])
                version = 6 if family == "ip6" else 4
                inventories.add(version)
                # The same name in another table is not the filter chain.
                body = json.dumps({"nftables": [{"metainfo": {"version": "1.0.6"}},
                    {"chain": {"family": family, "table": "filter", "name": "ufw-before-input"}},
                    {"chain": {"family": family, "table": "nat", "name": "SURELAY_SOURCE"}}]})
            elif name == "ufw":
                body = "Status: active\n"
            else:
                version = 6 if name.startswith("ip6tables") else 4
                if name.endswith("-restore"):
                    self.assertIn(version, inventories, "prove the chain is absent before creation")
                    state[version]["SURELAY_SOURCE"] = firewall.parse_rules(input)
                    body = ""
                elif "-I" in args:
                    state[version]["INPUT"].insert(0, ["-A", "INPUT", "-j", "SURELAY_SOURCE"])
                    body = ""
                elif "-S" in args:
                    rules = state[version][args[-1]]
                    if rules is None:
                        error = f"{name} v1.8.9 (nf_tables): chain `SURELAY_SOURCE' in table `filter' is incompatible, use 'nft' tool."
                        return subprocess.CompletedProcess(args, 1, "", error)
                    body = rule_text(rules)
                else:
                    self.fail(f"unexpected firewall operation: {args}")
            return subprocess.CompletedProcess(args, 0, body, "")

        firewall.apply(plan, runner=runner)
        self.assertEqual(inventories, {4, 6})
        for version in (4, 6):
            self.assertEqual(state[version]["SURELAY_SOURCE"], firewall.expected_rules(plan, version))

    def test_nft_existing_incompatible_chain_and_unreadable_inventory_fail_closed(self):
        for version in (4, 6):
            family = "ip6" if version == 6 else "ip"
            existing = json.dumps({"nftables": [{"chain": {
                "family": family, "table": "filter", "name": "SURELAY_SOURCE"}}]})
            for response in (subprocess.CompletedProcess([], 0, existing, ""),
                             subprocess.CompletedProcess([], 1, "", "Operation not permitted"),
                             subprocess.CompletedProcess([], 1, "", "No such file or directory"),
                             subprocess.CompletedProcess([], 0, "not JSON", ""),
                             subprocess.CompletedProcess([], 0, '{"nftables":[{"chain":{"name":"other"}}]}', ""),
                             subprocess.CompletedProcess([], 0, '{"nftables":[{"error":"fixture"}]}', ""),
                             firewall.FirewallError("nft executable unavailable")):
                calls = []

                def runner(args, *, input=None, check=True):
                    calls.append(args)
                    if args[0] == "nft":
                        if isinstance(response, Exception):
                            raise response
                        return response
                    self.assertIn("-S", args)
                    error = f"{args[0]} v1.8.9 (nf_tables): chain `SURELAY_SOURCE' in table `filter' is incompatible, use 'nft' tool."
                    return subprocess.CompletedProcess(args, 1, "", error)

                with self.subTest(version=version, response=response), self.assertRaises(firewall.FirewallError):
                    firewall._owned_chain(version, runner)
                self.assertEqual(len(calls), 2)
                self.assertFalse(any("-I" in args or Path(args[0]).name.endswith("-restore") for args in calls))

    def test_other_iptables_errors_do_not_invoke_nft_or_create_chain(self):
        for status, error in ((4, "Permission denied"), (1, "fixture failure"),
                              (1, "iptables v1.8.9 (nf_tables): Permission denied"),
                              (1, "another chain is incompatible, use 'nft' tool.")):
            calls = []

            def runner(args, *, input=None, check=True):
                calls.append(args)
                return subprocess.CompletedProcess(args, status, "", error)

            with self.subTest(error=error), self.assertRaises(firewall.FirewallError):
                firewall._owned_chain(4, runner)
            self.assertEqual(len(calls), 1)

    def test_nonraising_runner_errors_still_stop_apply(self):
        calls = []

        def runner(args, *, input=None, check=True):
            calls.append(args)
            return subprocess.CompletedProcess(args, 23, "", "fixture failure")

        with self.assertRaises(firewall.FirewallError):
            firewall.apply(self.plan(), runner=runner)
        self.assertEqual(len(calls), 1)

    @unittest.skipUnless(BASH, "Requires background Bash")
    def test_existing_ufw_hook_exit_or_exec_does_not_bypass_restoration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker, helper, hook = root / "restored", root / "helper.py", root / "after.init"
            helper.write_text("from pathlib import Path\n" + f"Path({str(marker)!r}).write_text('restored')\n", encoding="utf-8")
            for original, status in (("#!/bin/sh\nexit 0\n", 0),
                                     ("#!/bin/sh\nexec true\n", 0),
                                     ("#!/bin/sh\nexit 7\n", 7)):
                with self.subTest(original=original):
                    marker.unlink(missing_ok=True)
                    content = firewall.after_hook_text(original, helper, root / "plan.json")
                    content = content.replace("/usr/bin/python3", shlex.quote(Path(sys.executable).as_posix()))
                    hook.write_text(content, encoding="utf-8")
                    result = subprocess.run([BASH, "--noprofile", "--norc", hook.as_posix(), "start"],
                                            capture_output=True, text=True, encoding="utf-8", timeout=15)
                    self.assertEqual(result.returncode, status, result.stderr)
                    self.assertEqual(marker.read_text(), "restored")

    def test_persist_preserves_original_hook_once_and_writes_exact_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path, helper, hook = root / "policy.json", root / "source_firewall.py", root / "after.init"
            hook.write_text("#!/bin/sh\nprintf original-hook\\n\nexit 0\n", encoding="utf-8")
            plan = self.plan()
            firewall.persist(plan, plan_path, helper_path=helper, hook_path=hook)
            first = hook.read_text(encoding="utf-8")
            firewall.persist(plan, plan_path, helper_path=helper, hook_path=hook)
            self.assertEqual(hook.read_text(encoding="utf-8"), first)
            self.assertEqual(first.count("original-hook"), 1)
            self.assertEqual(json.loads(plan_path.read_text()), plan)
            self.assertEqual(helper.read_bytes(), (ROOT / "scripts/source_firewall.py").read_bytes())
            if os.name == "posix":
                self.assertEqual(plan_path.stat().st_mode & 0o777, 0o600)

    def test_damaged_existing_hook_marker_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hook = root / "after.init"
            original = "#!/bin/sh\n" + firewall.HOOK_MARKER + "\n# incomplete\n"
            hook.write_text(original, encoding="utf-8")
            with self.assertRaises(firewall.FirewallError):
                firewall.persist(self.plan(), root / "policy.json", helper_path=root / "helper.py", hook_path=hook)
            self.assertEqual(hook.read_text(encoding="utf-8"), original)

    def test_remove_unwraps_owned_hook_and_leaves_other_input_rules_and_saved_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path, helper, hook = root / "policy.json", root / "helper.py", root / "after.init"
            original = "#!/bin/sh\nprintf original-hook\\n\nexit 0\n"
            hook.write_text(original, encoding="utf-8")
            plan = self.plan()
            firewall.persist(plan, plan_path, helper_path=helper, hook_path=hook)
            state = {version: {"INPUT": [["-A", "INPUT", "-j", "SURELAY_SOURCE"],
                                         ["-A", "INPUT", "-j", "ufw-before-input"]],
                               "SURELAY_SOURCE": firewall.expected_rules(plan, version)} for version in (4, 6)}

            def runner(args, *, input=None, check=True):
                version = 6 if Path(args[0]).name.startswith("ip6tables") else 4
                if "-S" in args:
                    body = rule_text(state[version][args[args.index("-S") + 1]])
                elif "-D" in args:
                    index = args.index("-D")
                    self.assertEqual(args[index + 1:], ["INPUT", "-j", "SURELAY_SOURCE"])
                    state[version]["INPUT"].remove(["-A", "INPUT", "-j", "SURELAY_SOURCE"])
                    body = ""
                elif "-F" in args or "-X" in args:
                    self.assertEqual(args[-1], "SURELAY_SOURCE")
                    body = ""
                else:
                    self.fail(f"unexpected removal command: {args}")
                return subprocess.CompletedProcess(args, 0, body, "")

            firewall.remove_persistence(plan_path, runner=runner, helper_path=helper, hook_path=hook)
            self.assertEqual(hook.read_text(encoding="utf-8"), original)
            self.assertTrue(plan_path.is_file())
            self.assertTrue(helper.is_file())
            for family in (4, 6):
                self.assertEqual(state[family]["INPUT"], [["-A", "INPUT", "-j", "ufw-before-input"]])

    def test_remove_preserves_a_same_named_unowned_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path = root / "policy.json"
            plan_path.write_text(json.dumps(self.plan()), encoding="utf-8")
            calls = []

            def runner(args, *, input=None, check=True):
                calls.append(args)
                self.assertIn("-S", args)
                body = "-A INPUT -j SURELAY_SOURCE\n" if args[-1] == "INPUT" else "-A SURELAY_SOURCE -j RETURN\n"
                return subprocess.CompletedProcess(args, 0, body, "")

            with self.assertRaises(firewall.FirewallError):
                firewall.remove_persistence(plan_path, runner=runner, helper_path=root / "helper.py", hook_path=root / "after.init")
            self.assertTrue(calls)
            self.assertFalse(any("-D" in args or "-F" in args or "-X" in args for args in calls))


if __name__ == "__main__":
    unittest.main()
