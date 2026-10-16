# SuRelay

轻量 Reality 部署、中转与 VPS 安全管理。安装后输入 **sr** 打开终端控制面板。

## 推荐一键安装

新安装推荐直接使用一键安装指令。在 VPS 上以 root 执行：

```bash
bash <(wget -qO- https://raw.githubusercontent.com/suyi-92/SuRelay/main/install.sh)
```

执行后按提示完成安装，随后继续阅读下方步骤。

## 旧安装如何更新

在 VPS 上以 root 执行：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/suyi-92/SuRelay/main/install.sh) --update
```

旧默认目录自动迁移到 `/opt/SuRelay`，保留配置、节点和订阅。完成后输入 `sr`，以后用 `sr update` 更新。首次部署见下方「安装」。

## 如何卸载（含旧安装）

运行 `sr uninstall`，或选择 `sr → 8) 卸载 SuRelay`。旧版本没有 sr 时执行：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/suyi-92/SuRelay/main/install.sh) --uninstall
```

展示清单并备份后，输入 `UNINSTALL` 才卸载。保留 SSH 配置、公钥、系统软件包、代码目录和备份；节点与订阅停止。

## 管理菜单

| 选项 | 功能 |
| --- | --- |
| 1) 节点与订阅 | 查看订阅/节点、导出配置、刷新订阅、修改名称和主入口端口 |
| 2) 出口管理 | HTTP/SOCKS5 与上游 VLESS 的添加、修改、删除和连通性检查 |
| 3) IPv6 设置 | 查看、探测启用、更换地址、关闭 IPv6 节点 |
| 4) 域名与证书 | 配置 CF 域名和凭据、同步 DNS、检查续期、停用域名模式 |
| 5) 服务与诊断 | 状态、启动/停止/重启代理与订阅、日志、配置和监听检查 |
| 6) VPS 安全 | UFW 自定义端口规则、SSH 端口、公钥与密码认证、fail2ban |
| 7) 高级维护 | 更新脚本/内核、备份恢复、高级配置、继续安装、目录迁移和重置 |
| 8) 卸载 SuRelay | 预览清单、备份并卸载部署，保留 SSH |

面板在同一屏幕内切换菜单和输入：方向键或数字选择，Enter 确认，Esc 返回，q 退出。节点与订阅页也使用居中面板，直接展示完整链接；**Enter / c** 请求复制完整原始链接（需终端支持 OSC 52），**p** 在当前面板展开/收起原文，便于鼠标选中复制；**←→** 切换节点或订阅格式，**Esc / 0** 返回。过长内容可用 ↑↓ 或 PgUp/PgDn 翻页。添加上游 VLESS 每次完成一个节点，依次填写链接、名称和入口端口后保存。`sr --plain` 使用逐行菜单，`NO_COLOR=1 sr` 关闭颜色。打开菜单只读取状态；更新脚本等常规维护选中后执行，需要核对的变更使用确认/取消选项，不必输入 YES 等确认词。出口编辑会固定已有名称与端口，更新节点不重新生成身份。详细行为与恢复边界见 [SuRelay 管理说明](docs/surelay.md)。

在 Ubuntu 22.04/24.04、Debian 10–13 和 Alpine 3.19（OpenRC，x86_64/aarch64）VPS 上部署 root 公钥登录、fail2ban、UFW 和 sing-box VLESS + Reality。默认保留现有节点身份，SSH 分两阶段加固。

## 安装

Ubuntu/Debian 在 VPS 上以 root 执行远程入口，无需提前进入项目目录：

```bash
bash <(wget -qO- https://raw.githubusercontent.com/suyi-92/SuRelay/main/install.sh)
```

Alpine 3.19 默认 shell 是 ash，需要先安装 Bash。以 root 执行：

```sh
apk add --no-cache bash curl ca-certificates
curl -fsSL https://raw.githubusercontent.com/suyi-92/SuRelay/main/install.sh -o /tmp/surelay-install.sh
bash /tmp/surelay-install.sh
```

