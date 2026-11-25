"""Optional local data-path checks using an installed sing-box, without services."""
from __future__ import annotations

import http.server
import json
import os
import shutil
import socket
import ssl
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from tests.test_protocols import GEN, OUTPUT, UUID


SINGBOX = os.environ.get("SURELAY_SINGBOX_BIN") or shutil.which("sing-box")


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Origin(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"surelay-local-protocol-check"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class IPv6HTTPServer(http.server.ThreadingHTTPServer):
    address_family = socket.AF_INET6


class FamilyOrigin(Origin):
    def do_GET(self):
        body = b"ipv6" if self.server.address_family == socket.AF_INET6 else b"ipv4"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class LocalDoH(Origin):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        query = self.rfile.read(int(self.headers["Content-Length"]))
        end = 12
        labels = []
        while query[end]:
            length = query[end]
            labels.append(query[end + 1:end + 1 + length].decode())
            end += length + 1
        name = ".".join(labels)
        query_type = struct.unpack("!H", query[end + 1:end + 3])[0]
        self.server.queries.append((name, query_type))
        answer = b""
        if query_type == 1 and name != "v6-only.test":
            answer = socket.inet_pton(socket.AF_INET, "127.0.0.1")
        if query_type == 28 and name != "v4-only.test":
            answer = socket.inet_pton(socket.AF_INET6, "::1")
        response = query[:2] + struct.pack("!HHHHH", 0x8180, 1, int(bool(answer)), 0, 0)
        response += query[12:end + 5]
        if answer:
            response += b"\xc0\x0c" + struct.pack("!HHIH", query_type, 1, 60, len(answer)) + answer
        self.send_response(200)
        self.send_header("Content-Type", "application/dns-message")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


@unittest.skipUnless(SINGBOX and shutil.which("openssl") and shutil.which("curl"),
                     "requires local sing-box, openssl and curl")
class LiveProtocolTest(unittest.TestCase):
    def wait_tcp(self, process, port, log):
        for _ in range(100):
            if process.poll() is not None:
                self.fail(f"sing-box exited: {log.read_text()}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    return
            except OSError:
                time.sleep(0.05)
        self.fail(f"listener {port} did not start: {log.read_text()}")

    def start(self, directory, name, config):
        path = directory / f"{name}.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        checked = subprocess.run([SINGBOX, "check", "-c", str(path)], cwd=directory,
                                 capture_output=True, text=True)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        log = directory / f"{name}.log"
        output = log.open("w", encoding="utf-8")
        process = subprocess.Popen([SINGBOX, "run", "-c", str(path)], cwd=directory,
                                   stdout=output, stderr=subprocess.STDOUT)
        self.addCleanup(output.close)
        self.addCleanup(self.stop, process)
        return process, log

    @staticmethod
    def stop(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def test_dual_stack_same_port_uses_family_doh_and_destination_preference(self):
        with socket.socket(socket.AF_INET6) as check:
            try:
                check.bind(("::1", 0))
            except OSError:
                self.skipTest("requires IPv6 loopback")
        with tempfile.TemporaryDirectory(prefix="surelay-family-") as directory:
            root = Path(directory)
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                            "-keyout", str(root / "key.pem"), "-out", str(root / "cert.pem"),
                            "-subj", "/CN=localhost"], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.load_cert_chain(root / "cert.pem", root / "key.pem")
            tls.set_alpn_protocols(["http/1.1"])
            origin4 = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FamilyOrigin)
            origin6 = IPv6HTTPServer(("::1", origin4.server_port), FamilyOrigin)
            dns4 = http.server.ThreadingHTTPServer(("127.0.0.1", 0), LocalDoH)
            dns6 = IPv6HTTPServer(("::1", 0), LocalDoH)
            for server in (origin4, origin6, dns4, dns6):
                self.addCleanup(server.server_close)
                if server in (dns4, dns6):
                    server.queries = []
                    server.socket = tls.wrap_socket(server.socket, server_side=True)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                self.addCleanup(server.shutdown)
            pair = subprocess.check_output([SINGBOX, "generate", "reality-keypair"], text=True)
            private = dict(line.split(": ", 1) for line in pair.splitlines() if ": " in line)["PrivateKey"]
            direct_port, socks_port = unused_port(), unused_port()
            while direct_port == socks_port:
                socks_port = unused_port()
            env = GEN.defaults({"ENABLE_IPV6_LISTEN": "true", "ENABLE_SOCKS5": "true",
                                "REALITY_HANDSHAKE_SERVER": "127.0.0.1",
                                "DIRECT_PORT": str(direct_port), "SOCKS5_PORT": str(socks_port),
                                "PROXY_USERNAME": "fixture", "PROXY_PASSWORD": "fixture"})
            config = GEN.build_config(env, [], UUID, private, "abcd")
            # Replace only the public DoH endpoints with local TLS fixtures.
            for item in config["dns"]["servers"]:
                fixture = dns6 if "ipv6" in item["tag"] else dns4
                item.update(server=fixture.server_address[0], server_port=fixture.server_port,
                            tls={"enabled": True, "server_name": "localhost", "insecure": True})
            process, log = self.start(root, "family-server", config)
            self.wait_tcp(process, socks_port, log)
            for host, target, expected in (
                ("127.0.0.1", "dual.test", "ipv4"),
                ("[::1]", "dual.test", "ipv6"),
                ("127.0.0.1", "v6-only.test", "ipv6"),
                ("[::1]", "v4-only.test", "ipv4"),
                ("[::1]", "127.0.0.1", "ipv4"),
            ):
                with self.subTest(entrance=host, destination=target):
                    result = subprocess.run([
                        "curl", "--silent", "--show-error", "--fail", "--max-time", "10", "--noproxy", "",
                        "--proxy", f"socks5h://fixture:fixture@{host}:{socks_port}",
                        f"http://{target}:{origin4.server_port}/fixture",
                    ], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr + log.read_text())
                    self.assertEqual(result.stdout, expected, log.read_text())
            self.assertEqual({name for name, _ in dns4.queries}, {"dual.test", "v6-only.test"})
            self.assertEqual({name for name, _ in dns6.queries}, {"dual.test", "v4-only.test"})

    def test_new_protocols_forward_tcp_and_reject_wrong_proxy_credentials(self):
        with tempfile.TemporaryDirectory(prefix="surelay-live-") as directory:
            root = Path(directory)
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                            "-keyout", str(root / "key.pem"), "-out", str(root / "cert.pem"),
                            "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost"],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            pair = subprocess.check_output([SINGBOX, "generate", "reality-keypair"], text=True)
            keys = dict(line.split(": ", 1) for line in pair.splitlines() if ": " in line)
            (root / "uuid").write_text(UUID)
            (root / "public").write_text(keys["PublicKey"])
            (root / "short").write_text("abcd")
            # Reserve distinct local listener numbers, including the UDP-only HY2.
            ports = set()
            while len(ports) < 7:
                ports.add(unused_port())
            direct, hy2, socks, ss, http_port, client_port, origin_port = sorted(ports)
            env = GEN.defaults({"SERVER_ALIAS": "local", "SERVER_IP_IPV4": "127.0.0.1",
                                "REALITY_HANDSHAKE_SERVER": "127.0.0.1",
                                "DIRECT_PORT": str(direct), "ENABLE_HY2": "true", "HY2_PORT": str(hy2),
                                "HY2_CERT_PATH": str(root / "cert.pem"), "HY2_KEY_PATH": str(root / "key.pem"),
                                "HY2_SERVER_NAME": "localhost", "HY2_INSECURE": "true",
                                "ENABLE_SOCKS5": "true", "SOCKS5_PORT": str(socks),
                                "ENABLE_SHADOWSOCKS": "true", "SHADOWSOCKS_PORT": str(ss),
                                "ENABLE_HTTP": "true", "HTTP_PORT": str(http_port),
                                "VLESS_UUID_PATH": str(root / "uuid"), "REALITY_PUBLIC_KEY_PATH": str(root / "public"),
                                "REALITY_SHORT_ID_PATH": str(root / "short")})
            config = GEN.build_config(env, [], UUID, keys["PrivateKey"], "abcd")
            for item in config["inbounds"]:
                item["listen"] = "127.0.0.1"
            server, server_log = self.start(root, "server", config)
            self.wait_tcp(server, socks, server_log)
            origin = http.server.ThreadingHTTPServer(("127.0.0.1", origin_port), Origin)
            thread = threading.Thread(target=origin.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{origin_port}/fixture"
                for node in OUTPUT.build_client_nodes(env, [], GEN)[1:]:
                    with self.subTest(protocol=node["type"]):
                        client_config = OUTPUT.build_singbox_client_config({**env, "CLASH_MIXED_PORT": str(client_port)}, [node])
                        client, client_log = self.start(root, f"client-{node['type']}", client_config)
                        try:
                            self.wait_tcp(client, client_port, client_log)
                            result = subprocess.run(["curl", "--silent", "--show-error", "--fail", "--max-time", "10",
                                                     "--noproxy", "", "--proxy", f"socks5h://127.0.0.1:{client_port}", url],
                                                    capture_output=True, text=True)
                            self.assertEqual(result.returncode, 0, result.stderr + client_log.read_text() + server_log.read_text())
                            self.assertEqual(result.stdout, "surelay-local-protocol-check")
                        finally:
                            self.stop(client)
                for scheme, port in (("socks5h", socks), ("http", http_port)):
                    with self.subTest(wrong_credentials=scheme):
                        result = subprocess.run(["curl", "--silent", "--show-error", "--fail", "--max-time", "3",
                                                 "--noproxy", "", "--proxy", f"{scheme}://wrong:wrong@127.0.0.1:{port}", url],
                                                capture_output=True, text=True)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertNotIn("surelay-local-protocol-check", result.stdout)
                for node in OUTPUT.build_client_nodes(env, [], GEN):
                    if node["type"] not in {"hysteria2", "ss"}:
                        continue
                    with self.subTest(wrong_credentials=node["type"]):
                        wrong = {**node, "password": "wrong-credential"}
                        client_config = OUTPUT.build_singbox_client_config({**env, "CLASH_MIXED_PORT": str(client_port)}, [wrong])
                        client, client_log = self.start(root, f"wrong-{node['type']}", client_config)
                        try:
                            self.wait_tcp(client, client_port, client_log)
                            result = subprocess.run(["curl", "--silent", "--show-error", "--fail", "--max-time", "3",
                                                     "--noproxy", "", "--proxy", f"socks5h://127.0.0.1:{client_port}", url],
                                                    capture_output=True, text=True)
                            self.assertNotEqual(result.returncode, 0)
                            self.assertNotIn("surelay-local-protocol-check", result.stdout)
                        finally:
                            self.stop(client)
            finally:
                origin.shutdown()
                origin.server_close()
                thread.join(timeout=5)
                self.stop(server)


if __name__ == "__main__":
    unittest.main()
