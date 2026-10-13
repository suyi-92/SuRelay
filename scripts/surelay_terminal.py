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
        self.flow = None
        self.cursor = None
        self.size = (0, 0)
        self.streaming = False
        self.last_draw = 0
        self.menu_count = 0
        self.external_count = 0
        self.pending = ("", [])
        self.links = []
        self.link_kind = "节点"
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
        saved = self.trail, self.buffer, self.streaming, self.pending, self.links, self.link_kind
        self.trail = [*self.trail, title] if not self.trail or self.trail[-1] != title else self.trail[:]
        self.buffer, self.streaming, self.pending = "", False, ("", [])
        self.links = []
        self.link_kind = "节点"
        try:
            yield
        finally:
            self.trail, self.buffer, self.streaming, self.pending, self.links, self.link_kind = saved

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
                "border": "90", "selected": "1;36", "danger_selected": "1;31",
                "danger": "31", "ok": "32", "warn": "33", "text": "0"}[style]
        return f"{ESC}{code}m{text}{ESC}0m"

    def render(self, body, hint, cursor=None, flow=None):
        """Draw ordinary rows plus an optional continuous, soft-wrapped block.

        flow is (body row offset, text). It uses the terminal's full width:
        cursor addressing or gutters between its rows would turn a copied URL
        into several hard lines. Keep the whole block untouched on idle/feedback
        redraws so mouse selection and the terminal's wrap markers survive.
        """
        width, height = self.dimensions()
        columns = width
        # Ordinary rows leave a spare column; flow may fill it but never uses
        # the bottom row, so even a pending autowrap cannot scroll the screen.
        width = max(2, width - 1)
        if (width, height) != self.size:
            self.emit(ESC + "2J")
            self.frame = []
            self.flow = None
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
        flow = (flow[0] + 3, flow[1], len(wrap(flow[1], columns))) if flow else None
        flow_rows = set(range(flow[0], flow[0] + flow[2])) if flow else set()
        stale_rows = (set(range(self.flow[0], self.flow[0] + self.flow[2]))
                      if self.flow and self.flow != flow else set())
        # Erase the entire former flow row before ordinary text: erasing only
        # its tail leaves a stale soft-wrap marker in terminals such as xterm.
        changed = [f"{ESC}{i + 1};1H" + (ESC + "2K" if i in stale_rows else "") + line + ESC + "K"
                   for i, line in enumerate(rendered)
                   if i not in flow_rows and (i in stale_rows or i >= len(self.frame) or self.frame[i] != line)]
        if flow and flow != self.flow:
            # Clear before writing, never after: at an exact width boundary the
            # cursor still occupies the final character (pending autowrap).
            changed.extend(f"{ESC}{i + 1};1H{ESC}2K" for i in sorted(flow_rows))
            changed.append(f"{ESC}{flow[0] + 1};1H" + self.paint(flow[1], "accent"))
        suffix = ""
        if changed or cursor != self.cursor:
            suffix = ESC + "?25l" if cursor is None else f"{ESC}{cursor[0]};{cursor[1]}H{ESC}?25h"
        if changed or suffix:
            self.emit("".join(changed) + suffix)
        self.frame = rendered
        self.flow = flow
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

    def menu_rows(self, names, selected, back="返回"):
        rows = []
        for i, name in enumerate([*names, back]):
            number = str(i + 1) if i < len(names) else "0"
            danger = name.startswith(("卸载", "重置"))
            style = ("danger_selected" if danger else "selected") if i == selected else "danger" if danger else "text"
            rows.append((f" {'›' if i == selected else ' '} {number}) {name}", style))
        return rows

    def choose(self, actions, status="", selected=0, *, navigation=True):
        if navigation:
            self.menu_count += 1
        count = len(actions) + 1
        start, follow_selection = 0, True
        with self.keys():
            while True:
                if self.too_small():
                    continue
                _, height = self.dimensions()
                capacity = max(1, height - 7)
                body = []
                for line in clean(status).strip().splitlines():
                    line = line.strip()
                    style = ("warn" if "未运行" in line or "尚未" in line else "ok" if "● 运行中" in line
                             else "heading" if line.startswith("节点") else "muted")
                    body.extend((part, style) for part in wrap(line, self.content_width()))
                if body:
                    body.append(("", "text"))
                focus = len(body)
                for i, (text, style) in enumerate(self.menu_rows([name for name, _ in actions], selected,
                                                              "退出" if len(self.trail) == 1 else "返回")):
                    parts = wrap(text, self.content_width())
                    body.extend((part, style) for part in parts)
                    if i == selected:
                        focus = len(body) - 1
                last_start = max(0, len(body) - capacity)
                start = min(start, last_start)
                if follow_selection:
                    start = max(0, focus - capacity + 1)
                hint = "↑↓ / 数字选择 · Enter 确认 · Esc 返回"
                if last_start:
                    hint += f"\nPgUp/PgDn 翻页 · {start + 1}–{min(start + capacity, len(body))}/{len(body)}"
                self.render(body[start:start + capacity], hint)
                key = self.key()
                if key == UP:
                    selected = (selected - 1) % count
                    follow_selection = True
                elif key in (DOWN, "\t"):
                    selected = (selected + 1) % count
                    follow_selection = True
                elif key in (PAGE_UP, PAGE_DOWN):
                    start = max(0, min(last_start, start + (capacity if key == PAGE_DOWN else -capacity)))
                    follow_selection = False
                elif isinstance(key, str) and key in "0123456789" and key:
                    number = int(key)
                    if number <= len(actions):
                        selected = number - 1 if number else len(actions)
                        follow_selection = True
                elif key in ("\r", "\n"):
                    return None if selected == len(actions) else selected
                elif key == "\x1b":
                    return None
                elif key in ("q", "Q"):
                    raise EOFError

    def confirm(self, message, *, action="确认执行", danger=False):
        """Review a change with the same controls as menus, without typed tokens."""
        self.pending = ("", [])
        selected, start = (1 if danger else 0), 0
        with self.keys():
            while True:
                if self.too_small():
                    continue
                lines = wrap(message, self.content_width())
                capacity = max(1, self.dimensions()[1] - 10)
                start = min(start, max(0, len(lines) - capacity))
                body = [(line, "text") for line in lines[start:start + capacity]]
                body.append(("", "text"))
                choices = self.menu_rows([action], selected, back="取消")
                if danger:
                    choices[0] = (choices[0][0], "danger_selected" if selected == 0 else "danger")
                body += choices
                hint = "↑↓ / 数字选择 · Enter 确认 · Esc 取消"
                if len(lines) > capacity:
                    hint += f" · PgUp/PgDn 查看说明 {start + 1}–{min(start + capacity, len(lines))}/{len(lines)}"
                self.render(body, hint)
                key = self.key()
                if key == "\x1b":
                    return False
                if key in ("q", "Q"):
                    raise EOFError
                if key in (UP, DOWN, "\t"):
                    selected = 1 - selected
                elif key in ("1", "0"):
                    selected = 0 if key == "1" else 1
                elif key in (PAGE_DOWN, PAGE_UP):
                    start = max(0, min(len(lines) - capacity, start + (capacity if key == PAGE_DOWN else -capacity)))
                elif key in (HOME, ESC + "1~"):
                    start = 0
                elif key in (END, ESC + "4~"):
                    start = max(0, len(lines) - capacity)
                elif key in ("\r", "\n"):
                    if selected == 1:
                        return False
                    if start + capacity < len(lines):
                        start = min(len(lines) - capacity, start + capacity)
                    else:
                        return True

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
            style = ("danger" if text.startswith(("ERROR", "FAIL", "未完成", "失败", "检查未完成")) else
                     "warn" if text.startswith(("WARN", "操作已取消")) else
                     "ok" if text.startswith("通过  ") else
                     "accent" if text.startswith(("INFO", "https://", "http://", "vless://")) else
                     "heading" if heading or text.startswith("检查完成") or re.match(r"\d+/\d+  ", text) else "text")
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

    def set_links(self, links, kind="节点"):
        """Accept any number of formats/nodes, keeping each exact URL intact."""
        links = list(links)
        if any(not url or any(c.isspace() or unicodedata.category(c)[0] == "C" for c in url)
               for _, _, url in links):
            raise ValueError("链接包含空白或控制字符，请检查配置。")
        self.links = links
        self.link_kind = kind
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
                name, fields, url = self.links[selected]
                fields = [(self.link_kind + "名称", name), *fields]
                label_width = max(cells(label) for label, _ in fields)
                rows = []
                for index, (label, value) in enumerate(fields):
                    prefix = label + " " * (label_width - cells(label) + 2)
                    for part_index, part in enumerate(wrap(value, max(2, width - label_width - 2))):
                        indent = prefix if part_index == 0 else " " * (label_width + 2)
                        rows.append((indent + part, "heading" if index == 0 else "text"))
                rows += [("", "text"), (self.link_kind + "链接 · 选中复制", "heading")]
                link_start = len(rows)
                # These chunks only measure the viewport. Concatenate the
                # visible span again before emitting it, with no added bytes.
                link_lines = wrap(url, self.dimensions()[0])
                rows += [("", "text")] * len(link_lines)
                last_start = max(0, len(rows) - capacity)
                start = min(start, last_start)
                first, end = max(start, link_start), min(start + capacity, len(rows))
                flow = ((first - start, "".join(link_lines[first - link_start:end - link_start]))
                        if first < end else None)
                navigation = []
                if len(self.links) > 1:
                    item = "格式" if self.link_kind == "订阅" else self.link_kind
                    navigation.append(f"←→ {item} {selected + 1}/{len(self.links)}")
                if last_start:
                    navigation.append(f"↑↓ 翻页 {start + 1}–{min(start + capacity, len(rows))}/{len(rows)}")
                hint = notice or "Enter / c 复制链接 · Esc / 0 返回"
                self.render(rows[start:start + capacity], hint + "\n" + " · ".join(navigation), flow=flow)
                key = self.key()
                if key in ("\x1b", "0"):
                    return
                if key in ("q", "Q"):
                    raise EOFError
                if not key:
                    continue
                notice = ""
                if key in (LEFT, RIGHT):
                    selected = (selected + (1 if key == RIGHT else -1)) % len(self.links)
                    start = 0
                elif key in (DOWN, PAGE_DOWN):
                    start = min(last_start, start + (capacity if key == PAGE_DOWN else 1))
                elif key in (UP, PAGE_UP):
                    start = max(0, start - (capacity if key == PAGE_UP else 1))
                elif key in (HOME, ESC + "1~"):
                    start = 0
                elif key in (END, ESC + "4~"):
                    start = last_start
                elif key in ("\r", "\n", "1", "c", "C"):
                    self.copy_link(url)
                    notice = "已发送复制请求；未生效请选中链接复制。"

    def read_line(self, label, default="", secret=False, multiline=False, *, help="", context=(), step="",
                  default_label="当前值", empty_hint="", validate=None):
        if multiline and self.pending[0] == label and self.pending[1]:
            candidate = self.pending[1].pop(0)
            try:
                return validate(candidate) if validate else candidate
            except ValueError as exc:
                # Discard the remaining paste after an invalid entry; it must
                # not become answers for fields on the following form.
                initial_notice = str(exc)
        else:
            initial_notice = ""
        self.pending = ("", [])
        value, position, queued, notice, prompt_start = "", 0, [], initial_notice, 0
        with self.keys():
            while True:
                if self.too_small():
                    continue
                width, height = self.content_width(), self.dimensions()[1]
                capacity, field_width = max(3, height - 7), max(2, width - 4)
                labels = []
                for key, item in context:
                    labels.extend((line, "muted") for line in wrap(f"{key}  {item}", width))
                if labels:
                    labels.append(("", "text"))
                heading = f"{step}  {label}" if step else label
                labels += [(line, "accent") for line in wrap(heading, width)]
                if help:
                    labels += [(line, "muted") for line in wrap(help, width)]
                if default:
                    labels += [(line, "muted") for line in wrap(
                        f"{default_label}：{'已设置（内容隐藏）' if secret else default} · 回车使用", width)]
                elif empty_hint:
                    labels += [(line, "muted") for line in wrap(empty_hint, width)]
                label_capacity = capacity - 2
                prompt_start = min(prompt_start, max(0, len(labels) - label_capacity))
                visible_labels = labels[prompt_start:prompt_start + label_capacity]
                # Prompt context is deliberate metadata, never the tail of raw
                # command output or previously pasted credentials/links.
                body = visible_labels + [("", "text")]
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
                    try:
                        result = validate(value) if validate else value
                    except ValueError as exc:
                        notice = str(exc)
                        continue
                    if multiline:
                        self.pending = (label, queued)
                    return result
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
            self.flow = None

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
