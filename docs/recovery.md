# 恢复与回滚

## SSH 故障原则

不要关闭当前还能登录的 SSH 窗口。先执行：

```bash
sudo /usr/sbin/sshd -t
sudo /usr/sbin/sshd -T -C user=root,host=localhost,addr=127.0.0.1
```

如果新窗口无法登录，可在旧窗口回滚：

```bash
sudo CONFIRM_ROLLBACK=yes bash bootstrap.sh --phase rollback
```

默认回滚会移除 `/etc/ssh/sshd_config` 开头的本项目 SSH 策略标记块，以及旧版的两个项目 drop-in；服务商配置会重新参与生效值选择。完整 SSH 备份恢复包含主配置中的策略块，可恢复到所选备份对应的阶段。

默认回滚只移除上述认证策略，不会将新装随机生成的 SSH 端口迁回旧端口。端口迁移使用独立的 `SuRelay SSH settings` 块，原来的 `Port` 可能已加注释；需要恢复监听端口时，请使用本次迁移前的 SSH 配置备份。保留当前窗口，先确认备份对应的旧端口仍有安全组和 NAT 映射，再操作。

## 完整 SSH 配置回滚

```bash
sudo CONFIRM_ROLLBACK=yes RESTORE_FULL_SSH_BACKUP=yes bash bootstrap.sh --phase rollback
```

应指定迁移前、包含 `etc/ssh/sshd_config` 的实际阶段备份目录，例如通过下文的 `ROLLBACK_BACKUP_DIR`；最新备份不一定早于端口迁移。SSH 迁移另行生成的 `surelay-*` 备份使用 `manifest.json` 与数字文件，不能直接作为上述命令的目录。

Ubuntu 若使用 `ssh.socket`，监听端口还由 `/etc/systemd/system/ssh.socket.d/surelay.conf`（旧安装可能为 `reality-relay-bootstrap.conf`）控制。上述完整配置回滚不会恢复这个 socket override；需按 SSH 迁移备份的 `manifest.json` 恢复它原先的内容，原先不存在时移除本次新增文件，再执行 `systemctl daemon-reload` 并重启 `ssh.socket` 与 SSH 服务。先运行 `sshd -t`，保留当前会话，并在新窗口验证实际旧端口恢复后再关闭当前窗口。

## UFW 导致失联风险

如果当前旧窗口还能用，可以临时禁用 UFW：

```bash
sudo CONFIRM_ROLLBACK=yes ROLLBACK_DISABLE_UFW=yes bash bootstrap.sh --phase rollback
```

或者手动：

```bash
sudo ufw disable
```

## sing-box 配置回滚

默认 rollback 会尝试恢复最近备份中的 `/etc/sing-box/config.json`，恢复后执行 `sing-box check` 并重启。

## 233boy conf 恢复

如果之前使用了 233boy 并希望恢复其 `/etc/sing-box/conf`：

```bash
sudo CONFIRM_ROLLBACK=yes RESTORE_233BOY_CONF=yes bash bootstrap.sh --phase rollback
```


## 指定备份目录

默认使用最近一次备份。如需指定备份目录：

```bash
sudo CONFIRM_ROLLBACK=yes ROLLBACK_BACKUP_DIR=/root/surelay-backups/backup-xxxx bash bootstrap.sh --phase rollback
```


## 从备份恢复 UFW 配置

确认要整目录恢复 `/etc/ufw` 时：

```bash
sudo CONFIRM_ROLLBACK=yes RESTORE_UFW_FROM_BACKUP=yes bash bootstrap.sh --phase rollback
```
