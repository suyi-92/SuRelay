# 安装流程

Alpine 3.19 先按 [Alpine 部署说明](alpine.md) 安装 Bash，并保持 `INSTALL_SINGBOX_METHOD="auto"`。下列阶段在 Alpine 使用 OpenRC，在 Debian/Ubuntu 使用 systemd。

## 1. 准备配置

一键安装使用可返回的分组向导，默认开启 CF 域名与静态网页，并为 SSH、公网主入口、订阅服务和 Nginx 内部站点生成互不冲突的高位随机端口。分组页直接按 **← 上一步 / 下一步 →** 切换，↑↓ / 数字选择字段后 Enter 编辑；输入框内左右键仍移动光标。文字菜单可用 `<` / `>`，最终预览需明确确认才部署。已有 `config.env` 保留原配置，不重新随机。以下手动分阶段流程需要自行填写这些服务的实际端口；NAT 主机应使用服务商允许的本机映射端口。

如果只需要服务器安全防护，可跳过节点配置并使用：

```bash
sudo bash install.sh --security-only
```

该模式只执行 SSH 公钥初始化与二阶段加固、fail2ban 和 UFW。UFW 只确保当前 SSH 端口放行，已有其他规则保持不变；sing-box、订阅、Reality 密钥、域名和节点文件都不会安装或生成。它仍需要在新 SSH 窗口确认 root 公钥登录后，才会关闭密码登录。

以后要继续部署节点时，输入 `sr`，从主菜单选择“部署节点代理”；面板会确认后将 `SURELAY_SECURITY_ONLY` 切换为 `false` 并运行普通安装流程。需要订阅时再在“高级维护 → 编辑高级配置并应用”中开启 `ENABLE_SUBSCRIPTION_SERVER`。

已有部署需要连同项目 SSH/安全属性重新配置时，在 `sr` 主菜单选择「开始引导式节点布置」，或运行 `bash install.sh --redeploy`。它先备份并清理旧项目部署，再打开完整向导；新端口和新公钥验证前保留当前 SSH 通道，验证后关闭旧项目 SSH 入口。原有 `bash install.sh --reset` 继续使用保留历史 SSH 公钥和自定义业务端口规则的重装流程，普通重跑仍保留节点身份。见 [重置安装说明](reset-install.md)。

```bash
cp config.example.env config.env
nano config.env
cp home-proxies.example.csv home-proxies.csv
nano home-proxies.csv
cp upstream-nodes.example.txt upstream-nodes.txt
nano upstream-nodes.txt
```

`DIRECT_PORT` 不写入 CSV。CSV 只管理 `HOME_PORT_START` 起的 VLESS 中转入口端口，并避开 SSH、订阅、Nginx 和其他已启用协议占用的端口。推荐字段顺序为 `tag,type,server,server_port,username,password,network,listen_port`；`listen_port` 可留空自动分配，旧字段顺序仍兼容。

`upstream-nodes.txt` 是可选文件，用来接入上游节点分享链接。当前只支持 `vless://...security=none...` 和 `vless://...security=reality...`。推荐写 `tag,node_url,listen_port`，也可逐行只写节点链接让脚本自动分配端口；旧 `tag,listen_port,node_url` 仍兼容。

链接中的 `type=tcp` 表示上游 VLESS transport；生成的 VLESS outbound 省略 `network`，默认可代理 TCP+UDP，UDP 默认使用 XUDP。固定 inbound → 指定 outbound 映射及 UFW 的 TCP 入口规则保持不变；客户端与上游落地机需要支持 UDP relay/XUDP。已有节点文件无需新增字段，更新代码后重跑 `singbox` 阶段即可重新生成配置并应用修复。

如需启用自有域名，DNS 默认按 Cloudflare 灰云 / DNS only 处理，A/AAAA 指向服务器公网 IP，并在 `config.env` 填写：

```bash
ENABLE_CUSTOM_DOMAIN="true"
CUSTOM_DOMAIN="edge.example.com"
CLOUDFLARE_ZONE_NAME="example.com"
LE_EMAIL="admin@example.com"
CLOUDFLARE_API_TOKEN="Cloudflare DNS API Token"
NGINX_FALLBACK_PORT="53002"
```

