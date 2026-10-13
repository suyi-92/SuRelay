"""Dependency-free terminal window for the interactive SuRelay panel."""
from __future__ import annotations

import base64
import codecs
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from contextvars import ContextVar
import io
import os
import re
import select
import signal
import subprocess
import sys
import time
import unicodedata


_current = ContextVar("surelay_terminal", default=None)
ESC = "\033["
UP, DOWN, LEFT, RIGHT = (ESC + c for c in "ABDC")
PAGE_UP, PAGE_DOWN = ESC + "5~", ESC + "6~"
HOME, END, DELETE = ESC + "H", ESC + "F", ESC + "3~"


def current():
    return _current.get()


def clean(text):
    # Command output may contain SGR, OSC hyperlinks or cursor controls. Only
    # this renderer may move the cursor or alter the terminal's active buffer.
    text = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\|$)", "", str(text))
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = re.sub(r"\x1b(?:\[[0-?]*[ -/]*)?$", "", text)
    return "".join(c for c in text.expandtabs(4) if c == "\n" or unicodedata.category(c)[0] != "C")


def cells(text):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in "WF" else 1
               for c in text)


def clip(text, width):
    result, used = [], 0
    for char in text:
        size = cells(char)
        if used + size > width:
            break
        result.append(char)
        used += size
    return "".join(result)


def wrap(text, width):
    lines = []
    for line in clean(text).split("\n"):
        part, used = [], 0
        for char in line:
            size = cells(char)
            if used + size > width:
                lines.append("".join(part))
                part, used = [], 0
            part.append(char)
            used += size
        lines.append("".join(part))
    return lines


class Output(io.TextIOBase):
    def __init__(self, window):
        self.window = window

    @property
    def encoding(self):
        return "utf-8"

    def write(self, text):
        self.window.append(text)
        return len(text)

    def flush(self):
        self.window.refresh_output()


