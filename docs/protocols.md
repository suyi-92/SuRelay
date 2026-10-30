# 入口协议

SuRelay 保留原有 VLESS Reality，并可另外开启 Hysteria2（HY2）、SOCKS5、Shadowsocks（SS）和 HTTP。这里的入口指客户端连接本机的协议，与 `home-proxies.csv` 中的家宽 HTTP/SOCKS5 **出口**不同。

首次安装在「入口协议」组逐项开启，四种新增协议默认关闭。已安装主机可在 `sr → 节点与订阅 → 配置入口协议（HY2 / SOCKS5 / SS / HTTP）` 中使用同样的协议表单，或在高级配置中修改；不会为旧配置自动开启协议或重新随机端口。

| 入口 | 配置开关与端口 | 认证 | 监听与客户端要求 |
| --- | --- | --- | --- |
| VLESS Reality | 继续使用 `DIRECT_PORT` | 原有 UUID、Reality keypair、short-id | TCP；UDP 数据封装在原 TCP 会话中 |
| HY2 | `ENABLE_HY2`、`HY2_PORT` | `HY2_PASSWORD` | UDP；需要有效 TLS 证书与客户端 SNI |
| SOCKS5 | `ENABLE_SOCKS5`、`SOCKS5_PORT` | `PROXY_USERNAME`、`PROXY_PASSWORD` | TCP；当前导出不声明 UDP relay 支持 |
| SS | `ENABLE_SHADOWSOCKS`、`SHADOWSOCKS_PORT` | `SHADOWSOCKS_METHOD`、`SHADOWSOCKS_PASSWORD` | TCP；`CLIENT_UDP=true` 时同时使用 UDP |
| HTTP | `ENABLE_HTTP`、`HTTP_PORT` | `PROXY_USERNAME`、`PROXY_PASSWORD` | TCP；HTTP 代理认证 |

新增入口默认走 VPS 直连。家宽和上游列表继续为各行生成专属的 VLESS Reality 中转入口，维持原有入口到出口的对应关系。

SOCKS5 和 HTTP 入口使用用户名与密码，但连接本身没有 TLS 加密；需要加密链路时选用 HY2、SS 或 VLESS Reality。不要在不可信网络中传送未加密的代理认证信息。

## 配置与端口

下面是手动配置示例。端口只是示例，请避开 SSH、订阅、Nginx、主入口和现有中转入口；新装向导会分配并保留草稿中的端口。公网端口还需要服务商安全组与 NAT 映射配合。

```bash
ENABLE_HY2="true"
HY2_PORT="53010"
HY2_PASSWORD=""

ENABLE_SOCKS5="true"
SOCKS5_PORT="53011"
PROXY_USERNAME="surelay"
PROXY_PASSWORD=""

ENABLE_SHADOWSOCKS="true"
SHADOWSOCKS_PORT="53012"
SHADOWSOCKS_METHOD="aes-128-gcm"
SHADOWSOCKS_PASSWORD=""

ENABLE_HTTP="true"
HTTP_PORT="53013"
```

密码留空时根据现有 VLESS UUID 为不同用途稳定派生，不使用空认证；SOCKS5 与 HTTP 共用 `PROXY_USERNAME` / `PROXY_PASSWORD`，HY2 与 SS 各自派生。重复应用会保留同一密码。填写显式密码会优先使用该值。`RESET_PROXY_KEYS=true` 轮换 UUID 后，派生密码也会改变，原有客户端需要更新；日常修改请保持 `false`。

SSH、主入口、内部订阅和 Nginx 的高位随机端口仅在新装生成一次，并写入 `config.env`。`DIRECT_PORT` 同时承担 VLESS Reality、域名 HTTPS 订阅和公网静态网页，新装访问地址类似 `https://edge.example.com:53000/`；内部 `NGINX_FALLBACK_PORT` 只在本机监听，无需单独向公网开放。

## HY2 证书

开启 CF 域名模式时，HY2 默认使用 `CUSTOM_DOMAIN` 作为服务器名称，复用该域名的 Let's Encrypt 证书。DNS 记录保持灰云 / DNS only，HY2 的 UDP 端口需直接连接 VPS；普通 Cloudflare 橙云不能代理这条 UDP 链路。

不启用域名模式时，必须准备证书、对应私钥及匹配的服务器名称：

```bash
HY2_CERT_PATH="/绝对路径/fullchain.pem"
HY2_KEY_PATH="/绝对路径/privkey.pem"
HY2_SERVER_NAME="证书中包含的域名"
HY2_INSECURE="false"
```

已启用 HY2 且使用自动派生的 CF 证书路径时，停用域名模式前需先关闭 HY2，或在「配置入口协议」中明确填写证书、私钥和服务器名称这三项。仅停用 CF 不会自动生成替代证书。三项留空时会继续跟随 CF 域名；自定义的非空值保持为显式配置。

证书和私钥必须在服务器存在且可由 sing-box 读取。不自动签发自签证书，也不默认关闭验证。使用自签证书且客户端不能验证其信任链时，可明确设置 `HY2_INSECURE=true`，导出的 HY2 客户端配置会跳过证书验证；这会降低服务器身份校验强度。CF 管理的证书续期后会通过项目 hook 重启运行中的 sing-box。自行管理的证书需要自行安排续期并重启 sing-box 使新证书生效；请单独备份这些外部证书和私钥，节点备份不会复制任意外部路径的证书内容。

## 导出与客户端

现有 Mihomo / Clash Meta 订阅会包含所有已启用入口，以及原有 VLESS 中转入口。刷新订阅后在客户端选择对应协议节点即可；客户端需支持所选协议。

```bash
cd /opt/SuRelay
sudo bash bootstrap.sh --phase output-nodes
```

本地文件仍使用兼容名称：

- `/root/surelay-vless.txt`：各协议分享链接，可能包含 `vless://`、`hysteria2://`、`socks5://`、`ss://` 和 `http://`。
- `/root/surelay-clash.yaml`：可导入的完整 Mihomo 配置。
- `/root/surelay-nodes.txt`：节点字段与分享链接。

不同客户端对 SOCKS5 和 HTTP URI 的识别方式不同；无法导入时使用 Mihomo YAML，或按节点字段手工填写。需要 sing-box 客户端 JSON 时可另外指定输出文件：

```bash
sudo python3 scripts/11-output-nodes.py \
  --config-env config.env \
  --generator scripts/08-generate-singbox-config.py \
  --singbox-out /root/surelay-client.json
```

以上文件和链接都包含完整认证信息，权限应保持 `600`。本机监听检查通过后，还需用实际客户端验证公网 TCP/UDP、证书和认证；TCP 端口测试无法证明 HY2 的 UDP 链路可用。
