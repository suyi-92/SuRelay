# Reality Relay Bootstrap

在 Ubuntu 22.04/24.04、Debian 10–13 VPS 上部署 root 公钥登录、fail2ban、UFW 和 sing-box VLESS + Reality。默认保留现有节点身份，SSH 分两阶段加固。

## 安装

以 root 执行远程入口，或在已经下载的项目目录执行 `sudo bash install.sh`：

```bash
bash <(wget -qO- https://raw.githubusercontent.com/suyi-92/reality-relay-bootstrap/main/install.sh)
```

首次只填写三组信息：

1. **基础信息**：节点名称、服务器公网 IPv4/IPv6（至少一个）、SSH 端口。
2. **Cloudflare（可选）**：域名、Zone、证书邮箱和 API Token；Token 输入显示 `*`。域名应已在 CF 激活，Token 需要对应 Zone 的 DNS 编辑和 Zone 读取权限；[首次配置说明](docs/cloudflare-domain-preflight.md)。
3. **管理员公钥**：每行一把，空行结束；明确输入 `SKIP` 才会复用服务器已有公钥。

随后脚本显示一条 SSH 验证命令。**保留当前 SSH 窗口，在新终端执行；看到 `root` 后输入 `yes`，才关闭密码登录。** 直接回车会暂停。命令使用公钥和本地 SSH agent；有口令的私钥需先在本机用 `ssh-add` 加载。

成功后显示一个订阅链接及所有本地 `vless://` 节点链接。常规进度写入日志，告警和错误仍显示；需要详细输出时用 `sudo env RRB_VERBOSE=true bash install.sh`。

默认值：VLESS TCP 443、Mihomo 订阅、保留 UUID/Reality 密钥、启用 UDP 支持。CF 模式走域名 HTTPS，订阅服务仅监听本机 51040；无域名时使用公网 HTTP 51040。其他选项在 [config.example.env](config.example.env) 中查阅并修改 `config.env`。

**重跑安装会沿用已有 `config.env` 和出口列表，不重新提问或覆盖它们。** 新增/删除出口按下文操作。首次只生成配置、不部署可用：

```bash
sudo env RRB_RUN_PHASES=false bash install.sh
```

## IPv6、CF 域名与 IPv4 不通

CF 在这里提供 DNS 和证书验证，记录必须为**灰云 / DNS only**。A 指向 VPS IPv4，AAAA 指向 VPS IPv6；Reality 流量直接连接 VPS，不能用普通橙云代理。

- 已配置公网 IPv6、开启 IPv6 监听并放行云安全组时，域名订阅可经 IPv6 获取。
- **域名不能绕过被封的 IPv4，也不能保证客户端自动改走 IPv6。** 客户端自身需要可用 IPv6，且 DNS、VPS IPv6 和对应 TCP 端口均可达。
- 订阅和本地链接使用同一套节点：保留原域名/IPv4 节点，并为每个出口增加 `-IPv6` 节点。备用节点直接连接 IPv6，SNI 仍是原 Reality 域名。IPv4 失败时可手动选 `-IPv6`。
- `PROXY_IP_VERSION` 控制 VPS 出站/DNS 偏好，不等于客户端连接 VPS 时使用的地址族。

已部署的服务器补充 IPv6：

```bash
cd /opt/reality-relay-bootstrap
sudo bash install.sh -6 2001:db8::10
```

省略地址可自动探测后确认。该模式复用现有 UUID、Reality keypair、short-id 和端口，不重跑 SSH；验证双栈监听后刷新已启用的订阅，并在 CF 模式更新 A/AAAA。若订阅或 DNS 发布失败，已验证的本地 IPv6 配置会保留，可分别重跑 `subscription` 或 `cloudflare-dns` 阶段。同时保留 `/root/reality-relay-bootstrap-v6-vless.txt`、`/root/reality-relay-bootstrap-v6-nodes.txt` 和 `/root/reality-relay-bootstrap-v6-clash.yaml` 三份本地 v6 文件。