class Terminal:
    def __init__(self, enabled=True):
        self.input, self.output = sys.stdin, sys.stdout
        self.enabled = enabled
        self.active = False
        self.trail = []
        self.buffer = ""
        self.frame = []
        self.cursor = None
        self.size = (0, 0)
        self.streaming = False
        self.last_draw = 0
        self.menu_count = 0
        self.external_count = 0
        self.pending = ("", [])
        self.links = []
        self.sink = Output(self)

    def dimensions(self):
        size = os.get_terminal_size(self.output.fileno())
        return size.columns, size.lines

    def content_width(self):
        return max(2, min(100, self.dimensions()[0] - 5))

    def emit(self, text):
        self.output.write(text)
        self.output.flush()

    def __enter__(self):
        if not (self.enabled and os.name == "posix" and self.input.isatty() and self.output.isatty()
                and os.environ.get("TERM", "dumb") != "dumb"):
            return self
        width, height = self.dimensions()
        if width < 50 or height < 18:
            return self
        import termios
        self.original_attributes = termios.tcgetattr(self.input.fileno())
        self.handlers = {}
        for sig in (signal.SIGTERM, signal.SIGHUP):
            self.handlers[sig] = signal.signal(sig, self.interrupted)
        self.token = _current.set(self)
        self.active = True
        try:
            import tty
            # Keep echo disabled between screens too, while status/commands run.
            tty.setcbreak(self.input.fileno(), termios.TCSANOW)
            self.emit(ESC + "?1049h" + ESC + "2J" + ESC + "?25l")
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise
        return self

    @staticmethod
    def interrupted(signum, _frame):
        raise SystemExit(128 + signum)

    def __exit__(self, *_):
        if self.active:
            import termios
            try:
                termios.tcsetattr(self.input.fileno(), termios.TCSANOW, self.original_attributes)
                self.emit(ESC + "?2004l" + ESC + "0m" + ESC + "?25h" + ESC + "?1049l")
            finally:
                _current.reset(self.token)
                for sig, handler in self.handlers.items():
                    signal.signal(sig, handler)
                self.active = False

    @contextmanager
    def page(self, title):
        saved = self.trail, self.buffer, self.streaming, self.pending, self.links
        self.trail = [*self.trail, title] if not self.trail or self.trail[-1] != title else self.trail[:]
        self.buffer, self.streaming, self.pending = "", False, ("", [])
        self.links = []
        try:
            yield
        finally:
            self.trail, self.buffer, self.streaming, self.pending, self.links = saved

    @contextmanager
    def capture(self):
        previous = self.streaming
        self.streaming = True
        try:
            with redirect_stdout(self.sink), redirect_stderr(self.sink):
                yield
        finally:
            self.streaming = previous

    def append(self, text):
        self.buffer += text
        if len(self.buffer) > 262144:
            self.buffer = "[较早输出已省略]\n" + self.buffer[-200000:]
        if "\n" in text and time.monotonic() - self.last_draw >= 0.05:
            self.refresh_output()

    def paint(self, text, style):
        if "NO_COLOR" in os.environ:
            return text
        code = {"title": "1;36", "heading": "1", "accent": "36", "muted": "37",
                "border": "90", "selected": "1;30;46", "danger_selected": "1;37;41",
                "danger": "31", "ok": "32", "warn": "33", "text": "0"}[style]
        return f"{ESC}{code}m{text}{ESC}0m"

    def render(self, body, hint, cursor=None):
        width, height = self.dimensions()
        # Keep one unused column and row so drawing cannot scroll the terminal.
        width = max(2, width - 1)
        if (width, height) != self.size:
            self.emit(ESC + "2J")
            self.frame = []
            self.size = width, height
        content_width = self.content_width()
        title = "SuRelay  ·  VPS 管理"
        trail = " / ".join("首页" if t == "SuRelay" else t for t in self.trail)
        if cells(trail) > content_width:
            trail = "… / " + " / ".join(self.trail[-2:])
        capacity = max(1, height - 7)
        rule = ("─" * content_width, "border")
        rows = [(title, "title"), (trail, "muted"), rule]
        rows += body[:capacity] + [("", "text")] * max(0, capacity - len(body))
        hints = wrap(hint.strip(), content_width)
        rows += [rule, (hints[0], "muted"), (hints[1] if len(hints) > 1 else "", "muted")]
        rows = rows[:max(1, height - 1)]
        rendered = ["  " + self.paint(clip(clean(text), content_width), style) for text, style in rows]
        changed = [f"{ESC}{i + 1};1H{line}{ESC}K" for i, line in enumerate(rendered)
                   if i >= len(self.frame) or self.frame[i] != line]
        suffix = ""
        if changed or cursor != self.cursor:
            suffix = ESC + "?25l" if cursor is None else f"{ESC}{cursor[0]};{cursor[1]}H{ESC}?25h"
        if changed or suffix:
            self.emit("".join(changed) + suffix)
        self.frame = rendered
        self.cursor = cursor
        self.last_draw = time.monotonic()

    def too_small(self):
        width, height = self.dimensions()
        if width >= 50 and height >= 18:
            return False
        self.render([("窗口过小，请扩大到 50 列、18 行。", "warn")], " Esc 返回")
        if self.key() == "\x1b":
            raise KeyboardInterrupt
        return True

    @contextmanager
    def keys(self):
        import termios
        import tty
        fd = self.input.fileno()
        saved = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd, termios.TCSANOW)
            self.emit(ESC + "?2004h")
            yield
        finally:
            termios.tcsetattr(fd, termios.TCSANOW, saved)
            self.emit(ESC + "?2004l" + ESC + "?25l")

    def key(self):
        fd = self.input.fileno()
        if not select.select([fd], [], [], 0.2)[0]:
            return ""
        first = os.read(fd, 1)
        if not first or first == b"\x04":
            raise EOFError
        if first == b"\x03":
            raise KeyboardInterrupt
        if first == b"\x1b":
            data = first
            while len(data) < 32 and select.select([fd], [], [], 0.05)[0]:
                data += os.read(fd, 1)
                if len(data) >= 3 and (65 <= data[-1] <= 90 or 97 <= data[-1] <= 122 or data[-1] == 126):
                    break
            if data == b"\x1b[200~":
                paste = bytearray()
                while not paste.endswith(b"\x1b[201~"):
                    chunk = os.read(fd, 1)
                    if not chunk:
                        raise EOFError
                    paste.extend(chunk)
                    if len(paste) > 262144:
                        raise ValueError("粘贴内容过长。")
                return ("paste", paste[:-6].decode("utf-8", errors="replace"))
            key = data.decode("ascii", errors="ignore")
            return ESC + key[2:] if key.startswith("\033O") else key
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        char = decoder.decode(first)
        while not char:
            chunk = os.read(fd, 1)
            if not chunk:
                raise EOFError
            char = decoder.decode(chunk)
        return char

    def choose(self, actions, status="", selected=0):
        self.menu_count += 1
        count = len(actions) + 1
        with self.keys():
            while True:
                if self.too_small():
                    continue
                width, height = self.dimensions()
                capacity = max(1, height - 7)
                body = []
                for line in clean(status).strip().splitlines():
                    line = line.strip()
                    style = ("warn" if "未运行" in line or "尚未" in line else "ok" if "● 运行中" in line
                             else "heading" if line.startswith("节点") else "muted")
                    body.extend((part, style) for part in wrap(line, self.content_width()))
                if body:
                    body.append(("", "text"))
                offset = len(body)
                for i, (name, _) in enumerate([*actions, ("退出" if len(self.trail) == 1 else "返回", None)]):
                    number = str(i + 1) if i < len(actions) else "0"
                    danger = name.startswith(("卸载", "重置"))
                    style = ("danger_selected" if danger else "selected") if i == selected else "danger" if danger else "text"
                    line = f" {'›' if i == selected else ' '} {number}) {name}"
                    line += " " * max(0, min(44, self.content_width()) - cells(line))
                    body.append((line, style))
                start = max(0, offset + selected - capacity + 1)
                self.render(body[start:start + capacity], " ↑↓ / 数字选择 · Enter 确认 · Esc 返回 · q 退出")
                key = self.key()
                if key == UP:
                    selected = (selected - 1) % count
                elif key in (DOWN, "\t"):
                    selected = (selected + 1) % count
                elif isinstance(key, str) and key in "0123456789" and key:
                    number = int(key)
                    if number <= len(actions):
                        selected = number - 1 if number else len(actions)
                elif key in ("\r", "\n"):
                    return None if selected == len(actions) else selected
                elif key == "\x1b":
                    return None
                elif key in ("q", "Q"):
                    raise EOFError

    def output_rows(self):
        rows, heading = [], False
        for line in clean(self.buffer).strip("\n").splitlines():
            if not line.strip() and (not rows or not rows[-1][0]):
                continue
            text = line.lstrip()
            if re.fullmatch(r"[─━=-]{4,}", text):
                heading = True
                continue
            if heading and text in self.trail:
                heading = False
                continue
            style = ("danger" if text.startswith(("ERROR", "FAIL", "未完成")) else
                     "warn" if text.startswith(("WARN", "操作已取消")) else
                     "accent" if text.startswith(("INFO", "https://", "http://", "vless://")) else
                     "heading" if heading else "text")
            if text:
                heading = False
            rows.extend((part, style) for part in wrap(line, self.content_width()))
        while rows and not rows[-1][0].strip():
            rows.pop()
        return rows or [("操作已完成。", "ok")]

    def refresh_output(self):
        if self.streaming:
            _, height = self.dimensions()
            self.render(self.output_rows()[-max(1, height - 7):],
                        " 正在执行… · Ctrl+C 取消")

    def wait(self):
        if self.links:
            return self.wait_links()
        start = 0
        with self.keys():
            while True:
                if self.too_small():
                    continue
                _, height = self.dimensions()
                capacity = max(1, height - 7)
                lines = self.output_rows()
                start = min(start, max(0, len(lines) - capacity))
                hint = "Enter 返回 · p 原文"
                if len(lines) > capacity:
                    hint += f" · ↑↓ / PgUp PgDn 翻页 · 行 {start + 1}–{min(start + capacity, len(lines))}/{len(lines)}"
                self.render(lines[start:start + capacity], hint)
                key = self.key()
                if key in ("\r", "\n", "\x1b", "0"):
                    return
                if key in ("q", "Q"):
                    raise EOFError
                if key in (DOWN, PAGE_DOWN):
                    start = min(max(0, len(lines) - capacity), start + (capacity if key == PAGE_DOWN else 1))
                elif key in (UP, PAGE_UP):
                    start = max(0, start - (capacity if key == PAGE_UP else 1))
                elif key in (HOME, ESC + "1~"):
                    start = 0
                elif key in (END, ESC + "4~"):
                    start = max(0, len(lines) - capacity)
                elif key in ("p", "P"):
                    with self.external():
                        print(clean(self.buffer))

    def set_links(self, links):
        """Keep exact copy payloads separate from wrapped display rows."""
        self.links = list(links)
        self.buffer = "\n".join(url for _, _, url in self.links)

    def copy_link(self, url):
        # OSC 52 crosses SSH to the user's terminal clipboard. Never copy the
        # rendered frame, query the clipboard, or add a trailing newline.
        payload = base64.b64encode(url.encode("utf-8")).decode("ascii")
        self.emit("\033]52;c;" + payload + "\a")

    def wait_links(self):
        selected, start, notice = 0, 0, ""
        with self.keys():
            while True:
                if self.too_small():
                    continue
                width = self.content_width()
                capacity = max(1, self.dimensions()[1] - 7)
                name, detail, url = self.links[selected]
                heading = f"{selected + 1} / {len(self.links)}  ·  {name}"
                rows = [(part, "heading") for part in wrap(heading, width)]
                rows += [(part, "muted") for part in wrap(detail, width)]
                rows += [("", "text"), ("链接预览", "accent")]
                rows += [(part, "text") for part in wrap(url, width)]
                rows += [("", "text")]
                message = notice or "按 c 复制完整链接；预览有折行，请勿拖选。"
                rows += [(part, "accent" if notice else "muted") for part in wrap(message, width)]
                start = min(start, max(0, len(rows) - capacity))
                hint = "Enter 返回 · c 复制 · p 原文"
                if len(self.links) > 1:
                    hint += " · ←→ 切换节点"
                if len(rows) > capacity:
                    hint += f" · ↑↓ 翻页 · 行 {start + 1}–{min(start + capacity, len(rows))}/{len(rows)}"
                self.render(rows[start:start + capacity], hint)
                key = self.key()
                if key in ("\r", "\n", "\x1b", "0"):
                    return
                if key in ("q", "Q"):
                    raise EOFError
                if key in (LEFT, RIGHT):
                    selected = (selected + (1 if key == RIGHT else -1)) % len(self.links)
                    start, notice = 0, ""
                elif key in (DOWN, PAGE_DOWN):
                    start = min(max(0, len(rows) - capacity), start + (capacity if key == PAGE_DOWN else 1))
                elif key in (UP, PAGE_UP):
                    start = max(0, start - (capacity if key == PAGE_UP else 1))
                elif key in (HOME, ESC + "1~"):
                    start = 0
                elif key in (END, ESC + "4~"):
                    start = max(0, len(rows) - capacity)
                elif key in ("c", "C"):
                    self.copy_link(url)
                    message = "已发送复制请求；若剪贴板未更新，按 p 查看原文。"
                    # Make feedback visible even for a very long link.
                    start = max(0, len(rows) - len(wrap(notice or "按 c 复制完整链接；预览有折行，请勿拖选。", width))
                                + len(wrap(message, width)) - capacity)
                    notice = message
                elif key in ("p", "P"):
                    with self.external():
                        print(url)

    def read_line(self, label, default="", secret=False, multiline=False):
        if multiline and self.pending[0] == label and self.pending[1]:
            return self.pending[1].pop(0)
        self.pending = ("", [])
        value, position, queued, notice, prompt_start = "", 0, [], "", 0
        with self.keys():
            while True:
                if self.too_small():
                    continue
                width, height = self.content_width(), self.dimensions()[1]
                capacity, field_width = max(3, height - 7), max(2, width - 4)
                labels = [(line, "heading") for line in wrap(label, width)]
                if default and not secret:
                    labels += [(line, "muted") for line in wrap(f"当前：{default}（回车保留）", width)]
                label_capacity = capacity - 2
                prompt_start = min(prompt_start, max(0, len(labels) - label_capacity))
                visible_labels = labels[prompt_start:prompt_start + label_capacity]
                history = wrap(self.buffer.strip("\n"), width) if self.buffer else []
                space = min(4, capacity - len(visible_labels) - 3)
                body = [(line, "muted") for line in (history[-space:] if space > 0 else [])]
                if body:
                    body.append(("", "text"))
                body += visible_labels + [("", "text")]
                visible = "*" * len(value) if secret else value
                left = position
                while left > 0 and cells(visible[left - 1:position]) < field_width:
                    left -= 1
                field = clip(visible[left:], field_width)
                body.append((" > " + field, "accent"))
                hint = notice or "Enter 确认 · Esc 取消 · Ctrl+U 清空"
                if len(labels) > label_capacity:
                    hint += f" · PgUp/PgDn 提示 {prompt_start + 1}–{prompt_start + len(visible_labels)}/{len(labels)}"
                self.render(body, hint, cursor=(3 + len(body), 6 + cells(visible[left:position])))
                key = self.key()
                if key in ("\r", "\n"):
                    if prompt_start + label_capacity < len(labels):
                        prompt_start = min(len(labels) - label_capacity, prompt_start + label_capacity)
                        notice = "请阅读完整提示，再按 Enter 确认。"
                        continue
                    if multiline:
                        self.pending = (label, queued)
                    self.buffer += label + "：" + ("[已隐藏]" if secret else value or str(default)) + "\n"
                    return value
                if key == "\x1b":
                    raise KeyboardInterrupt
                if key in (PAGE_UP, PAGE_DOWN):
                    prompt_start = max(0, min(len(labels) - label_capacity,
                                             prompt_start + (label_capacity if key == PAGE_DOWN else -label_capacity)))
                    continue
                if key in ("\x7f", "\b") and position:
                    value, position = value[:position - 1] + value[position:], position - 1
                elif key == DELETE:
                    value = value[:position] + value[position + 1:]
                elif key == LEFT:
                    position = max(0, position - 1)
                elif key == RIGHT:
                    position = min(len(value), position + 1)
                elif key in (HOME, ESC + "1~", "\x01"):
                    position = 0
                elif key in (END, ESC + "4~", "\x05"):
                    position = len(value)
                elif key == "\x15":
                    value, position, queued = "", 0, []
                elif isinstance(key, tuple):
                    text = clean(key[1].replace("\r\n", "\n").replace("\r", "\n"))
                    if "\n" in text and multiline:
                        text, *queued = text.split("\n")
                    elif "\n" in text.strip("\n"):
                        notice = " 此项只接受一行；请分开输入。"
                        continue
                    text = text.strip("\n")
                    value = value[:position] + text + value[position:]
                    position += len(text)
                elif isinstance(key, str) and key and all(unicodedata.category(c)[0] != "C" for c in key):
                    value = value[:position] + key + value[position:]
                    position += len(key)

    @contextmanager
    def external(self):
        """Lend the real terminal to installers/editors, restoring it afterwards."""
        import termios
        fd = self.input.fileno()
        saved = termios.tcgetattr(fd)
        token = _current.set(None)
        self.external_count += 1
        try:
            termios.tcsetattr(fd, termios.TCSANOW, self.original_attributes)
            self.emit(ESC + "?2004l" + ESC + "0m" + ESC + "?25h" + ESC + "?1049l")
            with redirect_stdout(self.output), redirect_stderr(self.output):
                yield
                input("按 Enter 返回 SuRelay…")
        finally:
            _current.reset(token)
            termios.tcsetattr(fd, termios.TCSANOW, saved)
            self.emit(ESC + "?1049h" + ESC + "2J" + ESC + "?25l")
            if not saved[3] & termios.ICANON:
                self.emit(ESC + "?2004h")
            self.frame = []

    def run(self, args, env, input=None):
        """Stream noninteractive commands into the result pane, never the shell."""
        if input is not None:
            result = subprocess.run(args, input=input, text=True, encoding="utf-8", errors="replace",
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
            self.append(result.stdout)
            return result
        with self.keys(), subprocess.Popen(
                args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env={**env, "PYTHONUNBUFFERED": "1", "PAGER": "cat"}, start_new_session=True) as process:
            try:
                decoder = codecs.getincrementaldecoder("utf-8")("replace")
                while True:
                    if not select.select([process.stdout], [], [], 0.1)[0]:
                        self.refresh_output()
                        continue
                    chunk = os.read(process.stdout.fileno(), 4096)
                    if not chunk:
                        break
                    self.append(decoder.decode(chunk))
                self.append(decoder.decode(b"", final=True))
                return subprocess.CompletedProcess(args, process.wait())
            except BaseException:
                # Stop the whole command group before allowing rollback to run.
                for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(process.pid, sig)
                    except ProcessLookupError:
                        break
                    try:
                        process.wait(timeout=2)
                        break
                    except subprocess.TimeoutExpired:
                        continue
                raise
