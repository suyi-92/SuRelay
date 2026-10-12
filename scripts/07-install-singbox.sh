#!/usr/bin/env bash
# 安装 sing-box，准备 VLESS UUID、Reality keypair、short-id、出口文件和备份。
set -Eeuo pipefail
PHASE_NAME="singbox-install"
source "${RRB_PROJECT_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}/scripts/00-lib.sh"
require_root
load_config
require_supported_os

apt_install ca-certificates curl gnupg openssl python3 iproute2

backup_path /etc/sing-box/config.json || true
[[ -d /etc/sing-box/conf ]] && backup_path /etc/sing-box/conf || true
backup_path "$RRB_STATE_DIR" || true

if port_in_use_by_other "$DIRECT_PORT"; then
  die "DIRECT_PORT=$DIRECT_PORT 已被非 sing-box 进程占用。请停止 nginx/apache/其他服务或修改 DIRECT_PORT。"
fi

ensure_state_dirs() {
  local path
  if is_dry_run; then
    log "DRY-RUN: mkdir -p $RRB_STATE_DIR /etc/sing-box and secret-file parent directories"
  else
    mkdir -p "$RRB_STATE_DIR" /etc/sing-box
    for path in "$VLESS_UUID_PATH" "$REALITY_PRIVATE_KEY_PATH" "$REALITY_PUBLIC_KEY_PATH" "$REALITY_SHORT_ID_PATH"; do
      mkdir -p "$(dirname "$path")"
    done
    chmod 700 "$RRB_STATE_DIR"
  fi
}

ensure_state_dirs

STATE_CSV="$RRB_STATE_DIR/home-proxies.csv"
if [[ -f "$CSV_PATH" ]]; then
  if is_dry_run; then
    log "DRY-RUN: install -m 600 $CSV_PATH $STATE_CSV"
  else
    install -m 600 -o root -g root "$CSV_PATH" "$STATE_CSV"
  fi
elif [[ -f "$STATE_CSV" ]]; then
  info "CSV_PATH 不存在，继续使用已保存的家宽 CSV：$STATE_CSV"
else
  die "找不到家宽 CSV：$CSV_PATH，也找不到 $STATE_CSV"
fi

STATE_UPSTREAM_NODES="$RRB_STATE_DIR/upstream-nodes.txt"
if [[ -f "$UPSTREAM_NODES_PATH" ]]; then
  if is_dry_run; then
    log "DRY-RUN: install -m 600 $UPSTREAM_NODES_PATH $STATE_UPSTREAM_NODES"
  else
    install -m 600 -o root -g root "$UPSTREAM_NODES_PATH" "$STATE_UPSTREAM_NODES"
  fi
elif [[ -f "$STATE_UPSTREAM_NODES" ]]; then
  info "UPSTREAM_NODES_PATH 不存在，继续使用已保存的上游节点文件：$STATE_UPSTREAM_NODES"
else
  info "未配置 upstream-nodes.txt，仅使用 home-proxies.csv 和 direct 出口。"
fi

python3 "$SCRIPT_DIR/08-generate-singbox-config.py" --config-env "$RRB_CONFIG_FILE" --check-only --quiet

install_by_apt() {
  info "使用 sing-box 官方 APT 源安装。"
  if is_dry_run; then
    log "DRY-RUN: add sing-box official apt source and apt-get install sing-box"
    return 0
  fi
  install -d -m 0755 /etc/apt/keyrings
  curl -fsSL https://sing-box.app/gpg.key -o /etc/apt/keyrings/sagernet.asc
  chmod a+r /etc/apt/keyrings/sagernet.asc
  cat >/etc/apt/sources.list.d/sagernet.sources <<'APT'
Types: deb
URIs: https://deb.sagernet.org/
Suites: *
Components: *
Enabled: yes
Signed-By: /etc/apt/keyrings/sagernet.asc
APT
  apt_install sing-box
}

install_by_233boy() {
  [[ "${CONFIRM_USE_233BOY_INSTALLER:-}" == "yes" ]] || die "启用 233boy 安装器需要显式 CONFIRM_USE_233BOY_INSTALLER=yes。默认不建议依赖 233boy。"
  info "将调用 233boy/sing-box 安装脚本，仅作为 core/管理工具基础；随后会隔离其默认配置并由本项目接管。"
  if is_dry_run; then
    log "DRY-RUN: download and run 233boy install.sh, then disable /etc/sing-box/conf"
    return 0
  fi
  mkdir -p /root/reality-relay-bootstrap-cache
  curl -fsSL https://github.com/233boy/sing-box/raw/main/install.sh -o /root/reality-relay-bootstrap-cache/233boy-install.sh
  chmod 700 /root/reality-relay-bootstrap-cache/233boy-install.sh
  if [[ "$RRB_VERBOSE" == "true" ]]; then
    bash /root/reality-relay-bootstrap-cache/233boy-install.sh 2>&1 | tee -a "$LOG_FILE"
  else
    bash /root/reality-relay-bootstrap-cache/233boy-install.sh >>"$LOG_FILE" 2>&1
  fi
  local stamp
  stamp="$(date '+%Y%m%d-%H%M%S')"
  if [[ -d /etc/sing-box/conf ]]; then
    mv /etc/sing-box/conf "/etc/sing-box/conf.233boy.disabled.${stamp}"
    mkdir -p /etc/sing-box/conf
    chmod 755 /etc/sing-box/conf
    info "已隔离 233boy 默认 conf 目录：/etc/sing-box/conf.233boy.disabled.${stamp}"
  fi
}

