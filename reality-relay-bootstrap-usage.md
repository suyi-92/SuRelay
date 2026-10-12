# reality-relay-bootstrap 完整使用手册

> 适用项目：`reality-relay-bootstrap` 
> 目标系统：Ubuntu 22.04 / Ubuntu 24.04 / Debian 10-13
> 核心目标：新 VPS 可复现部署、SSH 不锁机、fail2ban、UFW、sing-box VLESS+Reality 多入口多出口、客户端节点生成、验证与回滚。

---

## 1. 这套脚本会做什么

这套脚本把一台全新 VPS 的初始化拆成多个阶段执行：

```text
preflight      -> 检查系统、配置文件、SSH 基础条件
ssh-phase1     -> 给 root 安装 SSH 公钥，只开启公钥登录，不禁 root/密码
ssh-final      -> 二阶段 SSH 最终加固，root 仅允许公钥登录，禁密码
fail2ban       -> 安装并配置 fail2ban
singbox        -> 安装 sing-box，生成 VLESS+Reality 多入口/多出口配置
firewall       -> 安装/配置 UFW，只开放实际使用端口
validate       -> 验证 SSH、fail2ban、UFW、sing-box、监听端口、家宽代理和上游节点
output-nodes   -> 可选生成客户端节点文件
subscription   -> 单独安装/更新简单订阅端口
cloudflare-dns -> 可选在 Cloudflare 准备 DNS only / 灰云 A/AAAA 记录
custom-domain  -> 可选配置 Cloudflare DNS-01 证书和本机 Nginx fallback
rollback       -> 回滚 SSH 加固、sing-box 配置，可选处理 UFW
```

它不是“一把梭”脚本。SSH 相关危险步骤被拆成两阶段，目的是防止你把自己锁在服务器外面。

---

## 2. 最重要的安全原则

执行前先记住这几条：

1. **不要关闭当前已登录 SSH 窗口。**
2. **`ssh-phase1` 不会禁 root，也不会禁密码。**
3. **只有另开窗口确认 `root` 可以用 SSH key 登录后，才执行 `ssh-final`。**
4. **UFW 启用前必须先放行当前 SSH 端口。脚本会自动放行实际使用的中转端口，但服务商安全组仍要你手动确认。**
5. **不要公开以下文件：**

```text
config.env
home-proxies.csv
upstream-nodes.txt
/etc/reality-relay-bootstrap/vless-uuid.txt
/etc/reality-relay-bootstrap/reality-private.key
/etc/reality-relay-bootstrap/reality-public.key
/etc/reality-relay-bootstrap/reality-short-id.txt
/etc/reality-relay-bootstrap/home-proxies.csv
/etc/reality-relay-bootstrap/upstream-nodes.txt
/root/reality-relay-bootstrap-nodes.txt
/root/reality-relay-bootstrap-clash.yaml
```

完整路径总表见 [`docs/path-map.md`](docs/path-map.md)，里面按路径列出了用途、权限、敏感性和清理建议。

---


## 3.0 一键安装（推荐给首次使用）

如果你希望尽量少编辑文件，可以直接使用 `install.sh`。首次只收集基础信息、Cloudflare 配置和管理员公钥，然后生成 `config.env`、`home-proxies.csv` 和 `upstream-nodes.txt`：

```bash
bash <(wget -qO- https://raw.githubusercontent.com/suyi-92/reality-relay-bootstrap/main/install.sh)
```

已经 `git clone` 到服务器时，也可以进入项目目录后运行：

```bash
sudo bash install.sh
```

### 3.0.1 已部署机器单独补 IPv6 本地节点

如果常规部署已经完成，只需要在不改现有 v4 身份和出口策略的前提下补一套 IPv6 字面量节点，使用短选项 `-6`：

```bash
# 自动探测公网 IPv6，并允许交互确认
sudo bash install.sh -6

# 非交互地直接指定地址；方括号可省略
sudo bash install.sh -6 2001:db8::10
sudo bash install.sh --add-ipv6-node '[2001:db8::10]'
```

远程一键入口的参数写在进程替换之后：

```bash
bash <(wget -qO- https://raw.githubusercontent.com/suyi-92/reality-relay-bootstrap/main/install.sh) -6 2001:db8::10
```

维护模式的前置条件与边界：

1. 安装目录里必须已有 `config.env`，系统必须已有可运行的 sing-box。
2. `VLESS_UUID_PATH`、Reality private/public key 和 short-id 四份材料必须存在；缺任一项都会在修改前停止。
3. 修改前会把 `config.env`、当前 sing-box 配置和必要的 UFW 配置放进 `/root/reality-relay-bootstrap-backups/backup-*-ipv6-node/`。
4. 只更新 `SERVER_IP_IPV6`、`ENABLE_IPV6_LISTEN=true`、`CLIENT_UDP=true`、`RESET_PROXY_KEYS=false`；不会修改 `SERVER_IP_IPV4` 或 `PROXY_IP_VERSION`，因此 v6 只是新增客户端入口地址，原有出口/DNS 策略保持不变。
5. `net.ipv6.bindv6only=1` 且已有 IPv4 节点时会在修改前停止，避免 `::` 监听导致 v4 节点掉线。重启后还会按每个实际入口端口分别验证 IPv4 和 IPv6 listener。
6. UFW 启用时会确保 `/etc/default/ufw` 使用 `IPV6=yes`，规则仍只开放实际 VLESS TCP 端口；云厂商 IPv6 安全组需要手动同步。
7. 即使启用了 `CUSTOM_DOMAIN`，这三份专用文件也会强制使用 IPv6 字面量作为 `server`，但继续复用现有 Reality SNI 与其它参数。
8. 验证监听后刷新已启用的订阅，并在自有域名模式更新 CF A/AAAA；域名订阅也包含每个出口的 IPv6 字面量备用节点。发布失败会保留已验证的本地配置，可重跑 `subscription` 或 `cloudflare-dns` 阶段。

输出文件：

```text
/root/reality-relay-bootstrap-v6-vless.txt  # 每行一个可导入的 vless:// Reality 分享链接
/root/reality-relay-bootstrap-v6-nodes.txt  # VLESS 字段文本，含 udp=true
/root/reality-relay-bootstrap-v6-clash.yaml # Mihomo/Clash，含 ipv6: true 与 udp: true
```

