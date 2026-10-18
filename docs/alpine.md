# Alpine 3.19 部署

Alpine 3.19 使用 apk、OpenRC 和 musl。安装、维护、`-6` 和回滚使用同一套项目入口；不需要安装 systemd。当前系统版本门禁只放行 Alpine 3.19.x，其他 Alpine 版本需另行验证。

## 前提与入口

- 以 root 运行，使用已经启动 OpenRC 的 VPS/虚拟机镜像。
- `/etc/apk/repositories` 启用同一个 3.19 分支的 `main` 和 `community`。脚本不混入 edge，也不自动替换镜像源。
- x86_64 或 aarch64；能访问 Alpine 软件源、GitHub Release；自有域名模式还需要 PyPI、Cloudflare 和 ACME 网络访问。
- 小内存主机至少需要 160 MiB 内存；128 MiB 主机必须先准备足够的 Swap（建议 512 MiB 以上）。安装器会在 SSH 加固前检查内存限制，不满足条件时直接停止。
- UFW 需要实际的 netfilter 权限。受限 LXC/容器若没有这些权限，会在防火墙阶段失败，不能宣称已完成防护。

默认 ash 不能直接使用 README 中 Debian/Ubuntu 的 Bash 进程替换入口。先执行：

```sh
apk add --no-cache bash curl ca-certificates
curl -fsSL https://raw.githubusercontent.com/suyi-92/SuRelay/main/install.sh -o /tmp/surelay-install.sh
bash /tmp/surelay-install.sh
```

本地尚未发布的修改应复制整个项目到服务器，再在该目录执行 `bash install.sh`；远程入口只会取得已推送的分支内容。安装过程中仍须在第二个终端验证 root 公钥登录后，才关闭密码登录。

## 平台差异

需要卸载旧部署后重新配置时，用 `bash install.sh --reset`。它会先显示清单、确认并核验备份，再清理项目配置及 OpenRC 专用服务；范围与恢复说明见 [重置安装](reset-install.md)。

| 项目 | Alpine 行为 |
| --- | --- |
| `INSTALL_SINGBOX_METHOD=auto` | 选择 `release`。从 SagerNet 官方 Release 下载固定的 sing-box 1.14.3 Linux musl 归档，按架构检查固定 SHA256；替换前运行版本检查和现有配置检查。没有隐式升级到 latest。 |
| sing-box 服务 | 程序位于 `/usr/local/bin/sing-box`，通过 `/etc/init.d/sing-box` 和 `/etc/conf.d/sing-box` 启动；OpenRC supervise-daemon 负责进程和有限次数的失败重启。 |
| 订阅 | `/etc/init.d/surelay-subscription` 与对应 `conf.d` 保存启动参数；保留原有 token、监听地址和权限。 |
| SSH/SFTP | 使用 `sshd` OpenRC 服务和 shadow 用户管理。SFTP 账户使用丢弃的随机密码代替锁定密码标记，因为非 PAM OpenSSH 会拒绝锁定账户的公钥；最终 Match 策略仍只允许公钥和 internal-sftp。 |
| fail2ban | 使用 `polling` 文件后端。默认检查 `/var/log/messages`，可通过 `FAIL2BAN_SSH_LOG` 指定实际日志。安装前发送一条 authpriv 探针，确认日志确实可读；没有现有 syslog 服务时启动系统自带的 syslog。 |
| UFW | 安装 OpenRC 服务和 IPv6 iptables 工具；先放行 SSH，再应用实际入口和订阅端口，启用 IPv6 时同步 `IPV6=yes`，添加开机启动。 |
| Nginx | `NGINX_FALLBACK_CONF=auto` 使用 `/etc/nginx/http.d/surelay.conf`。自定义路径会链接到 `http.d`，`nginx -t` 通过后才重载。 |
| 证书 | `/opt/surelay-certbot` 独立 Python 环境安装固定版本的 Certbot、ACME 和 Cloudflare 插件（4.0.0）。`requirements/alpine-certbot.txt` 固定完整依赖的官方文件 URL 和 SHA256，跳过版本索引解析、依赖搜索、缓存和预编译；原生依赖使用 musl wheel，仅 Cloudflare 的小型纯 Python 源码包在本地打包。安装后运行 `pip check`，不修改系统 Python 包。每日 cron 执行续期，成功后通过 OpenRC 重载 Nginx。 |

