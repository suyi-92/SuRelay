# Reality Relay Bootstrap

这是一套面向全新 Ubuntu/Debian VPS 的可复现部署脚本，目标是把 **SSH 安全初始化 + fail2ban + UFW + sing-box VLESS+Reality 多入口/多出口 + 客户端节点生成 + 回滚验证** 做成分阶段执行的项目，而不是一次性把服务器改到不可恢复状态。

默认支持：Ubuntu 22.04、Ubuntu 24.04、Debian 10-13。其他系统会停止并提示。

## 设计原则

1. **不锁机优先**：SSH 拆成 phase1 和 final。phase1 只准备 root 公钥登录，不禁 root、不禁密码。只有另开窗口确认 root key 登录成功后，才能用 `CONFIRM_ROOT_KEY_LOGIN=yes` 执行 final。
2. **先备份后修改**：SSH、fail2ban、UFW、sing-box 配置都会备份到 `/root/reality-relay-bootstrap-backups/`。
3. **先检查后重启**：`sshd -t` 通过后才 reload SSH；`sing-box check -c /etc/sing-box/config.json` 通过后才 restart sing-box。
4. **默认最小开放端口**：UFW 只放行当前 SSH 端口、`DIRECT_PORT`、家宽 CSV 和上游节点里的实际 `listen_port`，不会开放从 51043 起的整段端口。
5. **敏感信息不进普通日志**：VLESS UUID、Reality 私钥、家宽代理密码不会打印到 `/var/log/reality-relay-bootstrap.log`。节点输出文件权限为 600。
6. **233boy 可选且不强依赖**：默认使用 sing-box 官方 APT 源。只有显式启用 `USE_233BOY_INSTALLER=true` 并设置 `CONFIRM_USE_233BOY_INSTALLER=yes` 时才调用 233boy 安装脚本。调用后会隔离 `/etc/sing-box/conf`，由本项目接管 `/etc/sing-box/config.json`。

## 项目结构

```text
reality-relay-bootstrap/
  README.md
  reality-relay-bootstrap-usage.md
  install.sh
  config.example.env
  home-proxies.example.csv
  upstream-nodes.example.txt
  bootstrap.sh
  scripts/
    00-lib.sh
    01-preflight.sh
    02-setup-root-ssh.sh
    03-ssh-hardening-phase1.sh
    04-ssh-hardening-final.sh
    05-install-fail2ban.sh
    06-install-ufw.sh
    07-install-singbox.sh
    08-generate-singbox-config.py
    09-apply-firewall.sh
    10-validate.sh
    11-output-nodes.py
    11-output-nodes-wrapper.sh
    12-rollback.sh
    13-setup-subscription.sh
    13-subscription-server.py
    14-setup-cloudflare-dns.sh
    14-setup-cloudflare-dns.py
    15-setup-custom-domain.sh
    sshd_policy.py
  templates/
    sshd-hardening.conf.tpl
    fail2ban-sshd.local.tpl
    clash-mihomo.yaml.tpl
  docs/
    install-flow.md
    cloudflare-domain-preflight.md
    path-map.md
    recovery.md
    client-setup.md
```

## 完整手册与一键安装

更详细的参数解释、Windows PowerShell 示例、回滚和客户端导入说明，请阅读 [`reality-relay-bootstrap-usage.md`](./reality-relay-bootstrap-usage.md)。
全新域名第一次托管到 Cloudflare 时，先看 [`docs/cloudflare-domain-preflight.md`](./docs/cloudflare-domain-preflight.md)。

如果是在全新 VPS 上使用推荐默认值部署，可以直接运行一键安装脚本。脚本会自动拉取/定位项目、生成 `config.env`、`home-proxies.csv` 和 `upstream-nodes.txt`，会提示填写服务器 IPv4/IPv6、SSH、入口端口、自有域名、节点订阅配置、root 公钥、家宽代理 CSV 以及可选上游节点。重跑一键脚本时，`RESET_PROXY_KEYS` 默认继承旧 `config.env`，找不到旧值时才使用 `false`，避免修改落地节点时误重置中转机节点身份：

```bash
bash <(wget -qO- https://raw.githubusercontent.com/suyi-92/reality-relay-bootstrap/main/install.sh)
```

如果已经下载了本仓库，也可以在项目目录运行：

```bash
sudo bash install.sh
```