VLESS Reality 使用 TCP 作为承载，但客户端的 UDP 数据会封装在该连接里，所以防火墙不需要额外开放同号 UDP 端口。

首次交互只包含基础信息（名称、IPv4/IPv6、SSH 端口）、可选 CF 配置（域名、Zone、邮箱、Token）和多行管理员公钥。端口、订阅、密钥重置及出口列表均改为在 `config.env` 或对应列表文件中手动维护，具体操作见 [README](README.md)。

默认使用 TCP 443、Mihomo 订阅和 `RESET_PROXY_KEYS=false`。已有 `config.env` 时整份沿用，同时保留已有家宽/上游出口列表，不重复录入或覆盖。CF Token 显示星号，多行公钥空行结束；只有显式 `SKIP` 才跳过公钥录入。

安装过程保留 SSH 二阶段确认：另开终端执行输出的公钥验证命令，看到 `root` 后输入 `yes`，才执行 final；默认不继续。完成后只显示一个首选订阅链接和全部可复制的本地 `vless://` 链接，必要告警与错误仍显示。

只生成配置、不执行部署：

```bash
sudo env RRB_RUN_PHASES=false bash install.sh
```

---

## 3. 部署前准备

### 3.1 本地准备 SSH 公钥

在 Windows PowerShell 执行：

```powershell
$SshDir = Join-Path $env:USERPROFILE ".ssh"
New-Item -Path $SshDir -ItemType Directory -Force | Out-Null

$KeyPath = Join-Path $SshDir "id_ed25519"

if (-not (Test-Path $KeyPath)) {
  ssh-keygen -t ed25519 -f $KeyPath -N ""
}

Get-Content "$env:USERPROFILE\.ssh\id_ed25519.pub"
```

把最后输出的公钥复制下来，后面填入 `config.env` 的：

```bash
ADMIN_PUBKEY="ssh-ed25519 AAAA..."
```

注意：只复制 `.pub` 公钥，不要复制私钥 `id_ed25519`。

### 3.2 第一次登录服务器

假设服务商给你的初始用户是 `root`：

```powershell
ssh root@服务器IP
```

如果 SSH 端口不是 22：

```powershell
ssh -p 端口 root@服务器IP
```

进入服务器后，不要关闭这个窗口。

### 3.3 从 GitHub 拉取项目

 安装 `Git` ：

```bash
sudo apt update
sudo apt install git -y
```

拉取 `Reality Relay Bootstrap` 项目：

```bash
git clone https://github.com/suyi-92/reality-relay-bootstrap.git
```

---

## 4. 创建配置文件

在服务器上执行：

```bash
cd reality-relay-bootstrap

cp config.example.env config.env
cp home-proxies.example.csv home-proxies.csv
cp upstream-nodes.example.txt upstream-nodes.txt
```

编辑主配置：

```bash
nano config.env
```

查看第5节看完整说明，检查是否编辑正确：

~~~bash
cat config.env
~~~

编辑家宽代理 CSV：

```bash
nano home-proxies.csv
```

查看第6节看完整说明，检查是否编辑正确：

~~~bash
cat home-proxies.csv
~~~

可选：编辑上游节点文件：

```bash
nano upstream-nodes.txt
```

如果暂时没有上游节点，可以保持模板里的注释不动。

---

## 5. `config.env` 完整说明

复制模板文件并打开编辑：

~~~bash
cp config.example.env config.env
nano config.env
~~~

下面是典型配置示例：

```bash
SERVER_ALIAS="my-vps"
SERVER_IP="1.2.3.4"
SERVER_IP_IPV4="1.2.3.4"
SERVER_IP_IPV6=""
SSH_PORT="22"
INITIAL_USER="root"
ADMIN_USER="root"
ADMIN_PUBKEY="ssh-ed25519 AAAA...你的本地公钥"
# 多个 root 公钥可以写在同一个变量里：
# ADMIN_PUBKEY=$'ssh-ed25519 AAAA... user1\nssh-ed25519 BBBB... user2'
# 或把额外公钥逐行写入 ADMIN_PUBKEYS。
ADMIN_PUBKEYS=""
ENABLE_SFTP_USER="false"
SFTP_USER="sftpuser"
USE_233BOY_INSTALLER="false"
INSTALL_SINGBOX_METHOD="apt"
MODE_443="direct"
DIRECT_PORT="443"
HOME_PORT_START="51043"
HOME_PORT_END="65535"
ENABLE_UFW="true"
ENABLE_FAIL2BAN="true"
ENABLE_IPV6_LISTEN="false"

SFTP_PUBKEY=""
CSV_PATH="./home-proxies.csv"
UPSTREAM_NODES_PATH="./upstream-nodes.txt"
RRB_STATE_DIR="/etc/reality-relay-bootstrap"
ENABLE_CUSTOM_DOMAIN="false"
CUSTOM_DOMAIN=""
CUSTOM_DOMAIN_PROVIDER="cloudflare"
LE_EMAIL=""
LE_STAGING="false"
CLOUDFLARE_API_TOKEN=""
CLOUDFLARE_API_TOKEN_FILE="/etc/reality-relay-bootstrap/cloudflare.ini"
CLOUDFLARE_ZONE_NAME=""
CLOUDFLARE_ACCOUNT_ID=""
CLOUDFLARE_CREATE_ZONE="false"
CLOUDFLARE_ENSURE_DNS_RECORDS="true"
CLOUDFLARE_DNS_OVERWRITE_EXISTING="true"
CLOUDFLARE_REQUIRE_ACTIVE_ZONE="true"
CLOUDFLARE_DNS_TTL="1"
CLOUDFLARE_DNS_PROPAGATION_SECONDS="60"
NGINX_FALLBACK_HOST="127.0.0.1"
NGINX_FALLBACK_PORT="8443"
SINGBOX_CONFIG_PATH="/etc/sing-box/config.json"

PROXY_PROTOCOL="vless-reality"
PROXY_IP_VERSION="ipv4"
RESET_PROXY_KEYS="false"
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
CLASH_MIXED_PORT="7890"
```

### 5.1 服务器与 SSH 变量