Alpine 需要已启动的 OpenRC、同版本的 `main`/`community` 软件源以及可用的 IPv4/IPv6 netfilter 权限。`INSTALL_SINGBOX_METHOD="auto"` 会选择固定版本的官方二进制；服务通过 OpenRC 管理。已有配置若写死 `apt`，先改为 `auto`。详细差异、日志和验证范围见 [Alpine 部署说明](docs/alpine.md)。

一键脚本默认安装到 `/opt/SuRelay`。下文的服务器操作都按此路径给出；如果使用了 `RRB_INSTALL_DIR` 或手动下载到其他位置，请替换每处 `cd` 的路径。编辑示例使用 `nano`，保存按 `Ctrl+O`、回车，退出按 `Ctrl+X`；未安装时 Debian/Ubuntu 执行 `sudo apt install nano`，Alpine 以 root 执行 `apk add nano`。

首次只填写三组信息：

1. **基础信息**：节点名称、服务器公网 IPv4（自动探测、可修改）、SSH 端口。首次安装不探测或启用 IPv6；需要时安装后使用 **sr → IPv6 设置**，或下文的 -6 维护指令。
2. **Cloudflare（可选）**：域名、Zone、证书邮箱和 API Token；Token 输入显示 `*`。域名应已在 CF 激活，Token 需要对应 Zone 的 DNS 编辑和 Zone 读取权限；[首次配置说明](docs/cloudflare-domain-preflight.md)。
3. **管理员公钥**：每行一把，空行结束；明确输入 `SKIP` 才会复用服务器已有公钥。

随后脚本显示一条 SSH 验证命令。**保留当前 SSH 窗口，在新终端执行；看到 `root` 后再确认关闭密码登录。** 提示为 `[Y/n]`，默认 `Y`：回车或输入 `y` 继续，输入 `n` 暂停；输入中断也会暂停。命令使用公钥和本地 SSH agent；有口令的私钥需先在本机用 `ssh-add` 加载。

安装流程以标题和分隔线区分部署准备、SSH 验证、应用配置与最终节点输出，并显示各阶段的开始、完成或失败。成功后显示 IPv6 节点状态、一个订阅链接及所有本地 `vless://` 节点链接；订阅链接为青色，本地节点为绿色。重定向输出、`TERM=dumb` 或设置 `NO_COLOR=1` 时使用纯文本。命令详细输出写入日志，告警和错误仍显示。

默认值：VLESS TCP 443、Mihomo 订阅、保留 UUID/Reality 密钥、启用 UDP 支持。CF 模式走域名 HTTPS，订阅服务仅监听本机 51040；无域名时使用公网 HTTP 51040。其他选项在 [config.example.env](config.example.env) 中查阅并修改 `config.env`。

NAT VPS 若只有高位业务端口，支持把 `DIRECT_PORT` 设置为服务商的同号映射端口；Reality 与域名 HTTPS 订阅共用该端口。`SSH_PORT` 仍填写本机监听端口，迁移与验证步骤见 [NAT 端口配置](docs/nat-ports.md)。

**重跑安装会沿用已有 `config.env` 和出口列表，不重新提问或覆盖它们。** 已安装后，需要修改配置并重新运行安装流程：

```bash
cd /opt/SuRelay
sudo nano config.env
sudo bash install.sh
```

不改配置时跳过编辑命令；需要详细输出时，将最后一行换成 `sudo env RRB_VERBOSE=true bash install.sh`。只增删出口无需重跑完整安装，按下文对应步骤操作。

首次只下载项目、生成配置而不部署，可在 VPS 上以 root 执行：

```bash
RRB_RUN_PHASES=false bash <(wget -qO- https://raw.githubusercontent.com/suyi-92/SuRelay/main/install.sh)
```

生成后，按上面的“进入目录 → 编辑 `config.env` → 运行安装”步骤继续部署。

## 重置后重新安装

需要清空旧部署、重新填写配置并生成全新节点时，在服务器项目目录执行：

```bash
cd /opt/SuRelay
sudo bash install.sh --reset
```

Alpine 以 root 执行 `bash install.sh --reset`，无需 sudo。远程入口也支持追加 `--reset`；Alpine 下载 `install.sh` 后执行 `bash /tmp/surelay-install.sh --reset`。

