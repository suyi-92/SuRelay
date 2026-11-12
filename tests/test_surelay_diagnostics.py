"""No network or live services: exercise proxy routing, outcomes and cleanup."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import surelay_diagnostics as diagnostics


class DiagnosticsTest(unittest.TestCase):
    def test_probe_routes_only_to_chosen_exit_and_keeps_credentials_private(self):
        row = {"type": "socks", "tag": "chosen", "server": "2001:db8::1", "server_port": 1080,
               "username": "private-user", "password": "private-password"}
        process = MagicMock()
        process.__enter__.return_value = process
        process.poll.return_value = None
        temporary_paths = []
        def launch(args, **kwargs):
            config = Path(args[-1])
            temporary_paths.append(config)
            data = json.loads(config.read_text())
            self.assertEqual(data["outbounds"], [diagnostics.GEN.family_outbound(row, "ipv4")])
            self.assertEqual(data["route"]["final"], "chosen")
            resolve = diagnostics.GEN.resolve_rule("probe", "ipv4")
            resolve.pop("source_ip_cidr", None)
            self.assertEqual(data["route"]["rules"], [resolve])
            self.assertEqual(data["inbounds"][0]["listen"], "127.0.0.1")
            self.assertNotIn("private-", " ".join(args))
            return process
        with patch.object(diagnostics.subprocess, "Popen", side_effect=launch), \
             patch.object(diagnostics.socket, "create_connection", return_value=MagicMock()), \
             patch.object(diagnostics.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "192.0.2.8\n", "")) as curl:
            ok, detail = diagnostics.probe("fixture-core", row, {"PROXY_IP_VERSION": "ipv4"})
        self.assertTrue(ok)
        self.assertIn("192.0.2.8", detail)
        args = curl.call_args.args[0]
        self.assertEqual(args[args.index("--noproxy") + 1], "")
        self.assertTrue(args[args.index("--proxy") + 1].startswith("socks5h://127.0.0.1:"))
        self.assertNotIn("private-", " ".join(args))
        process.terminate.assert_called_once()
        self.assertTrue(all(not p.exists() for p in temporary_paths))
        self.assertNotIn("domain_resolver", row)

    def test_probe_config_uses_selected_family_for_dns_and_outbound(self):
        original = {"type": "direct", "tag": "chosen"}
        for env, explicit, family, tag in (
            ({"PROXY_IP_VERSION": "ipv6"}, None, "ipv6", "chosen/ipv6"),
            ({"PROXY_IP_VERSION": "dual"}, None, "ipv4", "chosen"),
            ({"PROXY_IP_VERSION": "ipv4"}, "ipv6", "ipv6", "chosen/ipv6"),
        ):
            with self.subTest(family=family, explicit=explicit):
                config = diagnostics.probe_config(original, 1080, env, explicit)
                resolver = diagnostics.GEN.dns_server_tag(family)
                self.assertEqual(config["dns"]["final"], resolver)
                self.assertEqual(config["route"]["default_domain_resolver"]["server"], resolver)
                self.assertEqual(config["route"]["rules"][0]["server"], resolver)
                self.assertEqual(config["route"]["rules"][0]["action"], "resolve")
                self.assertNotIn("source_ip_cidr", config["route"]["rules"][0])
                self.assertEqual(config["outbounds"][0]["domain_resolver"]["server"], resolver)
                self.assertEqual(config["route"]["final"], tag)
        self.assertEqual(original, {"type": "direct", "tag": "chosen"})

    def test_ipv6_probe_reports_reachable_ipv4_fallback(self):
        process = MagicMock()
        process.__enter__.return_value = process
        process.poll.return_value = None
        with patch.object(diagnostics.subprocess, "Popen", return_value=process), \
             patch.object(diagnostics.socket, "create_connection", return_value=MagicMock()), \
             patch.object(diagnostics.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "192.0.2.8\n", "")):
            ok, detail = diagnostics.probe("fixture-core", {"type": "direct", "tag": "direct"},
                                           {"PROXY_IP_VERSION": "ipv4"}, "ipv6")
        self.assertTrue(ok)
        self.assertIn("回退 IPv4", detail)
        process.terminate.assert_called_once()

    def test_probe_failure_timeout_and_cancellation_clean_up(self):
        outcomes = [subprocess.CompletedProcess([], 97, "", "secret response"),
                    subprocess.CompletedProcess([], 0, "<html>not an IP</html>", ""),
                    subprocess.TimeoutExpired("curl", 15), KeyboardInterrupt()]
        for outcome in outcomes:
            with self.subTest(outcome=type(outcome).__name__):
                process = MagicMock()
                process.__enter__.return_value = process
                process.poll.return_value = None
                args = {"side_effect": outcome} if isinstance(outcome, BaseException) else {"return_value": outcome}
                with patch.object(diagnostics.subprocess, "Popen", return_value=process), \
                     patch.object(diagnostics.socket, "create_connection", return_value=MagicMock()), \
                     patch.object(diagnostics.subprocess, "run", **args):
                    if isinstance(outcome, KeyboardInterrupt):
                        with self.assertRaises(KeyboardInterrupt):
                            diagnostics.probe("fixture-core", {"type": "direct", "tag": "direct"}, {"PROXY_IP_VERSION": "ipv4"})
                    else:
                        ok, detail = diagnostics.probe("fixture-core", {"type": "direct", "tag": "direct"}, {"PROXY_IP_VERSION": "ipv4"})
                        self.assertFalse(ok)
                        self.assertNotIn("secret", detail)
                process.terminate.assert_called_once()

    def test_exit_checks_continue_after_failure_and_skip_system_checks(self):
        manager = Mock()
        manager.env = {"PROXY_IP_VERSION": "ipv4", "ENABLE_IPV6_LISTEN": "false"}
        manager.rows.return_value = [{"tag": "fixture", "type": "http", "server": "proxy.example", "server_port": 8080,
                                      "username": "private-user", "password": "private-password"}]
        output = io.StringIO()
        with patch.object(diagnostics, "probe", side_effect=[(False, "请求超时"), (True, "出口 IP 192.0.2.9")]) as probe, \
             contextlib.redirect_stdout(output):
            diagnostics.check_exits(manager)
        self.assertEqual(probe.call_count, 2)
        self.assertIn("通过 1 项，失败 1 项", output.getvalue())
        self.assertIn("proxy.example:8080", output.getvalue())
        self.assertNotIn("private-", output.getvalue())
        manager.phase.assert_not_called()
        manager.service.assert_not_called()
        manager.active.assert_not_called()

    def test_exit_checks_cover_every_enabled_family(self):
        manager = Mock()
        manager.env = {"PROXY_IP_VERSION": "ipv4", "ENABLE_IPV6_LISTEN": "true"}
        manager.rows.return_value = [{"tag": "fixture", "type": "http", "server": "proxy.example", "server_port": 8080,
                                      "username": "private-user", "password": "private-password"}]
        with patch.object(diagnostics, "probe", return_value=(True, "出口 IP 192.0.2.9")) as probe, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            diagnostics.check_exits(manager)
        self.assertEqual([call.args[3] for call in probe.call_args_list], ["ipv4", "ipv4", "ipv6", "ipv6"])
        self.assertIn("通过 4 项，失败 0 项", output.getvalue())
        self.assertIn("服务器直连 · IPv4", output.getvalue())
        self.assertIn("服务器直连 · IPv6", output.getvalue())
        self.assertNotIn("private-", output.getvalue())

    def test_ipv6_only_exit_checks_skip_public_ipv4(self):
        manager = Mock()
        manager.env = {"PROXY_IP_VERSION": "ipv6", "ENABLE_IPV6_LISTEN": "true", "SERVER_IP_IPV4": ""}
        manager.rows.return_value = []
        with patch.object(diagnostics, "probe", return_value=(True, "出口 IP 2001:db8::8")) as probe, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            diagnostics.check_exits(manager)
        self.assertEqual([call.args[3] for call in probe.call_args_list], ["ipv6"])
        self.assertIn("通过 1 项，失败 0 项", output.getvalue())
        self.assertNotIn("IPv4", output.getvalue())
        self.assertEqual(diagnostics.GEN.connection_families(manager.env), ["ipv4", "ipv6"])
        dual = {**manager.env, "SERVER_IP_IPV4": "192.0.2.10"}
        self.assertEqual(diagnostics.probe_families(dual), ["ipv4", "ipv6"])

    def test_install_validation_reuses_probes_and_continues_after_failures(self):
        scripts = Path(diagnostics.__file__).resolve().parent
        source = (scripts / "10-validate.sh").read_text(encoding="utf-8")
        embedded = source.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
        env = {"PROXY_IP_VERSION": "ipv4", "ENABLE_IPV6_LISTEN": "true"}
        rows = [
            {"source": "home", "tag": "home", "type": "socks5", "server": "2001:db8::1", "server_port": 1080,
             "network": "tcp", "username": "private-user", "password": "private-password"},
            {"source": "upstream", "tag": "relay", "type": "vless", "server": "upstream.example", "server_port": 443,
             "outbound": {"type": "vless", "tag": "out-relay", "server": "upstream.example", "server_port": 443}},
        ]
        results = [OSError("private-error"), (False, "请求超时"), (True, "出口 IP 192.0.2.8"),
                   (True, "出口 IP 2001:db8::8")]
        with patch.object(sys, "argv", ["-", str(scripts.parent / "config.env"), str(scripts), "fixture-core"]), \
             patch.dict(os.environ, {"RRB_VERBOSE": "true"}), \
             patch.object(diagnostics.GEN, "parse_env_file", return_value={}), \
             patch.object(diagnostics.GEN, "defaults", return_value=env), \
             patch.object(diagnostics.GEN, "load_rows", return_value=rows), \
             patch.object(diagnostics, "probe", side_effect=results) as probe, \
             contextlib.redirect_stdout(io.StringIO()) as output, \
             contextlib.redirect_stderr(io.StringIO()) as warnings:
            exec(compile(embedded, "10-validate.sh", "exec"), {})
        self.assertEqual([call.args[3] for call in probe.call_args_list], ["ipv4", "ipv4", "ipv6", "ipv6"])
        self.assertIn("socks5://[2001:db8::1]:1080", output.getvalue())
        self.assertIn("home · IPv4", warnings.getvalue())
        self.assertIn("relay · IPv4", warnings.getvalue())
        self.assertIn("仅告警不阻断", warnings.getvalue())
        self.assertNotIn("private-", output.getvalue() + warnings.getvalue())

    def test_ipv6_only_install_validation_skips_public_ipv4(self):
        scripts = Path(diagnostics.__file__).resolve().parent
        source = (scripts / "10-validate.sh").read_text(encoding="utf-8")
        embedded = source.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
        env = {"PROXY_IP_VERSION": "ipv6", "ENABLE_IPV6_LISTEN": "true", "SERVER_IP_IPV4": ""}
        rows = [{"source": "home", "tag": "home", "type": "socks5", "server": "2001:db8::1",
                 "server_port": 1080, "network": "tcp", "username": "", "password": ""}]
        with patch.object(sys, "argv", ["-", str(scripts.parent / "config.env"), str(scripts), "fixture-core"]), \
             patch.dict(os.environ, {"RRB_VERBOSE": "true"}), \
             patch.object(diagnostics.GEN, "parse_env_file", return_value={}), \
             patch.object(diagnostics.GEN, "defaults", return_value=env), \
             patch.object(diagnostics.GEN, "load_rows", return_value=rows), \
             patch.object(diagnostics, "probe", return_value=(True, "出口 IP 2001:db8::8")) as probe, \
             contextlib.redirect_stdout(io.StringIO()) as output, \
             contextlib.redirect_stderr(io.StringIO()) as warnings:
            exec(compile(embedded, "10-validate.sh", "exec"), {})
        self.assertEqual([call.args[3] for call in probe.call_args_list], ["ipv6"])
        self.assertNotIn("IPv4", output.getvalue())
        self.assertEqual(warnings.getvalue(), "")

    def test_config_checks_ipv6_domain_subscription_on_ipv4_loopback(self):
        manager = Mock()
        manager.env = {"SERVER_IP_IPV4": "", "SERVER_IP_IPV6": "2001:db8::1", "ENABLE_IPV6_LISTEN": "true",
                       "ENABLE_CUSTOM_DOMAIN": "true", "ENABLE_SUBSCRIPTION_SERVER": "true",
                       "SUBSCRIPTION_SERVICE": "surelay-sub", "SUBSCRIPTION_PORT": "", "SUBSCRIPTION_LISTEN_PORT": "51040"}
        with patch.object(diagnostics.GEN, "transport_ports", return_value=[(443, "tcp")]), \
             patch.object(diagnostics, "run", return_value=subprocess.CompletedProcess([], 0)), \
             patch.object(diagnostics.socket, "create_connection", return_value=MagicMock()) as connect, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            diagnostics.check_config(manager)
        self.assertEqual([c.args[0] for c in connect.call_args_list], [("::1", 443), ("127.0.0.1", 51040)])
        manager.phase.assert_not_called()
        manager.service.assert_not_called()
        self.assertIn("全部通过", output.getvalue())


if __name__ == "__main__":
    unittest.main()