Cloudflare API Token 和逐条填写的家宽代理密码会按输入字符数显示 `*`，支持直接粘贴、退格删除和 `Ctrl+U` 清空；直接回车沿用提示中的默认值。输入明文不会显示在终端，按 `Ctrl+C` 可取消并恢复终端输入状态。

管理员 SSH 公钥支持一次性粘贴多行，每行一把，完成后输入空行结束。尚未录入公钥时直接回车会警告并继续等待，格式不正确的行也会提示重新输入；只有单独输入大写 `SKIP` 才能跳过本次录入。跳过后仍需 root 已有可用公钥，否则后续 SSH 部署检查会停止。

已经完成部署、现在只想补一套 IPv6 字面量的本地 VLESS 节点时，使用 `-6` 维护模式。地址可自动探测后确认，也可以直接传入：

```bash
sudo bash install.sh -6
sudo bash install.sh -6 2001:db8::10
```

远程一键脚本同样可以接收该选项：

```bash
bash <(wget -qO- https://raw.githubusercontent.com/suyi-92/reality-relay-bootstrap/main/install.sh) -6 2001:db8::10
```

`-6` 要求 `/opt/reality-relay-bootstrap/config.env`（或 `RRB_INSTALL_DIR` 指定目录）以及现有 sing-box、VLESS UUID、Reality keypair、short-id 都已经就绪。它不会重跑 SSH 初始化，也不会轮换身份材料；只设置 `SERVER_IP_IPV6`、`ENABLE_IPV6_LISTEN=true`、`CLIENT_UDP=true` 和 `RESET_PROXY_KEYS=false`，保留原有 `PROXY_IP_VERSION` 出口/DNS 策略，然后同步 UFW v6 规则、重启并验证 sing-box 的 IPv4/IPv6 监听。

成功后生成三份仅含 IPv6 地址的 600 权限本地文件：

```text
/root/reality-relay-bootstrap-v6-vless.txt
/root/reality-relay-bootstrap-v6-nodes.txt
/root/reality-relay-bootstrap-v6-clash.yaml
```

其中 VLESS 分享链接会正确使用 `[IPv6]` authority；Mihomo/Clash 显式包含 `ipv6: true`、`type: vless`、`network: tcp` 和 `udp: true`。VLESS UDP 流量封装在 TCP/Reality 入口内，不需要开放同号 UDP 端口。仍需在云厂商 IPv6 安全组中放行实际使用的 VLESS TCP 端口。该模式只生成本地 v6 文件，不主动改动现有订阅服务。

一键脚本仍然保留 SSH 二阶段安全确认：完成 `ssh-phase1` 后，需要你另开窗口确认 root 公钥登录正常，才会继续执行 `ssh-final`。家宽代理既可以按提示逐个填写，也可以选择一次性粘贴多行 CSV；上游节点可以直接逐行粘贴普通 `vless://` 或 VLESS Reality 分享链接，也可以粘贴 `tag,node_url,listen_port` CSV。

## 第一次使用

```bash
sudo apt update
sudo apt install -y unzip nano
unzip reality-relay-bootstrap.zip
cd reality-relay-bootstrap

cp config.example.env config.env
nano config.env

cp home-proxies.example.csv home-proxies.csv
nano home-proxies.csv

cp upstream-nodes.example.txt upstream-nodes.txt
nano upstream-nodes.txt
```

`home-proxies.csv` 推荐字段顺序为 `tag,type,server,server_port,username,password,network,listen_port`。`listen_port` 可留空，脚本会从 `HOME_PORT_START` 起自动分配；旧顺序 `tag,listen_port,type,...` 仍兼容。

`upstream-nodes.txt` 用来把别人给你的上游代理节点接到新的中转入口端口。当前只支持 `vless://...security=none...` 和 `vless://...security=reality...`，可以写成 `tag,node_url,listen_port`，也可以只放节点链接让脚本自动分配 tag 和端口；旧顺序 `tag,listen_port,node_url` 仍兼容。

先 dry-run：

```bash
sudo bash bootstrap.sh --phase preflight --dry-run
sudo bash bootstrap.sh --phase singbox --dry-run
sudo bash bootstrap.sh --phase firewall --dry-run
```

## config.env 基础项

