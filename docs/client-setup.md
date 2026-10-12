# 客户端配置与测试

默认部署后启用订阅服务，不主动生成 `/root` 下的节点文件。默认订阅配置：

```bash
ENABLE_SUBSCRIPTION_SERVER="true"
SUBSCRIPTION_PORT=""
SUBSCRIPTION_INTERNAL_PORT="51040"
SUBSCRIPTION_TARGET="ClashMeta"
SUBSCRIPTION_BASE_URL=""
```

未启用自有域名时，`SUBSCRIPTION_PORT` 留空会自动回退为 `SUBSCRIPTION_INTERNAL_PORT`，也就是默认 `51040`。

订阅地址：

```text
http://服务器IP:51040/sub/<VLESS_UUID>?target=ClashMeta
```

`<VLESS_UUID>` 来自 `/etc/reality-relay-bootstrap/vless-uuid.txt`。内置订阅服务是 HTTP；如需 HTTPS，请在外层接入带证书的反向代理，并把 `SUBSCRIPTION_BASE_URL` 设置为反代外部地址。

如果启用了自有域名：

```bash
ENABLE_CUSTOM_DOMAIN="true"
CUSTOM_DOMAIN="edge.example.com"
```

主节点的 `server` 和 Reality `servername` 使用 `CUSTOM_DOMAIN`；配置 IPv6 并开启监听时，还会为每个出口增加 `-IPv6` 字面量备用节点，其 SNI 保持相同。订阅服务只监听本机，外部订阅地址为：

```text
https://edge.example.com/sub/<VLESS_UUID>?target=ClashMeta
```

自有域名模式下 `SUBSCRIPTION_PORT` 默认留空，只显示上面的域名 HTTPS 订阅。显式填写 `SUBSCRIPTION_PORT` 会额外开放 `http://SERVER_IP:SUBSCRIPTION_PORT/sub/...`，简洁输出仍只显示首选域名订阅。

如需在服务器本地额外生成节点文件：

```bash
sudo bash bootstrap.sh --phase output-nodes
/root/reality-relay-bootstrap-nodes.txt
/root/reality-relay-bootstrap-clash.yaml
/root/reality-relay-bootstrap-vless.txt
```

这些文件包含 VLESS UUID、Reality public key、short-id 等节点信息，权限为 600，不要公开。

如果机器最初只配置了 IPv4，后来只想补出一套 IPv6 字面量本地节点，可以运行：

```bash
sudo bash install.sh -6 2001:db8::10
```

它会保留现有 UUID、Reality key、端口与出口映射，刷新已启用的订阅并更新 CF A/AAAA，同时在 `/root` 生成 `reality-relay-bootstrap-v6-vless.txt`、`reality-relay-bootstrap-v6-nodes.txt` 和 `reality-relay-bootstrap-v6-clash.yaml`。v6 Mihomo/Clash 文件显式启用 `ipv6: true` 与 `udp: true`；VLESS UDP 通过现有 TCP/Reality 会话承载，不需要再开放 UDP 监听端口。

如果同时配置了 IPv4 和 IPv6，两个订阅链接都返回同一份完整节点；IPv4 节点名称保持不变，IPv6 节点名称追加 `-IPv6`。
如果 Clash Verge 或浏览器无法导入 IPv6 字面量订阅地址，有 IPv4 时直接用 IPv4 订阅地址即可，它会返回同一份完整节点。

`SUBSCRIPTION_TARGET` 当前只生成 ClashMeta/Mihomo YAML；旧配置里的其它 target 会警告并按 ClashMeta 处理，旧 URL target 不再兼容。

当节点包含 IPv6 地址，或 `PROXY_IP_VERSION` 为 `ipv6`/`dual` 时，生成的 Mihomo 配置启用 `ipv6: true`。客户端 IPv6 入口不依赖 VPS 出站是否优先 IPv4。CF 灰云域名不能绕过被封的 IPv4，客户端必须具备可用 IPv6；必要时选择 `-IPv6` 节点，或通过 `curl -6` 下载域名订阅。参见 [README](../README.md#ipv6cf-域名与-ipv4-不通)。

## Clash/Mihomo 节点字段

每个节点类似：

```yaml
name: home-01
type: vless
server: 服务器IP
port: 51043
uuid: "自动生成的 VLESS UUID"
flow: xtls-rprx-vision
tls: true
servername: www.microsoft.com
client-fingerprint: chrome
udp: true
reality-opts:
  public-key: "自动生成的 Reality public key"
  short-id: "自动生成的 short-id"
alpn:
  - h2
  - http/1.1
```

443 节点：

- `MODE_443=direct`：使用 `SERVER_ALIAS`
- `MODE_443=smart`：使用 `SERVER_ALIAS`
- 启用自有域名时：节点 server 使用 `CUSTOM_DOMAIN`

CSV 行节点：

- `home-01,51043,...` -> `home-01`

## Windows PowerShell 测端口

```powershell
Test-NetConnection 服务器IP -Port 443
Test-NetConnection 服务器IP -Port 51043
```

## Clash/Mihomo 测出口 IP

导入 `/root/reality-relay-bootstrap-clash.yaml` 后，在本地执行：

```powershell
curl.exe -x http://127.0.0.1:7890 https://ifconfig.me
```

切换不同节点，出口 IP 应该对应：

- `SERVER_ALIAS` 对应的节点（例如 `my-vps`）：中转机自身公网 IP
- 家宽 `tag` 对应的节点（例如 `home-xx`）：对应家宽代理出口 IP

## 常见问题

1. 端口不通：检查服务商安全组、UFW、sing-box 监听。
2. 只有某个家宽节点失败：先在服务器执行 `validate`，看该家宽代理本身是否可用。
3. VLESS+Reality 握手失败：确认客户端 `servername`、`public-key`、`short-id`、`uuid` 与服务端输出一致。
4. HTTP 家宽代理通常不支持 UDP：默认 `CLIENT_UDP=true`；如果对应出口不支持 UDP，可显式改为 `CLIENT_UDP=false`。
