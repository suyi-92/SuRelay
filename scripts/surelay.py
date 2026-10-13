#!/usr/bin/env python3
"""SuRelay terminal control panel. Run sr, sr status, sr update or sr help."""
from __future__ import annotations
import argparse
import getpass
import ipaddress
import os
import re
import shlex
import shutil
import sys
import tempfile
from pathlib import Path

from surelay_core import Error, GEN, Manager, ROOT, Snapshot, atomic, install_command, module, port, run
from surelay_security import Security, firewall_rule


def color(text, style):
    if not sys.stdout.isatty() or "NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb":
        return str(text)
    codes = {"title": "1;36", "accent": "36", "ok": "32", "warn": "33", "danger": "31", "muted": "2"}
    return f"\033[{codes[style]}m{text}\033[0m"


def divider():
    print(color("─" * min(60, max(24, shutil.get_terminal_size().columns - 2)), "muted"))


def ask(label, default="", secret=False):
    prompt = f"{label}" + (f" [{default}]" if default and not secret else "") + "："
    value = getpass.getpass(prompt) if secret else input(prompt)
    return (value if secret else value.strip()) or str(default)


def confirm(message, word="YES"):
    if ask(f"{message}\n输入 {word} 继续，其他输入取消") != word:
        raise Error("操作已取消。")