脚本先显示卸载清单，输入大写 `RESET` 后，将旧文件备份到 `/root/surelay-backups/reset-*` 并核验，然后停止旧节点/订阅、删除项目配置与专用服务、清理项目代理端口规则，再进入全新安装。**旧节点、订阅 token、出口列表会失效或清空，域名模式可能需要重新签发证书。**

系统软件包、当前 SSH 通道及公钥、其他站点/证书/防火墙规则保留。重装仍需另开终端验证 root 公钥登录。`--reset` 不能与 `-6` 或 `RRB_RUN_PHASES=false` 合用；备份失败或路径归属不明时停止。详细清理范围、预览及恢复步骤见 [重置安装说明](docs/reset-install.md)。

## IPv6、CF 域名与 IPv4 不通

CF 在这里提供 DNS 和证书验证，记录必须为**灰云 / DNS only**。A 指向 VPS IPv4，AAAA 指向 VPS IPv6；Reality 流量直接连接 VPS，不能用普通橙云代理。

- 已配置公网 IPv6、开启 IPv6 监听并放行云安全组时，域名订阅可经 IPv6 获取。
- **域名不能绕过被封的 IPv4，也不能保证客户端自动改走 IPv6。** 客户端自身需要可用 IPv6，且 DNS、VPS IPv6 和对应 TCP 端口均可达。
- 订阅和本地链接使用同一套节点：保留原域名/IPv4 节点，并为每个出口增加 `-IPv6` 节点。备用节点直接连接 IPv6，SNI 仍是原 Reality 域名。IPv4 失败时可手动选 `-IPv6`。
- `PROXY_IP_VERSION` 控制 VPS 出站/DNS 偏好，不等于客户端连接 VPS 时使用的地址族。

首次安装留空 IPv6 的服务器，可先在 VPS 控制台确认已分配公网 IPv6，再执行以下命令自动探测并确认：

```bash
cd /opt/SuRelay
sudo bash install.sh -6
```

也可直接填写 VPS 的真实公网 IPv6（替换下面的示例地址；脚本会修改 `config.env` 并应用，无需手工编辑）：

```bash
cd /opt/SuRelay
sudo bash install.sh -6 2001:db8::10
```

该模式复用现有 UUID、Reality keypair、short-id 和端口，不重跑 SSH；验证双栈监听后刷新已启用的订阅，并在 CF 模式更新 A/AAAA。若订阅或 DNS 发布失败，已验证的本地 IPv6 配置会保留，按下方“常用维护”中的对应步骤重试。同时保留 `/root/surelay-v6-vless.txt`、`/root/surelay-v6-nodes.txt` 和 `/root/surelay-v6-clash.yaml` 三份本地 v6 文件。

如果客户端通过域名拉取订阅时仍卡在 IPv4，可先导入输出的本地 IPv6 节点，或在支持 IPv6 的**客户端电脑**上强制经 IPv6 下载 YAML 后导入（无需进入 VPS 项目目录，文件保存到当前目录）：

```bash
curl -6 --fail --show-error 'https://你的域名/sub/你的token?target=ClashMeta' -o relay.yaml
```

这不会绕过 IPv6 自身的网络故障。勿把域名 HTTPS 链接直接改成 IPv6 字面量，否则证书名称不匹配。

## 订阅与本地导入

在线订阅供 Mihomo / Clash Meta 使用，形式为：

```text
https://edge.example.com/sub/<token>?target=ClashMeta
```

未启用 CF 时为 `http://服务器IP:51040/sub/<token>?target=ClashMeta`。脚本优先显示已配置的外部订阅地址，其次显示 IPv4，只有 IPv6 时使用 `[IPv6]` 地址。所有地址返回同一份完整节点。

“本地节点”是一行一个的 `vless://...` 分享链接，可复制到支持 VLESS Reality 的客户端导入；有家宽或上游出口时会同时输出这些**中转入口**，不会泄露上游原始链接。本地节点是静态副本，后续增删出口需要重新导入；在线订阅可刷新。