启用前需要先在 Cloudflare 添加/托管 zone，并在域名注册商处把 NS 改成 Cloudflare 分配的 nameserver，等待 zone active。细节见 [`cloudflare-domain-preflight.md`](./cloudflare-domain-preflight.md)。脚本会自动创建或更新 `CUSTOM_DOMAIN` 的 A/AAAA 灰云记录；公网 `DIRECT_PORT` 由 sing-box 监听，普通 TLS fallback 到本机 Nginx 的 `NGINX_FALLBACK_PORT`。新装 `DIRECT_PORT` 是高位随机端口，HTTPS 静态页与订阅 URL 需要带此端口；本例内部站点使用 `127.0.0.1:53002`。

## 2. SSH 初始化与加固

```bash
sudo bash bootstrap.sh --phase preflight
sudo bash bootstrap.sh --phase ssh-phase1
```

保留当前 SSH 窗口，另开窗口测试。下例 `53000` 需要替换为 `config.env` 中的 `SSH_PORT`，并先确保安全组与 NAT 已放行：

```powershell
ssh -p 53000 -o PreferredAuthentications=publickey -o PasswordAuthentication=no root@服务器IP
```

确认 `whoami` 为 `root` 后：

```bash
sudo CONFIRM_ROOT_KEY_LOGIN=yes bash bootstrap.sh --phase ssh-final
```

## 3. fail2ban

```bash
sudo bash bootstrap.sh --phase fail2ban
```

## 4. sing-box 多协议节点

```bash
sudo bash bootstrap.sh --phase singbox
```

此阶段会：

1. 安装 sing-box，并准备或复用 UUID、Reality keypair 与 short-id。
2. 复制出口文件到 `/etc/surelay`，保留原有中转映射。
3. 如果 `ENABLE_CUSTOM_DOMAIN=true`，先准备 Cloudflare 灰云 A/AAAA，再申请 DNS-01 证书并配置本机 Nginx。HY2 会在内核校验和启动时读取证书，首次部署也必须先完成这一步。
4. 按协议开关生成 `/etc/sing-box/config.json`，包含原有 VLESS Reality 及主动启用的 HY2、SOCKS5、SS、HTTP 入口。新增入口默认走 VPS 直连；字段与证书要求见[协议配置](protocols.md)。
5. 执行 `sing-box check`，通过后重启 sing-box 并验证监听。
6. 如果 `ENABLE_SUBSCRIPTION_SERVER=true`，生成并启动订阅服务；不会默认输出 `/root` 节点文件。

如需在服务器本地额外生成节点文件，可在部署完成后手动执行：

```bash
sudo bash bootstrap.sh --phase output-nodes
```

## 5. 防火墙

```bash
sudo bash bootstrap.sh --phase firewall
```

UFW 按传输类型放行 SSH、`DIRECT_PORT`、出口文件中的 VLESS 中转入口，以及主动启用的新协议入口。VLESS Reality、SOCKS5 和 HTTP 使用 TCP，HY2 使用 UDP，SS 使用 TCP，且 `CLIENT_UDP=true` 时同时开放 UDP。服务商安全组也要手动放行同样端口和传输类型。

如果 `ENABLE_SUBSCRIPTION_SERVER=true` 且公网直连订阅端口已启用，还会放行 `SUBSCRIPTION_PORT`。启用自有域名且 `SUBSCRIPTION_PORT` 留空时，订阅服务只绑定本机 `SUBSCRIPTION_INTERNAL_PORT` 并由 Nginx fallback 反代，不开放订阅端口。

## 6. 验证

```bash
sudo bash bootstrap.sh --phase validate
```

`validate` 会测试家宽 HTTP/SOCKS 出口；对 `upstream-nodes.txt` 里的上游节点，会临时启动本地 socks 入口再通过对应 vless 上游访问 `https://ifconfig.me`。连通性失败只告警，不阻断部署。