```bash
SERVER_ALIAS="my-vps"
SERVER_IP=""
SERVER_IP_IPV4="你的服务器公网IPv4"
SERVER_IP_IPV6=""
SSH_PORT="22"
ADMIN_PUBKEY="ssh-ed25519 AAAA... 你的本地公钥"
# 多个公钥可写成逐行内容，例如：
# ADMIN_PUBKEY=$'ssh-ed25519 AAAA... user1\nssh-ed25519 BBBB... user2'
# 或把额外公钥写入 ADMIN_PUBKEYS。
MODE_443="direct"
```

新配置推荐填写 `SERVER_IP_IPV4` 和/或 `SERVER_IP_IPV6`；`SERVER_IP` 仍保留为旧配置兼容字段，脚本会自动把它归并到对应的 IPv4/IPv6 字段。当前管理用户固定为 root，非 root 的 `ADMIN_USER` 会被警告并按 root 处理。

分步骤运行时，如果 root 已经有正确公钥，`ADMIN_PUBKEY` 可以留空，脚本会复用 `/root/.ssh/authorized_keys`；多个公钥可用 `$'key1\nkey2'` 写入 `ADMIN_PUBKEY`，也可以把额外公钥逐行写入 `ADMIN_PUBKEYS`。一键安装时直接粘贴多行公钥，输入空行结束；若要复用服务器已有公钥，需明确输入 `SKIP`。

## VLESS+Reality 参数

默认参数：

```bash
PROXY_PROTOCOL="vless-reality"
PROXY_IP_VERSION="ipv4"
RESET_PROXY_KEYS="false"
RRB_STATE_DIR="/etc/reality-relay-bootstrap"
VLESS_UUID_PATH="/etc/reality-relay-bootstrap/vless-uuid.txt"
VLESS_FLOW="xtls-rprx-vision"
REALITY_PRIVATE_KEY_PATH="/etc/reality-relay-bootstrap/reality-private.key"
REALITY_PUBLIC_KEY_PATH="/etc/reality-relay-bootstrap/reality-public.key"
REALITY_SHORT_ID_PATH="/etc/reality-relay-bootstrap/reality-short-id.txt"
REALITY_SERVER_NAME="www.microsoft.com"
REALITY_HANDSHAKE_SERVER="www.microsoft.com"
REALITY_HANDSHAKE_PORT="443"
REALITY_MAX_TIME_DIFFERENCE="1m"
CLIENT_FINGERPRINT="chrome"
CLIENT_UDP="true"
CLIENT_ALPN="h2,http/1.1"
```

`PROXY_IP_VERSION` 可选 `ipv4`、`ipv6`、`dual`；默认 `ipv4`。`dual` 表示双栈可用并优先 IPv4。
`RESET_PROXY_KEYS=false` 时重复运行不会轮换 VLESS UUID、Reality keypair 和 short-id；一键安装重跑时会优先继承旧 `config.env` 的值，找不到旧值时默认 `false`。修改家宽/上游落地节点时通常保持 `false`；改为 `true` 会生成新密钥，旧节点和旧订阅 token 会失效。

首次执行 `--phase singbox` 会自动生成：

```text
/etc/reality-relay-bootstrap/vless-uuid.txt
/etc/reality-relay-bootstrap/reality-private.key
/etc/reality-relay-bootstrap/reality-public.key
/etc/reality-relay-bootstrap/reality-short-id.txt
```

这些文件权限为 600。`home-proxies.csv` 和 `upstream-nodes.txt` 也会以 600 权限复制到 `/etc/reality-relay-bootstrap/`。不要公开 Reality 私钥、UUID、CSV、上游节点和节点文件。

完整生成路径、权限、敏感性和清理建议见 [`docs/path-map.md`](docs/path-map.md)。

## 自有域名与 Nginx fallback

可选启用自有域名。默认假设 DNS 托管在 Cloudflare，记录保持灰云 / DNS only，A/AAAA 指向服务器公网 IP：

```bash
ENABLE_CUSTOM_DOMAIN="true"
CUSTOM_DOMAIN="edge.example.com"
CLOUDFLARE_ZONE_NAME="example.com"
LE_EMAIL="admin@example.com"
CLOUDFLARE_API_TOKEN="Cloudflare DNS API Token"
CLOUDFLARE_API_TOKEN_FILE="/etc/reality-relay-bootstrap/cloudflare.ini"
CLOUDFLARE_ENSURE_DNS_RECORDS="true"
CLOUDFLARE_DNS_OVERWRITE_EXISTING="true"
CLOUDFLARE_REQUIRE_ACTIVE_ZONE="true"
NGINX_FALLBACK_HOST="127.0.0.1"
NGINX_FALLBACK_PORT="8443"
```

