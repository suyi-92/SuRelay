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
import tempfile
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
                    window.render(body, "Enter / c 复制", flow=(1, url), expanded=True)
                    self.assertIn(url, stream.getvalue())
                    self.assertNotIn("\n", stream.getvalue())
                    stream.seek(0)
                    stream.truncate()
                    window.render(body, "Enter / c 复制", flow=(1, url), expanded=True)
                    self.assertEqual(stream.getvalue(), "")
                    window.render(body, "已发送复制请求", flow=(1, url), expanded=True)
                    self.assertNotIn(url, stream.getvalue())
                    addressed = [int(row) for row in re.findall(r"\033\[(\d+);1H", stream.getvalue())]
                    first, _, count = window.flow
                    self.assertTrue(all(row not in range(first + 1, first + count + 1) for row in addressed), addressed)

    def test_link_display_rejects_whitespace_and_terminal_controls(self):
        for suffix in ("\n", "\r", " ", "\033[2J", "\033]52;c;bad\a"):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                terminal.Terminal().set_links([("fixture", [], "https://example.com/" + suffix)])

    def test_leaving_link_view_erases_old_wrap_markers_before_menu_text(self):
        window = terminal.Terminal()
        stream = window.output = io.StringIO()
        with patch.object(window, "dimensions", return_value=(50, 18)):
            window.render([("", "text")] * 4, "复制", flow=(0, "https://example.com/".ljust(150, "x")), expanded=True)
            first, _, count = window.flow
            stream.seek(0)
            stream.truncate()
            window.render([("1) 返回菜单", "text")], "选择")
        # EL0 after writing spaces only erases the tail and can retain wrap
        # markers. EL2 at column 1 must precede all former link rows.
        for row in range(first + 1, first + count + 1):
            self.assertIn(f"\033[{row};1H\033[2K", stream.getvalue())

    def test_cards_are_centered_and_shortcuts_stay_next_to_content(self):
        for columns, height in ((160, 48), (90, 26), (50, 18)):
            with self.subTest(size=(columns, height)):
                window = terminal.Terminal()
                window.output = io.StringIO()
                window.trail = ["SuRelay", "出口管理"]
                with patch.object(window, "dimensions", return_value=(columns, height)):
                    window.render([("1) 查看出口", "selected"), ("0) 返回", "text")], "Enter 确认 · Esc 返回")
                    lines = [terminal.clean(line) for line in window.frame]
                    top = next(i for i, line in enumerate(lines) if "╭" in line)
                    bottom = next(i for i, line in enumerate(lines) if "╰" in line)
                    left = len(lines[top]) - len(lines[top].lstrip())
                    right = columns - terminal.cells(lines[top])
                    self.assertLessEqual(abs(left - right), 1)
                    self.assertLessEqual(abs(top - (height - bottom - 1)), 1)
                    self.assertLessEqual(bottom - top, 10)
                    last_item = next(i for i, line in enumerate(lines) if "0) 返回" in line)
                    shortcuts = next(i for i, line in enumerate(lines) if "Enter" in line)
                    self.assertEqual(shortcuts - last_item, 2)
                    self.assertTrue(all(terminal.cells(line) < columns for line in lines))
                    window.output.seek(0)
                    window.output.truncate()
                    window.render([("1) 查看出口", "selected"), ("0) 返回", "text")], "Enter 确认 · Esc 返回")
                    self.assertEqual(window.output.getvalue(), "")

    def test_input_cursor_stays_inside_box_across_editing_and_resize(self):
        window = terminal.Terminal()
        window.output = io.StringIO()
        size, frames = [160, 48], []
        value = "节点" * 45 + "-end"
        actions = iter([("paste", value), terminal.HOME, terminal.END, "resize", "\x15", "\r"])

        def key():
            lines = [terminal.clean(line) for line in window.frame]
            row, column = window.cursor
            line = lines[row - 1]
            # Outer border, inner border, text/cursor, inner border, outer border.
            borders, offset = [], 1
            for char in line:
                if char == "│":
                    borders.append(offset)
                offset += terminal.cells(char)
            self.assertEqual(len(borders), 4, line)
            self.assertTrue(borders[1] < column < borders[2], (column, borders))
            self.assertLess(row, size[1])
            self.assertTrue(all(terminal.cells(line) < size[0] for line in lines))
            frames.append(lines)
            action = next(actions)
            if action == "resize":
                size[:] = [50, 18]
                return ""
            return action

        with patch.object(window, "dimensions", side_effect=lambda: tuple(size)), \
                patch.object(window, "keys", side_effect=contextlib.nullcontext), \
                patch.object(window, "key", side_effect=key):
            self.assertEqual(window.read_line("出口名称", "relay-01", context=[("节点", "HK")]), "")
        self.assertIn("relay-01", "".join(frames[0]))
        self.assertIn("-end", "".join(frames[4]))
        self.assertNotIn("-end", "".join(frames[5]))

    def test_validation_keeps_box_fixed_and_long_errors_can_be_read(self):
        window = terminal.Terminal()
        window.output = io.StringIO()
        frames, positions = [], []
        error = "\n".join(f"错误说明 {i}" for i in range(6))
        actions = iter(["bad", "\r", terminal.PAGE_DOWN, terminal.PAGE_DOWN, "\x15", "ok", "\r"])

        def validate(value):
            if value != "ok":
                raise ValueError(error)
            return value

        def key():
            frames.append("\n".join(terminal.clean(line) for line in window.frame))
            positions.append(window.cursor)
            return next(actions)

        with patch.object(window, "dimensions", return_value=(50, 18)), \
                patch.object(window, "keys", side_effect=contextlib.nullcontext), \
                patch.object(window, "key", side_effect=key):
            self.assertEqual(window.read_line("端口", validate=validate), "ok")
        self.assertEqual(len({row for row, _ in positions}), 1)
        for i in range(6):
            self.assertIn(f"错误说明 {i}", "\n".join(frames))
        self.assertNotIn("错误说明", frames[-1])

    def test_links_use_centered_card_and_copy_original_in_both_views(self):
        url = "https://example.com/sub/" + "a" * 100
        for size in ((160, 48), (90, 26), (50, 18)):
            with self.subTest(size=size):
                window = terminal.Terminal()
                window.output = io.StringIO()
                window.trail = ["SuRelay", "查看订阅链接"]
                window.set_links([("HK", [("客户端", "Mihomo / Clash Meta")], url)], kind="订阅")
                frames, chunks, flows = [], [], []
                actions = iter(["", "c", "p", "", "c", "p", "0"])
                def key():
                    frames.append([terminal.clean(line) for line in window.frame])
                    chunks.append(window.output.getvalue())
                    flows.append(window.flow)
                    window.output.seek(0)
                    window.output.truncate()
                    return next(actions)
                with patch.object(window, "dimensions", return_value=size), \
                     patch.object(window, "keys", side_effect=contextlib.nullcontext), \
                     patch.object(window, "key", side_effect=key):
                    window.wait_links()
                self.assertEqual(chunks[1], "")
                self.assertEqual(chunks[4], "")
                self.assertIsNone(flows[0])
                self.assertIsNotNone(flows[3])
                self.assertIsNone(flows[-1])
                self.assertIn("╭─ SuRelay ", "\n".join(frames[0]))
                self.assertIn("查看订阅链接", "\n".join(frames[0]))
                first_card_row = next(line for line in frames[0] if "╭" in line)
                self.assertLessEqual(abs(len(first_card_row) - len(first_card_row.lstrip()) -
                                         (size[0] - terminal.cells(first_card_row))), 1)
                width = min(72, size[0] - 10)
                for part in terminal.wrap(url, width):
                    self.assertIn(part, "\n".join(frames[0]))
                    self.assertNotIn(part, chunks[2])  # copying doesn't repaint URL
                self.assertIn(url, chunks[3])
                self.assertNotIn(url, chunks[5])
                payloads = re.findall(r"\033\]52;c;([^\a]+)\a", "".join(chunks))
                self.assertEqual([base64.b64decode(p).decode() for p in payloads], [url, url])


