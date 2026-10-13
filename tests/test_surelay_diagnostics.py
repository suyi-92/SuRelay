"""No network or live services: exercise proxy routing, outcomes and cleanup."""
import contextlib
import io
import json
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
            self.assertEqual(data["outbounds"], [row])
            self.assertEqual(data["route"]["final"], "chosen")
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
        manager.env = {"PROXY_IP_VERSION": "ipv4"}
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

    def test_config_checks_ipv6_domain_subscription_on_ipv4_loopback(self):
        manager = Mock()
        manager.env = {"SERVER_IP_IPV4": "", "SERVER_IP_IPV6": "2001:db8::1", "ENABLE_IPV6_LISTEN": "true",
                       "ENABLE_CUSTOM_DOMAIN": "true", "ENABLE_SUBSCRIPTION_SERVER": "true",
                       "SUBSCRIPTION_SERVICE": "surelay-sub", "SUBSCRIPTION_PORT": "", "SUBSCRIPTION_LISTEN_PORT": "51040"}
        with patch.object(diagnostics.GEN, "port_list", return_value=[443]), \
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