Cloudflare 侧分两类：

- 必须手动完成：在 Cloudflare 添加/托管 zone，并到域名注册商把 NS 改成 Cloudflare 分配的 nameserver，等待 zone active。
- 脚本自动完成：用 API token 创建或更新 `CUSTOM_DOMAIN` 的 A/AAAA 记录，`proxied=false`，也就是灰云 / DNS only；记录注释会直接写当前 `SERVER_ALIAS`；默认要求 zone 已经 active，避免后续 Let’s Encrypt DNS-01 失败。

启用后：

- 公网 `tcp/443` 仍由 sing-box VLESS+Reality 监听。
- 普通 HTTPS/TLS fallback 到本机 `127.0.0.1:8443` 的 Nginx。
- Nginx 默认站点根目录是 `/var/www/reality-fallback`，脚本会写入一个 `Digital Notes` 静态首页、`robots.txt` 和空 `favicon.ico`。
- 证书通过 certbot `dns-cloudflare` 插件使用 Cloudflare DNS-01 申请 Let’s Encrypt。
- 节点输出里的 `server` 和 `servername` 会使用 `CUSTOM_DOMAIN`。
- 如果启用订阅服务，订阅 URL 默认为 `https://CUSTOM_DOMAIN/sub/<VLESS_UUID>?target=ClashMeta`，订阅服务只绑定本机并由 Nginx 反代。

Cloudflare API Token 至少需要对应 zone 的 DNS 编辑权限；如果要让脚本自动创建 zone，还需要额外配置 `CLOUDFLARE_CREATE_ZONE=true`、`CLOUDFLARE_ACCOUNT_ID` 和具备 Zone 编辑权限的 token。脚本会把 token 写入 600 权限的 credentials 文件。不要打开橙云代理，否则 Reality 443 入口不会直连到服务器。

## 443 模式

```bash
MODE_443="direct"
```

表示 443 永远走中转机自身公网出口。脚本不再生成 AI 域名、CN/private 或其它目标域名/IP 分流规则；每个家宽或上游节点端口都固定走对应出口。

## home-proxies.csv

格式固定：

```csv
tag,type,server,server_port,username,password,network,listen_port
home-01,socks5,proxy1.example.com,1080,user1,"pass,with,comma",tcp,
home-02,http,proxy2.example.com,8080,user2,password2,tcp,51044
```

规则：

- `443` 不能写进 CSV，它由 `DIRECT_PORT` 固定生成。
- `listen_port` 可留空自动分配；填写时不能重复。
- `listen_port` 必须在 `HOME_PORT_START` 到 `HOME_PORT_END` 范围内，默认从 `51043` 起分配。
- `type` 只支持 `socks5` 或 `http`。
- 密码里如果有英文逗号，必须用英文双引号包起来，例如 `"abc,123"`。
- CSV 含有家宽代理账号密码，不要公开。

## upstream-nodes.txt

可选文件，用来把上游节点分享链接接入到新的 VLESS+Reality 入口端口：

```csv
tag,node_url,listen_port
vless-relay,vless://uuid@example.com:443?encryption=none&security=reality&flow=xtls-rprx-vision&type=tcp&sni=www.cloudflare.com&pbk=PUBLIC_KEY&fp=chrome&sid=abcd&spx=%2F#vless,
vless-plain,vless://uuid@example.com:12345?type=tcp&encryption=none&security=none#vless-plain,51049
```

也可以不写表头，只逐行放 `vless://` 节点链接；脚本会从 `HOME_PORT_START` 到 `HOME_PORT_END` 的空闲端口中自动分配。当前支持普通 VLESS 和 VLESS Reality 上游，均使用 TCP transport。

