from __future__ import annotations

import base64
import os
import select
import shutil
import signal
import struct
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

if os.name == "posix":
    import fcntl
    import pty
    import termios


ROOT = Path(__file__).resolve().parents[1]


def example_key(number: int, comment: str) -> str:
    algorithm = b"ssh-ed25519"
    blob = struct.pack(">I", len(algorithm)) + algorithm + struct.pack(">I", 32) + bytes([number]) * 32
    return f"ssh-ed25519 {base64.b64encode(blob).decode('ascii')} {comment}"


KEYS = [example_key(number, f"test-device-{number}") for number in range(1, 4)]


def controlling_terminal():
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


class AdminKeySession:
    def __init__(self, functions: str, read_next: bool = False):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.master, self.slave = pty.openpty()
        script = """\
set -Eeuo pipefail
BOLD='' DIM='' CYAN='' RESET=''
INPUT_TTY="$1"
line() { :; }
section() { printf '%s\\n' "$*"; }
info() { printf 'INFO: %s\\n' "$*"; }
warn() { printf 'WARN: %s\\n' "$*" >&2; }
die() { printf 'ERROR: %s\\n' "$*" >&2; exit 1; }
""" + functions + """\
collect_admin_pubkeys "$2/keys.txt"
admin_pubkey="$(cat "$2/keys.txt")"
write_config "$2" test-vps 192.0.2.10 '' 22 "$admin_pubkey" 443 true '' ClashMeta false 51043 65535 false '' '' '' '' 8443
source "$2/config.env"
printf '%s' "$ADMIN_PUBKEY" >"$2/stored-keys.txt"
if [[ "$3" == true ]]; then
  printf 'NEXT> '
  IFS= read -r next <"$INPUT_TTY"
  printf '%s' "$next" >"$2/next-input.txt"
fi
"""
        self.process = subprocess.Popen(
            ["bash", "--noprofile", "--norc", "-c", script, "admin-key-input-test", os.ttyname(self.slave), str(self.root), str(read_next).lower()],
            stdin=self.slave,
            stdout=self.slave,
            stderr=self.slave,
            env={**os.environ, "LC_ALL": "C.UTF-8"},
            preexec_fn=controlling_terminal,
        )
        self.output = b""

    def __enter__(self):
        try:
            self.read_until(b"> ")
        except BaseException:
            self.__exit__()
            raise
        return self

    def read_until(self, marker: str | bytes, count: int = 1):
        marker = marker.encode("utf-8") if isinstance(marker, str) else marker
        deadline = time.monotonic() + 5
        while self.output.count(marker) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.master], [], [], remaining)[0]:
                raise AssertionError(f"Terminal did not display {marker!r}; output: {self.output!r}")
            self.output += os.read(self.master, 65536)
        return self.output

    def send(self, value: str | bytes):
        os.write(self.master, value.encode("utf-8") if isinstance(value, str) else value)

    def finish(self):
        status = self.process.wait(timeout=5)
        while select.select([self.master], [], [], 0.05)[0]:
            self.output += os.read(self.master, 65536)
        return status

    def __exit__(self, *_):
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGKILL)
        self.process.wait(timeout=5)
        os.close(self.master)
        os.close(self.slave)
        self.directory.cleanup()


@unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "Requires a Linux/POSIX terminal and Bash")
class AdminPubkeyInputTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        script = (ROOT / "install.sh").read_text(encoding="utf-8")

        def function(name, following):
            start = script.index(f"{name}() ")
            end = script.index(f"\n{following}()", start)
            return script[start:end]

        cls.functions = "\n".join([
            function("collect_admin_pubkeys", "read_bool_value"),
            function("shell_quote", "collect_admin_pubkeys"),
            function("write_config", "ensure_proxy_lists"),
        ])

    def assert_keys(self, session, expected):
        self.assertEqual(session.finish(), 0, session.output.decode("utf-8", errors="replace"))
        expected_text = "\n".join(expected)
        self.assertEqual((session.root / "keys.txt").read_text(), expected_text + ("\n" if expected else ""))
        self.assertEqual((session.root / "stored-keys.txt").read_text(), expected_text)

    def test_pastes_three_keys_at_once_and_preserves_next_prompt_input(self):
        with AdminKeySession(self.functions, read_next=True) as session:
            session.send("\n".join(KEYS) + "\n\nnext-option\n")
            self.assert_keys(session, KEYS)
            self.assertEqual((session.root / "next-input.txt").read_text(), "next-option")

    def test_empty_or_whitespace_input_warns_and_keeps_waiting(self):
        with AdminKeySession(self.functions) as session:
            for count, empty in enumerate(["\n", " \t\n", "\n"], 1):
                session.send(empty)
                session.read_until("WARN: 尚未输入管理员 SSH 公钥", count=count)
                self.assertIsNone(session.process.poll())
                self.assertFalse((session.root / "config.env").exists())
            session.send(KEYS[0] + "\n\n")
            self.assert_keys(session, KEYS[:1])

    def test_explicit_skip_allows_empty_input(self):
        with AdminKeySession(self.functions) as session:
            session.send("SKIP\n")
            self.assert_keys(session, [])
            self.assertIn("已明确跳过本次公钥录入".encode(), session.output)

    def test_skip_after_empty_input_is_explicit(self):
        with AdminKeySession(self.functions) as session:
            session.send("\n")
            session.read_until("WARN: 尚未输入管理员 SSH 公钥")
            session.send("SKIP\n")
            self.assert_keys(session, [])

    def test_invalid_lines_and_non_exact_skip_are_not_saved(self):
        with AdminKeySession(self.functions) as session:
            session.send("skip\nSKIP this\nssh-ed25519\nnot-a-key\n\n")
            session.read_until("WARN: 这一行不是支持的 SSH 公钥格式", count=4)
            session.read_until("WARN: 尚未输入管理员 SSH 公钥")
            self.assertIsNone(session.process.poll())
            session.send(KEYS[1] + "\n\n")
            self.assert_keys(session, KEYS[1:2])

    def test_skip_does_not_discard_already_entered_keys(self):
        with AdminKeySession(self.functions) as session:
            session.send(KEYS[0] + "\nSKIP\n")
            session.read_until("WARN: 已录入 1 把公钥")
            self.assertIsNone(session.process.poll())
            session.send("\n")
            self.assert_keys(session, KEYS[:1])

    def test_bracketed_paste_markers_and_outer_whitespace_are_removed(self):
        with AdminKeySession(self.functions) as session:
            session.send("\x1b[200~  " + KEYS[0] + "  \n\t" + KEYS[1] + "\t\n" + KEYS[2] + "\x1b[201~\n\n")
            self.assert_keys(session, KEYS)

    def test_key_comments_survive_config_serialization(self):
        key = example_key(4, 'device name "literal" \\ $HOME $(printf changed)')
        with AdminKeySession(self.functions) as session:
            session.send(key + "\n\n")
            self.assert_keys(session, [key])

    def test_eof_does_not_act_as_skip_or_confirm_partial_input(self):
        for partial in [False, True]:
            with self.subTest(partial=partial), AdminKeySession(self.functions) as session:
                if partial:
                    session.send(KEYS[0] + "\n")
                    session.read_until(b"> ", count=2)
                session.send(b"\x04")
                self.assertNotEqual(session.finish(), 0)
                self.assertFalse((session.root / "config.env").exists())


if __name__ == "__main__":
    unittest.main()
