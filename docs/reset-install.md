# 重置安装

普通 `bash install.sh` 沿用已有配置和节点身份；存在未完成的引导交接记录时，会先按记录续跑。`bash install.sh --reset` 用于卸载旧部署后重新安装：重新填写配置、清空出口列表、重新生成 UUID、Reality keypair、short-id 和订阅 token；未手动指定的新协议密码也会随 UUID 更新。

## 开始引导式节点布置（`--redeploy`）

在 `sr` 主菜单选择 **开始引导式节点布置**，或在已更新的项目目录以 root 执行：

```bash
cd /opt/SuRelay
bash install.sh --redeploy
```

该入口用于重新设置 SuRelay 管理的全部部署属性，包含名称、地址/协议设置、出口、节点身份与订阅 token、CF 凭据、项目防火墙规则、登录防护设置及管理员公钥来源。新向导不沿用旧配置；SSH、公网主入口、订阅及静态页内部端口重新生成随机建议值。管理员公钥必须明确录入，允许填写原公钥；SSH 端口仍必须与旧监听不同。执行前先准备好本次使用的私钥及对应公钥。

这里的“关闭全部端口”指关闭旧 SuRelay 监听和本项目创建的放行规则。旧代理、订阅和项目自定义业务入口先关闭；当前 SSH 所需的旧端口与公钥会保留到本次录入的公钥通过新端口验证，再撤下旧项目 SSH 入口。其他软件的规则、无项目归属的手工公钥、系统 SSH 主机密钥、Cloudflare 远程 DNS 记录、云厂商安全组和 NAT 映射保留。本机项目证书和凭据按归属清理，不撤销远端证书。

流程如下：

1. 只读识别项目文件、规则及 SSH 来源，展示清理范围和数量摘要（`RRB_VERBOSE=true` 可看完整清单）；输入大写 `REDEPLOY` 才继续，取消或 EOF 时旧部署不变。确认后完整清单保存到备份目录的私有 `plan.txt`。
2. 创建并核验仅 root 可读的备份，记录当前 SSH 配置、公钥、socket override 和 UFW 规则；备份未通过时不开始清理。
3. 停用并删除旧节点/订阅和项目专用配置，删除旧项目自定义安全设置及不承担当前 SSH 的项目端口规则。SSH 交接记录保存到 `/root/surelay-backups/redeploy-handoff.json`。
4. 打开完整向导；分组页用 **← 上一步 / 下一步 →** 直接切换，文字菜单用 `<` / `>`。修改字段后草稿保留，最终总览明确确认后才写新配置。
5. 先准备本次录入的公钥并开启新 SSH 端口，保留旧登录方式。另开终端，明确通过本次公钥和新端口登录成功后，再回答 SSH 交接提示 `[y/N]`；直接回车会暂停并保留旧入口。确认后才应用最终认证策略、移除未被本次选用且可确认属于旧部署的公钥和旧项目 SSH 端口规则。本次选用的公钥按此次输入保存，避免旧行的限制选项与新端口验证时的配置不同。
6. 部署新节点、订阅、域名与安全配置，检查监听和规则。旧节点和订阅已失效，客户端需导入新输出。

旧公钥归属根据旧 `config.env` 和项目公钥登记记录判定。早期版本通过 `sr` 改过公钥后可能没有保留完整来源；无法确认归属的历史公钥会在预览说明并保留，不按“root 的全部公钥”一并删除。本次没有重新录入的旧节点密钥和已确认的项目公钥不会被新配置自动复用。

交接记录区分准备完成（`prepared`）、旧项目已清理（`removed`）、交接完成（`finalized`）和失败（`failed`）。清理后在向导取消、或在 SSH 验证处暂停时，旧代理不会自动恢复；保留当前 SSH 窗口，运行 `sr install`，或再次选择“开始引导式节点布置”/运行 `sr redeploy`，都会接续尚未完成的引导，不重复清理。只有 `removed` 允许接续未完成交接；`finalized` 表示上次已完成，普通安装沿用配置，明确重新布置则开始新的清理。记录仍为 `prepared`、`failed` 或无法通过校验时，后续操作会停止并显示恢复信息；先检查备份和失败原因，不手工删除记录来跳过门禁。

本次清理还会清除可确认由项目管理的 `sshd` jail 封禁和维护参数，其他 fail2ban jail 保留。若备份中有单独的 `fail2ban.sqlite3`，它用于核对旧状态；恢复时应只处理对应 `sshd` jail，不直接覆盖共享数据库中的其他 jail 或后续新状态。

`--redeploy` 必须执行完整安装，不能与更新、卸载、普通 `--reset`、IPv6 维护或 `RRB_RUN_PHASES=false` 合用。原有 `--reset` 和卸载边界保持不变，见下文。

## 普通重置的入口与顺序

在已更新代码的服务器项目目录，以 root 执行：