重新显示并导出本地文件：

```bash
cd /opt/SuRelay
sudo bash bootstrap.sh --phase output-nodes
```

生成 `/root/surelay-vless.txt`（分享链接）、`/root/surelay-clash.yaml`（可本地导入的完整配置）和 `/root/surelay-nodes.txt`（字段明细），权限均为 600。

## 新增或删除出口

安装时只为缺失的出口列表创建空表头。下面分别给出两种出口的完整操作，按需要选择；保持 `config.env` 中 `RESET_PROXY_KEYS="false"`，已有节点的 `tag` 和 `listen_port` 尽量不改。

### 家宽 HTTP/SOCKS 出口

先进入目录，检查密钥保留设置，再编辑出口列表：

```bash
cd /opt/SuRelay
sudo nano home-proxies.csv
```

~~~bash
sudo nano config.env
~~~

在 `config.env` 中保持 `RESET_PROXY_KEYS="false"`。在 `home-proxies.csv` 中保留表头，新增或修改真实出口；删除出口就删除对应数据行：

```csv
tag,type,server,server_port,username,password,network,listen_port
home-01,socks5,proxy.example.com,1080,user,password,tcp,51043
```

保存退出后，应用配置并验证：

```bash
cd /opt/SuRelay
sudo bash bootstrap.sh --phase singbox
sudo bash bootstrap.sh --phase firewall
sudo bash bootstrap.sh --phase validate
```

### 上游 VLESS 出口

先进入目录，检查密钥保留设置，再编辑上游列表：

```bash
cd /opt/SuRelay
sudo nano upstream-nodes.txt
```

~~~bash
sudo nano config.env
~~~

在 `config.env` 中保持 `RESET_PROXY_KEYS="false"`。在 `upstream-nodes.txt` 中保留表头，新增或修改真实上游；删除出口就删除对应数据行：

```csv
tag,node_url,listen_port
relay-01,vless://11111111-2222-4333-8444-555555555555@upstream.example.com:443?security=reality&type=tcp&sni=www.microsoft.com&pbk=PUBLIC_KEY&sid=abcd&flow=xtls-rprx-vision,51044
```

替换为真实上游链接；支持 TCP transport 的普通 VLESS（`security=none`）和 VLESS Reality。也可每行只放一条 `vless://` 链接，让脚本分配名称和端口。`listen_port` 留空从 51043 起分配；为了增删时不改变旧入口，推荐固定端口。CSV 中含逗号的字段需用双引号包裹。

保存退出后，应用配置并验证：

```bash
cd /opt/SuRelay
sudo bash bootstrap.sh --phase singbox
sudo bash bootstrap.sh --phase firewall
sudo bash bootstrap.sh --phase validate
```

两种出口的应用步骤相同：`singbox` 会重新生成配置、检查并重启、刷新订阅，最后显示订阅与本地节点；`firewall` 只调整带本项目注释的规则。上游连通性失败会告警，不代表已经验证出口可用。不要为增删出口删除密钥文件或打开 `RESET_PROXY_KEYS`。

## 常用维护

### 修改节点参数、入口端口或 sing-box 安装方式

参照 [示例配置](config.example.env) 修改 `DIRECT_PORT`、`PROXY_IP_VERSION`、`INSTALL_SINGBOX_METHOD` 等，保存后应用：

```bash
cd /opt/SuRelay
sudo nano config.env
sudo bash bootstrap.sh --phase singbox
sudo bash bootstrap.sh --phase firewall
sudo bash bootstrap.sh --phase validate
```

SSH、SFTP 参数由 SSH 阶段处理，不属于上述节点配置；修改它们时按前面的完整安装流程或 [分阶段流程](docs/install-flow.md) 执行，并保留公钥登录确认步骤。

### 刷新订阅内容

沿用已保存的配置重新生成订阅，无需编辑文件。新增或删除出口请先完成上面的出口应用步骤。

```bash
cd /opt/SuRelay
sudo bash bootstrap.sh --phase subscription
```

### 重试发布 CF A/AAAA

沿用已保存的地址和域名配置，无需编辑文件：