@unittest.skipUnless(os.name == "posix", "Requires POSIX terminal")
class WindowTest(unittest.TestCase):
    def start(self, body, *, direct=False, **env):
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
'''
        script += body
        if not direct:
            script += '''
try:
    with Terminal() as window:
        menu("SuRelay", actions, lambda: print("节点  Fixture"))
except (EOFError, KeyboardInterrupt):
    pass
'''
        script += '\nprint("CLEAN_EXIT", flush=True)\n'
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

    def start_update(self, *, fail=False, pid_path=""):
        # Exercise main's real lock and terminal lifetime; only the backend
        # update and host file paths are replaced with disposable fixtures.
        self.start(f'''
import builtins, fcntl, tempfile
from pathlib import Path
from unittest.mock import Mock, patch
import surelay
from surelay_terminal import current
original = termios.tcgetattr(0)
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    lock_path = root / "surelay.lock"
    manager = Mock(root=root / "old")
    manager.config = root / "config.env"
    manager.config.touch()
    def update():
        print("MIGRATION_DETAIL")
        code = "print('GIT_PROGRESS_DETAIL')"
        if {bool(pid_path)!r}:
            code += "; import os, time; from pathlib import Path; Path(" + repr({pid_path!r}) + ").write_text(str(os.getpid())); time.sleep(60)"
        if {fail!r}:
            code += "; raise SystemExit(1)"
        run([sys.executable, "-u", "-c", code])
        manager.root = root / "migrated"
        print("MIGRATION_DONE")
    manager.update_code.side_effect = update
    def fixture_open(path, *args, **kwargs):
        assert path == "/run/surelay.lock", path
        return builtins.open(lock_path, *args, **kwargs)
    def restart(executable, argv):
        assert current() is None
        assert termios.tcgetattr(0) == original
        with builtins.open(lock_path, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert argv == [sys.executable, str(root / "migrated/scripts/surelay.py")], argv
        assert executable == sys.executable
        print("RESTARTED_AFTER_CLEANUP", flush=True)
    def main_menu(panel):
        menu("SuRelay", [("高级维护", lambda: menu("高级维护", [("更新 SuRelay", panel.update_code)]))])
    with patch.object(surelay, "Manager", return_value=manager), \\
         patch.object(surelay, "open", side_effect=fixture_open, create=True), \\
         patch.object(surelay.Panel, "main", main_menu), \\
         patch.object(os, "geteuid", return_value=0), \\
         patch.object(os, "execv", side_effect=restart), \\
         patch.object(sys, "argv", ["sr"]):
        try:
            assert surelay.main() == 0
        except (EOFError, KeyboardInterrupt):
            pass  # same outer exit handling as the sr entry point
''', direct=True)
        self.expect("高级维护")
        self.send("1\r")
        self.expect("更新 SuRelay")
        self.send("1\r")
        self.expect("正在更新 SuRelay")

    def test_update_restarts_migrated_entry_after_restoring_terminal_and_lock(self):
        self.start_update()
        self.expect("SuRelay 更新完成")
        self.expect("1) 重启管理面板")
        self.expect("0) 关闭面板")
        self.send("\r")
        self.expect("RESTARTED_AFTER_CLEANUP")
        self.finish()
        self.assertNotIn(b"GIT_PROGRESS_DETAIL", self.all_output)
        self.assertNotIn(b"MIGRATION_DETAIL", self.all_output)
        self.assertNotIn("Enter 返回".encode(), self.all_output)

    def test_update_close_controls_exit_without_another_result_prompt(self):
        for key in ("0\r", "\033", "q", "\x03"):
            with self.subTest(key=repr(key)):
                try:
                    self.start_update()
                    self.expect("0) 关闭面板")
                    self.send(key)
                    self.finish()
                    self.assertNotIn(b"RESTARTED_AFTER_CLEANUP", self.all_output)
                    self.assertNotIn("Enter 返回".encode(), self.all_output)
                finally:
                    self.doCleanups()

    def test_update_failure_keeps_logs_hidden_until_details_requested(self):
        self.start_update(fail=True)
        self.expect("未完成：")
        self.expect("p 详情")
        self.assertNotIn(b"GIT_PROGRESS_DETAIL", self.all_output)
        self.assertNotIn(b"MIGRATION_DETAIL", self.all_output)
        self.assertNotIn("更新完成".encode(), self.all_output)
        self.send("p")
        self.expect("MIGRATION_DETAIL")
        self.expect("GIT_PROGRESS_DETAIL")
        self.expect("按 Enter 返回 SuRelay")
        self.send("\r")
        self.expect("p 详情")
        self.send("\r")
        self.expect("1) 更新 SuRelay")
        self.send("q")
        self.finish()

    def test_quiet_update_can_cancel_child_and_return_to_menu(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_path = Path(directory) / "child.pid"
            self.start_update(pid_path=str(pid_path))
            deadline = time.monotonic() + 5
            while not pid_path.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(pid_path.exists(), "Update child did not start")
            pid = int(pid_path.read_text())
            self.send("\x03")
            self.expect("操作已取消")
            self.expect("p 详情")
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
            self.assertNotIn(b"GIT_PROGRESS_DETAIL", self.all_output)
            self.send("\r")
            self.expect("1) 更新 SuRelay")
            self.send("q")
            self.finish()

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
        self.assertIn("╭─ SuRelay ".encode(), self.all_output)
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
        self.expect("代理提供方")
        self.expect("4/7  远端代理端口")
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

    def test_vless_addition_goes_directly_to_name_and_port_and_rejects_batch_paste(self):
        url = "vless://11111111-2222-4333-8444-555555555555@one.example:443?security=none&type=tcp#Original-One"
        self.start('''
from unittest.mock import Mock
from surelay import Panel
manager = Mock()
manager.env = {"DIRECT_PORT":"443", "SSH_PORT":"22", "SUBSCRIPTION_LISTEN_PORT":"51040",
               "HOME_PORT_START":"51043", "HOME_PORT_END":"65535"}
manager.exit_files.return_value = ([], [])
manager.save_exits.side_effect = lambda home, upstream: print("SAVED-COUNT:" + str(len(upstream)))
panel = Panel(manager)
actions = [("添加上游", panel.add_upstream)]
''', NO_COLOR="1")
        self.expect("添加上游")
        self.send("\r")
        self.expect("1/3  VLESS 节点链接")
        self.send("\033[200~" + url + "\n" + url + "\033[201~")
        self.expect("此项只接受一行")
        self.assertNotIn("2/3  出口名称".encode(), self.all_output)
        self.send("\033[200~" + url + "\n\033[201~\r")
        self.expect("Original-One")
        self.expect("2/3  出口名称")
        self.send("\r")
        self.expect("3/3  本机入口端口")
        self.send("\r")
        self.expect("保存出口")
        self.send("\r")
        self.expect("SAVED-COUNT:1")
        self.assertNotIn("已接收".encode(), self.all_output)
        self.assertNotIn("空行回车结束".encode(), self.all_output)
        self.send("\r")
        self.expect("添加上游")
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
        self.expect(first[:60])
        self.assertNotIn(b"\033]52;", self.all_output)
        self.send("\r")
        self.expect("已发送复制请求")
        self.assertEqual(self.all_output.count(first[:60].encode()), 1)
        self.send("\033[C")
        self.expect("NODE-TWO")
        self.expect(second[:60])
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
        self.expect("resize-value")
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