```bash
cd /opt/SuRelay
bash install.sh --reset
```

Ubuntu/Debian 的远程入口：

```bash
bash <(wget -qO- https://raw.githubusercontent.com/suyi-92/SuRelay/main/install.sh) --reset
```

Alpine 3.19 的远程入口：

```sh
apk add --no-cache bash curl ca-certificates
curl -fsSL https://raw.githubusercontent.com/suyi-92/SuRelay/main/install.sh -o /tmp/surelay-install.sh
bash /tmp/surelay-install.sh --reset
```

远程入口下载已发布的 main；尚未推送的功能需复制完整项目后使用本地入口。

1. 检查系统、现有配置路径、服务与文件归属，显示实际删除清单。
2. 输入大写 `RESET` 确认；其他输入或 EOF 取消，旧部署不变。
3. 在 `/root/surelay-backups/reset-*` 创建仅 root 可读的 `files.tar.gz` 和 `manifest.json`。逐文件核验备份后才停止服务；磁盘满、文件读取失败等错误会阻止卸载。
4. 停止并禁用旧 sing-box/订阅服务；删除已列出的项目文件，清理带项目标记的代理/订阅 UFW 规则，重载共享 Nginx/fail2ban。OpenRC 从所有已登记运行级别移除专用服务。
5. 打开分组向导，重新填写配置、公钥和默认开启的 CF 域名静态页信息，可返回修改；SSH、公网主入口、订阅与 Nginx 内部端口重新生成高位随机建议值，可手动覆盖。确认后创建空出口列表。检查系统、更新 root 公钥后，保留当前 SSH 策略并要求第二个终端验证；确认后替换最终 SSH 策略及旧项目 SFTP Match 块，继续安装。

卸载后旧代理会中断，直到重装完成。确认后若中断填写或安装失败，备份保留；已有完整新 `config.env` 时用普通 `bash install.sh` 继续，不必再次重置。公钥验证拒绝后，先解决 SSH 登录，再设置 `RRB_RESET_INSTALL=true bash install.sh` 继续最终策略清理。

## 普通 `--reset` 的清理边界

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

重置支持默认目录，以及具有 `surelay` / `surelay-*` 名称的专用 state 目录；密钥和订阅必须位于该目录内。自定义 sing-box 路径、项目外出口输入、无归属标记的同名 Nginx 配置、指向其他位置的目录符号链接，会在卸载前报错。先核实并迁回支持的路径再执行，不能用批量删除绕过。证书共享检查覆盖 Nginx、Apache、HTTPD、HAProxy 和 Caddy 配置；其他应用共享证书时，应先迁出该项目的证书/凭据管理。

## 只预览

不传 `--apply` 时，只读取清单和防火墙规则，不备份、停止服务或删除：

```bash
cd /opt/SuRelay
python3 scripts/17-reset-install.py --project-dir "$PWD"
```

## 失败与恢复

失败会打印备份目录并停止后续安装。`manifest.json` 记录删除清单、旧服务是否存在/运行、防火墙删除命令及卸载状态。备份包含旧项目数据，以及卸载前的 SSH/UFW 文件；含有私钥和 token，不要上传或公开。

如果要放弃新配置、恢复旧部署，保留当前 SSH 会话，先检查指定的备份内容（将目录替换为实际值）：

```bash
backup=/root/surelay-backups/reset-实际目录
tar -tzf "$backup/files.tar.gz"
cat "$backup/manifest.json"
```

确认恢复的是这台机器本次重置前的文件后，以 root 执行 `tar -xzpf "$backup/files.tar.gz" -C /`。这会覆盖同名文件；如已完成部分新域名安装，应先检查并移除新生成而旧备份中没有的项目配置。此备份不适合直接交给 `bootstrap.sh --phase rollback`。

恢复后，systemd 执行 `systemctl daemon-reload`，按 manifest 为原先启用的专用服务重新 `enable --now`；OpenRC 按原运行级别重新 `rc-update add` 并 `rc-service ... start`。检查 `sing-box check -c /etc/sing-box/config.json`、`nginx -t`、SSH 公钥登录和订阅，再按原状态重载 UFW。不要在未检查的情况下批量重启 SSH、关闭防火墙或覆盖其他站点。

## 开发验证

`python3 -m unittest tests.test_reset_install` 验证备份/停止失败门禁、取消与 EOF、systemd/OpenRC 服务分派、文件与防火墙清理边界、重复重置、自定义路径和符号链接拒绝、证书共享引用及 SSH 确认顺序。

2026-10-12：Ubuntu 24.04 WSL 自动化回归 112 项，104 通过、8 项因缺少对应原生依赖跳过；包括 16 项重置测试及真实伪终端确认测试，修改的 Shell 入口通过 ShellCheck。尚未完成真实 Alpine/systemd 主机的完整卸载重装及真实证书重新签发验证；文件隔离测试不替代 VPS 实机验收。