```bash
cd /opt/SuRelay
sudo bash bootstrap.sh --phase cloudflare-dns
```

### 修改域名、CF 或证书配置

在 `config.env` 中调整 `CUSTOM_DOMAIN`、`CLOUDFLARE_*`、`LE_EMAIL` 等，再应用；`singbox` 阶段会同时更新节点、订阅、CF DNS、证书和 Nginx fallback：

```bash
cd /opt/SuRelay
sudo nano config.env
sudo bash bootstrap.sh --phase singbox
sudo bash bootstrap.sh --phase firewall
sudo bash bootstrap.sh --phase validate
```

配置未变、只需重试证书与 Nginx fallback 时，无需编辑文件：

```bash
cd /opt/SuRelay
sudo bash bootstrap.sh --phase custom-domain
```

### 继续已暂停的 SSH final

保留当前 SSH 窗口，在新终端验证公钥登录成功后执行，无需编辑配置：

```bash
cd /opt/SuRelay
sudo CONFIRM_ROOT_KEY_LOGIN=yes bash bootstrap.sh --phase ssh-final
```

### 查看详细校验结果

```bash
cd /opt/SuRelay
sudo bash bootstrap.sh --phase validate --verbose
```

### 安装停在“应用配置与验证”

安装/升级软件包时会自动保留已有配置文件（例如 `/etc/sing-box/config.json`），不会等待隐藏的覆盖确认，也不会读取当前 SSH 终端的输入。`DEBIAN_FRONTEND=noninteractive` 本身并不能禁用 dpkg 的配置文件确认，因此脚本同时设置了保留旧配置的策略。后续节点生成阶段仍会根据本项目配置更新 sing-box。

如果运行的是旧版脚本，且日志出现 `*** config.json (Y/I/N/O/D/Z) [default=N] ?`，回到原安装窗口输入 `N` 并回车即可继续；不要在该窗口输入 Shell 命令或另外启动安装器。

新版脚本会实时写入 APT 输出。需要查看当前步骤时，保留安装窗口，另开 SSH 窗口执行：

```bash
tail -f /var/log/surelay.log
```

### APT 提示锁被占用

出现 `lock-frontend`、`Could not get lock`，且日志注明锁由 `unattended-upgr` 等进程持有时，表示系统自动更新或另一个包管理进程正在运行。部署阶段会仅针对锁占用自动重试，最多等待 5 分钟；超过时间后停止。不要删除锁文件或强行终止更新进程。

可先查看自动更新状态（只读）：

```bash
sudo systemctl status apt-daily.service apt-daily-upgrade.service --no-pager
```

等待更新结束后，从项目目录重跑；已有配置和出口列表会保留：

```bash
cd /opt/SuRelay
sudo bash install.sh
```

如需延长每条 APT 命令的锁等待时间，可将最后一行改为 `sudo env RRB_APT_LOCK_TIMEOUT=600 bash install.sh`。网络、软件源、软件包或 dpkg 配置错误不会按锁占用重试；控制台只显示本次失败命令的日志末尾，完整历史仍保留在阶段日志中。

### 回滚

先阅读 [恢复说明](docs/recovery.md)，保留当前还能登录的 SSH 窗口，再执行：

```bash
cd /opt/SuRelay
sudo CONFIRM_ROLLBACK=yes bash bootstrap.sh --phase rollback
```

云厂商安全组必须放行 SSH、443 和实际出口入口的 **TCP** 端口，IPv4/IPv6 分别检查。无 CF 时还需订阅 HTTP 端口；CF 模式且 `SUBSCRIPTION_PORT` 留空时无需公网开放 51040。VLESS 的 UDP 封装在 TCP/Reality 中，不需要额外放行同号 UDP。

安装日志：`/tmp/surelay-install.log`；阶段日志：`/var/log/surelay.log`；配置备份：`/root/surelay-backups/`。

订阅 URL/token、节点链接、`config.env`、出口列表及 Reality 私钥都不要公开。未启用域名时内置订阅为 HTTP，建议在云安全组限制来源；完整路径和权限见 [路径说明](docs/path-map.md)。