| 变量 | 说明 | 示例 |
|---|---|---|
| `SERVER_ALIAS` | 服务器别名；会作为 443 节点名称 | `my-vps` |
| `SERVER_IP` | 兼容字段；默认写入 IPv4，IPv4 为空时写入 IPv6 | `1.2.3.4` |
| `SERVER_IP_IPV4` | 服务器公网 IPv4 | `1.2.3.4` |
| `SERVER_IP_IPV6` | 服务器公网 IPv6，可为空 | `2001:db8::1` |
| `SSH_PORT` | 当前 SSH 端口 | `22` |
| `INITIAL_USER` | 服务商初始用户；当前仅作为预留/记录字段，实际 SSH 登录仍由你手动完成 | `root` |
| `ADMIN_USER` | 兼容字段；当前固定使用 root，旧配置写其它值会被按 root 处理 | `root` |
| `ADMIN_PUBKEY` | 本地 SSH 公钥；支持逐行多个 | `ssh-ed25519 AAAA...` |

一键安装时，管理员 SSH 公钥支持一次性粘贴多行，每行一把，完成后输入空行结束。尚未录入公钥时直接回车会警告并继续等待，格式不正确的行需要重新输入；只有单独输入大写 `SKIP` 才能跳过本次录入。分步骤编辑 `config.env` 时，`ADMIN_PUBKEY` 留空仍会复用 `/root/.ssh/authorized_keys`；两种跳过方式都依赖 root 当前已有正确公钥，否则后续 SSH 部署检查会停止。

需要给多个管理员设备授权时，有两种写法：

```bash
ADMIN_PUBKEY=$'ssh-ed25519 AAAA... user1\nssh-ed25519 BBBB... user2'
```

或者保留第一条在 `ADMIN_PUBKEY`，把额外公钥逐行放进 `ADMIN_PUBKEYS`：

```bash
ADMIN_PUBKEY="ssh-ed25519 AAAA... user1"
ADMIN_PUBKEYS=$'ssh-ed25519 BBBB... user2\nssh-ed25519 CCCC... user3'
```

`INITIAL_USER` 当前不会被脚本用来发起 SSH 登录或切换用户；它只是记录你服务商初始登录用户，真正的第一次登录仍需要你手动完成。

### 5.2 SFTP-only 用户变量

默认不开启：

```bash
ENABLE_SFTP_USER="false"
```

需要创建 SFTP-only 用户时改成：

```bash
ENABLE_SFTP_USER="true"
SFTP_USER="sftpuser"
SFTP_PUBKEY="ssh-ed25519 AAAA...SFTP用户公钥"
```

脚本会创建 chroot 目录：

```text
/sftp/sftpuser
/sftp/sftpuser/upload
```

注意：SFTP-only 用户不会给 sudo 权限，只能通过 SFTP 访问被锁定目录。

### 5.3 sing-box 安装方式

默认使用官方 APT 源：

```bash
USE_233BOY_INSTALLER="false"
INSTALL_SINGBOX_METHOD="apt"
```

这是推荐方式。

如果你确实想先调用 233boy 安装 core/管理脚本，可以改成：

```bash
USE_233BOY_INSTALLER="true"
INSTALL_SINGBOX_METHOD="233boy"
```

执行时还必须显式确认：

```bash
sudo CONFIRM_USE_233BOY_INSTALLER=yes bash bootstrap.sh --phase singbox
```

启用 233boy 后，本项目会隔离 `/etc/sing-box/conf`，避免 233boy 默认配置和本项目生成的多入口 VLESS+Reality 配置冲突。最终仍由本项目接管：

```text
/etc/sing-box/config.json
```

### 5.4 443 出口

```bash
MODE_443="direct"
DIRECT_PORT="443"
```

含义：

```text
客户端 -> VPS:443 -> VPS 自身公网出口 -> 互联网
```

生成的 443 节点名就是 `SERVER_ALIAS`。例如：

```text
my-vps
```

脚本不再生成 AI 域名、CN/private 或其它目标域名/IP 分流规则。443 固定走 VPS 自身 direct 出口；`home-proxies.csv` 和 `upstream-nodes.txt` 生成的每个专用端口固定走对应出口。

### 5.5 端口范围变量

```bash
DIRECT_PORT="443"
HOME_PORT_START="51043"
HOME_PORT_END="65535"
```

规则：

- `DIRECT_PORT` 默认是 443。
- 家宽入口端口必须在 `HOME_PORT_START` 到 `HOME_PORT_END` 之间。
- CSV 里的 `listen_port` 不能重复。
- CSV 不能使用 `DIRECT_PORT`，也就是默认不能写 443。

### 5.6 VLESS+Reality 变量

`RRB_STATE_DIR` 是默认状态目录，用来保存复制后的 `home-proxies.csv`、VLESS UUID、Reality keypair 和 short-id。除非你明确要迁移目录，否则建议保持默认：

```bash
RRB_STATE_DIR="/etc/reality-relay-bootstrap"
```

```bash
PROXY_PROTOCOL="vless-reality"
PROXY_IP_VERSION="ipv4"
RESET_PROXY_KEYS="false"
VLESS_UUID_PATH="/etc/reality-relay-bootstrap/vless-uuid.txt"
VLESS_FLOW="xtls-rprx-vision"
REALITY_PRIVATE_KEY_PATH="/etc/reality-relay-bootstrap/reality-private.key"
REALITY_PUBLIC_KEY_PATH="/etc/reality-relay-bootstrap/reality-public.key"
REALITY_SHORT_ID_PATH="/etc/reality-relay-bootstrap/reality-short-id.txt"
REALITY_SERVER_NAME="www.microsoft.com"
REALITY_HANDSHAKE_SERVER="www.microsoft.com"
REALITY_HANDSHAKE_PORT="443"
REALITY_MAX_TIME_DIFFERENCE="1m"
```

`PROXY_IP_VERSION` 控制代理解析/出站 IP 版本：

| 值 | 说明 |
|---|---|
| `ipv4` | 只走 IPv4，默认值 |
| `ipv6` | 只走 IPv6 |
| `dual` | IPv4/IPv6 双栈，优先 IPv4 |

`RESET_PROXY_KEYS=false` 时重复运行 `singbox` 或一键安装不会轮换 VLESS UUID、Reality keypair 和 short-id；一键安装重跑时会优先继承旧 `config.env` 的值，找不到旧值时默认 `false`。修改家宽/上游落地节点时通常保持 `false`；改成 `true` 会生成新密钥，旧节点和旧订阅 token 会失效。