链接中的 `type=tcp` 表示上游 VLESS transport，不限制可代理的目标流量。生成器不设置 VLESS outbound 的 `network`，因此默认允许 TCP 和 UDP；UDP 默认使用 VLESS 的 XUDP 编码，详见 [sing-box 官方 VLESS outbound 文档](https://sing-box.sagernet.org/configuration/outbound/vless/)。Reality、Vision 和现有链接参数继续保留，不支持的 transport（例如 `type=ws`、`type=udp`）仍会被拒绝。

这可承载实时通信、VoIP、Wi-Fi Calling 等应用的 UDP 流量。UDP 500/4500 与其他 UDP 目标一样沿固定的 inbound → 指定 outbound 转发，无需专用域名、IP 或端口分流规则；客户端与上游落地机也需要支持相应的 UDP relay/XUDP。VLESS 入口及上游连接仍使用 TCP，UFW 继续只放行实际使用的 VLESS TCP 入口端口。

已有 `upstream-nodes.txt` 无需新增字段或更改格式。更新代码后，保持 `RESET_PROXY_KEYS=false`，执行 `sudo bash bootstrap.sh --phase singbox` 重新生成并校验配置、重启服务，即可应用此修复。`home-proxies.csv` 中 SOCKS/HTTP 代理的 `network` 语义保持不变。

## 标准执行流程

```bash
sudo bash bootstrap.sh --phase preflight
sudo bash bootstrap.sh --phase ssh-phase1

# 另开窗口测试 root key 登录成功后：
sudo CONFIRM_ROOT_KEY_LOGIN=yes bash bootstrap.sh --phase ssh-final

sudo bash bootstrap.sh --phase fail2ban
sudo bash bootstrap.sh --phase singbox
sudo bash bootstrap.sh --phase firewall
sudo bash bootstrap.sh --phase validate
```

当前 `singbox` 阶段会安装/确认 sing-box、准备 VLESS UUID/Reality keypair/short-id、生成 `/etc/sing-box/config.json`、重启并做 sing-box-only 验证，同时刷新订阅服务，并在 `ENABLE_CUSTOM_DOMAIN=true` 时自动执行 Cloudflare DNS 和 Nginx fallback 配置。`cloudflare-dns`、`custom-domain`、`subscription` 仍可作为独立阶段单独重跑。

## SSH 二阶段说明

`ssh-phase1` 会给 root 安装 SSH 公钥、可选创建 SFTP-only 用户，并在 `/etc/ssh/sshd_config` 开头写入本项目管理的 phase1 策略块，只开启公钥登录，不禁 root、不禁密码。

OpenSSH 对多数配置项采用先读取到的值。服务商的 `00-mofang.conf` 等文件可能比旧版项目 drop-in 更早加载，导致最终加固的 `PermitRootLogin`、`PasswordAuthentication` 被抢先设置；这同样会发生在 Debian 13。现在两个 SSH 阶段都将当前策略放到主配置最前面，先备份再清理旧版项目 drop-in，服务商配置文件保持原样。阶段重跑会替换本项目策略块，`ssh-phase1` 会移除上一次 final 的项目策略；`Match` 条件仍按 OpenSSH 规则生效，冲突时继续阻止 reload，并显示实际值。

不要关闭当前 SSH 窗口。另开 Windows PowerShell 测试：

```powershell
ssh -p 22 -o PreferredAuthentications=publickey -o PasswordAuthentication=no root@服务器IP
```

登录后执行：

```bash
whoami
```

只有确认输出为 `root` 后，才执行：

```bash
sudo CONFIRM_ROOT_KEY_LOGIN=yes bash bootstrap.sh --phase ssh-final
```

## 安装 sing-box 与生成配置

默认官方 APT 安装：

```bash
sudo bash bootstrap.sh --phase singbox
```

该阶段会把 `home-proxies.csv` 和 `upstream-nodes.txt` 复制到 `RRB_STATE_DIR`，然后按最新文件重新生成 sing-box 多入口配置。只要 `RESET_PROXY_KEYS=false`，已有 VLESS UUID、Reality keypair 和 short-id 会继续复用；只有显式设为 `true` 时才删除并重建这些密钥材料。

可选使用 233boy 作为 core/管理工具基础：

```bash
USE_233BOY_INSTALLER="true"
INSTALL_SINGBOX_METHOD="233boy"
sudo CONFIRM_USE_233BOY_INSTALLER=yes bash bootstrap.sh --phase singbox
```

启用 233boy 后，本项目会隔离 `/etc/sing-box/conf` 为 `/etc/sing-box/conf.233boy.disabled.TIMESTAMP`，避免 233boy 默认 VLESS-REALITY 配置和本项目多入口配置同时生效。

## 新增/删除出口

新增家宽 HTTP/SOCKS 出口时编辑 `home-proxies.csv`；新增上游节点出口时编辑 `upstream-nodes.txt`。改落地节点通常不需要重置中转节点身份，请保持 `RESET_PROXY_KEYS=false`。改完后执行：

```bash
sudo bash bootstrap.sh --phase singbox
sudo bash bootstrap.sh --phase firewall
sudo bash bootstrap.sh --phase validate
```

### 添加落地机并保留节点订阅不变

这里的“订阅不变”指订阅 URL token、VLESS UUID、Reality public key、short-id 以及已有节点的名称/端口不变；订阅内容会多出新添加的落地节点。按下面做，旧客户端节点不会因为添加落地机而失效：

1. 确认 `config.env` 中保持：

```bash
RESET_PROXY_KEYS="false"
```

2. 追加新的落地机行，不要改已有行的 `tag` 和 `listen_port`。如果已有节点的 `listen_port` 是自动分配的空值，建议先把当前已使用端口写死，再追加新行，避免以后重排或删除行时端口漂移。

```csv
# home-proxies.csv
tag,type,server,server_port,username,password,network,listen_port
home-01,socks5,proxy1.example.com,1080,user1,password1,tcp,51043
home-02,socks5,proxy2.example.com,1080,user2,password2,tcp,51044
```

或者追加上游 VLESS 落地节点：

```csv
# upstream-nodes.txt
tag,node_url,listen_port
relay-01,vless://uuid@example.com:443?encryption=none&security=reality&type=tcp&pbk=PUBLIC_KEY#relay-01,51045
```

3. 重新生成配置、刷新订阅和防火墙：

```bash
sudo bash bootstrap.sh --phase singbox
sudo bash bootstrap.sh --phase firewall
sudo bash bootstrap.sh --phase validate
```

不要为了添加落地机去删除 `/etc/reality-relay-bootstrap/vless-uuid.txt`、Reality key 文件或 short-id 文件，也不要把 `RESET_PROXY_KEYS` 改成 `true`。否则订阅 token 和旧节点都会变化，客户端需要重新导入。

删除：从 CSV 删除对应行，再执行同样命令。`firewall` 阶段会自动清理带 `reality-relay-bootstrap` 注释且不再使用的本项目 UFW 规则，不会碰你手工添加的其它规则。

`validate` 会直接测试家宽 HTTP/SOCKS 代理；对 `upstream-nodes.txt` 里的 vless 上游节点，会临时启动本地 socks 入口并通过该上游访问 `https://ifconfig.me`。连通性失败只告警，不阻断部署。

## 订阅和可选节点文件

默认只启用简单订阅端口，不主动生成 `/root` 下的节点文件。在 `config.env` 设置：

```bash
ENABLE_SUBSCRIPTION_SERVER="true"
SUBSCRIPTION_PORT=""
SUBSCRIPTION_INTERNAL_PORT="51040"
SUBSCRIPTION_TARGET="ClashMeta"
# 可选：如外层有 HTTPS 反代，仅用于输出订阅链接。
SUBSCRIPTION_BASE_URL=""
```

未启用自有域名时，`SUBSCRIPTION_PORT` 留空会自动回退为 `SUBSCRIPTION_INTERNAL_PORT`，也就是默认 `51040`。

更新订阅服务：

```bash
sudo bash bootstrap.sh --phase subscription
sudo bash bootstrap.sh --phase firewall
```

订阅地址：

```text
http://服务器IP:51040/sub/<VLESS_UUID>?target=ClashMeta
```

内置服务是 HTTP，URL 里的 token 使用 `/etc/reality-relay-bootstrap/vless-uuid.txt`。未启用自有域名时，仍建议只在服务商安全组里放行你的常用来源 IP。启用自有域名时，订阅服务默认只监听本机 `SUBSCRIPTION_INTERNAL_PORT`，外部通过 `https://CUSTOM_DOMAIN/sub/<token>?target=ClashMeta` 访问。
启用自有域名时，`SUBSCRIPTION_PORT` 默认留空；只有显式填写它时，才会额外开放并显示 `http://SERVER_IP:SUBSCRIPTION_PORT/sub/...` 直连订阅链接。
`SUBSCRIPTION_TARGET` 当前只生成 ClashMeta/Mihomo YAML；旧配置里的其它 target 会警告并按 ClashMeta 处理，旧 URL target 不再兼容。
如果同时配置了 IPv4 和 IPv6，两个订阅链接都返回同一份完整节点；IPv4 节点名称保持不变，IPv6 节点名称追加 `-IPv6`。
如果 Clash Verge 或浏览器无法导入 IPv6 字面量订阅地址，有 IPv4 时直接用 IPv4 订阅地址即可，它会返回同一份完整节点。

如需在服务器本地额外生成节点文件，再执行：

```bash
sudo bash bootstrap.sh --phase output-nodes
sudo cat /root/reality-relay-bootstrap-nodes.txt
sudo cat /root/reality-relay-bootstrap-clash.yaml
```

`output-nodes` 阶段只在 `/root` 额外生成可复制的节点文本和 Mihomo/Clash YAML；如果订阅服务已启用，它也会顺手刷新订阅目录和 systemd 服务。

输出节点包括：

- 443 节点使用 `SERVER_ALIAS`，例如 `my-vps`
- 每个 CSV 行一个节点，直接使用该行 `tag`，例如 `home-01`
- 启用自有域名时，所有节点的 `server` 使用 `CUSTOM_DOMAIN`，不再额外生成 IPv6 字面量节点

Mihomo/Clash 节点字段为：

```yaml
name: home-01
type: vless
server: 服务器IP
port: 51043
uuid: "..."
flow: xtls-rprx-vision
tls: true
servername: www.microsoft.com
client-fingerprint: chrome
udp: true
reality-opts:
  public-key: "..."
  short-id: "..."
alpn:
  - h2
  - http/1.1
```

## 防火墙和服务商安全组

脚本只会开放实际端口：

```text
SSH:        tcp/$SSH_PORT
VLESS:      tcp/$DIRECT_PORT
VLESS:      tcp/CSV 中每个 listen_port
订阅:       tcp/$SUBSCRIPTION_PORT（仅 ENABLE_SUBSCRIPTION_SERVER=true 且公网直连订阅端口已启用）
```

服务商安全组/云防火墙也必须手动放行同样端口。
启用自有域名且 `SUBSCRIPTION_PORT` 留空时，订阅服务只监听本机 `SUBSCRIPTION_INTERNAL_PORT` 并由 Nginx fallback 反代，公网安全组只需要放行 SSH 和实际 VLESS 端口。

## 验证

```bash
sudo bash bootstrap.sh --phase validate
```

验证内容：

- `sshd -t`
- `sshd -T`
- fail2ban 状态
- UFW 状态
- `sing-box check -c /etc/sing-box/config.json`
- sing-box systemd 状态
- 监听端口
- 每个家宽代理本身是否可用：`curl -x socks5h/http https://ifconfig.me`

Windows PowerShell：

```powershell
Test-NetConnection 服务器IP -Port 443
Test-NetConnection 服务器IP -Port 51043
ssh -p 22 -o PreferredAuthentications=publickey -o PasswordAuthentication=no root@服务器IP
ssh -p 22 -o PubkeyAuthentication=no -o PreferredAuthentications=password root@服务器IP
```

## 回滚

```bash
sudo CONFIRM_ROLLBACK=yes bash bootstrap.sh --phase rollback
```

可选：

```bash
sudo CONFIRM_ROLLBACK=yes RESTORE_FULL_SSH_BACKUP=yes bash bootstrap.sh --phase rollback
sudo CONFIRM_ROLLBACK=yes ROLLBACK_DISABLE_UFW=yes bash bootstrap.sh --phase rollback
sudo CONFIRM_ROLLBACK=yes RESTORE_UFW_FROM_BACKUP=yes bash bootstrap.sh --phase rollback
sudo CONFIRM_ROLLBACK=yes ROLLBACK_BACKUP_DIR=/root/reality-relay-bootstrap-backups/backup-xxxx bash bootstrap.sh --phase rollback
sudo CONFIRM_ROLLBACK=yes RESTORE_233BOY_CONF=yes bash bootstrap.sh --phase rollback
```

## 安全提醒

不要公开：

- `/etc/reality-relay-bootstrap/vless-uuid.txt`
- `/etc/reality-relay-bootstrap/reality-private.key`
- `home-proxies.csv`
- `upstream-nodes.txt`
- `/etc/reality-relay-bootstrap/home-proxies.csv`
- `/etc/reality-relay-bootstrap/upstream-nodes.txt`
- `/root/reality-relay-bootstrap-nodes.txt`
- `/root/reality-relay-bootstrap-clash.yaml`