if [[ "$USE_233BOY_INSTALLER" == "true" || "$INSTALL_SINGBOX_METHOD" == "233boy" ]]; then
  install_by_233boy
else
  install_by_apt
fi

bin="$(singbox_bin)"
if [[ -z "$bin" ]]; then
  if is_dry_run; then
    log "DRY-RUN: sing-box 尚未安装，跳过 version 和 Reality keypair 检查。"
  else
    die "sing-box 安装后仍找不到可执行文件。"
  fi
else
  "$bin" version >>"$LOG_FILE" 2>&1 || true
  info "sing-box 可执行文件已就绪：$bin"
fi

if [[ "$RESET_PROXY_KEYS" == "true" ]]; then
  info "RESET_PROXY_KEYS=true，将重置 VLESS UUID、Reality keypair 和 short-id。旧节点会失效。"
  for secret_path in "$VLESS_UUID_PATH" "$REALITY_PRIVATE_KEY_PATH" "$REALITY_PUBLIC_KEY_PATH" "$REALITY_SHORT_ID_PATH"; do
    [[ -e "$secret_path" ]] && backup_path "$secret_path" || true
    if is_dry_run; then
      log "DRY-RUN: rm -f $secret_path"
    else
      rm -f "$secret_path"
    fi
  done
fi

if [[ ! -s "$VLESS_UUID_PATH" ]]; then
  info "生成 VLESS UUID 并保存到 $VLESS_UUID_PATH（不会打印 UUID 到日志）。"
  if is_dry_run; then
    log "DRY-RUN: sing-box generate uuid > $VLESS_UUID_PATH"
  else
    umask 077
    "$bin" generate uuid > "$VLESS_UUID_PATH"
    chmod 600 "$VLESS_UUID_PATH"
  fi
else
  info "VLESS UUID 已存在，不会轮换：$VLESS_UUID_PATH"
  chmod 600 "$VLESS_UUID_PATH" || true
fi

if [[ ! -s "$REALITY_PRIVATE_KEY_PATH" || ! -s "$REALITY_PUBLIC_KEY_PATH" ]]; then
  info "生成 Reality keypair（不会打印私钥到日志）。"
  if is_dry_run; then
    log "DRY-RUN: sing-box generate reality-keypair > $REALITY_PRIVATE_KEY_PATH / $REALITY_PUBLIC_KEY_PATH"
  else
    umask 077
    pair="$("$bin" generate reality-keypair)"
    private_key="$(printf '%s\n' "$pair" | awk -F': ' 'tolower($1) ~ /private/ {print $2; exit}')"
    public_key="$(printf '%s\n' "$pair" | awk -F': ' 'tolower($1) ~ /public/ {print $2; exit}')"
    [[ -n "$private_key" && -n "$public_key" ]] || die "无法解析 sing-box generate reality-keypair 输出。"
    printf '%s\n' "$private_key" > "$REALITY_PRIVATE_KEY_PATH"
    printf '%s\n' "$public_key" > "$REALITY_PUBLIC_KEY_PATH"
    chmod 600 "$REALITY_PRIVATE_KEY_PATH" "$REALITY_PUBLIC_KEY_PATH"
  fi
else
  info "Reality keypair 已存在，不会轮换：$REALITY_PRIVATE_KEY_PATH / $REALITY_PUBLIC_KEY_PATH"
  chmod 600 "$REALITY_PRIVATE_KEY_PATH" "$REALITY_PUBLIC_KEY_PATH" || true
fi

if [[ ! -s "$REALITY_SHORT_ID_PATH" ]]; then
  info "生成 Reality short-id 并保存到 $REALITY_SHORT_ID_PATH。"
  if is_dry_run; then
    log "DRY-RUN: openssl rand -hex 8 > $REALITY_SHORT_ID_PATH"
  else
    umask 077
    openssl rand -hex 8 > "$REALITY_SHORT_ID_PATH"
    chmod 600 "$REALITY_SHORT_ID_PATH"
  fi
else
  info "Reality short-id 已存在，不会轮换：$REALITY_SHORT_ID_PATH"
  chmod 600 "$REALITY_SHORT_ID_PATH" || true
fi

info "sing-box 安装和 VLESS+Reality 密钥材料准备完成。"
