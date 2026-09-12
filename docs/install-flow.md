# 安装流程

## 1. 准备配置

```bash
cp config.example.env config.env
nano config.env
cp home-proxies.example.csv home-proxies.csv
nano home-proxies.csv
cp upstream-nodes.example.txt upstream-nodes.txt
nano upstream-nodes.txt
```

`443` 不写入 CSV。CSV 只管理 `51043` 起的出口端口。推荐字段顺序为 `tag,type,server,server_port,username,password,network,listen_port`；`listen_port` 可留空自动分配，旧字段顺序仍兼容。

`upstream-nodes.txt` 是可选文件，用来接入上游节点分享链接。当前只支持 `vless://...security=none...` 和 `vless://...security=reality...`。推荐写 `tag,node_url,listen_port`，也可逐行只写节点链接让脚本自动分配端口；旧 `tag,listen_port,node_url` 仍兼容。

链接中的 `type=tcp` 表示上游 VLESS transport；生成的 VLESS outbound 省略 `network`，默认可代理 TCP+UDP，UDP 默认使用 XUDP。固定 inbound → 指定 outbound 映射及 UFW 的 TCP 入口规则保持不变；客户端与上游落地机需要支持 UDP relay/XUDP。已有节点文件无需新增字段，更新代码后重跑 `singbox` 阶段即可重新生成配置并应用修复。

如需启用自有域名，DNS 默认按 Cloudflare 灰云 / DNS only 处理，A/AAAA 指向服务器公网 IP，并在 `config.env` 填写：

```bash
ENABLE_CUSTOM_DOMAIN="true"
CUSTOM_DOMAIN="edge.example.com"
CLOUDFLARE_ZONE_NAME="example.com"
LE_EMAIL="admin@example.com"
CLOUDFLARE_API_TOKEN="Cloudflare DNS API Token"
NGINX_FALLBACK_PORT="8443"
```

启用前需要先在 Cloudflare 添加/托管 zone，并在域名注册商处把 NS 改成 Cloudflare 分配的 nameserver，等待 zone active。细节见 [`cloudflare-domain-preflight.md`](./cloudflare-domain-preflight.md)。脚本会自动创建或更新 `CUSTOM_DOMAIN` 的 A/AAAA 灰云记录；启用后公网 `443` 仍由 sing-box 监听，普通 TLS fallback 到本机 Nginx `127.0.0.1:8443`。

## 2. SSH 初始化与加固

```bash
sudo bash bootstrap.sh --phase preflight
sudo bash bootstrap.sh --phase ssh-phase1
```

保留当前 SSH 窗口，另开窗口测试：

```powershell
ssh -p 22 -o PreferredAuthentications=publickey -o PasswordAuthentication=no root@服务器IP
```

确认 `whoami` 为 `root` 后：

```bash
sudo CONFIRM_ROOT_KEY_LOGIN=yes bash bootstrap.sh --phase ssh-final
```

## 3. fail2ban

```bash
sudo bash bootstrap.sh --phase fail2ban
```

## 4. sing-box VLESS+Reality

```bash
sudo bash bootstrap.sh --phase singbox
```

此阶段会：

1. 安装 sing-box。
2. 复制 `home-proxies.csv` 到 `/etc/reality-relay-bootstrap/home-proxies.csv`，如果存在也会复制 `upstream-nodes.txt`。
3. 生成 VLESS UUID、Reality keypair、short-id。
4. 生成 `/etc/sing-box/config.json`。
5. 执行 `sing-box check`。
6. 通过后重启 sing-box。
7. 如果 `ENABLE_SUBSCRIPTION_SERVER=true`，生成并启动订阅服务；不会默认输出 `/root` 节点文件。
8. 如果 `ENABLE_CUSTOM_DOMAIN=true`，先准备 Cloudflare 灰云 A/AAAA，再通过 Cloudflare DNS-01 申请证书并配置 Nginx fallback。

如需在服务器本地额外生成节点文件，可在部署完成后手动执行：

```bash
sudo bash bootstrap.sh --phase output-nodes
```

## 5. 防火墙

```bash
sudo bash bootstrap.sh --phase firewall
```

UFW 只放行 SSH、`DIRECT_PORT`、`home-proxies.csv` 和 `upstream-nodes.txt` 中实际使用的 `listen_port`。服务商安全组也要手动放行同样端口。

如果 `ENABLE_SUBSCRIPTION_SERVER=true` 且公网直连订阅端口已启用，还会放行 `SUBSCRIPTION_PORT`。启用自有域名且 `SUBSCRIPTION_PORT` 留空时，订阅服务只绑定本机 `SUBSCRIPTION_INTERNAL_PORT` 并由 Nginx fallback 反代，不开放订阅端口。

## 6. 验证

```bash
sudo bash bootstrap.sh --phase validate
```

`validate` 会测试家宽 HTTP/SOCKS 出口；对 `upstream-nodes.txt` 里的上游节点，会临时启动本地 socks 入口再通过对应 vless 上游访问 `https://ifconfig.me`。连通性失败只告警，不阻断部署。
