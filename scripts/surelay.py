#!/usr/bin/env python3
"""SuRelay terminal control panel. Run sr, sr status, sr update or sr help."""
from __future__ import annotations
import argparse
from contextlib import redirect_stdout
import getpass
import io
import ipaddress
import os
import re
import shlex
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

from surelay_core import Error, GEN, Manager, ROOT, Snapshot, atomic, install_command, module, port, run
from surelay_security import Security, firewall_rule
from surelay_terminal import Terminal, current as current_terminal


def color(text, style):
    if not sys.stdout.isatty() or "NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb":
        return str(text)
    codes = {"title": "1;36", "accent": "36", "ok": "32", "warn": "33", "danger": "31", "muted": "2"}
    return f"\033[{codes[style]}m{text}\033[0m"


def divider():
    print(color("─" * min(60, max(24, shutil.get_terminal_size().columns - 2)), "muted"))


def ask(label, default="", secret=False, multiline=False, *, help="", context=(), step="",
        default_label="当前值", empty_hint="", validate=None):
    prompt = f"{label}" + (f" [{default}]" if default and not secret else "") + "："
    window = current_terminal()
    def checked(value):
        value = (value if secret else value.strip()) or str(default)
        try:
            return validate(value) if validate else value
        except (Error, GEN.ConfigError) as exc:
            raise ValueError(str(exc)) from exc
    if window:
        return window.read_line(label, default, secret, multiline, help=help, context=context, step=step,
                                default_label=default_label, empty_hint=empty_hint, validate=checked)
    for key, value in context:
        print(f"{key}：{value}")
    if help:
        print(help)
    if empty_hint:
        print(empty_hint)
    while True:
        try:
            return checked(getpass.getpass(prompt) if secret else input(prompt))
        except ValueError as exc:
            print(f"请重新输入：{exc}")


def pick(label, options, *, context=(), default=0):
    """Choose a value in-place without treating the selector as a submenu."""
    if not options:
        raise Error("暂无可选项目。")
    window = current_terminal()
    if window:
        with window.page(label):
            status = "\n".join(f"{key}：{value}" for key, value in context)
            index = window.choose([(name, None) for name in options], status, default, navigation=False)
        if index is None:
            raise Cancelled("操作已取消。")
        return index
    print(label)
    for index, name in enumerate(options, 1):
        print(f"{index}) {name}")
    value = ask("请选择（0 取消）", str(default + 1), context=context)
    if value == "0":
        raise Cancelled("操作已取消。")
    if not value.isdigit() or not 1 <= int(value) <= len(options):
        raise Error("请选择列表中的编号。")
    return int(value) - 1


def endpoint(host, port):
    return f"[{host.strip('[]')}]:{port}" if ":" in host else f"{host}:{port}"


def required(value):
    if not value:
        raise Error("此项不能为空。")
    return value


def positive_integer(value):
    if not value.isdigit() or int(value) < 1:
        raise Error("请填写大于 0 的整数。")
    return str(int(value))


def exit_name(value):
    if not GEN.TAG_RE.fullmatch(value):
        raise Error("出口名称只可使用字母、数字、横线和下划线。")
    return value


def upstream_info(url):
    try:
        return GEN.parse_upstream_node("preview", 51043, url, 1)
    except (GEN.ConfigError, ValueError) as exc:
        raise Error(str(exc).replace(url, "[链接]").replace("upstream-nodes.txt 第 1 行", "链接")) from exc


class Cancelled(Error):
    """Return from a declined preview without opening another result screen."""


class PanelExit(SystemExit):
    """Exit every menu without a second result prompt; optionally reload code."""
    def __init__(self, restart_root=None):
        super().__init__(0)
        self.restart_root = restart_root


def confirm(message, *, action="确认执行", danger=False):
    window = current_terminal()
    if window:
        accepted = window.confirm(message, action=action, danger=danger)
    else:
        print(f"{message}\n1) {action}\n0) 取消")
        accepted = ask("请选择", "0" if danger else "1") == "1"
    if not accepted:
        raise Cancelled("操作已取消。")


def menu(title, actions, before=None):
    if current_terminal():
        return window_menu(title, actions, before)
    while True:
        divider()
        print(color("  " + title, "title"))
        if title == "SuRelay":
            print(color("  Reality · 中转 · VPS 管理", "muted"))
        divider()
        if before:
            before()
            divider()
        for index, (name, _) in enumerate(actions, 1):
            style = "danger" if name.startswith(("卸载", "重置")) else "accent"
            print(f"  {color(str(index) + ')', style)} {color(name, style) if style == 'danger' else name}")
        print("  " + color("0)", "muted") + (" 退出" if title == "SuRelay" else " 返回"))
        divider()
        try:
            choice = ask("请选择", "0")
            if choice == "0":
                return
            if not choice.isdigit() or not 1 <= int(choice) <= len(actions):
                print("请输入菜单中的编号。")
                continue
            actions[int(choice) - 1][1]()
        except Cancelled:
            continue
        except (Error, OSError, ValueError, GEN.ConfigError) as exc:
            print(color(f"\n未完成：{exc}", "warn"))
        except KeyboardInterrupt:
            print("\n操作已取消，返回菜单。")