Debian/Ubuntu 的 `auto` 仍使用原有 APT 和 systemd 流程。`apt`、`233boy` 不能用于 Alpine；从其他机器复制来的旧配置应先修改安装方式。

## 检查与维护

```sh
rc-status default
rc-service sshd status
rc-service sing-box status
rc-service surelay-subscription status
rc-service fail2ban status
fail2ban-client status sshd
ufw status verbose
tail -n 80 /var/log/surelay/sing-box.log
tail -n 80 /var/log/surelay/surelay-subscription.log
tail -n 80 /var/log/messages
```

自有域名模式另检查 `rc-service nginx status`、`rc-service crond status` 和 `/opt/surelay-certbot/bin/certbot certificates`。首次签发、实际 Cloudflare DNS 权限和公网端口连通性必须用部署者的真实域名验证。

### 小内存主机从证书安装失败处继续

`Killed` / 退出码 137 表示进程收到 SIGKILL。结合 cgroup 的 `memory.max` 和 `memory.events` 判断是否发生 OOM；进程退出后的 `free` 余量不能代表安装时的峰值。不要仅凭 137 就自动创建 swap，受限容器可能无权启用，且需要先核对磁盘余量。

128 MiB Alpine 主机可以先确认并启用 Swap，再重新执行安装：

```sh
free -m
cat /sys/fs/cgroup/memory.max /sys/fs/cgroup/memory.events 2>/dev/null || true
dd if=/dev/zero of=/swapfile bs=1M count=512
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
free -m
```

如果 `swapon` 被拒绝，说明 VPS/LXC 没有启用 Swap 的权限，需要在服务商面板增加内存或 Swap；不要继续重复安装。

更新项目中的 `scripts/00-platform.sh` 和 `requirements/alpine-certbot.txt` 后，如果已经完成 sing-box、订阅和 DNS，而失败停在 Certbot 安装，可沿用现有配置继续：

```sh
cd /opt/SuRelay &&
bash bootstrap.sh --phase custom-domain --verbose &&
bash bootstrap.sh --phase firewall --verbose &&
bash bootstrap.sh --phase validate --verbose &&
bash bootstrap.sh --phase output-nodes
```

此流程不重复重置或轮换节点身份。`custom-domain` 会重新核对 DNS，再安装证书依赖、签发证书和配置 Nginx；任一步失败就停止。无需删除现有 Certbot 环境，依赖检查会验证安装是否完整。创建虚拟环境时关闭 virtualenv 的后台周期更新，避免额外下载进程占用内存；已有缓存的新版本构建工具通过依赖锁中的 `packaging` 兼容。

增删出口、刷新订阅、增加 IPv6 和回滚仍按 README 对应阶段执行。回滚恢复 UFW 时同步恢复已有的 `/etc/default/ufw` 备份并重载；显式禁用 UFW 时也移除 OpenRC 默认运行级别中的自启。默认回滚范围仍是 SSH、sing-box 配置和可选防火墙，不卸载全部依赖或证书服务。

## 开发验证

`python3 -m unittest discover -s tests` 覆盖系统识别、平台配置、apk/systemd/OpenRC 分派、参数引用、日志投递检查和现有节点/SSH 回归。

`tests/alpine-certbot-lowmem.sh` 在全新、可丢弃的 Alpine 3.19 容器中验证 Certbot 冷安装、重复安装、Cloudflare 插件发现和 OOM 计数。还会通过 `tests/fixtures/alpine-certbot-seed.txt` 安装实际故障主机使用的 pip 26.2.1 / setuptools 84.0.0 / wheel 0.48.0，复现缺少 `packaging` 的失败，并验证安装器能修复已有环境。`packaging` 已列入正式依赖锁，覆盖新版 wheel 的运行依赖。

