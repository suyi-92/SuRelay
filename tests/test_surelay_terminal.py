"""Interactive panel regressions in a real PTY; never use host services/config."""
import base64
import contextlib
import io
import os
import re
from pathlib import Path
import select
import signal
import struct
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import surelay_terminal as terminal


class PlainTerminalTest(unittest.TestCase):
    def test_redirected_output_never_enters_alternate_screen(self):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), terminal.Terminal():
            self.assertIsNone(terminal.current())
        self.assertEqual(stream.getvalue(), "")

    def test_display_width_and_control_sequences(self):
        self.assertEqual(terminal.cells("节点e\u0301"), 5)
        self.assertEqual(terminal.wrap("节点abcd", 4), ["节点", "abcd"])
        self.assertEqual(terminal.clean("\033[31m正常\033[0m\033[2J\033]0;bad\a"), "正常")

    def test_soft_wrapped_link_is_continuous_and_not_repainted_for_feedback(self):
        for length in (49, 50, 51, 99, 100, 101, 350):
            with self.subTest(length=length):
                window = terminal.Terminal()
                stream = window.output = io.StringIO()
                url = "https://example.com/".ljust(length, "a")
                body = [("链接", "heading")] + [("", "text")] * 7
                with patch.object(window, "dimensions", return_value=(50, 18)):
                    window.render(body, "Enter / c 复制", flow=(1, url))
                    self.assertIn(url, stream.getvalue())
                    self.assertNotIn("\n", stream.getvalue())
                    stream.seek(0)
                    stream.truncate()
                    window.render(body, "Enter / c 复制", flow=(1, url))
                    self.assertEqual(stream.getvalue(), "")
                    window.render(body, "已发送复制请求", flow=(1, url))
                    self.assertNotIn(url, stream.getvalue())
                    addressed = [int(row) for row in re.findall(r"\033\[(\d+);1H", stream.getvalue())]
                    self.assertTrue(all(row >= 16 for row in addressed), addressed)

    def test_link_display_rejects_whitespace_and_terminal_controls(self):
        for suffix in ("\n", "\r", " ", "\033[2J", "\033]52;c;bad\a"):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                terminal.Terminal().set_links([("fixture", [], "https://example.com/" + suffix)])

    def test_leaving_link_view_erases_old_wrap_markers_before_menu_text(self):
        window = terminal.Terminal()
        stream = window.output = io.StringIO()
        with patch.object(window, "dimensions", return_value=(50, 18)):
            window.render([("", "text")] * 4, "复制", flow=(0, "https://example.com/".ljust(150, "x")))
            stream.seek(0)
            stream.truncate()
            window.render([("1) 返回菜单", "text")], "选择")
        # EL0 after writing spaces only erases the tail and can retain wrap
        # markers. EL2 at column 1 must precede all former link rows.
        for row in (4, 5, 6):
            self.assertIn(f"\033[{row};1H\033[2K", stream.getvalue())