def menu(title, actions, before=None):
    while True:
        print()
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
        except (Error, OSError, ValueError, GEN.ConfigError) as exc:
            print(color(f"\n未完成：{exc}", "warn"))
        except KeyboardInterrupt:
            print("\n操作已取消，返回菜单。")


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
            ("查看节点链接", lambda: self.m.output("--skip-node-files", "--quiet", "--print-summary")),
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
        print(output.subscription_url(self.m.env, GEN.read_vless_uuid(self.m.env)))

    def setting(self, key, label, validate=lambda value: value):
        self.m.reload()
        value = validate(ask(label, self.m.env[key]))
        if not value:
            raise Error(f"{label}不能为空。")
        confirm(f"{label}：{self.m.env[key]} → {value}，应用并刷新订阅？")
        values = {key: value}
        if key == "DIRECT_PORT" and self.m.env["ENABLE_CUSTOM_DOMAIN"] == "true":
            values["SUBSCRIPTION_BASE_URL"] = "https://" + self.m.env["CUSTOM_DOMAIN"] + (":" + value if value != "443" else "")
        self.m.apply(values, domain=key == "DIRECT_PORT" and self.m.env["ENABLE_CUSTOM_DOMAIN"] == "true")

    def show_exits(self):
        print(f"固定出口：服务器直连，入口 {self.m.env['DIRECT_PORT']}/tcp")
        rows = self.m.rows()
        for i, row in enumerate(rows, 1):
            print(f"{i}) {row['tag']} | {row['type']} | {row['server']}:{row['server_port']} | 入口 {row['listen_port']}/tcp")
        return rows

    def select_exit(self):
        rows = self.show_exits()
        value = ask("出口编号")
        if not value.isdigit() or not 1 <= int(value) <= len(rows):
            raise Error("出口编号无效。")
        return rows[int(value) - 1]

    def exits(self):
        menu("出口管理", [
            ("查看出口列表", self.show_exits), ("添加 HTTP/SOCKS5 出口", self.add_home),
            ("添加上游 VLESS", self.add_upstream), ("修改出口", self.edit_exit),
            ("删除出口", self.delete_exit), ("检查出口连通性", lambda: self.m.phase("validate")),
        ])

    def next_port(self, home, upstream):
        used = {int(r["listen_port"]) for r in [*home, *upstream]}
        used |= {int(self.m.env["DIRECT_PORT"]), int(self.m.env["SSH_PORT"])}
        if self.m.env["SUBSCRIPTION_LISTEN_PORT"]:
            used.add(int(self.m.env["SUBSCRIPTION_LISTEN_PORT"]))
        return str(GEN.next_available_port(int(self.m.env["HOME_PORT_START"]), int(self.m.env["HOME_PORT_END"]), used))

    def home_inputs(self, existing=None):
        row = dict(existing or {})
        for key, label, default in (
            ("tag", "出口名称（字母/数字/横线/下划线）", "home-01"),
            ("type", "代理类型 socks5/http", "socks5"), ("server", "代理地址", ""),
            ("server_port", "代理端口", "1080"), ("username", "用户名（- 清空）", ""),
            ("password", "密码（回车保留，- 清空）", ""), ("listen_port", "本机入口端口", ""),
        ):
            value = ask(label, row.get(key, default), secret=key == "password")
            row[key] = "" if key in {"username", "password"} and value == "-" else value
        row.setdefault("network", "tcp")
        return row

    def add_home(self):
        home, upstream = self.m.exit_files()
        row = self.home_inputs({"listen_port": self.next_port(home, upstream)})
        confirm(f"添加出口 {row['tag']}，入口 {row['listen_port']}/tcp？")
        self.m.save_exits([*home, row], upstream)

    def add_upstream(self):
        home, upstream = self.m.exit_files()
        print("每行粘贴一条 VLESS 链接，空行结束。")
        urls = []
        while True:
            value = ask("VLESS")
            if not value:
                break
            urls.append(value)
        if not urls:
            return
        for url in urls:
            name = GEN.safe_tag("", f"relay-{len(upstream) + 1:02}", {r["tag"] for r in [*home, *upstream]})
            name = ask("出口名称", name)
            listen = port(ask("本机入口端口", self.next_port(home, upstream)))
            GEN.parse_upstream_node(name, int(listen), url, 1)
            upstream.append({"tag": name, "listen_port": listen, "node_url": url})
        confirm(f"添加 {len(urls)} 个出口并刷新订阅？")
        self.m.save_exits(home, upstream)

    def edit_exit(self):
        selected = self.select_exit()
        home, upstream = self.m.exit_files()
        group = home if selected["source"] == "home" else upstream
        index = next(i for i, row in enumerate(group) if row["tag"] == selected["tag"])
        if selected["source"] == "home":
            group[index] = self.home_inputs(group[index])
        else:
            row = dict(group[index])
            row["tag"] = ask("出口名称", row["tag"])
            row["listen_port"] = port(ask("本机入口端口", row["listen_port"]))
            row["node_url"] = ask("上游 VLESS（回车保留）", row["node_url"], secret=True)
            group[index] = row
        confirm(f"应用出口 {selected['tag']} 的修改？")
        self.m.save_exits(home, upstream)

    def delete_exit(self):
        row = self.select_exit()
        confirm(f"删除出口 {row['tag']} 及其入口节点？", row["tag"])
        home, upstream = self.m.exit_files()
        self.m.save_exits([r for r in home if r["tag"] != row["tag"]],
                          [r for r in upstream if r["tag"] != row["tag"]])

    def ipv6(self):
        menu("IPv6 设置", [
            ("查看当前配置", lambda: print(f"IPv6：{self.m.env['SERVER_IP_IPV6'] or '未配置'}；监听：{self.m.env['ENABLE_IPV6_LISTEN']}")),
            ("启用 IPv6 节点", self.enable_ipv6), ("修改 IPv6 地址", self.enable_ipv6),
            ("关闭 IPv6 节点", self.disable_ipv6),
        ])

    def enable_ipv6(self):
        detected = run(["curl", "-6fsS", "--max-time", "4", "https://api64.ipify.org"], capture=True, check=False)
        default = detected.stdout.strip() if detected.returncode == 0 else self.m.env["SERVER_IP_IPV6"]
        if not default:
            output = run(["ip", "-6", "-o", "addr", "show", "scope", "global"], capture=True)
            default = output.split()[3].split("/")[0] if output else ""
        value = ask("公网 IPv6", default)
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
            ("查看当前配置", lambda: print(f"域名：{self.m.env['CUSTOM_DOMAIN'] or '未配置'}；启用：{self.m.env['ENABLE_CUSTOM_DOMAIN']}\nZone：{self.m.env['CLOUDFLARE_ZONE_NAME']}")),
            ("配置或修改 CF 域名", self.configure_domain), ("更新 CF 凭据", self.cf_credentials),
            ("同步 DNS 记录", lambda: self.m.phase("cloudflare-dns")),
            ("检查或续期证书", self.certificate), ("停用域名模式", self.disable_domain),
        ])

    def configure_domain(self):
        env = self.m.env
        domain = ask("域名", env["CUSTOM_DOMAIN"]).lower()
        values = {"ENABLE_CUSTOM_DOMAIN": "true", "CUSTOM_DOMAIN": domain,
                  "CLOUDFLARE_ZONE_NAME": ask("Cloudflare Zone", env["CLOUDFLARE_ZONE_NAME"] or ".".join(domain.split(".")[-2:])),
                  "LE_EMAIL": ask("证书邮箱", env["LE_EMAIL"]),
                  "CLOUDFLARE_API_TOKEN": ask("CF API Token（回车保留）", env["CLOUDFLARE_API_TOKEN"], secret=True),
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
        value = ask("新 CF API Token", secret=True)
        if not value:
            raise Error("Token 不能为空。")
        confirm("更新 CF 凭据并验证域名配置？")
        self.m.apply({"CLOUDFLARE_API_TOKEN": value}, domain=True)

    def certificate(self):
        if self.m.env["ENABLE_CUSTOM_DOMAIN"] != "true":
            raise Error("尚未启用域名模式。")
        domain = self.m.env["CUSTOM_DOMAIN"]
        run(["openssl", "x509", "-in", f"/etc/letsencrypt/live/{domain}/fullchain.pem", "-noout", "-dates"], check=False)
        confirm("检查并在需要时续期当前域名证书？")
        self.m.phase("custom-domain", RRB_SKIP_PACKAGE_INSTALL="false")

    def disable_domain(self):
        old = dict(self.m.env)
        confirm("停用域名模式，切换为 IP 节点与 HTTP 订阅（公网 51040），删除对应项目 DNS 记录？")
        self.m.apply({"ENABLE_CUSTOM_DOMAIN": "false", "CUSTOM_DOMAIN": "",
                      "SUBSCRIPTION_BASE_URL": "", "SUBSCRIPTION_PORT": "51040", "SUBSCRIPTION_LISTEN_PORT": "51040",
                      "REALITY_SERVER_NAME": "www.microsoft.com", "REALITY_HANDSHAKE_SERVER": "www.microsoft.com",
                      "REALITY_HANDSHAKE_PORT": "443"})
        self.m.remove_old_dns(old, ["A", "AAAA"])
        print("原证书与 Nginx 本机站点保留，可再次启用域名时复用。")

    def service_action(self, action):
        names = ["sing-box", self.m.env["SUBSCRIPTION_SERVICE"]]
        choice = ask("服务：1 代理 / 2 订阅 / 3 全部", "3")
        if choice not in {"1", "2", "3"}:
            raise Error("服务选择无效。")
        targets = names if choice == "3" else [names[int(choice) - 1]]
        confirm(f"{action}：{', '.join(targets)}？")
        for name in targets:
            self.m.service(name, action)

    def services(self):
        menu("服务与诊断", [
            ("查看详细状态", self.detail_status),
            ("启动服务", lambda: self.service_action("start")), ("停止服务", lambda: self.service_action("stop")),
            ("重启服务", lambda: self.service_action("restart")), ("查看日志", self.logs),
            ("检查配置与监听", lambda: self.m.phase("validate")),
        ])

    def detail_status(self):
        self.status()
        run(["ss", "-lntup"])
        for service in ("sing-box", self.m.env["SUBSCRIPTION_SERVICE"]):
            if self.m.alpine():
                self.m.service(service, "status", check=False)
            else:
                run(["systemctl", "status", service, "--no-pager", "--lines=5"], check=False)

    def logs(self):
        choice = ask("日志：1 SuRelay / 2 sing-box / 3 订阅", "1")
        if choice == "1":
            run(["tail", "-n", "80", "/var/log/surelay.log"], check=False)
        elif choice in {"2", "3"}:
            name = "sing-box" if choice == "2" else self.m.env["SUBSCRIPTION_SERVICE"]
            if self.m.alpine():
                paths = [Path(folder) / f"{name}.log" for folder in ("/var/log/surelay", "/var/log/reality-relay")]
                run(["tail", "-n", "80", next((p for p in paths if p.exists()), paths[0])], check=False)
            else:
                run(["journalctl", "-u", name, "-n", "80", "--no-pager"], check=False)

    def security(self):
        menu("VPS 安全", [
            ("查看安全状态", self.security_status), ("防火墙与端口", self.firewall),
            ("修改 SSH 端口", lambda: self.s.change_ssh_port(ask("新 SSH 端口", self.m.env["SSH_PORT"]), self.confirm_login)),
            ("管理 SSH 公钥", self.keys), ("SSH 登录设置", self.ssh_login),
            ("登录防护（fail2ban）", self.fail2ban),
        ])

    def security_status(self):
        for line in self.s.effective().splitlines():
            if line.split()[0] in {"port", "permitrootlogin", "pubkeyauthentication", "passwordauthentication", "authenticationmethods"}:
                print(line)
        print(f"SSH 公钥数量：{len(self.s.keys()[2])}")
        if shutil.which("ufw"):
            run(["ufw", "status", "verbose"], check=False)
        if shutil.which("fail2ban-client"):
            run(["fail2ban-client", "status", "sshd"], check=False)

    def confirm_login(self, value=None):
        value = value or self.m.env["SSH_PORT"]
        host = self.m.env["SERVER_IP_IPV4"] or self.m.env["SERVER_IP_IPV6"]
        print("保留当前窗口，在新终端验证公钥登录。NAT 主机使用服务商分配的公网端口。")
        print(f"ssh -p {value} -o PreferredAuthentications=publickey -o PasswordAuthentication=no root@{host} whoami")
        confirm("仅当新窗口输出 root 后继续", "VERIFIED")

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
        lines, indexes = self.show_keys()
        choice = ask("公钥编号")
        if not choice.isdigit() or not 1 <= int(choice) <= len(indexes):
            raise Error("公钥编号无效。")
        return lines, indexes[int(choice) - 1]

    def add_keys(self):
        _, lines, _ = self.s.keys()
        print("每行一把公钥，空行结束。")
        added = []
        while True:
            value = ask("公钥")
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
        confirm("删除所选公钥？请先使用另一把保留的公钥登录新窗口。")
        self.confirm_login()
        self.s.write_keys(lines[:index] + lines[index + 1:])

    def replace_key(self):
        lines, index = self.key_selection()
        key = ask("新公钥")
        print(self.s.validate_key(key))
        if key in lines:
            raise Error("该公钥已存在。")
        self.s.write_keys([*lines, key])
        print("新公钥已添加，旧公钥暂时保留。请用新公钥完成登录验证。")
        self.confirm_login()
        self.s.write_keys(lines[:index] + lines[index + 1:] + [key])

    def ssh_login(self):
        menu("SSH 登录设置", [
            ("查看有效策略", lambda: print(self.s.effective())),
            ("关闭密码登录", lambda: self.s.password(False, self.confirm_login)),
            ("临时开启密码登录", self.enable_password),
        ])

    def enable_password(self):
        confirm("临时允许 root 密码认证？此操作不创建或更改密码。", "PASSWORD")
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
        return firewall_rule(ask("端口或范围"), ask("协议 tcp/udp/both", "tcp"),
                             ask("来源 IP/网段，any 为所有地址", "any"),
                             ask("地址族 4/6/both", "both"), ask("备注", "application"))

    def add_rule(self):
        rule = self.rule_input()
        confirm(f"开放本机端口：{rule}？")
        self.s.edit_rule(rule)

    def change_rule(self, replace):
        _, rules = self.s.custom_rules()
        for i, rule in enumerate(rules, 1):
            print(f"{i}) {rule}")
        choice = ask("自定义规则编号")
        if not choice.isdigit() or not 1 <= int(choice) <= len(rules):
            raise Error("自定义规则编号无效；其他软件的规则仅展示。")
        replacement = self.rule_input() if replace else None
        confirm("修改所选规则？" if replace else "删除所选自定义规则？")
        self.s.edit_rule(replacement, int(choice) - 1)

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
        confirm("启用 SSH 登录防护？")
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
        confirm("停用 SSH jail，其他服务的 jail 保留？")
        self.s.fail2ban_settings({"enabled": "false"})

    def unban(self):
        value = str(ipaddress.ip_address(ask("解除封禁的 IP")))
        confirm(f"解除 {value} 的 SSH 封禁？")
        run(["fail2ban-client", "set", "sshd", "unbanip", value])

    def whitelist(self):
        run(["fail2ban-client", "get", "sshd", "ignoreip"])
        value = ask("新的白名单，IP/网段以空格分隔；本机回环始终保留")
        values = ["127.0.0.1/8", "::1", *value.split()]
        for item in values:
            ipaddress.ip_network(item, strict=False)
        confirm("替换 SSH 防护白名单？")
        self.s.fail2ban_settings({"ignoreip": " ".join(dict.fromkeys(values))})

    def ban_settings(self):
        values = {key: ask(label, default) for key, label, default in (
            ("bantime", "封禁秒数", "3600"), ("findtime", "统计窗口秒数", "600"), ("maxretry", "允许失败次数", "5"))}
        confirm(f"应用 SSH 防护参数：{values}？")
        self.s.fail2ban_settings(values)

    def advanced(self):
        menu("高级维护", [
            ("更新 SuRelay", self.update_code), ("更新 sing-box", self.update_core),
            ("备份节点配置", self.m.backup), ("恢复节点配置", self.restore),
            ("编辑高级配置并应用", self.edit_config), ("重置并重新安装", self.reset),
            ("继续安装", self.install), ("迁移旧安装目录", self.migrate),
        ])

    def update_code(self):
        confirm("从 origin/main 快进更新 SuRelay，保留配置和节点身份？")
        self.m.update_code()
        raise SystemExit(0)

    def update_core(self):
        confirm("按现有安装方式更新 sing-box？Alpine 使用本项目校验过的固定版本。")
        self.m.backup()
        run(["bash", self.m.root / "scripts/18-update-core.sh"], env={"RRB_PROJECT_DIR": str(self.m.root), "RRB_VERBOSE": "true"})

    def restore(self):
        path = ask("SuRelay 节点备份目录")
        confirm("恢复节点配置、出口与密钥；现有节点和订阅身份可能变化，SSH 设置保持不变？", "RESTORE")
        self.m.restore_nodes(path)

    def edit_config(self):
        # Edit a private copy. Do not leave a half-edited live config on cancel/error.
        with tempfile.TemporaryDirectory(prefix="surelay-edit-") as folder:
            path = Path(folder) / "config.env"
            atomic(path, self.m.config.read_bytes())
            editor = shlex.split(os.environ.get("EDITOR", "nano" if shutil.which("nano") else "vi"))
            run([*editor, path])
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
        run(["bash", self.m.root / "install.sh", "--reset"])
        self.m.reload()

    def install(self):
        run(["bash", self.m.root / "install.sh"])
        raise SystemExit(0)

    def migrate(self):
        self.m.migrate()
        raise SystemExit(0)

    def uninstall(self):
        run(["python3", self.m.root / "scripts/17-reset-install.py",
             "--project-dir", self.m.root, "--uninstall", "--apply"])
        raise SystemExit(0)


def main():
    parser = argparse.ArgumentParser(prog="sr", description="SuRelay：sr 打开菜单；sr status 查看状态；sr update 更新管理脚本。")
    parser.add_argument("command", nargs="?", default="menu", choices=["menu", "status", "update", "install", "migrate", "uninstall", "help"])
    parser.add_argument("--install-command", action="store_true", help="安装 /usr/local/bin/sr，不修改运行配置")
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
            panel.main()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (Error, OSError, ValueError, GEN.ConfigError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except (EOFError, KeyboardInterrupt):
        print("\n已退出 SuRelay。")
