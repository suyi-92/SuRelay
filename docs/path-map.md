# 路径总表

本文按默认配置说明脚本会读取、生成或修改哪些路径。若在 `config.env` 中改了 `RRB_STATE_DIR`、`NGINX_FALLBACK_ROOT`、`NGINX_FALLBACK_CONF`、`SINGBOX_CONFIG_PATH` 等变量，以实际配置为准。

下表是新安装的 SuRelay 路径。旧部署迁移代码目录后，原有 `/etc/reality-relay-bootstrap`、`reality-relay-subscription` 服务及证书路径继续兼容，以保留节点身份和运行状态。

Alpine 的平台专用路径（OpenRC、`http.d`、Certbot 虚拟环境、cron 和服务日志）见 [Alpine 部署说明](alpine.md)。下表中的 systemd 和 `sites-available` 路径适用于 Debian/Ubuntu。

## 总览

| 路径 | 类别 | 阶段 | 默认权限 | 敏感 | 用途与处理建议 |
|---|---|---|---|---|---|
| `/root/surelay/` | 项目目录 | 一键安装或手动 clone | 由系统默认决定 | 部分敏感 | 放项目代码、`config.env`、输入 CSV。部署和重跑阶段需要保留。 |
| `/root/surelay/config.env` | 主配置 | `install.sh` 或手动创建 | `600`（一键脚本） | 是 | 可能包含 Cloudflare token、域名、端口、SSH 公钥等。不要公开。 |
| `/root/surelay/home-proxies.csv` | 输入文件 | `install.sh` 或手动创建 | 由系统默认决定 | 是 | 家宽出口账号、密码、端口来源。运行 `singbox` 阶段时会复制到 state 目录。 |
| `/root/surelay/upstream-nodes.txt` | 输入文件 | `install.sh` 或手动创建 | 由系统默认决定 | 是 | 可选上游节点分享链接或 CSV。运行 `singbox` 阶段时会复制到 state 目录。 |
| `/etc/surelay/` | state 根目录 | `singbox` | `700` | 是 | 本项目运行态数据根目录，root-only。不要整目录公开或随意删除。 |
| `/etc/surelay/vless-uuid.txt` | 节点密钥材料 | `singbox` | `600` | 是 | VLESS UUID，也是默认订阅 token。删除后重跑会生成新 token，旧节点失效。 |
| `/etc/surelay/reality-private.key` | Reality 密钥 | `singbox` | `600` | 是 | 服务端 Reality 私钥。删除或轮换会让旧节点失效。 |
| `/etc/surelay/reality-public.key` | Reality 公钥 | `singbox` | `600` | 是 | 客户端节点需要的 public key。虽然不是私钥，也不要和完整节点文件一起公开。 |
| `/etc/surelay/reality-short-id.txt` | Reality short-id | `singbox` | `600` | 是 | 客户端节点参数。删除或轮换会让旧节点失效。 |
| `/etc/surelay/home-proxies.csv` | state 输入副本 | `singbox` | `600` | 是 | 从项目目录复制来的家宽出口配置。项目目录 CSV 缺失时可继续复用。 |
| `/etc/surelay/upstream-nodes.txt` | state 输入副本 | `singbox` | `600` | 是 | 从项目目录复制来的上游节点配置。项目目录文件缺失时可继续复用。 |
| `/etc/surelay/cloudflare.ini` | Cloudflare 凭据 | `custom-domain` | `600` | 是 | certbot `dns-cloudflare` credentials 文件。包含 API token，不要公开。 |
| `/etc/surelay/subscription/` | 订阅目录 | `subscription` | `700` | 是 | 内置订阅服务根目录。包含按当前节点生成的订阅文件。 |
| `/etc/surelay/subscription/all/nodes.txt` | 订阅文件 | `subscription` | `600` | 是 | 文本节点列表。含 UUID、public key、short-id。 |
| `/etc/surelay/subscription/all/clash.yaml` | 订阅文件 | `subscription` | `600` | 是 | Clash/Mihomo 配置。含完整节点信息。 |
| `/etc/surelay/subscription/all/clashmeta.yaml` | 订阅文件 | `subscription` | `600` | 是 | token 保护订阅返回的 ClashMeta 内容。 |
| `/etc/sing-box/config.json` | sing-box 配置 | `singbox` | 由 sing-box/系统决定 | 是 | 最终服务端配置，包含 Reality 私钥和出站信息。会先备份再覆盖。 |
| `/etc/sing-box/conf.233boy.disabled.TIMESTAMP` | 233boy 隔离目录 | `singbox`（仅启用 233boy） | 原目录权限 | 可能 | 避免 233boy 默认配置和本项目配置冲突。确认不需要恢复后可手动清理。 |
| `/etc/ssh/sshd_config` 中的 `surelay SSH policy` 标记块 | SSH 当前阶段策略 | `ssh-phase1` / `ssh-final` | 保留原文件权限 | 否 | 放在主配置开头，优先于服务商配置；phase1 仅开启公钥认证，final 经确认后禁用密码认证。块外内容在策略更新时按原字节保留。 |
| `/etc/ssh/sshd_config.d/00-surelay-{phase1,hardening}.conf` | 旧版 SSH drop-in | 旧版安装 | `0644` | 否 | 新版执行任一 SSH 阶段时先备份再移除这两个旧文件，其他 drop-in 保留。 |
| `/root/.ssh/authorized_keys` | root 公钥 | `ssh-phase1` | `600` | 是 | root 登录公钥。不要随意清空，避免锁机。 |
| `/etc/fail2ban/jail.d/sshd.local` | fail2ban 配置 | `fail2ban` | `0644` | 否 | SSH jail 配置。会先备份 `/etc/fail2ban`。 |
| `/etc/ufw/` | UFW 配置目录 | `ufw` / `firewall` | 系统默认 | 否 | 脚本会放行 SSH、Reality 入口端口和必要订阅端口。默认回滚不整目录恢复 UFW。 |
| `/etc/nginx/sites-available/surelay.conf` | Nginx 站点配置 | `custom-domain` | `0644` | 否 | 本机 `127.0.0.1:8443` HTTPS fallback 站点配置。 |
| `/etc/nginx/sites-enabled/surelay.conf` | Nginx 站点链接 | `custom-domain` | symlink | 否 | 指向 `sites-available` 中的站点配置。 |
| `/var/www/surelay-fallback/` | fallback 网站根目录 | `custom-domain` | 由系统默认决定 | 否 | 由脚本写入轻量静态站。重跑 `custom-domain` 会覆盖默认生成的文件。 |
| `/var/www/surelay-fallback/index.html` | fallback 首页 | `custom-domain` | `0644` | 否 | 默认展示 `Digital Notes` 静态页面。 |
| `/var/www/surelay-fallback/robots.txt` | fallback 静态文件 | `custom-domain` | `0644` | 否 | 默认允许爬虫访问。 |
| `/var/www/surelay-fallback/favicon.ico` | fallback 静态文件 | `custom-domain` | `0644` | 否 | 默认空文件，用于避免浏览器 favicon 请求 404。 |
| `/etc/letsencrypt/live/$CUSTOM_DOMAIN/` | 证书目录 | `custom-domain` | certbot 管理 | 是 | Let's Encrypt 证书和私钥。不要手动改权限或移动。 |
| `/etc/letsencrypt/renewal-hooks/deploy/surelay-nginx.sh` | 证书续期 hook | `custom-domain` | `0755` | 否 | 证书续期后自动 reload/restart Nginx。 |
| `/etc/systemd/system/surelay-subscription.service` | systemd 服务 | `subscription` | `0644` | 否 | 内置订阅 HTTP 服务。启用自有域名且 `SUBSCRIPTION_PORT` 留空时只监听本机，由 Nginx 反代。 |
| `/usr/local/lib/surelay/subscription-server.py` | 订阅服务程序 | `subscription` | `0755` | 否 | 安装后的订阅服务脚本。 |
| `/root/surelay-nodes.txt` | 手动输出文件 | `output-nodes` | `600` | 是 | 纯文本节点列表。默认不主动生成，除非执行 `output-nodes`。 |
| `/root/surelay-clash.yaml` | 手动输出文件 | `output-nodes` | `600` | 是 | Clash/Mihomo 配置。默认不主动生成，除非执行 `output-nodes`。 |
| `/root/surelay-backups/backup-*` | 备份目录 | 多个阶段 | `700` | 可能 | 修改系统配置前生成。可能包含旧 SSH、UFW、sing-box、Nginx 配置，清理前确认不需要回滚。 |
| `/var/log/surelay.log` | 日志 | 所有阶段 | 系统默认 | 可能 | 脚本避免打印核心密钥，但日志仍可能包含 IP、域名、命令输出。不要公开原始日志。 |
| `/root/surelay-cache/233boy-install.sh` | 可选缓存 | `singbox`（仅启用 233boy） | `700` 文件 | 否 | 233boy 安装脚本缓存。未启用 233boy 时不会生成；确认不用后可清理。 |

## 清理建议

- 不要删除 `/etc/surelay/`、`/etc/sing-box/config.json`、`/root/.ssh/authorized_keys`、`/etc/letsencrypt/live/$CUSTOM_DOMAIN/`，除非你明确要重置部署。
- `/root/surelay-nodes.txt` 和 `/root/surelay-clash.yaml` 是可重新生成的，但包含完整节点信息，删除前确认已备份到你的客户端或订阅管理工具。
- `/root/surelay-backups/backup-*` 可以在确认服务稳定后按时间清理，但至少保留最近一次可用备份更稳。
- `/var/www/surelay-fallback/` 可以按你的需要改成自己的静态站；注意重跑 `custom-domain` 阶段会重新写入默认页面。
- `/var/log/surelay.log` 可以按常规日志策略轮转或清理；排障前先保留。