如果客户端通过域名拉取订阅时仍卡在 IPv4，可先导入输出的本地 IPv6 节点，或在支持 IPv6 的电脑上强制经 IPv6 下载 YAML 后导入：

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
sudo bash bootstrap.sh --phase output-nodes
```

生成 `/root/reality-relay-bootstrap-vless.txt`（分享链接）、`/root/reality-relay-bootstrap-clash.yaml`（可本地导入的完整配置）和 `/root/reality-relay-bootstrap-nodes.txt`（字段明细），权限均为 600。

## 新增或删除出口

在 `/opt/reality-relay-bootstrap`（或实际项目目录）修改下列文件；安装时只为缺失的列表创建空表头。保持 `config.env` 中 `RESET_PROXY_KEYS="false"`，已有节点的 `tag` 和 `listen_port` 尽量不改。

**家宽 HTTP/SOCKS 出口**：编辑 `home-proxies.csv`。

```csv
tag,type,server,server_port,username,password,network,listen_port
home-01,socks5,proxy.example.com,1080,user,password,tcp,51043
```

**上游 VLESS 出口**：编辑 `upstream-nodes.txt`。

```csv
tag,node_url,listen_port
relay-01,vless://11111111-2222-4333-8444-555555555555@upstream.example.com:443?security=reality&type=tcp&sni=www.microsoft.com&pbk=PUBLIC_KEY&sid=abcd&flow=xtls-rprx-vision,51044
```

替换为真实上游链接；支持 TCP transport 的普通 VLESS（`security=none`）和 VLESS Reality。也可每行只放一条 `vless://` 链接，让脚本分配名称和端口。`listen_port` 留空从 51043 起分配；为了增删时不改变旧入口，推荐固定端口。CSV 中含逗号的字段需用双引号包裹。

删除出口就删除对应行，然后应用：

```bash
sudo bash bootstrap.sh --phase singbox
sudo bash bootstrap.sh --phase firewall
sudo bash bootstrap.sh --phase validate
```

`singbox` 会重新生成配置、检查并重启、刷新订阅，最后显示订阅与本地节点；`firewall` 只调整带本项目注释的规则。上游连通性失败会告警，不代表已经验证出口可用。不要为增删出口删除密钥文件或打开 `RESET_PROXY_KEYS`。

## 常用维护

| 需求 | 命令 / 配置 |
| --- | --- |
| 更新订阅内容 | `sudo bash bootstrap.sh --phase subscription` |
| 更新 CF A/AAAA | `sudo bash bootstrap.sh --phase cloudflare-dns` |
| 更新证书和 Nginx fallback | `sudo bash bootstrap.sh --phase custom-domain` |
| 继续已暂停的 SSH final | 验证公钥登录后，`sudo CONFIRM_ROOT_KEY_LOGIN=yes bash bootstrap.sh --phase ssh-final` |
| 修改端口、SFTP、安装方式等 | 编辑 `config.env`，参照 [示例配置](config.example.env) 和 [分阶段流程](docs/install-flow.md) |
| 详细校验输出 | `sudo bash bootstrap.sh --phase validate --verbose` |
| 回滚 | `sudo CONFIRM_ROLLBACK=yes bash bootstrap.sh --phase rollback`；[恢复说明](docs/recovery.md) |

云厂商安全组必须放行 SSH、443 和实际出口入口的 **TCP** 端口，IPv4/IPv6 分别检查。无 CF 时还需订阅 HTTP 端口；CF 模式且 `SUBSCRIPTION_PORT` 留空时无需公网开放 51040。VLESS 的 UDP 封装在 TCP/Reality 中，不需要额外放行同号 UDP。

安装日志：`/tmp/reality-relay-bootstrap-install.log`；阶段日志：`/var/log/reality-relay-bootstrap.log`；配置备份：`/root/reality-relay-bootstrap-backups/`。

订阅 URL/token、节点链接、`config.env`、出口列表及 Reality 私钥都不要公开。未启用域名时内置订阅为 HTTP，建议在云安全组限制来源；完整路径和权限见 [路径说明](docs/path-map.md)。