@unittest.skipUnless(os.name == "posix", "Requires POSIX terminal")
class WindowTest(unittest.TestCase):
    def start(self, body, **env):
        import fcntl
        import pty
        import termios
        self.master, self.slave = pty.openpty()
        self.original = termios.tcgetattr(self.slave)
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", 26, 90, 0, 0))
        self.all_output, self.pending = b"", b""
        def controlling_terminal():
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        script = f'''
import os, sys, termios
sys.path.insert(0, {str(ROOT / "scripts")!r})
from surelay import ask, confirm, menu
from surelay_core import run
from surelay_terminal import Terminal
{body}
try:
    with Terminal() as window:
        menu("SuRelay", actions, lambda: print("节点  Fixture"))
except (EOFError, KeyboardInterrupt):
    pass
print("CLEAN_EXIT", flush=True)
'''
        self.process = subprocess.Popen([sys.executable, "-u", "-c", script], stdin=self.slave,
                                        stdout=self.slave, stderr=self.slave, preexec_fn=controlling_terminal,
                                        env={**os.environ, "TERM": "xterm-256color", **env})
        self.addCleanup(self.cleanup)

    def cleanup(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process.wait(timeout=5)
        os.close(self.master)
        os.close(self.slave)

    def expect(self, text, timeout=5):
        expected = text.encode()
        deadline = time.monotonic() + timeout
        while expected not in self.pending and time.monotonic() < deadline:
            if select.select([self.master], [], [], 0.1)[0]:
                data = os.read(self.master, 65536)
                self.pending += data
                self.all_output += data
        self.assertIn(expected, self.pending, self.all_output.decode(errors="replace"))
        self.pending = self.pending.split(expected, 1)[1]

    def send(self, text):
        os.write(self.master, text.encode())

    def finish(self):
        import termios
        self.expect("CLEAN_EXIT")
        self.assertEqual(self.process.wait(timeout=5), 0)
        self.assertEqual(termios.tcgetattr(self.slave), self.original)
        self.assertIn(b"\x1b[?25h\x1b[?1049l", self.all_output)
        self.assertNotIn(b"Traceback", self.all_output)

    def test_navigation_updates_existing_rows_and_restores_terminal(self):
        self.start('''
def child():
    menu("节点与订阅", [("查看链接", lambda: print("fixture link"))])
actions = [("打开节点", child)]
''', NO_COLOR="1")
        self.expect("打开节点")
        self.send("\033OB\033OA\r")
        self.expect("查看链接")
        self.send("0\r")
        self.expect("打开节点")
        self.send("q")
        self.finish()
        self.assertEqual(self.all_output.count("  SuRelay  ·  VPS 管理".encode()), 1)
        self.assertEqual(self.all_output.count(b"\x1b[?1049h"), 1)
        self.assertNotIn(b"\n", self.all_output.split(b"CLEAN_EXIT")[0])
        self.assertNotIn(b"\x1b[1;36m", self.all_output)

    def test_forms_keep_secrets_hidden_and_require_confirmation(self):
        self.start('''
def edit():
    name = ask("节点名称", "old-name")
    token = ask("请输入 Token", secret=True)
    confirm("确认应用")
    print("SAVED:" + name + ":" + str(len(token)))
actions = [("修改设置", edit)]
''')
        self.expect("修改设置")
        self.send("1\r")
        self.expect("节点名称")
        self.send("临时名称\x15\r")
        self.expect("请输入 Token")
        self.send("secret-value-123\r")
        self.expect("确认应用")
        self.send("0\r")
        self.expect("修改设置")
        self.assertNotIn(b"SAVED:", self.all_output)
        self.assertNotIn(b"secret-value-123", self.all_output)
        self.send("\r")
        self.expect("节点名称")
        self.send("新名字\r")
        self.expect("请输入 Token")
        self.send("abc\r")
        self.expect("确认应用")
        self.send("\r")
        self.expect("SAVED:新名字:3")
        self.send("\r")
        self.expect("修改设置")
        self.send("q")
        self.finish()

    def test_form_context_replaces_raw_history_and_invalid_port_stays_in_field(self):
        self.start('''
from surelay_core import port
def edit():
    window.buffer = "vless://private-uuid@hidden.example/RAW-HISTORY"
    value = ask("远端代理端口", "1080", context=[("正在设置", "proxy-one")], step="4/7",
                help="填写代理提供方的端口，用于连接远端代理。", validate=port)
    window.buffer = ""
    print("SAVED:" + value)
actions = [("代理表单", edit)]
''', NO_COLOR="1")
        self.expect("代理表单")
        self.send("\r")
        self.expect("正在设置  proxy-one")
        self.expect("4/7  远端代理端口")
        self.expect("代理提供方")
        self.assertNotIn(b"RAW-HISTORY", self.all_output)
        self.send("65536\r")
        self.expect("端口必须为 1–65535")
        self.assertNotIn(b"SAVED:", self.all_output)
        self.send("\x15" + "1081\r")
        self.expect("SAVED:1081")
        self.send("\r")
        self.expect("代理表单")
        self.send("q")
        self.finish()

    def test_inline_selection_preserves_result_screen_and_cancel_returns(self):
        self.start('''
from surelay import pick
def edit():
    value = pick("选择服务", ["代理服务", "订阅服务"])
    print("SELECTED:" + str(value))
actions = [("服务选择", edit)]
''', NO_COLOR="1")
        self.expect("服务选择")
        self.send("\r")
        self.expect("订阅服务")
        self.send("2\r")
        self.expect("SELECTED:1")
        self.expect("Enter 返回")
        self.send("\r")
        self.expect("服务选择")
        self.send("\r")
        self.expect("订阅服务")
        self.send("0\r")
        self.expect("服务选择")
        self.send("q")
        self.finish()
        self.assertEqual(self.all_output.count(b"SELECTED:"), 1)

    def test_invalid_multiline_entry_cannot_fill_following_fields(self):
        self.start('''
def entry(value):
    if value and not value.startswith("vless://"):
        raise ValueError("请填写 VLESS 链接")
    return value
def edit():
    first = ask("链接", multiline=True, validate=entry)
    second = ask("链接", multiline=True, validate=entry)
    name = ask("出口名称", "relay-01")
    print("NAME:" + name)
actions = [("批量导入", edit)]
''', NO_COLOR="1")
        self.expect("批量导入")
        self.send("\r")
        self.expect("Ctrl+U 清空")
        self.send("\033[200~vless://fixture\nbad-link\nUNEXPECTED-NAME\033[201~\r")
        self.expect("请填写 VLESS 链接")
        self.send("\r")
        self.expect("出口名称")
        self.send("\r")
        self.expect("NAME:relay-01")
        self.assertNotIn(b"UNEXPECTED-NAME", self.all_output)
        self.send("\r")
        self.expect("批量导入")
        self.send("q")
        self.finish()

    def test_all_panel_menu_groups_open_and_return_in_one_window(self):
        self.start('''
from unittest.mock import Mock
from surelay import Panel
panel = Panel(Mock())
panel.status = lambda: print("节点  Fixture")
actions = [("全部管理界面", panel.main)]
''', NO_COLOR="1")
        self.expect("全部管理界面")
        self.send("\r")
        self.expect("卸载 SuRelay")
        for number, item in enumerate(["查看订阅链接", "检查出口连通性", "关闭 IPv6 节点", "停用域名模式",
                                       "检查配置与监听", "登录防护（fail2ban）", "迁移旧安装目录"], 1):
            self.send(str(number) + "\r")
            self.expect(item)
            self.send("0\r")
            self.expect("卸载 SuRelay")
        self.send("q")
        self.finish()
        self.assertEqual(self.all_output.count(b"\033[?1049h"), 1)

    def test_multiline_paste_cannot_answer_a_later_confirmation(self):
        self.start('''
def keys():
    values = []
    while True:
        value = ask("公钥", multiline=True)
        if not value:
            break
        values.append(value)
    confirm("确认公钥")
    print("KEY_COUNT:" + str(len(values)))
actions = [("添加公钥", keys)]
''')
        self.expect("添加公钥")
        self.send("1\r")
        self.expect("公钥")
        self.send("\033[200~key-one\nkey-two\n\n1\033[201~\r")
        self.expect("确认公钥")
        self.assertNotIn(b"KEY_COUNT:", self.all_output)
        self.send("\r")
        self.expect("KEY_COUNT:2")
        self.send("\r")
        self.expect("添加公钥")
        self.send("q")
        self.finish()

    def test_command_output_paging_and_copy_view(self):
        self.start('''
def output():
    run([sys.executable, "-u", "-c", "import sys; [print('LINE-%03d' % i) for i in range(60)]; print('END-ERROR', file=sys.stderr); sys.exit(3)"])
actions = [("查看日志", output)]
''')
        self.expect("查看日志")
        self.send("1\r")
        self.expect("Enter 返回")
        self.send("\033[F")
        self.expect("END-ERROR")
        self.send("p")
        self.expect("按 Enter 返回 SuRelay")
        self.assertIn(b"LINE-000\r\nLINE-001", self.all_output)
        self.send("\r")
        self.expect("Enter 返回")
        self.send("\r")
        self.expect("查看日志")
        self.send("q")
        self.finish()

    def test_external_program_receives_a_normal_terminal(self):
        self.start('''
def external():
    run([sys.executable, "-c", "print('RECEIVED:' + input('EXTERNAL_PROMPT:'))"], interactive=True)
actions = [("外部编辑器", external)]
''')
        self.expect("外部编辑器")
        self.send("1\r")
        self.expect("EXTERNAL_PROMPT")
        import termios
        self.assertEqual(termios.tcgetattr(self.slave), self.original)
        self.send("hello\r")
        self.expect("RECEIVED:hello")
        self.expect("按 Enter 返回 SuRelay")
        self.send("\r")
        self.expect("外部编辑器")
        self.send("q")
        self.finish()

    def test_link_copy_uses_exact_payload_after_resize_and_node_switch(self):
        import fcntl
        import termios
        first = "vless://fixture@edge.example:443?security=reality&pbk=" + "a" * 320 + "#%E8%8A%82%E7%82%B9"
        second = "vless://fixture@[2001:db8::10]:443?security=reality&pbk=" + "b" * 320 + "#IPv6"
        self.start(f'''
def links():
    window.set_links([("NODE-ONE", [("连接地址", "edge.example"), ("连接端口", "443")], {first!r}),
                      ("NODE-TWO", [("连接地址", "2001:db8::10"), ("连接端口", "443")], {second!r})])
actions = [("链接列表", links)]
''', NO_COLOR="1")
        self.expect("链接列表")
        self.send("1\r")
        self.expect(first)
        self.assertNotIn(b"\033]52;", self.all_output)
        self.send("\r")
        self.expect("已发送复制请求")
        self.assertEqual(self.all_output.count(first.encode()), 1)
        self.send("\033[C")
        self.expect("NODE-TWO")
        self.expect(second)
        self.pending = b""
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", 18, 50, 0, 0))
        self.expect("\033[2J")
        self.send("c")
        self.expect("已发送复制请求")
        payloads = re.findall(rb"\033\]52;c;([^\a]+)\a", self.all_output)
        self.assertEqual([base64.b64decode(p).decode() for p in payloads], [first, second])
        self.send("\033[F")
        self.expect("#IPv6")
        self.send("0")
        self.expect("链接列表")
        self.send("q")
        self.finish()
        self.assertEqual(self.all_output.count(b"\x1b[?1049h"), 1)
        self.assertNotIn("按 Enter 返回 SuRelay".encode(), self.all_output)
        self.assertNotIn(b"\n", self.all_output.split(b"CLEAN_EXIT")[0])

    def test_many_formats_and_long_link_remain_in_panel(self):
        url = "https://example.com/sub/" + "x" * 2100 + "?target=fixture#END-LINK"
        links = [("fixture", [("适用客户端", f"FORMAT-{i:02}")], url + str(i)) for i in range(12)]
        self.start(f'''
def links():
    window.set_links({links!r}, kind="订阅")
actions = [("格式列表", links)]
''', NO_COLOR="1")
        self.expect("格式列表")
        self.send("\r")
        self.expect("FORMAT-00")
        self.expect("https://example.com/sub/")
        self.send("\033[D")
        self.expect("FORMAT-11")
        self.send("\033[F")
        self.expect("END-LINK11")
        self.send("c")
        self.expect("已发送复制请求")
        payloads = re.findall(rb"\033\]52;c;([^\a]+)\a", self.all_output)
        self.assertEqual([base64.b64decode(p).decode() for p in payloads], [url + "11"])
        self.send("\033[C")
        self.expect("FORMAT-00")
        self.expect("https://example.com/sub/")
        self.send("0")
        self.expect("格式列表")
        self.send("q")
        self.finish()
        self.assertEqual(self.all_output.count(b"\x1b[?1049h"), 1)

    def test_long_confirmation_is_readable_before_enter_applies(self):
        message = "\n".join(f"确认内容 {i:02}" for i in range(30))
        self.start(f'''
def edit():
    confirm({message!r})
    print("CONFIRMED")
actions = [("长确认", edit)]
''')
        self.expect("长确认")
        self.send("1\r")
        self.expect("确认内容 00")
        self.send("\r")
        self.expect("确认内容 29")
        self.assertNotIn(b"CONFIRMED", self.all_output)
        self.assertIn("确认内容 29".encode(), self.all_output)
        self.send("\r")
        self.expect("CONFIRMED")
        self.send("\r")
        self.expect("长确认")
        self.send("q")
        self.finish()

    def test_destructive_confirmation_defaults_to_cancel_and_ignores_paste(self):
        self.start('''
def delete():
    confirm("将删除所选出口", action="删除出口", danger=True)
    print("DELETED")
actions = [("删除操作", delete)]
''')
        self.expect("删除操作")
        self.send("1\r")
        self.expect("将删除所选出口")
        self.send("\033[200~1\n\033[201~\r")
        self.expect("删除操作")
        self.assertNotIn(b"DELETED", self.all_output)
        self.send("1\r")
        self.expect("将删除所选出口")
        self.send("1\r")
        self.expect("DELETED")
        self.send("\r")
        self.expect("删除操作")
        self.send("q")
        self.finish()

    def test_escape_cancels_input_and_resize_preserves_value(self):
        import fcntl
        import termios
        self.start('''
def edit():
    print("VALUE:" + ask("名称"))
actions = [("输入名称", edit)]
''')
        self.expect("输入名称")
        self.send("1\r")
        self.expect("Ctrl+U 清空")
        self.send("\033")
        self.expect("操作已取消")
        self.send("\r")
        self.expect("输入名称")
        self.send("1\r")
        self.expect("Ctrl+U 清空")
        self.send("resize-value")
        self.expect(" > resize-value")
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", 12, 40, 0, 0))
        self.expect("窗口过小")
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        self.expect("Ctrl+U 清空")
        self.send("\r")
        self.expect("VALUE:resize-value")
        self.send("\r")
        self.expect("输入名称")
        self.send("q")
        self.finish()

    def test_sigterm_restores_echo_cursor_and_screen(self):
        self.start('actions = [("等待退出", lambda: None)]')
        self.expect("等待退出")
        self.process.send_signal(signal.SIGTERM)
        self.expect("\033[?25h\033[?1049l")
        self.assertEqual(self.process.wait(timeout=5), 128 + signal.SIGTERM)
        import termios
        self.assertEqual(termios.tcgetattr(self.slave), self.original)

    def test_ctrl_c_stops_child_command_and_returns_to_menu(self):
        self.start('''
def slow():
    run([sys.executable, "-u", "-c", "import os, time; print('CHILD_PID:%d:READY' % os.getpid()); time.sleep(60)"])
actions = [("慢速操作", slow)]
''')
        self.expect("慢速操作")
        self.send("1\r")
        self.expect(":READY")
        import re
        pid = int(re.search(rb"CHILD_PID:(\d+):READY", self.all_output).group(1))
        self.send("\x03")
        self.expect("操作已取消")
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.send("\r")
        self.expect("慢速操作")
        self.send("q")
        self.finish()

    def test_dumb_terminal_uses_the_plain_menu(self):
        self.start('actions = [("普通菜单", lambda: None)]', TERM="dumb")
        self.expect("请选择")
        self.send("0\r")
        self.expect("CLEAN_EXIT")
        self.assertEqual(self.process.wait(timeout=5), 0)
        self.assertNotIn(b"\x1b[?1049h", self.all_output)


if __name__ == "__main__":
    unittest.main()
