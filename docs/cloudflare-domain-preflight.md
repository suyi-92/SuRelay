# Cloudflare 域名预处理

这一步用于全新服务器和全新域名：域名已经准备托管到 Cloudflare，但还没有指向这台 VPS。一键安装向导默认开启此项；没有域名时可在对应组关闭。启用后会配置 HTTPS 静态网页与订阅，公网使用 `DIRECT_PORT`；新装为高位随机端口，访问 URL 必须包含该端口。Nginx 内部端口与订阅内部端口不需要直接对公网开放。

## 必须手动完成

1. 在 Cloudflare 控制台添加你的 zone，例如 `example.com`。
2. 到域名注册商控制台，把 NS 改成 Cloudflare 给出的 nameserver。
3. 回到 Cloudflare，等待 zone 变成 active。
4. 创建 API Token，至少包含：
   - Zone / Zone / Read
   - Zone / DNS / Edit
5. 保持 DNS 记录为 DNS only / 灰云，不要开橙云代理。

脚本可以用 Cloudflare API 创建 zone，但仍不能替你到域名注册商改 NS。除非你的域名本身也由 Cloudflare Registrar 管理，否则注册商 NS 这一步始终需要手动完成。

## config.env

```bash
ENABLE_CUSTOM_DOMAIN="true"
CUSTOM_DOMAIN="edge.example.com"
CLOUDFLARE_ZONE_NAME="example.com"
LE_EMAIL="admin@example.com"
CLOUDFLARE_API_TOKEN="Cloudflare DNS API Token"

# 默认开启：创建或更新 CUSTOM_DOMAIN 的 A/AAAA，且 proxied=false。
CLOUDFLARE_ENSURE_DNS_RECORDS="true"
CLOUDFLARE_DNS_OVERWRITE_EXISTING="true"
CLOUDFLARE_REQUIRE_ACTIVE_ZONE="true"
CLOUDFLARE_DNS_TTL="1"
```

如果 `CUSTOM_DOMAIN` 就是根域名，例如 `example.com`，`CLOUDFLARE_ZONE_NAME` 也填 `example.com`。

如果要让脚本在 Cloudflare 中创建 zone：

```bash
CLOUDFLARE_CREATE_ZONE="true"
CLOUDFLARE_ACCOUNT_ID="你的 Cloudflare account id"
```

这种模式需要 token 具备 zone 创建权限。创建后仍要到注册商改 NS。

## 预处理命令

先 dry-run：

```bash
sudo bash bootstrap.sh --phase cloudflare-dns --dry-run
```

确认无误后执行：

```bash
sudo bash bootstrap.sh --phase cloudflare-dns
```

成功后 Cloudflare 中应看到：

```text
A     edge.example.com -> SERVER_IP_IPV4    DNS only
AAAA  edge.example.com -> SERVER_IP_IPV6    DNS only
```

没有 IPv6 时只会创建 A 记录。

随后继续完整部署：

```bash
sudo bash bootstrap.sh --phase singbox
sudo bash bootstrap.sh --phase firewall
sudo bash bootstrap.sh --phase validate
```