def window_menu(title, actions, before=None):
    window = current_terminal()
    selected = 0
    with window.page(title):
        while True:
            status = io.StringIO()
            with redirect_stdout(status):
                if before:
                    before()
            try:
                choice = window.choose(actions, status.getvalue(), selected)
            except KeyboardInterrupt:
                return
            if choice is None:
                return
            selected = choice
            name, action = actions[choice]
            navigation, external = window.menu_count, window.external_count
            with window.page(name):
                window.render([], " 正在打开…")
                with window.capture():
                    try:
                        action()
                    except Cancelled:
                        continue
                    except (Error, OSError, ValueError, GEN.ConfigError) as exc:
                        print(f"未完成：{exc}")
                    except KeyboardInterrupt:
                        print("操作已取消。")
                    except PanelExit:
                        raise
                    except SystemExit as exc:
                        if exc.code in (None, 0) and (window.buffer or window.external_count == external):
                            window.wait()
                        raise
                # Returning from a submenu should immediately restore the parent.
                # Leaf results stay visible until the user has read/copied them.
                if window.menu_count == navigation and (window.buffer or window.external_count == external):
                    try:
                        window.wait()
                    except KeyboardInterrupt:
                        pass


class Panel:
    def __init__(self, manager):
        self.m = manager
        self.s = Security(manager)

    def status(self):
        self.m.reload()
        if not self.m.config.exists():
            print(color("  安装：尚未配置，请运行 sr install", "warn"))
            return
        print("  节点  " + color(self.m.env['SERVER_ALIAS'], "title"))
        for name, label in (("sing-box", "代理"), (self.m.env["SUBSCRIPTION_SERVICE"], "订阅")):
            active = self.m.active(name)
            print(f"  {label}  " + color("● 运行中" if active else "○ 未运行", "ok" if active else "warn"))
        print("  域名  " + (self.m.env["CUSTOM_DOMAIN"] if self.m.env["ENABLE_CUSTOM_DOMAIN"] == "true" else "未启用"))
        print("  IPv6  " + (self.m.env["SERVER_IP_IPV6"] if self.m.env["ENABLE_IPV6_LISTEN"] == "true" else "未启用"))
        if not self.m.path("SINGBOX_CONFIG_PATH").is_file():
            print(color("  安装尚未完成：运行 sr install 继续安装。", "warn"))

    def main(self):
        menu("SuRelay", [
            ("节点与订阅", self.nodes), ("出口管理", self.exits), ("IPv6 设置", self.ipv6),
            ("域名与证书", self.domain), ("服务与诊断", self.services),
            ("VPS 安全", self.security), ("高级维护", self.advanced),
            ("卸载 SuRelay", self.uninstall),
        ], self.status)

    def nodes(self):
        menu("节点与订阅", [
            ("查看订阅链接", self.subscription_link),
            ("查看节点链接", self.node_links),
            ("导出客户端配置", lambda: self.m.phase("output-nodes")),
            ("刷新订阅", lambda: self.m.phase("subscription", RRB_SHOW_RESULT="true")),
            ("修改节点名称", lambda: self.setting("SERVER_ALIAS", "节点名称")),
            ("修改主入口端口", lambda: self.setting("DIRECT_PORT", "主入口端口", port)),
        ])

    def subscription_link(self):
        output = module("surelay_output", "11-output-nodes.py")
        if self.m.env["ENABLE_SUBSCRIPTION_SERVER"] != "true":
            print("订阅服务未启用。")
            return
        formats = output.subscription_links(self.m.env, GEN.read_vless_uuid(self.m.env))
        if current_terminal():
            links = [(self.m.env["SERVER_ALIAS"], [("适用客户端", label)], url) for label, url in formats]
            current_terminal().set_links(links, kind="订阅")
        else:
            for label, url in formats:
                if len(formats) > 1:
                    print(label)
                print(url)

    def node_links(self):
        output = module("surelay_output", "11-output-nodes.py")
        nodes = output.build_client_nodes(self.m.env, self.m.rows(), GEN)
        links = []
        for node in nodes:
            host = f"[{node['server']}]" if ":" in node["server"] else node["server"]
            fields = [("连接地址", host), ("连接端口", str(node["port"])), ("连接协议", "VLESS + Reality")]
            links.append((node["name"], fields, output.node_to_vless_uri(node)))
        if current_terminal():
            current_terminal().set_links(links)
        else:
            for name, fields, url in links:
                print(f"节点名称：{name}")
                for label, value in fields:
                    print(f"{label}：{value}")
                print(f"节点链接：\n{url}\n")

    def setting(self, key, label, validate=lambda value: value):
        self.m.reload()
        help = ("修改客户端中显示的直连节点名称，保存后刷新订阅。" if key == "SERVER_ALIAS" else
                "客户端连接这台 VPS 使用的端口；域名模式下订阅地址也会一起更新。")
        value = validate(ask(label, self.m.env[key], help=help,
                             context=[("当前节点", self.m.env["SERVER_ALIAS"])], validate=validate))
        if not value:
            raise Error(f"{label}不能为空。")
        if value == self.m.env[key]:
            print(f"{label}未改变。")
            return
        if key != "SERVER_ALIAS":
            confirm(f"{label}：{self.m.env[key]} → {value}\n保存后刷新订阅。", action="应用修改")
        values = {key: value}
        if key == "DIRECT_PORT" and self.m.env["ENABLE_CUSTOM_DOMAIN"] == "true":
            values["SUBSCRIPTION_BASE_URL"] = "https://" + self.m.env["CUSTOM_DOMAIN"] + (":" + value if value != "443" else "")
        self.m.apply(values, domain=key == "DIRECT_PORT" and self.m.env["ENABLE_CUSTOM_DOMAIN"] == "true")

    def show_exits(self):
        print(f"直连出口 · 服务器本机网络 · 本机入口 {self.m.env['DIRECT_PORT']}/tcp")
        rows = self.m.rows()
        if not rows:
            print("尚未添加转发出口。")
        for i, row in enumerate(rows, 1):
            print(f"{i}) {row['tag']} · {row['type'].upper()}\n   远端 {endpoint(row['server'], row['server_port'])} · 本机入口 {row['listen_port']}/tcp")
        return rows

    def select_exit(self):
        rows = self.m.rows()
        if not rows:
            raise Error("还没有可修改的出口，请先添加 HTTP/SOCKS5 或 VLESS 出口。")
        index = pick("选择要操作的出口", [f"{r['tag']} · {r['type'].upper()} · {endpoint(r['server'], r['server_port'])} · 本机 {r['listen_port']}"
                                         for r in rows])
        return rows[index]

    def exits(self):
        menu("出口管理", [
            ("查看出口列表", self.show_exits), ("添加 HTTP/SOCKS5 出口", self.add_home),
            ("添加上游 VLESS", self.add_upstream), ("修改出口", self.edit_exit),
            ("删除出口", self.delete_exit), ("检查出口连通性", lambda: self.diagnostics("exits")),
        ])

    def next_port(self, home, upstream):
        used = {int(r["listen_port"]) for r in [*home, *upstream]}
        used |= {int(self.m.env["DIRECT_PORT"]), int(self.m.env["SSH_PORT"])}
        if self.m.env["SUBSCRIPTION_LISTEN_PORT"]:
            used.add(int(self.m.env["SUBSCRIPTION_LISTEN_PORT"]))
        return str(GEN.next_available_port(int(self.m.env["HOME_PORT_START"]), int(self.m.env["HOME_PORT_END"]), used))

    def home_inputs(self, existing=None, *, editing=False):
        row = dict(existing or {})
        original = row.get("tag", "新出口")
        def context():
            items = [("正在修改" if editing else "正在添加", row.get("tag", original))]
            if row.get("server"):
                items.append(("远端代理", row.get("type", "socks5").upper() + " · " + endpoint(row["server"], row.get("server_port", "待填写"))))
            return items
        definitions = [
            ("tag", "出口名称", "home-01", "显示在客户端节点列表中，用于区分出口。", exit_name),
            ("type", "代理协议", "socks5", "", None),
            ("server", "远端代理地址", "", "代理提供方的 IP 或域名，不含协议和端口。", self.proxy_host),
            ("server_port", "远端代理端口", "1080", "代理提供方的端口，用于 VPS 连接远端代理。", port),
            ("username", "代理用户名", "", "填写代理提供方的认证账号；无需认证可留空，输入 - 清空。", None),
            ("password", "代理密码", "", "填写该代理账号的密码；输入时隐藏，输入 - 清空。", None),
            ("listen_port", "本机入口端口", "", "客户端连接此 VPS 使用的端口，流量将转发至远端代理。", port),
        ]
        for index, (key, label, default, help, validator) in enumerate(definitions, 1):
            if key == "type":
                selected = pick("2/7  选择远端代理协议", ["SOCKS5", "HTTP"], context=context(),
                                default=1 if row.get(key) == "http" else 0)
                row[key] = ("socks5", "http")[selected]
                continue
            value = ask(label, row.get(key, default), secret=key == "password", help=help, context=context(),
                        step=f"{index}/7", default_label="当前值" if editing else "建议值",
                        empty_hint="回车留空，不使用此项认证。" if key in {"username", "password"} else "",
                        validate=validator)
            row[key] = "" if key in {"username", "password"} and value == "-" else value
        row.setdefault("network", "tcp")
        return row

    @staticmethod
    def proxy_host(value):
        value = required(value.strip("[]"))
        if any(c.isspace() for c in value) or any(c in value for c in "/@?#"):
            raise Error("只填写 IP 或域名，不要带协议、认证信息、路径或端口。")
        if ":" in value:
            try:
                ipaddress.IPv6Address(value)
            except ValueError as exc:
                raise Error("域名和端口请分开填写；IPv6 地址可以直接输入。") from exc
        return value

    def add_home(self):
        home, upstream = self.m.exit_files()
        name = GEN.safe_tag("", "home-01", {r["tag"] for r in [*home, *upstream]})
        row = self.home_inputs({"tag": name, "listen_port": self.next_port(home, upstream)})
        confirm(f"添加出口 {row['tag']}\n远端代理：{row['type'].upper()} · {endpoint(row['server'], row['server_port'])}\n"
                f"本机入口：{row['listen_port']}/tcp\n保存后生成对应节点并刷新订阅。", action="保存出口")
        self.m.save_exits([*home, row], upstream)

    def add_upstream(self):
        home, upstream = self.m.exit_files()
        def validate_url(value):
            if "\n" in value or "\r" in value:
                raise Error("每次只添加一个节点，请粘贴一条 VLESS 链接。")
            upstream_info(required(value))
            return value
        url = ask("VLESS 节点链接", step="1/3", help="粘贴一个节点的 vless:// 链接，回车后设置名称。",
                  validate=validate_url)
        node = upstream_info(url)
        fragment = urlsplit(url).fragment
        name = GEN.safe_tag(fragment, f"relay-{len(upstream) + 1:02}", {r["tag"] for r in [*home, *upstream]})
        context = [("原节点", unquote(fragment) or "未命名节点"),
                   ("上游地址", endpoint(node["server"], node["server_port"]))]
        name = ask("出口名称", name, context=context, step="2/3",
                   help="客户端显示的名称，可用字母、数字、横线和下划线。",
                   default_label="建议值", validate=exit_name)
        context = [("正在设置", name), *context[1:]]
        listen = port(ask("本机入口端口", self.next_port(home, upstream), context=context, step="3/3",
                          help="客户端连接此 VPS 使用的端口。", default_label="建议值", validate=port))
        GEN.parse_upstream_node(name, int(listen), url, 1)
        confirm(f"添加出口 {name}\n上游地址：{endpoint(node['server'], node['server_port'])}\n"
                f"本机入口：{listen}/tcp\n保存后生成对应节点并刷新订阅。", action="保存出口")
        self.m.save_exits(home, [*upstream, {"tag": name, "listen_port": listen, "node_url": url}])

    def edit_exit(self):
        selected = self.select_exit()
        home, upstream = self.m.exit_files()
        group = home if selected["source"] == "home" else upstream
        index = next(i for i, row in enumerate(group) if row["tag"] == selected["tag"])
        if selected["source"] == "home":
            group[index] = self.home_inputs(group[index], editing=True)
        else:
            row = dict(group[index])
            context = [("正在修改", selected["tag"]), ("上游地址", endpoint(selected["server"], selected["server_port"]))]
            row["tag"] = ask("出口名称", row["tag"], context=context, step="1/3", validate=exit_name,
                             help="修改客户端节点列表中显示的出口名称。")
            row["listen_port"] = port(ask("本机入口端口", row["listen_port"], context=context, step="2/3", validate=port,
                                           help="客户端连接这台 VPS 的端口；不修改上游服务器的端口。"))
            row["node_url"] = ask("上游 VLESS 链接", row["node_url"], secret=True, context=context, step="3/3",
                                  help="替换此出口连接的远端节点；回车保留现有链接。",
                                  validate=lambda value: (upstream_info(value), value)[1])
            group[index] = row
        confirm(f"保存出口：{selected['tag']} → {group[index]['tag']}\n本机入口：{group[index]['listen_port']}/tcp\n保存后刷新订阅。",
                action="保存修改")
        self.m.save_exits(home, upstream)

    def delete_exit(self):
        row = self.select_exit()
        confirm(f"删除出口 {row['tag']} 及其入口节点？", action="删除出口", danger=True)
        home, upstream = self.m.exit_files()
        self.m.save_exits([r for r in home if r["tag"] != row["tag"]],
                          [r for r in upstream if r["tag"] != row["tag"]])

    def ipv6(self):
        menu("IPv6 设置", [
            ("查看当前配置", lambda: print(f"公网 IPv6：{self.m.env['SERVER_IP_IPV6'] or '未配置'}\nIPv6 节点：{'已启用' if self.m.env['ENABLE_IPV6_LISTEN'] == 'true' else '未启用'}")),
            ("启用 IPv6 节点", self.enable_ipv6), ("修改 IPv6 地址", self.enable_ipv6),
            ("关闭 IPv6 节点", self.disable_ipv6),
        ])

    def enable_ipv6(self):
        detected = run(["curl", "-6fsS", "--max-time", "4", "https://api64.ipify.org"], capture=True, check=False)
        default = detected.stdout.strip() if detected.returncode == 0 else self.m.env["SERVER_IP_IPV6"]
        if not default:
            output = run(["ip", "-6", "-o", "addr", "show", "scope", "global"], capture=True)
            default = output.split()[3].split("/")[0] if output else ""
        value = ask("公网 IPv6 地址", default, help="填写分配给这台 VPS 的公网 IPv6，用于新增 IPv6 入口节点。",
                    context=[("当前节点", self.m.env["SERVER_ALIAS"])], default_label="检测值")
        address = ipaddress.IPv6Address(value.strip("[]"))
        if not address.is_global:
            raise Error("请填写可路由的公网 IPv6 地址。")
        if run(["sysctl", "-n", "net.ipv6.bindv6only"], capture=True) == "1" and self.m.env["SERVER_IP_IPV4"]:
            raise Error("bindv6only=1，无法保证现有 IPv4 节点继续工作。")
        confirm(f"启用 {address} 的 IPv6 节点？")
        self.m.apply({"SERVER_IP_IPV6": str(address), "ENABLE_IPV6_LISTEN": "true", "RESET_PROXY_KEYS": "false"})

    def disable_ipv6(self):
        if not self.m.env["SERVER_IP_IPV4"]:
            raise Error("没有 IPv4 地址，不能关闭唯一可用的入口地址族。")
        confirm("关闭 IPv6 节点、相关监听及本项目对应的 AAAA；IPv4 节点保留？")
        self.m.apply({"SERVER_IP_IPV6": "", "SERVER_IP": self.m.env["SERVER_IP_IPV4"],
                      "ENABLE_IPV6_LISTEN": "false", "PROXY_IP_VERSION": "ipv4"})
        for suffix in ("vless.txt", "nodes.txt", "clash.yaml"):
            path = Path("/root/surelay-v6-" + suffix)
            if path.exists():
                path.unlink()

    def domain(self):
        menu("域名与证书", [
            ("查看当前配置", lambda: print(f"节点域名：{self.m.env['CUSTOM_DOMAIN'] or '未配置'}\n域名模式：{'已启用' if self.m.env['ENABLE_CUSTOM_DOMAIN'] == 'true' else '未启用'}\nCloudflare 根域名：{self.m.env['CLOUDFLARE_ZONE_NAME'] or '未配置'}")),
            ("配置或修改 CF 域名", self.configure_domain), ("更新 CF 凭据", self.cf_credentials),
            ("同步 DNS 记录", lambda: self.m.phase("cloudflare-dns")),
            ("检查或续期证书", self.certificate), ("停用域名模式", self.disable_domain),
        ])

    def configure_domain(self):
        env = self.m.env
        context = [("当前节点", env["SERVER_ALIAS"])]
        domain = ask("节点域名", env["CUSTOM_DOMAIN"], context=context, step="1/4", validate=required,
                     help="用于节点连接和 HTTPS 订阅，例如 relay.example.com。填写托管在 Cloudflare 的域名。").lower()
        context = [("正在配置", domain)]
        values = {"ENABLE_CUSTOM_DOMAIN": "true", "CUSTOM_DOMAIN": domain,
                  "CLOUDFLARE_ZONE_NAME": ask("Cloudflare 根域名", env["CLOUDFLARE_ZONE_NAME"] or ".".join(domain.split(".")[-2:]),
                                             context=context, step="2/4", help="Cloudflare 中托管的 Zone，例如 example.com。请核对建议值。", validate=required),
                  "LE_EMAIL": ask("证书通知邮箱", env["LE_EMAIL"], context=context, step="3/4",
                                  help="用于申请和接收 TLS 证书相关通知。", validate=required),
                  "CLOUDFLARE_API_TOKEN": ask("Cloudflare API Token", env["CLOUDFLARE_API_TOKEN"], secret=True,
                                              context=context, step="4/4", help="需要此域名的 Zone 读取和 DNS 编辑权限；输入内容隐藏。", validate=required),
                  "REALITY_SERVER_NAME": domain, "REALITY_HANDSHAKE_SERVER": env["NGINX_FALLBACK_HOST"],
                  "REALITY_HANDSHAKE_PORT": env["NGINX_FALLBACK_PORT"],
                  "SUBSCRIPTION_BASE_URL": "https://" + domain + (":" + env["DIRECT_PORT"] if env["DIRECT_PORT"] != "443" else ""),
                  "SUBSCRIPTION_PORT": "", "SUBSCRIPTION_LISTEN_PORT": ""}
        old = dict(env)
        confirm(f"配置 {domain}，申请证书并更新节点和订阅地址？")
        self.m.apply(values, domain=True)
        if old["CUSTOM_DOMAIN"] and old["CUSTOM_DOMAIN"] != domain:
            self.m.remove_old_dns(old, ["A", "AAAA"])

    def cf_credentials(self):
        value = ask("新的 Cloudflare API Token", secret=True, context=[("域名", self.m.env["CUSTOM_DOMAIN"] or "未配置")],
                    help="替换域名解析使用的凭据，需要 Zone 读取和 DNS 编辑权限。", validate=required)
        if not value:
            raise Error("Token 不能为空。")
        confirm("更新 CF 凭据并验证域名配置？")
        self.m.apply({"CLOUDFLARE_API_TOKEN": value}, domain=True)

    def certificate(self):
        if self.m.env["ENABLE_CUSTOM_DOMAIN"] != "true":
            raise Error("尚未启用域名模式。")
        domain = self.m.env["CUSTOM_DOMAIN"]
        run(["openssl", "x509", "-in", f"/etc/letsencrypt/live/{domain}/fullchain.pem", "-noout", "-dates"], check=False)
        self.m.phase("custom-domain", RRB_SKIP_PACKAGE_INSTALL="false")

    def disable_domain(self):
        old = dict(self.m.env)
        confirm("停用域名模式，切换为 IP 节点与 HTTP 订阅（公网 51040），删除对应项目 DNS 记录？",
                action="停用域名模式", danger=True)
        self.m.apply({"ENABLE_CUSTOM_DOMAIN": "false", "CUSTOM_DOMAIN": "",
                      "SUBSCRIPTION_BASE_URL": "", "SUBSCRIPTION_PORT": "51040", "SUBSCRIPTION_LISTEN_PORT": "51040",
                      "REALITY_SERVER_NAME": "www.microsoft.com", "REALITY_HANDSHAKE_SERVER": "www.microsoft.com",
                      "REALITY_HANDSHAKE_PORT": "443"})
        self.m.remove_old_dns(old, ["A", "AAAA"])
        print("原证书与 Nginx 本机站点保留，可再次启用域名时复用。")

    def service_action(self, action):
        names = ["sing-box", self.m.env["SUBSCRIPTION_SERVICE"]]
        label = {"start": "启动", "stop": "停止", "restart": "重启"}[action]
        choice = pick(f"选择要{label}的服务", ["代理服务 · sing-box", "订阅服务", "代理与订阅服务"], default=2)
        targets = names if choice == 2 else [names[choice]]
        for name in targets:
            self.m.service(name, action)
        print(f"已{label}所选服务。")

    def services(self):
        menu("服务与诊断", [
            ("查看详细状态", self.detail_status),
            ("启动服务", lambda: self.service_action("start")), ("停止服务", lambda: self.service_action("stop")),
            ("重启服务", lambda: self.service_action("restart")), ("查看日志", self.logs),
            ("检查配置与监听", lambda: self.diagnostics("config")),
        ])

    def diagnostics(self, mode):
        run(["python3", self.m.root / "scripts/surelay_diagnostics.py", "--project-dir", self.m.root, "--mode", mode])

    def detail_status(self):
        self.status()
        run(["ss", "-lntup"])
        for service in ("sing-box", self.m.env["SUBSCRIPTION_SERVICE"]):
            if self.m.alpine():
                self.m.service(service, "status", check=False)
            else:
                run(["systemctl", "status", service, "--no-pager", "--lines=5"], check=False)

    def logs(self):
        choice = pick("选择要查看的日志", ["SuRelay 部署日志", "代理服务 · sing-box", "订阅服务"])
        if choice == 0:
            paths = [Path("/var/log/surelay.log"), Path("/var/log/reality-relay-bootstrap.log")]
            run(["tail", "-n", "80", next((p for p in paths if p.exists()), paths[0])], check=False)
        else:
            name = "sing-box" if choice == 1 else self.m.env["SUBSCRIPTION_SERVICE"]
            if self.m.alpine():
                paths = [Path(folder) / f"{name}.log" for folder in ("/var/log/surelay", "/var/log/reality-relay")]
                run(["tail", "-n", "80", next((p for p in paths if p.exists()), paths[0])], check=False)
            else:
                run(["journalctl", "-u", name, "-n", "80", "--no-pager"], check=False)

    def security(self):
        menu("VPS 安全", [
            ("查看安全状态", self.security_status), ("防火墙与端口", self.firewall),
            ("修改 SSH 端口", lambda: self.s.change_ssh_port(ask("新的 SSH 登录端口", self.m.env["SSH_PORT"], validate=port,
                help="修改这台 VPS 的 SSH 监听端口。验证新端口登录成功后才撤下旧端口。"), self.confirm_login)),
            ("管理 SSH 公钥", self.keys), ("SSH 登录设置", self.ssh_login),
            ("登录防护（fail2ban）", self.fail2ban),
        ])

    def security_status(self):
        labels = {"port": "SSH 端口", "permitrootlogin": "root 登录", "pubkeyauthentication": "公钥认证",
                  "passwordauthentication": "密码认证", "authenticationmethods": "认证方式"}
        values = {"yes": "允许", "no": "禁止", "publickey": "公钥", "any": "任一已启用方式",
                  "prohibit-password": "仅公钥", "without-password": "仅公钥"}
        for line in self.s.effective().splitlines():
            key, _, value = line.partition(" ")
            if key in labels:
                print(f"{labels[key]}：{values.get(value, value)}")
        print(f"SSH 公钥数量：{len(self.s.keys()[2])}")
        if shutil.which("ufw"):
            run(["ufw", "status", "verbose"], check=False)
        if shutil.which("fail2ban-client"):
            run(["fail2ban-client", "status", "sshd"], check=False)

    def confirm_login(self, value=None, *, purpose=""):
        value = value or self.m.env["SSH_PORT"]
        host = self.m.env["SERVER_IP_IPV4"] or self.m.env["SERVER_IP_IPV6"]
        message = (purpose + "\n" if purpose else "")
        message += "保留当前窗口，在新终端验证公钥登录。NAT 主机使用服务商分配的公网端口。\n"
        message += f"ssh -p {value} -o PreferredAuthentications=publickey -o PasswordAuthentication=no root@{host} whoami\n"
        message += "仅当新窗口输出 root 后继续。"
        confirm(message, action="已验证登录成功，继续", danger=True)

    def keys(self):
        menu("SSH 公钥", [
            ("查看已授权公钥", self.show_keys), ("添加公钥", self.add_keys),
            ("删除公钥", self.delete_key), ("替换公钥", self.replace_key),
        ])

    def show_keys(self):
        _, lines, indexes = self.s.keys()
        for i, index in enumerate(indexes, 1):
            print(f"{i}) {self.s.validate_key(lines[index])}")
        return lines, indexes

    def key_selection(self):
        _, lines, indexes = self.s.keys()
        if not indexes:
            raise Error("还没有已授权的 SSH 公钥。")
        choice = pick("选择 SSH 公钥", [self.s.validate_key(lines[index]).strip() for index in indexes])
        return lines, indexes[choice]

    def add_keys(self):
        _, lines, _ = self.s.keys()
        added = []
        while True:
            value = ask("SSH 公钥", multiline=True, help="粘贴 ssh-ed25519 或 ssh-rsa 等公钥，可一次粘贴多行。",
                        empty_hint="空行回车结束录入。", context=[("已接收", f"{len(added)} 把新公钥")])
            if not value:
                break
            print(self.s.validate_key(value))
            if value not in lines and value not in added:
                added.append(value)
        if added:
            confirm(f"添加 {len(added)} 把公钥？")
            self.s.write_keys([*lines, *added])

    def delete_key(self):
        lines, index = self.key_selection()
        self.confirm_login(purpose=f"验证后删除公钥：{self.s.validate_key(lines[index]).strip()}\n请使用另一把保留的公钥登录。")
        self.s.write_keys(lines[:index] + lines[index + 1:])

    def replace_key(self):
        lines, index = self.key_selection()
        key = ask("替换用的 SSH 公钥", context=[("待替换指纹", self.s.validate_key(lines[index]).strip())],
                  help="先添加新公钥；在新窗口验证登录成功后，才移除所选旧公钥。", validate=required)
        print(self.s.validate_key(key))
        if key in lines:
            raise Error("该公钥已存在。")
        self.s.write_keys([*lines, key])
        print("新公钥已添加，旧公钥暂时保留。请用新公钥完成登录验证。")
        self.confirm_login(purpose="请使用刚添加的新公钥登录，验证成功后移除旧公钥。")
        self.s.write_keys(lines[:index] + lines[index + 1:] + [key])

    def ssh_login(self):
        menu("SSH 登录设置", [
            ("查看有效策略", lambda: print(self.s.effective())),
            ("关闭密码登录", lambda: self.s.password(False, self.confirm_login)),
            ("临时开启密码登录", self.enable_password),
        ])

    def enable_password(self):
        confirm("临时允许 root 密码认证？此操作不创建或更改密码。", action="开启密码登录", danger=True)
        self.s.password(True, self.confirm_login)

    def firewall(self):
        menu("防火墙与端口", [
            ("查看规则与监听", self.firewall_status), ("开放端口", self.add_rule),
            ("关闭自定义规则", lambda: self.change_rule(False)),
            ("修改自定义规则", lambda: self.change_rule(True)),
            ("启用防火墙", self.enable_firewall),
        ])

    def firewall_status(self):
        run(["ufw", "status", "numbered"])
        run(["ss", "-lntup"])
        print("上述为本机规则与监听；云厂商安全组需在控制台核对。")

    def rule_input(self):
        ports = ask("本机端口或端口范围", step="1/5", help="开放这台 VPS 的入站端口，例如 8080 或 8000:8010。",
                    validate=lambda value: firewall_rule(value, "tcp", "any", "both", "application")["port"])
        context = [("正在配置", f"本机端口 {ports}")]
        proto = ("tcp", "udp", "both")[pick("2/5  选择协议", ["TCP", "UDP", "TCP 与 UDP"], context=context)]
        source = ask("允许访问的来源", "any", context=context, step="3/5",
                     help="填写 IP 或网段；any 允许所有来源访问。", default_label="建议值",
                     validate=lambda value: value if value == "any" else str(ipaddress.ip_network(value, strict=False)))
        family = ("4", "6", "both")[pick("4/5  选择地址族", ["仅 IPv4", "仅 IPv6", "IPv4 与 IPv6"], context=context, default=2)]
        note = ask("规则备注", "application", context=context, step="5/5", help="为这条规则起一个便于识别的名称。", default_label="建议值")
        return firewall_rule(ports, proto, source, family, note)

    def add_rule(self):
        rule = self.rule_input()
        confirm("将开放本机端口：\n" + self.rule_label(rule), action="开放端口")
        self.s.edit_rule(rule)

    @staticmethod
    def rule_label(rule):
        protocol = "TCP/UDP" if rule["protocol"] == "both" else rule["protocol"].upper()
        source = "、".join(rule["sources"])
        return f"{rule['note']} · {rule['port']}/{protocol} · 来源 {source}"

    def change_rule(self, replace):
        _, rules = self.s.custom_rules()
        if not rules:
            raise Error("暂无 SuRelay 自定义规则；其他软件的规则不在这里修改。")
        choice = pick("选择要修改的规则" if replace else "选择要删除的规则", [self.rule_label(r) for r in rules])
        replacement = self.rule_input() if replace else None
        preview = self.rule_label(rules[choice])
        if replacement:
            preview += "\n修改为：\n" + self.rule_label(replacement)
        confirm(("修改所选规则：\n" if replace else "删除所选自定义规则：\n") + preview,
                action="应用修改" if replace else "删除规则", danger=not replace)
        self.s.edit_rule(replacement, choice)

    def enable_firewall(self):
        confirm("保留 SSH 和项目业务端口，然后启用 UFW？")
        self.s.enable_firewall()

    def fail2ban(self):
        menu("SSH 登录防护", [
            ("查看封禁状态", lambda: run(["fail2ban-client", "status", "sshd"])),
            ("启用 SSH 防护", self.enable_fail2ban),
            ("停用 SSH 防护", self.disable_fail2ban),
            ("解除指定 IP 封禁", self.unban),
            ("管理白名单", self.whitelist),
            ("调整封禁参数", self.ban_settings),
        ])

    def enable_fail2ban(self):
        backup = Snapshot([self.m.config, Path("/etc/fail2ban/jail.d/zz-surelay.local")])
        try:
            self.m.update({"ENABLE_FAIL2BAN": "true"})
            self.m.phase("fail2ban", RRB_SKIP_PACKAGE_INSTALL="false")
            self.s.fail2ban_settings({"enabled": "true", "port": self.m.env["SSH_PORT"]})
        except BaseException:
            backup.restore()
            self.m.reload()
            raise

    def disable_fail2ban(self):
        confirm("停用 SSH 登录防护，其他服务的防护保留？", action="停用 SSH 防护", danger=True)
        self.s.fail2ban_settings({"enabled": "false"})

    def unban(self):
        value = str(ipaddress.ip_address(ask("解除封禁的 IP 地址", help="从 SSH 登录防护的封禁列表中移除此 IP。",
                                             validate=lambda value: str(ipaddress.ip_address(value)))))
        run(["fail2ban-client", "set", "sshd", "unbanip", value])
        print(f"已解除 {value} 的 SSH 封禁。")

    def whitelist(self):
        current = run(["fail2ban-client", "get", "sshd", "ignoreip"], capture=True)
        value = ask("新的 SSH 防护白名单", context=[("当前白名单", current)],
                    help="用空格分隔多个 IP 或网段；将替换当前白名单，本机回环始终保留。", empty_hint="回车仅保留本机回环。")
        values = ["127.0.0.1/8", "::1", *value.split()]
        for item in values:
            ipaddress.ip_network(item, strict=False)
        confirm("替换 SSH 防护白名单？")
        self.s.fail2ban_settings({"ignoreip": " ".join(dict.fromkeys(values))})

    def ban_settings(self):
        definitions = [("bantime", "封禁时长（秒）", "3600", "触发防护后，禁止此 IP 登录多久。"),
                       ("findtime", "统计窗口（秒）", "600", "在多长时间内累计登录失败次数。"),
                       ("maxretry", "允许失败次数", "5", "统计窗口内达到此次数后封禁 IP。")]
        values = {}
        for index, (key, label, fallback, help) in enumerate(definitions, 1):
            result = run(["fail2ban-client", "get", "sshd", key], capture=True, check=False)
            current = result.stdout.strip() if result.returncode == 0 else ""
            default = current if current.isdigit() else fallback
            values[key] = ask(label, default, step=f"{index}/3", help=help,
                              default_label="当前值" if current.isdigit() else "建议值", validate=positive_integer)
        confirm(f"SSH 登录防护：\n封禁 {values['bantime']} 秒\n在 {values['findtime']} 秒内允许失败 {values['maxretry']} 次", action="应用参数")
        self.s.fail2ban_settings(values)

    def advanced(self):
        menu("高级维护", [
            ("更新 SuRelay", self.update_code), ("更新 sing-box", self.update_core),
            ("备份节点配置", self.m.backup), ("恢复节点配置", self.restore),
            ("编辑高级配置并应用", self.edit_config), ("重置并重新安装", self.reset),
            ("继续安装", self.install), ("迁移旧安装目录", self.migrate),
        ])

    def update_code(self):
        window = current_terminal()
        if not window:
            self.m.update_code()
            raise SystemExit(0)
        window.buffer = ""
        window.render([("正在更新 SuRelay…", "muted")], "Ctrl+C 取消")
        try:
            with window.capture(streaming=False):
                self.m.update_code()
        except BaseException:
            # Keep diagnostics available with p, but don't flood the result
            # pane with Git output or migration details after an error/cancel.
            window.details, window.buffer = window.buffer, ""
            raise
        window.buffer = ""
        try:
            choice = window.choose([("重启管理面板", None)],
                                   "SuRelay 更新完成。\n重新打开后即可使用最新版。", navigation=False, back="关闭面板")
        except (EOFError, KeyboardInterrupt):
            choice = None
        raise PanelExit(self.m.root if choice == 0 else None)

    def update_core(self):
        self.m.backup()
        run(["bash", self.m.root / "scripts/18-update-core.sh"], env={"RRB_PROJECT_DIR": str(self.m.root), "RRB_VERBOSE": "true"})

    def restore(self):
        path = ask("备份目录路径", help="填写此前“备份节点配置”生成的完整目录，例如 /root/surelay-backups/surelay-xxxx。",
                   validate=required)
        confirm("恢复节点配置、出口与密钥；现有节点和订阅身份可能变化，SSH 设置保持不变？",
                action="恢复备份", danger=True)
        self.m.restore_nodes(path)

    def edit_config(self):
        # Edit a private copy. Do not leave a half-edited live config on cancel/error.
        with tempfile.TemporaryDirectory(prefix="surelay-edit-") as folder:
            path = Path(folder) / "config.env"
            atomic(path, self.m.config.read_bytes())
            editor = shlex.split(os.environ.get("EDITOR", "nano" if shutil.which("nano") else "vi"))
            run([*editor, path], interactive=True)
            run(["bash", "-n", path], capture=True)
            old = GEN.parse_env_file(self.m.config)
            new = GEN.parse_env_file(path)
            forbidden = [k for k in set(old) | set(new) if old.get(k) != new.get(k) and (
                k.startswith(("SSH_", "ADMIN_", "SFTP_", "RRB_", "FAIL2BAN_")) or
                k.endswith(("_PATH", "_DIR", "_FILE", "_CONF", "_ROOT")) or
                k in {"ENABLE_UFW", "ENABLE_FAIL2BAN", "ENABLE_SFTP_USER", "RESET_PROXY_KEYS"})]
            if forbidden:
                raise Error("这些字段请使用专用维护流程修改：" + ", ".join(forbidden))
            confirm("校验并应用编辑后的节点配置？")
            self.m.apply(files={self.m.config: path.read_bytes()}, domain=True)

    def reset(self):
        # The established reset flow has its own preview, backup and RESET confirmation.
        run(["bash", self.m.root / "install.sh", "--reset"], interactive=True)
        self.m.reload()

    def install(self):
        run(["bash", self.m.root / "install.sh"], interactive=True)
        raise SystemExit(0)

    def migrate(self):
        self.m.migrate()
        raise SystemExit(0)

    def uninstall(self):
        run(["python3", self.m.root / "scripts/17-reset-install.py",
             "--project-dir", self.m.root, "--uninstall", "--apply"], interactive=True)
        raise SystemExit(0)