首次执行 `singbox` 阶段时会自动生成：

```text
/etc/reality-relay-bootstrap/vless-uuid.txt
/etc/reality-relay-bootstrap/reality-private.key
/etc/reality-relay-bootstrap/reality-public.key
/etc/reality-relay-bootstrap/reality-short-id.txt
```

你一般不需要手动改这些路径。

因此 `/etc/reality-relay-bootstrap/home-proxies.csv` 也按敏感文件处理。

`REALITY_SERVER_NAME` 和 `REALITY_HANDSHAKE_SERVER` 默认是：

```text
www.microsoft.com
```

如果要换成其他网站，建议选一个稳定、真实、支持 TLS 443 的大站域名，并确保客户端节点里的 `servername` 和服务端一致。

如果启用自有域名：

```bash
ENABLE_CUSTOM_DOMAIN="true"
CUSTOM_DOMAIN="edge.example.com"
CLOUDFLARE_ZONE_NAME="example.com"
LE_EMAIL="admin@example.com"
CLOUDFLARE_API_TOKEN="Cloudflare DNS API Token"
NGINX_FALLBACK_PORT="8443"
```

Cloudflare 侧需要先在控制台添加/托管 zone，并到域名注册商把 NS 改成 Cloudflare 分配的 nameserver。脚本默认会用 API token 创建或更新 `CUSTOM_DOMAIN` 的 A/AAAA 记录，`proxied=false`，也就是灰云 / DNS only。公网 `443` 继续由 sing-box 监听，普通 TLS fallback 到本机 Nginx `127.0.0.1:8443`；Nginx 默认站点根目录是 `/var/www/reality-fallback`，会写入 `Digital Notes` 静态首页、`robots.txt` 和空 `favicon.ico`。`REALITY_SERVER_NAME` 会自动回填为 `CUSTOM_DOMAIN`，`REALITY_HANDSHAKE_SERVER` 会回填为 `127.0.0.1`，`REALITY_HANDSHAKE_PORT` 会回填为 `NGINX_FALLBACK_PORT`。订阅服务启用时只绑定本机，并通过 `https://CUSTOM_DOMAIN/sub/<VLESS_UUID>?target=ClashMeta` 对外访问。

### 5.7 客户端输出变量

```bash
CLIENT_FINGERPRINT="chrome"
CLIENT_UDP="true"
CLIENT_ALPN="h2,http/1.1"
CLASH_MIXED_PORT="7890"
```

默认开启 UDP。HTTP 家宽代理通常不支持 UDP，SOCKS5 也不一定支持 UDP；如果对应出口不支持 UDP，可显式设置 `CLIENT_UDP=false`。

### 5.8 保存关闭并检查

```bash
ctrl + o
enter
ctrl + x
```

检查是否编辑正确：

~~~bash
cat config.env
~~~

---

## 6. `home-proxies.csv` 完整说明

复制模板文件并打开编辑：

~~~bash
cp home-proxies.example.csv home-proxies.csv
nano home-proxies.csv
~~~

分步骤模式直接编辑这个文件；一键安装模式可以选择逐条填写，或把同样格式的多行 CSV 一次性粘贴进去。

CSV 示例：

```csv
tag,type,server,server_port,username,password,network,listen_port
home-01,socks5,proxy1.example.com,1080,user1,"pass,with,comma",tcp,
home-02,http,proxy2.example.com,8080,user2,password2,tcp,51044
```

字段说明：

| 字段 | 说明 | 示例 |
|---|---|---|
| `tag` | 家宽出口名称，只能用英文、数字、下划线、短横线 | `home-01` |
| `listen_port` | VPS 上监听给客户端连接的端口，可留空自动分配 | `51043` |
| `type` | 家宽代理类型 | `socks5` 或 `http` |
| `server` | 家宽代理服务器地址 | `proxy1.example.com` |
| `server_port` | 家宽代理端口 | `1080` |
| `username` | 家宽代理用户名 | `user1` |
| `password` | 家宽代理密码 | `pass,with,comma` |
| `network` | 建议先用 TCP | `tcp` |

规则：

1. `443` 不要写入 CSV。
2. `listen_port` 可留空自动分配；填写时不能重复。
3. `listen_port` 必须在 `HOME_PORT_START` 和 `HOME_PORT_END` 范围内，默认从 `51043` 起分配。
4. `type` 只支持 `socks5` 和 `http`。
5. 密码里如果有英文逗号，必须用英文双引号包起来：

```csv
home-01,socks5,proxy.example.com,1080,user,"abc,123",tcp,
```

6. CSV 里有家宽代理账号密码，不要公开、不要提交到 GitHub。

### 6.1 保存关闭并检查

```bash
ctrl + o
enter
ctrl + x
```

检查是否编辑正确：

~~~bash
cat home-proxies.csv
~~~

### 6.2 `upstream-nodes.txt`

这是可选文件，用来把别人给你的节点分享链接接到本机新的 VLESS+Reality 入口端口。当前支持：

```text
vless://...security=none...
vless://...security=reality...
```

复制模板文件并打开编辑：

~~~bash
cp upstream-nodes.example.txt upstream-nodes.txt
nano upstream-nodes.txt
~~~

推荐写成 CSV，`listen_port` 可留空自动分配：

```csv
tag,node_url,listen_port
vless-relay,vless://uuid@example.com:443?encryption=none&security=reality&flow=xtls-rprx-vision&type=tcp&sni=www.cloudflare.com&pbk=PUBLIC_KEY&fp=chrome&sid=abcd&spx=%2F#vless,
vless-plain,vless://uuid@example.com:12345?type=tcp&encryption=none&security=none#vless-plain,51049
```

也可以不写表头，只逐行放 `vless://` 节点链接；脚本会自动分配 tag 和 `HOME_PORT_START` 到 `HOME_PORT_END` 范围内的空闲端口。

规则：

1. `listen_port` 可留空自动分配；填写时不能与 `DIRECT_PORT`、`home-proxies.csv` 或其他上游节点重复。
2. `listen_port` 必须在 `HOME_PORT_START` 和 `HOME_PORT_END` 范围内。
3. 上游节点链接里包含密码、UUID、Reality public key 等连接信息，不要公开、不要提交到 GitHub。
4. 客户端连接本机 VLESS+Reality 入口，本机再通过 TCP transport 使用普通 VLESS 或 VLESS Reality 连接上游；原有 Reality、Vision 及链接参数保持有效。

