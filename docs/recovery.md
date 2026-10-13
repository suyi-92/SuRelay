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

## 完整 SSH 配置回滚

```bash
sudo CONFIRM_ROLLBACK=yes RESTORE_FULL_SSH_BACKUP=yes bash bootstrap.sh --phase rollback
```

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