def main():
    parser = argparse.ArgumentParser(prog="sr", description="SuRelay：sr 打开菜单；sr status 查看状态；sr update 更新管理脚本。")
    parser.add_argument("command", nargs="?", default="menu", choices=["menu", "status", "update", "install", "migrate", "uninstall", "help"])
    parser.add_argument("--install-command", action="store_true", help="安装 /usr/local/bin/sr，不修改运行配置")
    parser.add_argument("--plain", action="store_true", help="使用逐行菜单，适用于不支持全屏界面的终端")
    args = parser.parse_args()
    if args.command == "help":
        parser.print_help()
        return 0
    if os.name != "posix" or os.geteuid() != 0:
        raise Error("请在 VPS 上以 root 执行：sudo sr")
    os.umask(0o077)
    if args.install_command:
        install_command()
        return 0
    manager = Manager()
    if not manager.config.exists() and args.command == "menu":
        raise Error(f"未找到配置：{manager.config}。首次部署运行 sr install；更新脚本不会自动安装节点。")
    import fcntl
    restart_root = None
    with open("/run/surelay.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Error("另一个 sr 管理会话正在运行，请先退出该会话。")
        panel = Panel(manager)
        if args.command == "status":
            panel.status()
        elif args.command == "update":
            manager.update_code()
        elif args.command in {"install", "migrate", "uninstall"}:
            getattr(panel, args.command)()
        else:
            if not sys.stdin.isatty():
                raise Error("菜单需要交互终端；只查看状态可使用 sr status。")
            with Terminal(enabled=not args.plain):
                try:
                    panel.main()
                except PanelExit as result:
                    restart_root = result.restart_root
    # Restore echo/screen and release the process lock before starting the
    # updated entry point. migrate() may have changed the project directory.
    if restart_root is not None:
        os.execv(sys.executable, [sys.executable, str(restart_root / "scripts/surelay.py")])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (Error, OSError, ValueError, GEN.ConfigError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except (EOFError, KeyboardInterrupt):
        print("\n已退出 SuRelay。")
