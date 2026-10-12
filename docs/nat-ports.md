# NAT 同号端口映射

适用于服务商已将公网 TCP 业务端口转发到容器内同号端口的 VPS。例如公网 SSH `24036 → 22`，业务端口 `24037 → 24037`、`24038 → 24038`。软件不能替服务商创建 NAT 映射。

| 用途 | 公网端口 | 容器内端口 |
| --- | --- | --- |
| SSH | 24036 | 22 |
| VLESS Reality 与域名 HTTPS 订阅 | 24037 | 24037 |
| 备用业务端口 | 24038 | 暂不使用 |
| Nginx fallback | 无需直接映射 | 127.0.0.1:8443 |
| 订阅 HTTP 服务 | 无需直接映射 | 127.0.0.1:51040 |

Cloudflare 记录保持 DNS only / 灰云，A 记录指向服务商公网 IPv4。DNS-01 证书签发不要求公网 80/443；已有证书可以继续使用。Reality 客户端节点和 HTTPS 订阅链接均使用 `24037`，不要给 SNI 域名追加端口。

## 已安装主机改端口

先更新到包含 NAT 端口支持的项目版本，再以 root 操作。保留当前 SSH 窗口，先备份配置：

```sh
cd /opt/reality-relay-bootstrap
git pull --ff-only origin main
cp -p config.env "config.env.before-nat-$(date +%Y%m%d-%H%M%S).bak"
nano config.env
```

修改以下项；域名、Cloudflare、UUID 和密钥配置沿用原值：

```sh
SSH_PORT="22"
DIRECT_PORT="24037"
SUBSCRIPTION_PORT=""
SUBSCRIPTION_INTERNAL_PORT="51040"
SUBSCRIPTION_BASE_URL=""
NGINX_FALLBACK_PORT="8443"
RESET_PROXY_KEYS="false"
```

上述示例以已有 `ENABLE_CUSTOM_DOMAIN="true"`、正常的本机 Nginx fallback 和订阅服务为前提。`SUBSCRIPTION_BASE_URL` 留空后会自动生成 `https://你的域名:24037`；若仍保留旧的 `https://你的域名`，程序会尊重该显式覆盖，链接就仍会访问公网 443。`SSH_PORT` 表示本机 sshd 的监听端口，不能填写公网映射端口 24036。

只更新代理配置、订阅和防火墙即可。下面先进行配置校验，通过后才写入并重启代理；不会重跑 SSH 加固、重新签发证书或生成密钥：

```sh
cd /opt/reality-relay-bootstrap &&
python3 scripts/08-generate-singbox-config.py --config-env config.env --check-only --quiet &&
python3 scripts/08-generate-singbox-config.py --config-env config.env --write --quiet &&
bash scripts/10-validate.sh --singbox-only --skip-proxy-tests --restart-singbox &&
bash bootstrap.sh --phase subscription --verbose &&
bash bootstrap.sh --phase firewall --verbose &&
bash bootstrap.sh --phase validate --verbose &&
bash bootstrap.sh --phase output-nodes
```

预期 sing-box 监听 `24037`，UFW 放行本机 `22/tcp` 和 `24037/tcp`。防火墙阶段会清理带有本项目标记且已不再使用的旧 `443` 规则；其他规则保留。未添加出口时无需开放 `24038`。

从客户端重新导入输出的 `vless://` 链接；更新订阅地址为新输出的带端口 HTTPS URL。旧的无端口订阅 URL 无法通过此 NAT 访问。

## 外部验收

从客户端电脑执行，域名替换为自己的真实域名：

```sh
ssh -p 24036 root@服务商公网IPv4
curl --fail --show-error --max-time 10 https://你的域名:24037/healthz
```

`healthz` 应返回 `ok`；随后刷新订阅并测试 Reality 节点实际联网。服务端的监听检查只能确认本机状态，不能证明服务商 NAT 映射或客户端链路已打通。脚本输出的通用 SSH 命令使用本机配置端口，NAT 主机应将客户端命令中的 `-p` 替换为服务商公网 SSH 端口。

若还有家宽或上游出口，每个入口也需要服务商映射对应端口；可把下一入口设置为 `24038`。不要把未分配的 51043 等端口当作公网可用端口。

## 恢复配置

需要撤回时，将上面备份的文件复制回 `config.env`，重新执行“只更新代理配置、订阅和防火墙”的整段命令即可恢复旧监听及项目防火墙规则。SSH 配置保持原状。

本说明仅覆盖业务端口同号映射。公网业务端口与内部监听端口不同时，当前节点生成器没有独立的公网端口字段；仅设置 `SUBSCRIPTION_BASE_URL` 不能改写节点端口。
