# 重置安装

普通 `bash install.sh` 沿用已有配置和节点身份。`bash install.sh --reset` 用于卸载旧部署后重新安装：重新填写配置、清空出口列表、重新生成 UUID、Reality keypair、short-id 和订阅 token。

## 入口与顺序

在已更新代码的服务器项目目录，以 root 执行：

```bash
cd /opt/reality-relay-bootstrap
bash install.sh --reset
```

Ubuntu/Debian 的远程入口：

```bash
bash <(wget -qO- https://raw.githubusercontent.com/suyi-92/reality-relay-bootstrap/main/install.sh) --reset
```

Alpine 3.19 的远程入口：

```sh
apk add --no-cache bash curl ca-certificates
curl -fsSL https://raw.githubusercontent.com/suyi-92/reality-relay-bootstrap/main/install.sh -o /tmp/reality-relay-install.sh
bash /tmp/reality-relay-install.sh --reset
```

远程入口下载已发布的 main；尚未推送的功能需复制完整项目后使用本地入口。

1. 检查系统、现有配置路径、服务与文件归属，显示实际删除清单。
2. 输入大写 `RESET` 确认；其他输入或 EOF 取消，旧部署不变。
3. 在 `/root/reality-relay-bootstrap-backups/reset-*` 创建仅 root 可读的 `files.tar.gz` 和 `manifest.json`。逐文件核验备份后才停止服务；磁盘满、文件读取失败等错误会阻止卸载。
4. 停止并禁用旧 sing-box/订阅服务；删除已列出的项目文件，清理带项目标记的代理/订阅 UFW 规则，重载共享 Nginx/fail2ban。OpenRC 从所有已登记运行级别移除专用服务。
5. 重新收集配置、公钥、可选域名信息，创建空出口列表。检查系统、更新 root 公钥后，保留当前 SSH 策略并要求第二个终端验证；确认后替换最终 SSH 策略及旧项目 SFTP Match 块，继续安装。

卸载后旧代理会中断，直到重装完成。确认后若中断填写或安装失败，备份保留；已有完整新 `config.env` 时用普通 `bash install.sh` 继续，不必再次重置。公钥验证拒绝后，先解决 SSH 登录，再设置 `RRB_RESET_INSTALL=true bash install.sh` 继续最终策略清理。

## 清理边界

| 类别 | 处理 |
| --- | --- |
| 项目输入 | 删除 `config.env`、`home-proxies.csv`、`upstream-nodes.txt`，重装重新生成。保留源码和 `.git`。 |
| 节点数据 | 删除四个身份文件、state 中的输入副本、Cloudflare 凭据、已知订阅文件及默认 `/root` 节点导出（含 IPv6）。未知用户文件保留并提示。 |
| 专用服务 | 停用 sing-box 和订阅。删除项目 OpenRC 脚本/conf.d、订阅 systemd unit/服务程序、Alpine 专用 sing-box 二进制；旧 233boy 优先二进制及对应 unit override 一并清除。APT 软件包自带的 sing-box unit 保留但停用，重装重新启用。 |
| Nginx/fail2ban | 删除具有项目归属标记的站点、站点链接、sshd jail，以及项目 fallback 的三个生成文件；共享服务和其他配置保留。 |
| 证书 | 仅当 Certbot renewal 配置记录的 Cloudflare 凭据在本次删除清单内，才删除对应证书 lineage（renewal/live/archive）；发现常见 Web 服务配置还在引用时停止。其他证书与 ACME 账户保留，不撤销远端证书。 |
| 续期 | 删除项目 Nginx deploy hook、Alpine 每日续期任务及专用 Certbot Python 环境；不卸载系统 Certbot、不停用共享 cron。 |
| 防火墙 | 仅删除带项目代理/订阅注释的规则；SSH 注释规则、当前连接端口及旧 SSH 端口保留，不禁用 UFW、不清空全局规则。 |
| SSH/用户 | 卸载时保留 SSH 服务、策略、root 公钥、系统账户及 SFTP 上传目录；新公钥验证成功后才替换项目 SSH/SFTP 策略。不会删除历史公钥。 |
| 其他 | 保留软件包、Cloudflare DNS、服务商安全组、历史备份和排障日志。隔离的旧 233boy 配置及第三方管理工具不作递归清理。 |

重置支持默认目录，以及具有 `reality-relay-bootstrap` / `reality-relay-bootstrap-*` 名称的专用 state 目录；密钥和订阅必须位于该目录内。自定义 sing-box 路径、项目外出口输入、无归属标记的同名 Nginx 配置、指向其他位置的目录符号链接，会在卸载前报错。先核实并迁回支持的路径再执行，不能用批量删除绕过。证书共享检查覆盖 Nginx、Apache、HTTPD、HAProxy 和 Caddy 配置；其他应用共享证书时，应先迁出该项目的证书/凭据管理。

## 只预览

不传 `--apply` 时，只读取清单和防火墙规则，不备份、停止服务或删除：

```bash
cd /opt/reality-relay-bootstrap
python3 scripts/17-reset-install.py --project-dir "$PWD"
```

## 失败与恢复

失败会打印备份目录并停止后续安装。`manifest.json` 记录删除清单、旧服务是否存在/运行、防火墙删除命令及卸载状态。备份包含旧项目数据，以及卸载前的 SSH/UFW 文件；含有私钥和 token，不要上传或公开。

如果要放弃新配置、恢复旧部署，保留当前 SSH 会话，先检查指定的备份内容（将目录替换为实际值）：

```bash
backup=/root/reality-relay-bootstrap-backups/reset-实际目录
tar -tzf "$backup/files.tar.gz"
cat "$backup/manifest.json"
```

确认恢复的是这台机器本次重置前的文件后，以 root 执行 `tar -xzpf "$backup/files.tar.gz" -C /`。这会覆盖同名文件；如已完成部分新域名安装，应先检查并移除新生成而旧备份中没有的项目配置。此备份不适合直接交给 `bootstrap.sh --phase rollback`。

恢复后，systemd 执行 `systemctl daemon-reload`，按 manifest 为原先启用的专用服务重新 `enable --now`；OpenRC 按原运行级别重新 `rc-update add` 并 `rc-service ... start`。检查 `sing-box check -c /etc/sing-box/config.json`、`nginx -t`、SSH 公钥登录和订阅，再按原状态重载 UFW。不要在未检查的情况下批量重启 SSH、关闭防火墙或覆盖其他站点。

## 开发验证

`python3 -m unittest tests.test_reset_install` 验证备份/停止失败门禁、取消与 EOF、systemd/OpenRC 服务分派、文件与防火墙清理边界、重复重置、自定义路径和符号链接拒绝、证书共享引用及 SSH 确认顺序。

2026-10-12：Ubuntu 24.04 WSL 自动化回归 112 项，104 通过、8 项因缺少对应原生依赖跳过；包括 16 项重置测试及真实伪终端确认测试，修改的 Shell 入口通过 ShellCheck。尚未完成真实 Alpine/systemd 主机的完整卸载重装及真实证书重新签发验证；文件隔离测试不替代 VPS 实机验收。
