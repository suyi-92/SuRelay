#!/usr/bin/env python3
"""Reviewable installation draft, using the same terminal widgets as sr."""
from __future__ import annotations

import argparse
import ipaddress
import os
from pathlib import Path
import re
import sys

from surelay_core import Error, GEN, Manager, ROOT, atomic, env_text, port, random_high_ports
from surelay_terminal import STEP_NEXT, STEP_PREVIOUS, Terminal, current
from surelay import ask, Cancelled, pick


PROTOCOLS = (
    ("HY2", "Hysteria2 / HY2", "HY2_PORT"),
    ("SOCKS5", "SOCKS5", "SOCKS5_PORT"),
    ("SHADOWSOCKS", "Shadowsocks / SS", "SHADOWSOCKS_PORT"),
    ("HTTP", "HTTP", "HTTP_PORT"),
)


def required(value):
    if not value.strip():
        raise Error("此项不能为空。")
    return value.strip()


def ipv4(value):
    try:
        return str(ipaddress.IPv4Address(value))
    except ValueError as exc:
        raise Error("请填写有效 IPv4 地址。") from exc


def domain(value):
    value = value.strip().lower().rstrip(".")
    if not re.fullmatch(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", value):
        raise Error("请填写完整域名，例如 edge.example.com。")
    return value


def subdomain(value):
    value = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", value):
        raise Error("一级子域名须为 1–63 位英文字母、数字或内部连字符，不含点，例如 aaltr-verizon。")
    return value


def initial_values(address="", security_only=False):
    # Generate once per draft. Back/next never regenerates ports or credentials.
    keys = ["SSH_PORT", "DIRECT_PORT", "SUBSCRIPTION_INTERNAL_PORT", "NGINX_FALLBACK_PORT",
            "HY2_PORT", "SOCKS5_PORT", "SHADOWSOCKS_PORT", "HTTP_PORT"]
    values = dict(zip(keys, map(str, random_high_ports(len(keys), reserved=(443,)))))
    values.update({
        "SERVER_ALIAS": "security-only" if security_only else "",
        "SERVER_IP_IPV4": address, "SERVER_IP_IPV6": "", "ENABLE_IPV6_LISTEN": "false",
        "SURELAY_SECURITY_ONLY": str(security_only).lower(),
        "ENABLE_CUSTOM_DOMAIN": str(not security_only).lower(),
        "ENABLE_SUBSCRIPTION_SERVER": str(not security_only).lower(),
        "SUBSCRIPTION_PORT": "", "SUBSCRIPTION_TARGET": "ClashMeta",
        "CUSTOM_DOMAIN": "", "CLOUDFLARE_ZONE_NAME": "", "LE_EMAIL": "",
        "CLOUDFLARE_API_TOKEN": "", "ADMIN_PUBKEY": "", "ADMIN_PUBKEYS": "",
        "RESET_PROXY_KEYS": "false", "HOME_PORT_START": "51043", "HOME_PORT_END": "65535",
        "HY2_CERT_PATH": "", "HY2_KEY_PATH": "", "HY2_SERVER_NAME": "", "HY2_PASSWORD": "",
        "HY2_INSECURE": "false",
        "PROXY_USERNAME": "surelay", "PROXY_PASSWORD": "",
        "SHADOWSOCKS_METHOD": "aes-128-gcm", "SHADOWSOCKS_PASSWORD": "",
    })
    values.update({"ENABLE_" + key: "false" for key, _, _ in PROTOCOLS})
    return values


def prepare_values(values):
    result = dict(values)
    result["SERVER_IP"] = result["SERVER_IP_IPV4"]
    enabled = result["ENABLE_CUSTOM_DOMAIN"] == "true"
    result.update({
        "REALITY_SERVER_NAME": result["CUSTOM_DOMAIN"] if enabled else "www.microsoft.com",
        "REALITY_HANDSHAKE_SERVER": result.get("NGINX_FALLBACK_HOST", "127.0.0.1") if enabled else "www.microsoft.com",
        "REALITY_HANDSHAKE_PORT": result["NGINX_FALLBACK_PORT"] if enabled else "443",
        "SUBSCRIPTION_BASE_URL": ("https://" + result["CUSTOM_DOMAIN"] +
                                  (":" + result["DIRECT_PORT"] if result["DIRECT_PORT"] != "443" else "")) if enabled else "",
    })
    return result


class Wizard:
    def __init__(self, values, *, security_only=False, protocol_only=False, validate_extra=None, redeploy=False):
        self.values = dict(values)
        self.security_only, self.protocol_only = security_only, protocol_only
        self.redeploy = redeploy
        self.keys_confirmed = protocol_only
        self.validate_extra = validate_extra
        self.error = ""
        self._subdomain = None
        zone = self.values.get("CLOUDFLARE_ZONE_NAME", "").strip().lower().rstrip(".")
        hostname = self.values.get("CUSTOM_DOMAIN", "").strip().lower().rstrip(".")
        if zone and hostname.endswith("." + zone):
            # Existing hostnames are explicit choices. New drafts instead
            # follow the node name until the user changes the suggested label.
            self._subdomain = hostname[:-(len(zone) + 1)]

    def choose(self, title, actions, status="", selected=0, back="返回", *, steps=None):
        window = current()
        if window:
            with window.page(title):
                return window.choose([(label, None) for label, _ in actions], status, selected,
                                     navigation=False, back=back, steps=steps)
        print("\n" + title)
        if status:
            print(status)
        for index, (label, _) in enumerate(actions, 1):
            print(f"{index}) {label}")
        if steps:
            print(f"< {steps[0]}    {steps[1]} >")
        else:
            print(f"0) {back}")
        while True:
            choice = ask("编辑编号，< 上一步 / > 下一步" if steps else "请选择",
                         str(selected + 1) if actions else ">").strip().lower()
            if steps and choice in ("<", "left"):
                return STEP_PREVIOUS
            if steps and choice in (">", "right"):
                return STEP_NEXT
            if choice == "0":
                return None
            if choice.isdigit() and 1 <= int(choice) <= len(actions):
                return int(choice) - 1
            print("请输入编辑项编号，或用 < / > 切换步骤。" if steps else "请输入列表中的编号。")

    def edit(self, key, label, *, secret=False, validate=None, help=""):
        old = self.values.get(key, "")
        value = ask(label, old, secret=secret, validate=validate,
                    help=help + (" 输入 - 清空。" if not validate else ""))
        self.values[key] = "" if value == "-" and not validate else value
        if key in {"SERVER_ALIAS", "CLOUDFLARE_ZONE_NAME"}:
            self.sync_domain()

    def subdomain_value(self):
        return self._subdomain if self._subdomain is not None else self.values.get("SERVER_ALIAS", "").strip().lower()

    def sync_domain(self):
        if self.protocol_only:
            return
        prefix = self.subdomain_value()
        zone = self.values.get("CLOUDFLARE_ZONE_NAME", "").strip().lower().rstrip(".")
        self.values["CLOUDFLARE_ZONE_NAME"] = zone
        self.values["CUSTOM_DOMAIN"] = f"{prefix}.{zone}" if prefix and zone else ""

    def edit_subdomain(self):
        default = self.subdomain_value()
        value = ask("一级子域名", default, validate=subdomain,
                    help="只填写域名前的一段，例如 aaltr-verizon；默认取节点名称的小写形式，可修改。")
        value = subdomain(value)
        if self._subdomain is not None or value != default:
            self._subdomain = value
        self.sync_domain()

    def toggle(self, key):
        self.values[key] = "false" if self.values.get(key) == "true" else "true"

    def field(self, key, label, *, secret=False, validate=None, help=""):
        value = self.values.get(key, "")
        shown = (("已设置（隐藏）" if value else "自动生成" if key.endswith("PASSWORD") else "待填写")
                 if secret else value or "待填写")
        return f"{label}：{shown}", lambda: self.edit(key, label, secret=secret, validate=validate, help=help)

    def group(self, title, fields, *, status="", finish="下一步", step_navigation=True):
        selected = 0
        while True:
            actions = fields()
            if not step_navigation:
                actions.append((finish, None))
            index = self.choose(title, actions, self.error or status, selected, "上一步",
                                steps=("上一步", finish) if step_navigation else None)
            self.error = ""
            if index is None or index == STEP_PREVIOUS:
                return False
            if index == STEP_NEXT:
                return True
            selected = index
            if not step_navigation and index == len(actions) - 1:
                return True
            try:
                actions[index][1]()
                selected = min(index + 1, max(0, len(fields()) - 1))
            except (KeyboardInterrupt, Cancelled):
                pass
            except (Error, GEN.ConfigError, ValueError) as exc:
                self.error = str(exc)

    def port_fields(self):
        fields = [self.field("SSH_PORT", "SSH 端口", validate=port,
                             help="本机监听端口；稍后在新终端验证登录。")]
        if not self.security_only:
            fields += [self.field("DIRECT_PORT", "VLESS / 域名 HTTPS 入口", validate=port),
                       self.field("SUBSCRIPTION_INTERNAL_PORT", "订阅服务端口", validate=port),
                       self.field("NGINX_FALLBACK_PORT", "静态网页内部端口", validate=port)]
        fields.append(("重新随机生成高位端口", self.reroll))
        return fields

    def reroll(self):
        keys = ["SSH_PORT"] if self.security_only else ["SSH_PORT", "DIRECT_PORT", "SUBSCRIPTION_INTERNAL_PORT", "NGINX_FALLBACK_PORT"]
        reserved = [443]
        reserved += [self.values.get(key, "0") for _, _, key in PROTOCOLS]
        self.values.update(zip(keys, map(str, random_high_ports(len(keys), reserved=map(int, reserved)))))

    def edit_domain_mode(self):
        enabled = self.values["ENABLE_CUSTOM_DOMAIN"] == "true"
        mode = pick("CF 域名与静态网页", ["开启（使用域名和 HTTPS 静态网页、订阅）",
                                         "关闭（使用 IP 节点和 HTTP 订阅）"],
                    context=[("当前状态", "开启" if enabled else "关闭")], default=0 if enabled else 1)
        self.values["ENABLE_CUSTOM_DOMAIN"] = "true" if mode == 0 else "false"

    def domain_fields(self):
        self.sync_domain()
        enabled = self.values["ENABLE_CUSTOM_DOMAIN"] == "true"
        fields = [("CF 域名与静态网页：" + ("开启" if enabled else "关闭"),
                   self.edit_domain_mode)]
        if enabled:
            fields += [("一级子域名：" + (self.subdomain_value() or "待填写"), self.edit_subdomain),
                       self.field("CLOUDFLARE_ZONE_NAME", "域名", validate=domain,
                                  help="填写 Cloudflare 中托管的域名（Zone），例如 example.com；与一级子域名组合为完整节点域名。"),
                       self.field("LE_EMAIL", "证书邮箱", validate=required),
                       self.field("CLOUDFLARE_API_TOKEN", "Cloudflare API Token", secret=True,
                                  validate=required, help="需要该 Zone 的 DNS 编辑与 Zone 读取权限。")]
        return fields

    def protocol_fields(self):
        fields = [("VLESS + Reality：开启（主入口）", lambda: None)]
        for key, label, port_key in PROTOCOLS:
            enabled = self.values.get("ENABLE_" + key) == "true"
            fields.append((label + ("：开启 · " + self.values.get(port_key, "") if enabled else "：关闭"),
                           lambda k=key, l=label, p=port_key: self.protocol(k, l, p)))
        return fields

    def protocol(self, key, label, port_key):
        def fields():
            enabled = self.values.get("ENABLE_" + key) == "true"
            items = [("启用：" + ("是" if enabled else "否"), lambda: self.toggle("ENABLE_" + key))]
            if not enabled:
                return items
            items.append(self.field(port_key, "监听端口", validate=port))
            if key == "HY2":
                items += [self.field("HY2_PASSWORD", "认证密码", secret=True)]
                if self.values.get("ENABLE_CUSTOM_DOMAIN") == "true":
                    items.append(("TLS：默认使用 CF 域名证书", lambda: None))
                items += [self.field("HY2_SERVER_NAME", "TLS 域名（CF 模式可留空）"),
                          self.field("HY2_CERT_PATH", "证书绝对路径（CF 模式可留空）"),
                          self.field("HY2_KEY_PATH", "私钥绝对路径（CF 模式可留空）"),
                          ("跳过客户端证书校验：" + ("是" if self.values.get("HY2_INSECURE") == "true" else "否"),
                           lambda: self.toggle("HY2_INSECURE"))]
            elif key in {"SOCKS5", "HTTP"}:
                items += [self.field("PROXY_USERNAME", "代理用户名", validate=required),
                          self.field("PROXY_PASSWORD", "代理密码", secret=True)]
            else:
                items += [self.field("SHADOWSOCKS_METHOD", "加密算法", validate=self.ss_method),
                          self.field("SHADOWSOCKS_PASSWORD", "认证密码", secret=True)]
            return items
        self.group(label, fields, status="留空密码将生成稳定的独立凭证。SOCKS5 / HTTP 使用同一账号。",
                   finish="保存到草稿并返回")

    @staticmethod
    def ss_method(value):
        if value not in {"aes-128-gcm", "aes-256-gcm", "chacha20-ietf-poly1305"}:
            raise Error("支持 aes-128-gcm、aes-256-gcm、chacha20-ietf-poly1305。")
        return value

    def add_keys(self):
        keys = []
        while True:
            value = ask("管理员 SSH 公钥", multiline=True,
                        help=("重新部署需录入新管理员公钥。" if self.redeploy else "") +
                             "每行一把公钥，可粘贴多行；空行完成。取消后原草稿不变。",
                        context=[("本次已接收", f"{len(keys)} 把")])
            if not value:
                break
            for line in value.splitlines():
                line = line.strip()
                if not re.fullmatch(r"(?:ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp\d+|sk-ssh-ed25519@openssh.com|sk-ecdsa-sha2-nistp256@openssh.com)\s+[A-Za-z0-9+/]+={0,2}(?:\s+.*)?", line):
                    raise Error("公钥格式不正确，请粘贴完整 SSH 公钥。")
                if line not in keys:
                    keys.append(line)
        if not keys:
            raise Error("至少填写一把公钥，或明确选择复用已有公钥。")
        self.values["ADMIN_PUBKEY"] = "\n".join(keys)
        self.keys_confirmed = True

    def reuse_keys(self):
        if self.redeploy:
            raise Error("重新部署需录入新管理员公钥。")
        # Existing keys are checked by the SSH deployment phase before policy changes.
        self.values["ADMIN_PUBKEY"] = ""
        self.keys_confirmed = True

    def key_fields(self):
        count = len(self.values.get("ADMIN_PUBKEY", "").splitlines())
        if self.redeploy:
            return [(f"录入 / 替换新管理员公钥：已填 {count} 把", self.add_keys)]
        return [(f"录入 / 替换公钥：已填 {count} 把", self.add_keys),
                ("明确复用 root 已有 authorized_keys" + ("（已选择）" if self.keys_confirmed and not count else ""), self.reuse_keys)]

    def validate(self):
        self.sync_domain()
        values = prepare_values(self.values)
        if not self.protocol_only:
            ipv4(values["SERVER_IP_IPV4"])
            if not self.security_only:
                required(values["SERVER_ALIAS"])
            if self.redeploy and not values.get("ADMIN_PUBKEY", "").strip():
                raise Error("重新部署需录入新管理员公钥。")
            if not self.keys_confirmed:
                raise Error("请录入管理员公钥，或明确选择复用已有公钥。")
        if values["ENABLE_CUSTOM_DOMAIN"] == "true" and not self.protocol_only:
            subdomain(self.subdomain_value())
            for key in ("CUSTOM_DOMAIN", "CLOUDFLARE_ZONE_NAME"):
                domain(values[key])
            for key in ("LE_EMAIL", "CLOUDFLARE_API_TOKEN"):
                # Maintenance may already use a credentials file.
                if key != "CLOUDFLARE_API_TOKEN" or not self.protocol_only:
                    required(values[key])
        resolved = GEN.defaults(values)
        GEN.validate_env(resolved)
        if self.validate_extra:
            self.validate_extra(resolved)
        return values

    def summary(self):
        self.sync_domain()
        v = self.values
        lines = [f"节点：{v.get('SERVER_ALIAS') or '待填写'} · {v.get('SERVER_IP_IPV4') or '待填写'}",
                 f"SSH：{v['SSH_PORT']}"]
        if not self.security_only:
            lines += [f"主入口：{v['DIRECT_PORT']} · 订阅：{v['SUBSCRIPTION_INTERNAL_PORT']} · 静态页：{v['NGINX_FALLBACK_PORT']}",
                      "协议：VLESS Reality" + "".join("、" + label for key, label, _ in PROTOCOLS if v.get("ENABLE_" + key) == "true"),
                      "CF 域名与静态页：" + (v.get("CUSTOM_DOMAIN") or "待填写" if v["ENABLE_CUSTOM_DOMAIN"] == "true" else "关闭")]
        lines.append("草稿保留全部输入；可返回任意分组修改。密码和 Token 均隐藏。")
        return "\n".join(lines)

    def run(self):
        if self.protocol_only:
            while self.group("入口协议", self.protocol_fields, finish="校验并应用", step_navigation=False):
                try:
                    return self.validate()
                except (Error, GEN.ConfigError, ValueError) as exc:
                    self.error = str(exc)
            raise Cancelled("协议修改已取消。")
        groups = [("基础信息", lambda: [self.field("SERVER_ALIAS", "节点名称", validate=required),
                                       self.field("SERVER_IP_IPV4", "公网 IPv4", validate=ipv4)]),
                  ("服务端口", self.port_fields)]
        if not self.security_only:
            groups += [("CF 域名与静态网页", self.domain_fields), ("入口协议", self.protocol_fields)]
        groups += [("管理员 SSH 公钥", self.key_fields)]
        index = 0
        while True:
            if index < len(groups):
                title, fields = groups[index]
                if self.group(f"安装 {index + 1}/{len(groups)} · {title}", fields, status="草稿尚未写入或部署。"):
                    index += 1
                elif index:
                    index -= 1
                else:
                    raise Cancelled("安装已取消，未写入配置。")
                continue
            actions = [("保存配置并继续安装", None)] + [("修改 · " + title, None) for title, _ in groups]
            choice = self.choose("安装预览", actions, (self.error + "\n" if self.error else "") + self.summary())
            self.error = ""
            if choice is None:
                index = len(groups) - 1
            elif choice:
                index = choice - 1
            else:
                try:
                    return self.validate()
                except (Error, GEN.ConfigError, ValueError) as exc:
                    self.error = str(exc)


def save_config(root, values):
    root = Path(root)
    destination = root / "config.env"
    if destination.exists() or destination.is_symlink():
        raise Error("已有 config.env，停止覆盖；重跑安装会保留原配置。")
    atomic(destination, env_text((root / "config.example.env").read_text(encoding="utf-8"), values))


def redeploy_validator(project_dir, handoff):
    """Validate the unsaved identity against its handoff without staging SSH."""
    def validate(resolved):
        from surelay_redeploy import Redeployer
        manager = Manager(project_dir)
        manager.env = dict(resolved)
        operation = Redeployer(manager, handoff)
        lines = operation.new_keys()
        operation.validate_fresh_identity(lines)
    return validate


def main():
    parser = argparse.ArgumentParser(description="SuRelay 分组安装向导")
    parser.add_argument("--project-dir", type=Path, default=ROOT)
    parser.add_argument("--ipv4", default="")
    parser.add_argument("--security-only", action="store_true")
    parser.add_argument("--plain", action="store_true")
    args = parser.parse_args()
    tty = os.environ.get("RRB_INPUT_TTY", "/dev/tty")
    redeploy = os.environ.get("RRB_REDEPLOY", "false").lower() == "true"
    handoff = os.environ.get("RRB_REDEPLOY_HANDOFF", "")
    validate_extra = redeploy_validator(args.project_dir, handoff) if redeploy and handoff else None
    try:
        # The remote shell entry may have stdin attached to a pipe/process substitution.
        with open(tty, encoding="utf-8") as source, open(tty, "w", encoding="utf-8", buffering=1) as sink:
            original = sys.stdin, sys.stdout
            sys.stdin, sys.stdout = source, sink
            try:
                with Terminal(enabled=not args.plain):
                    values = Wizard(initial_values(args.ipv4, args.security_only), security_only=args.security_only,
                                    redeploy=redeploy, validate_extra=validate_extra).run()
                save_config(args.project_dir, values)
            finally:
                sys.stdin, sys.stdout = original
    except (KeyboardInterrupt, EOFError, Cancelled):
        print("安装已取消，未写入配置。", file=sys.stderr)
        return 1
    except (Error, GEN.ConfigError, OSError, ValueError) as exc:
        print(f"安装配置未完成：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