`type=tcp` 指上游 VLESS transport，可代理的目标流量同时包括 TCP 和 UDP。生成的 VLESS outbound 省略 `network`，使用 sing-box 默认的 TCP+UDP 支持和 XUDP 编码；`type=ws`、`type=udp` 等不支持的 transport 仍会报错。参见 [sing-box 官方 VLESS outbound 文档](https://sing-box.sagernet.org/configuration/outbound/vless/)。

实时通信、VoIP、Wi-Fi Calling 所需的 UDP（包括 UDP 500/4500）沿原有 inbound → 指定 outbound 映射转发，无需增加专用域名、IP 或端口规则。客户端与上游落地机也需要支持 UDP relay/XUDP。UDP 载荷封装在 VLESS TCP 连接内，UFW 仍只开放实际 VLESS TCP 入口端口。

升级时保留现有 `upstream-nodes.txt` 格式和字段，保持 `RESET_PROXY_KEYS=false`，然后执行 `sudo bash bootstrap.sh --phase singbox` 重新生成配置、校验并重启服务。`home-proxies.csv` 的 SOCKS/HTTP `network` 语义不变。

---

## 7. 先执行 dry-run

正式执行前建议先 dry-run：

```bash
sudo bash bootstrap.sh --phase preflight --dry-run
sudo bash bootstrap.sh --phase ssh-phase1 --dry-run
sudo bash bootstrap.sh --phase singbox --dry-run
sudo bash bootstrap.sh --phase firewall --dry-run
```

`--dry-run` 会尽量打印将要执行的动作，不做实际修改。

也可以显式指定配置文件路径：

```bash
sudo bash bootstrap.sh --phase preflight --config /root/reality-relay-bootstrap/config.env
```

`--yes` 当前为预留参数；危险步骤仍必须使用 `CONFIRM_*` 环境变量显式确认：

```bash
CONFIRM_ROOT_KEY_LOGIN=yes
CONFIRM_USE_233BOY_INSTALLER=yes
CONFIRM_ROLLBACK=yes
```

---

## 8. 标准首次部署流程

### 8.1 preflight

```bash
sudo bash bootstrap.sh --phase preflight
```

它会检查：

- 当前系统是否是 Ubuntu 22.04 / Ubuntu 24.04 / Debian 10-13。
- `config.env` 是否存在。
- 关键变量是否合法。
- SSH 配置和基础工具是否可用。

如果系统不是默认支持范围，脚本会停止，避免在未知环境下误操作。

### 8.2 SSH phase1

```bash
sudo bash bootstrap.sh --phase ssh-phase1
```

这个阶段会做：

- 安装基础包。
- 给 root 安装 SSH 公钥。
- 如果启用 SFTP-only，创建 SFTP 用户和 chroot 目录。
- 写入 SSH phase1 配置，只确保公钥登录可用。

这个阶段不会做：

- 不会禁用 root SSH。
- 不会禁用 SSH 密码登录。
- 不会只允许某个新建管理员用户。

### 8.3 另开窗口测试 root key 登录

不要关闭当前 root 窗口。

在本地 Windows PowerShell 另开一个新窗口：

```powershell
ssh -p 22 -o PreferredAuthentications=publickey -o PasswordAuthentication=no root@服务器IP
```

如果端口不是 22，替换 `-p 22`。

登录后执行：

```bash
whoami
```

期望输出：

```text
root
```

只有 root key 登录成功，才能继续最终加固。

### 8.4 SSH final

```bash
sudo CONFIRM_ROOT_KEY_LOGIN=yes bash bootstrap.sh --phase ssh-final
```

这个阶段会做：

- 备份 SSH 配置。
- root SSH 只允许公钥登录。
- 禁止 SSH 密码登录。
- 禁止 keyboard-interactive/challenge-response 登录。
- 设置 `AuthenticationMethods publickey`。
- 设置 `AllowUsers root`，如果启用 SFTP 用户则包含 SFTP 用户。
- 测试 `sshd -t`。
- 检查 `sshd -T` 生效值。
- 通过后 reload/restart SSH。

两个 SSH 阶段都把当前阶段的项目策略写在 `/etc/ssh/sshd_config` 开头，以避免 `00-mofang.conf` 等服务商文件先设置同名选项。更新前会备份主配置及 drop-in 目录，旧版的两个项目 drop-in 会迁移清理。重跑 phase1 会替换 final 的项目策略，回滚会移除项目标记块；服务商文件不需要手动改写。若 `Match` 条件仍造成冲突，最终加固会显示实际值并停止 reload。

执行后再次另开窗口测试：

```powershell
ssh -p 22 -o PreferredAuthentications=publickey -o PasswordAuthentication=no root@服务器IP
ssh -p 22 -o PubkeyAuthentication=no -o PreferredAuthentications=password root@服务器IP
```

预期：

- root key 登录成功。
- root 密码登录失败。

### 8.5 安装 fail2ban

```bash
sudo bash bootstrap.sh --phase fail2ban
```

脚本会安装 fail2ban，并为 SSH 写入 jail 配置。

检查：

```bash
sudo fail2ban-client status
sudo fail2ban-client status sshd
```

### 8.6 安装 sing-box 并生成 VLESS+Reality 配置

```bash
sudo bash bootstrap.sh --phase singbox
```

这个阶段会做：

- 安装 sing-box。
- 自动生成 VLESS UUID。
- 自动生成 Reality private/public keypair。
- 自动生成 Reality short-id。
- 备份旧 `/etc/sing-box/config.json`。
- 从 `home-proxies.csv` 读取家宽出口。
- 生成 `/etc/sing-box/config.json`。
- 执行 `sing-box check -c /etc/sing-box/config.json`。
- check 通过后重启 sing-box。
- 如果 `ENABLE_SUBSCRIPTION_SERVER=true`，生成并启动订阅服务。
- 如果 `ENABLE_CUSTOM_DOMAIN=true`，先准备 Cloudflare 灰云 A/AAAA，再使用 Cloudflare DNS-01 申请 Let's Encrypt 证书，并配置本机 Nginx fallback。

生成后的重要文件：

```text
/etc/sing-box/config.json
/etc/reality-relay-bootstrap/vless-uuid.txt
/etc/reality-relay-bootstrap/reality-private.key
/etc/reality-relay-bootstrap/reality-public.key
/etc/reality-relay-bootstrap/reality-short-id.txt
/etc/reality-relay-bootstrap/home-proxies.csv
/etc/reality-relay-bootstrap/subscription/
```

### 8.7 配置 UFW 防火墙

```bash
sudo bash bootstrap.sh --phase firewall
```

脚本只会放行实际使用端口：

```text
SSH_PORT
DIRECT_PORT
home-proxies.csv 里的每个 listen_port
upstream-nodes.txt 里的每个 listen_port
SUBSCRIPTION_PORT（仅 ENABLE_SUBSCRIPTION_SERVER=true 且公网直连订阅端口已启用）
```

不会默认开放从 `51043` 起的整段端口范围。
启用自有域名且 `SUBSCRIPTION_PORT` 留空时，订阅服务只监听本机 `SUBSCRIPTION_INTERNAL_PORT` 并由 Nginx fallback 反代，UFW 不开放订阅端口。

执行后查看：

```bash
sudo ufw status numbered
sudo ufw status verbose
```

### 8.8 服务商安全组手动放行

服务商安全组也必须放行同样端口。

至少放行：

```text
TCP SSH_PORT，例如 22
TCP DIRECT_PORT，例如 443
TCP 每个 CSV listen_port，例如 51043、51044
TCP 每个 upstream-nodes listen_port
```

不要只改服务器 UFW，不改服务商安全组。很多云厂商还有外层防火墙。

### 8.9 validate

```bash
sudo bash bootstrap.sh --phase validate
```

会检查：

- `sshd -t`
- `sshd -T`
- fail2ban 配置和运行状态
- UFW 状态
- sing-box 配置语法
- sing-box systemd 状态
- sing-box 是否监听所有端口
- 每个家宽代理和上游节点本身是否可用

### 8.10 输出节点

默认部署只启用订阅服务，不主动生成 `/root` 下的节点文件。需要在服务器本地额外生成时执行：

```bash
sudo bash bootstrap.sh --phase output-nodes
```

查看：

```bash
sudo cat /root/reality-relay-bootstrap-nodes.txt
sudo cat /root/reality-relay-bootstrap-clash.yaml
sudo cat /root/reality-relay-bootstrap-vless.txt
```

默认订阅端口配置：

```bash
ENABLE_SUBSCRIPTION_SERVER="true"
SUBSCRIPTION_PORT=""
SUBSCRIPTION_INTERNAL_PORT="51040"
SUBSCRIPTION_TARGET="ClashMeta"
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

启用自有域名时：

```text
https://CUSTOM_DOMAIN/sub/<VLESS_UUID>?target=ClashMeta
```

`<VLESS_UUID>` 来自 `/etc/reality-relay-bootstrap/vless-uuid.txt`。内置订阅服务是 HTTP；未启用自有域名时如需 HTTPS，请在外层接入带证书的反向代理，并把 `SUBSCRIPTION_BASE_URL` 设置为反代外部地址。启用自有域名时，订阅服务默认只监听本机 `SUBSCRIPTION_INTERNAL_PORT`，外部 HTTPS 由 sing-box Reality fallback 到 Nginx 后反代。
启用自有域名时，`SUBSCRIPTION_PORT` 默认留空；显式填写它时会额外开放 `http://SERVER_IP:SUBSCRIPTION_PORT/sub/...` 直连订阅；简洁输出仍只显示首选域名链接。

`SUBSCRIPTION_TARGET` 当前只生成 ClashMeta/Mihomo YAML；旧配置里的其它 target 会警告并按 ClashMeta 处理，旧 URL target 不再兼容。

如果同时配置了 IPv4 和 IPv6，两个订阅链接都返回同一份完整节点；IPv4 节点名称保持不变，IPv6 节点名称追加 `-IPv6`。
如果 Clash Verge 或浏览器无法导入 IPv6 字面量订阅地址，有 IPv4 时直接用 IPv4 订阅地址即可，它会返回同一份完整节点。
启用自有域名时保留域名节点；已配置 IPv6 且开启监听时，每个出口还提供 `-IPv6` 字面量备用节点，保留相同 Reality SNI。生成配置会启用客户端 IPv6，与 VPS 的 `PROXY_IP_VERSION` 出站策略分别处理。CF 灰云不会绕过 IPv4 封锁，客户端仍需可达的 IPv6。

---

## 9. 一条完整执行命令清单

首次部署推荐按下面顺序：

```bash
cd /root/reality-relay-bootstrap

cp config.example.env config.env
nano config.env

cp home-proxies.example.csv home-proxies.csv
nano home-proxies.csv

sudo bash bootstrap.sh --phase preflight
sudo bash bootstrap.sh --phase ssh-phase1

# 另开窗口测试 root key 登录成功后：
sudo CONFIRM_ROOT_KEY_LOGIN=yes bash bootstrap.sh --phase ssh-final

sudo bash bootstrap.sh --phase fail2ban
sudo bash bootstrap.sh --phase singbox
sudo bash bootstrap.sh --phase cloudflare-dns   # 可选预处理；singbox/custom-domain phase 已会自动调用
sudo bash bootstrap.sh --phase custom-domain   # 可选；singbox phase 已会自动调用
sudo bash bootstrap.sh --phase firewall
sudo bash bootstrap.sh --phase validate
```

---

## 10. 生成的 sing-box 结构

### 10.1 固定端口映射

生成后的流量路径：

```text
客户端 -> VPS:443   -> VPS 自身公网出口
客户端 -> VPS:51043 -> 家宽代理 home-01
客户端 -> VPS:51044 -> 家宽代理 home-02
```

节点示例：

```text
my-vps
home-01
home-02
```

---

## 11. 客户端节点信息

`/root/reality-relay-bootstrap-clash.yaml` 会生成 Clash/Mihomo 可用配置。

节点字段示例：

```yaml
proxies:
  - name: "home-01"
    type: vless
    server: "1.2.3.4"
    port: 51043
    uuid: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
    flow: "xtls-rprx-vision"
    tls: true
    servername: "www.microsoft.com"
    client-fingerprint: "chrome"
    udp: true
    reality-opts:
      public-key: "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
      short-id: "xxxxxxxxxxxxxxxx"
    alpn:
      - "h2"
      - "http/1.1"
```

`/root/reality-relay-bootstrap-nodes.txt` 是纯文本字段，便于你复制到其他客户端或订阅管理工具。

注意：这两个文件包含 UUID 和 Reality public key/short-id。虽然 public key 不像 private key 那么敏感，但完整节点仍应当当作敏感信息处理。

---

## 12. Windows 本地测试命令

### 12.1 测端口是否能连通

```powershell
Test-NetConnection 服务器IP -Port 443
Test-NetConnection 服务器IP -Port 51043
Test-NetConnection 服务器IP -Port 51044
```

预期：

```text
TcpTestSucceeded : True
```

如果是 `False`，优先检查：

1. 服务商安全组是否放行。
2. UFW 是否放行。
3. sing-box 是否正在监听。
4. 服务器 IP 是否填错。

### 12.2 测 SSH key 登录

```powershell
ssh -p 22 -o PreferredAuthentications=publickey -o PasswordAuthentication=no root@服务器IP
```

### 12.3 测 root 密码登录应失败

```powershell
ssh -p 22 -o PubkeyAuthentication=no -o PreferredAuthentications=password root@服务器IP
```

---

## 13. 客户端使用 Clash/Mihomo

把服务器上的文件复制到本地：

```bash
sudo cat /root/reality-relay-bootstrap-clash.yaml
```

复制内容到本地文件，例如：

```text
home-vless-reality.yaml
```

在 Clash Verge / Clash Verge Rev / Mihomo Party 中导入这个 YAML。

导入后：

1. 选择配置文件。
2. 打开代理页面。
3. 在 `PROXY` 组里选择节点。
4. 开启系统代理。
5. 测试出口 IP。

Windows PowerShell：

```powershell
curl.exe -x http://127.0.0.1:7890 https://ifconfig.me
```

预期：

```text
my-vps  -> VPS 自身公网 IP
home-01 -> home-01 对应家宽出口 IP
home-02 -> home-02 对应家宽出口 IP
```

---

## 14. 新增家宽出口

编辑 CSV：

```bash
cd /root/reality-relay-bootstrap
nano home-proxies.csv
```

新增一行：

```csv
home-03,51045,socks5,proxy3.example.com,1080,user3,"pass,with,comma",tcp
```

重新生成配置、放行端口、验证、输出节点：

```bash
sudo bash bootstrap.sh --phase singbox
sudo bash bootstrap.sh --phase firewall
sudo bash bootstrap.sh --phase validate
sudo bash bootstrap.sh --phase output-nodes
```

然后服务商安全组手动放行：

```text
TCP 51045
```

最后把新的 `/root/reality-relay-bootstrap-clash.yaml` 导入客户端，或更新你的订阅管理工具。

---

## 15. 删除家宽出口

编辑 CSV，删除对应行：

```bash
cd /root/reality-relay-bootstrap
nano home-proxies.csv
```

重新生成：

```bash
sudo bash bootstrap.sh --phase singbox
sudo bash bootstrap.sh --phase validate
sudo bash bootstrap.sh --phase output-nodes
```

UFW 不会自动删除旧端口规则。如果你想清理旧端口：

```bash
sudo ufw status numbered
sudo ufw delete 编号
```

服务商安全组里也可以手动删除旧端口。

---

## 16. 更换 VLESS UUID 或 Reality key

一般不建议频繁更换。更换后所有客户端节点都要重新导入。
如果只是修改家宽/上游落地节点，不要打开这个开关，保持 `RESET_PROXY_KEYS=false` 即可。

一键安装或 `config.env` 推荐用统一开关：

```bash
RESET_PROXY_KEYS="true"
sudo bash bootstrap.sh --phase singbox
sudo bash bootstrap.sh --phase output-nodes
```

执行完成后建议改回：

```bash
RESET_PROXY_KEYS="false"
```

### 16.1 更换 UUID

```bash
sudo rm -f /etc/reality-relay-bootstrap/vless-uuid.txt
sudo bash bootstrap.sh --phase singbox
sudo bash bootstrap.sh --phase output-nodes
```

### 16.2 更换 Reality keypair 和 short-id

```bash
sudo rm -f /etc/reality-relay-bootstrap/reality-private.key
sudo rm -f /etc/reality-relay-bootstrap/reality-public.key
sudo rm -f /etc/reality-relay-bootstrap/reality-short-id.txt
sudo bash bootstrap.sh --phase singbox
sudo bash bootstrap.sh --phase output-nodes
```

更换后必须重新导入：

```text
/root/reality-relay-bootstrap-clash.yaml
```

---

## 17. 回滚

### 17.1 普通回滚

```bash
sudo CONFIRM_ROLLBACK=yes bash bootstrap.sh --phase rollback
```

默认会处理：

- 移除本项目管理的 SSH hardening / phase1 drop-in。
- 回滚 sing-box 配置到最近备份。
- 默认不恢复 UFW，也不恢复被隔离的 233boy conf；这两个动作需要显式环境变量确认。

### 17.2 恢复完整 SSH 主配置备份

```bash
sudo CONFIRM_ROLLBACK=yes RESTORE_FULL_SSH_BACKUP=yes bash bootstrap.sh --phase rollback
```

### 17.3 临时关闭 UFW

如果你怀疑 UFW 导致无法连接，可以在仍保持的 SSH 会话里执行：

```bash
sudo CONFIRM_ROLLBACK=yes ROLLBACK_DISABLE_UFW=yes bash bootstrap.sh --phase rollback
```

或者直接：

```bash
sudo ufw disable
```

### 17.4 指定备份目录回滚

默认 rollback 会使用 `/root/reality-relay-bootstrap-backups/` 下最新的备份目录。需要指定某个备份目录时：

```bash
sudo CONFIRM_ROLLBACK=yes ROLLBACK_BACKUP_DIR=/root/reality-relay-bootstrap-backups/backup-xxxx bash bootstrap.sh --phase rollback
```

### 17.5 从备份恢复 UFW 配置

默认不会整目录恢复 `/etc/ufw`。确认要恢复备份中的 UFW 配置时：

```bash
sudo CONFIRM_ROLLBACK=yes RESTORE_UFW_FROM_BACKUP=yes bash bootstrap.sh --phase rollback
```

### 17.6 恢复 233boy conf

```bash
sudo CONFIRM_ROLLBACK=yes RESTORE_233BOY_CONF=yes bash bootstrap.sh --phase rollback
```

---

## 18. 日志和备份位置

日志文件：

```text
/var/log/reality-relay-bootstrap.log
```

备份目录：

```text
/root/reality-relay-bootstrap-backups/
```

常见备份内容：

```text
/etc/ssh/sshd_config
/etc/ssh/sshd_config.d/
/etc/fail2ban/
/etc/ufw/
/etc/sing-box/config.json
```

查看最近备份：

```bash
sudo ls -lah /root/reality-relay-bootstrap-backups/
```

---

## 19. 常见故障排查

### 19.1 `sshd: command not found`

普通用户 PATH 里可能没有 `/usr/sbin`。用完整路径：

```bash
sudo /usr/sbin/sshd -t
sudo /usr/sbin/sshd -T -C user=root,host=localhost,addr=127.0.0.1
```

### 19.2 root key 登录失败

不要执行 `ssh-final`。先检查：

```bash
ls -ld /root /root/.ssh
ls -l /root/.ssh/authorized_keys
sudo cat /root/.ssh/authorized_keys
```

权限应类似：

```text
/root                       root:root  700 或 750
/root/.ssh                  root:root  700
/root/.ssh/authorized_keys  root:root  600
```

### 19.3 `ssh-final` 后新窗口登录失败

不要关闭旧窗口。先临时移除加固 drop-in：

```bash
sudo rm -f /etc/ssh/sshd_config.d/00-reality-relay-bootstrap-hardening.conf
sudo rm -f /etc/ssh/sshd_config.d/00-reality-relay-bootstrap-phase1.conf
sudo /usr/sbin/sshd -t
sudo systemctl reload ssh 2>/dev/null || sudo systemctl reload sshd
```

再检查日志：

```bash
sudo journalctl -u ssh -n 100 --no-pager
sudo journalctl -u sshd -n 100 --no-pager
```

### 19.4 443 已被占用

检查：

```bash
sudo ss -lntp | grep ':443'
```

如果被 Nginx、Apache、Caddy 或其他服务占用，要么停止占用服务，要么把 `DIRECT_PORT` 改成其他端口，并同步改客户端和服务商安全组。

### 19.5 `sing-box check` 失败

执行：

```bash
sudo sing-box check -c /etc/sing-box/config.json
sudo journalctl -u sing-box -n 100 --no-pager
```

常见原因：

- CSV 格式错误。
- `listen_port` 重复。
- CSV 写了 443。
- `server_port` 不是数字。
- `type` 不是 `socks5` 或 `http`。
- Reality key 文件不存在或为空。
- sing-box 版本太旧，不支持当前配置字段。

### 19.6 端口不通

服务器上检查监听：

```bash
sudo ss -lntp | grep sing-box
```

检查 UFW：

```bash
sudo ufw status numbered
```

本地检查：

```powershell
Test-NetConnection 服务器IP -Port 443
Test-NetConnection 服务器IP -Port 51043
```

如果服务器监听正常、UFW 正常，但本地不通，多半是服务商安全组没放行。

### 19.7 某个家宽出口不可用

服务端单独测试代理：

SOCKS5：

```bash
curl --proxy socks5h://家宽地址:端口 --proxy-user '用户名:密码' https://ifconfig.me
```

HTTP：

```bash
curl --proxy http://家宽地址:端口 --proxy-user '用户名:密码' https://ifconfig.me
```

如果这里失败，问题在家宽代理本身，不在 VLESS+Reality 入站。

### 19.8 客户端能连但出口 IP 不对

检查你选择的节点端口是否正确：

```text
my-vps  -> VPS 自身出口
home-01 -> home-01 家宽出口
home-02 -> home-02 家宽出口
```

检查生成配置：

```bash
sudo grep -n 'vless-' /etc/sing-box/config.json
sudo grep -n 'out-home' /etc/sing-box/config.json
```

如果某个专用端口出口不对，优先确认该端口对应的 `home-proxies.csv` 或 `upstream-nodes.txt` 行，然后重新生成并验证：

```bash
sudo bash bootstrap.sh --phase singbox
sudo bash bootstrap.sh --phase validate
```

---

## 20. 最终检查清单

部署完成后逐项确认：

```text
[ ] config.env 已填写 SERVER_IP、至少一个 ADMIN_PUBKEY
[ ] home-proxies.csv 表头正确
[ ] CSV 里没有 443
[ ] CSV listen_port 没有重复
[ ] 密码里有英文逗号时已使用英文双引号
[ ] preflight 通过
[ ] ssh-phase1 通过
[ ] 另开窗口 root key 登录成功
[ ] ssh-final 已执行
[ ] root 密码 SSH 登录失败
[ ] fail2ban active
[ ] sing-box check 通过
[ ] sing-box active
[ ] UFW 放行 SSH_PORT、DIRECT_PORT、CSV 端口
[ ] 服务商安全组放行 SSH_PORT、DIRECT_PORT、CSV 端口
[ ] Test-NetConnection 443 成功
[ ] Test-NetConnection 51043 成功
[ ] /root/reality-relay-bootstrap-clash.yaml 已生成
[ ] 客户端导入节点后能连接
[ ] SERVER_ALIAS 对应的 443 节点出口是 VPS 自身公网 IP
[ ] 每个家宽节点出口 IP 符合预期
```

---

## 21. 最短命令版

已经理解风险后，可以按这组命令执行：

```bash
cd /root
unzip reality-relay-bootstrap.zip
cd reality-relay-bootstrap

cp config.example.env config.env
nano config.env

cp home-proxies.example.csv home-proxies.csv
nano home-proxies.csv

sudo bash bootstrap.sh --phase preflight
sudo bash bootstrap.sh --phase ssh-phase1

# 另开窗口测试 root key 登录成功后：
sudo CONFIRM_ROOT_KEY_LOGIN=yes bash bootstrap.sh --phase ssh-final

sudo bash bootstrap.sh --phase fail2ban
sudo bash bootstrap.sh --phase singbox
sudo bash bootstrap.sh --phase firewall
sudo bash bootstrap.sh --phase validate
sudo bash bootstrap.sh --phase output-nodes
```