需要预装 Bash、只读挂载项目，并设置 `RRB_TEST_DISPOSABLE=yes`；使用 cgroup v2，建议限制为 160 MiB 且禁止 swap。该检查不启动系统服务，不修改 DNS，不申请真实证书。更新锁定依赖时必须同时更新对应 URL/哈希并重跑此检查；aarch64 wheel 仍需该架构的实机验证。

`tests/alpine-smoke.sh` 只能在**可丢弃的 Alpine 3.19 系统**中以 root 执行。它修改 SSH、账户、防火墙和服务，不能在正式服务器运行。需要预装 Bash、OpenSSH、Python 3 并启动 OpenRC；项目中不能已有 `config.env`。显式设置 `RRB_TEST_DISPOSABLE=yes` 后，脚本验证两阶段 SSH 公钥登录、实际包安装、节点身份保持、订阅、IPv6 成功/失败恢复，以及本地 VLESS/Reality 完整流量往返。重启后的服务恢复需要另行检查；容器内验证不替代真实 VPS 的外部 IPv6、安全组及 Cloudflare/ACME 验证。

随后可在同一测试环境运行 `tests/alpine-maintenance-smoke.sh`，验证 SFTP、普通回滚、订阅停用/恢复、真实 Certbot 插件安装、测试证书下的 Nginx TLS 反代和续期 hook。重启该测试环境后运行 `tests/alpine-boot-check.sh` 检查自启、身份保持、双栈订阅及 SSH/SFTP。这两个脚本也要求 `RRB_TEST_DISPOSABLE=yes`，证书测试不发起真实 DNS 修改或 ACME 签发。

### 2026-10-12 验证记录

| 环境/检查 | 结果 |
| --- | --- |
| Ubuntu 24.04 WSL，自动化测试 | 96 项：88 通过，8 项因缺少对应原生测试依赖跳过。 |
| Alpine 3.19.9 x86_64，OpenRC 0.52.1，自动化测试 | 96 项：95 通过，仅真实 dpkg 安装测试跳过。包含真实 OpenSSH 配置回归。 |
| 全新 Alpine 隔离容器，PID 1 为 init/OpenRC | 完整 smoke 通过：真实包安装、两阶段 SSH 公钥登录、VLESS/Reality 流量往返、重复安装身份保持、UFW 双栈规则、订阅、IPv6 修改失败恢复。 |
| Alpine 维护与重启 | maintenance 和 boot-check 通过：SFTP、普通回滚、订阅停启、Certbot 插件、测试证书 TLS 反代、cron/hook，以及重启后的 8 个服务与双栈订阅恢复。 |
| Certbot 小内存安装修复 | Alpine 3.19.9 x86_64，160 MiB / 无 swap：实际冷安装、重复安装、`pip check`、插件加载通过；回归运行的 `oom` / `oom_kill` 为 0，环境占约 52 MiB。Ubuntu WSL 的 13 项平台回归及修改脚本的 ShellCheck 通过。未发起真实 DNS-01 签发。 |
| Certbot 新版构建工具兼容 | 相同 160 MiB / 无 swap 环境：冷安装、重复安装、pip 26.2.1 / setuptools 84.0.0 / wheel 0.48.0 缺失 `packaging` 的复现及修复通过；关闭 virtualenv 后台更新后 `oom` / `oom_kill` 为 0，最终环境约 50 MiB。13 项平台回归和 ShellCheck 通过。 |

小内存修复测试中，旧安装命令在隔离容器内也能完成，但出现内存上限回收事件；未完整复现用户主机上的 137。用户主机的 256 MiB 限额及历史 OOM 计数说明存在内存压力，不能据此把全部历史 OOM 都归因于 Certbot。锁定直接下载地址减少安装时的版本解析和缓存开销，不代表整套服务在任意 256 MiB 主机上都已完成验收。

尚未做 aarch64 实机、真实 VPS 外部 IPv6/云安全组、真实 Cloudflare DNS-01 签发与到期续期测试；Debian/Ubuntu 本次运行既有回归和平台分派测试，未重新进行完整 VPS 安装。
