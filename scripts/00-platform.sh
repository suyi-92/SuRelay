#!/usr/bin/env bash
# Platform boundary. Sourced by 00-lib.sh; package/service changes must use run().

is_alpine() (
  local ID=""
  # shellcheck disable=SC1090
  source "${RRB_OS_RELEASE_FILE:-/etc/os-release}"
  [[ "$ID" == alpine ]]
)

pkg_install() {
  if ! is_alpine; then
    apt_install "$@"
    return
  fi
  require_root
  local package
  local -a packages=()
  for package in "$@"; do
    case "$package" in
      openssh-server) packages+=(openssh openssh-server-common-openrc shadow) ;;
      fail2ban) packages+=(fail2ban fail2ban-openrc) ;;
      ufw) packages+=(ufw ufw-openrc ip6tables) ;;
      nginx) packages+=(nginx nginx-openrc) ;;
      *) packages+=("$package") ;;
    esac
  done
  run apk add --no-cache --wait 60 "${packages[@]}" </dev/null
}

require_service_manager() {
  if is_alpine; then
    have rc-service && have rc-update && [[ -x /sbin/openrc-run ]] \
      || die "Alpine 需要 OpenRC；请使用带 OpenRC 的 VPS/虚拟机系统镜像。"
    if ! is_dry_run; then
      [[ -r /run/openrc/softlevel ]] || die "OpenRC 尚未启动；不能在普通容器/chroot 中部署系统服务。"
    fi
  else
    have systemctl || die "Debian/Ubuntu 需要 systemd。"
    if ! is_dry_run; then
      systemctl show-environment >/dev/null 2>&1 || die "systemd 尚未运行。"
    fi
  fi
}

service_enable() {
  if is_alpine; then run rc-update add "$1" default; else run systemctl enable "$1"; fi
}

service_restart() {
  if is_alpine; then run rc-service "$1" restart; else run systemctl restart "$1"; fi
}

service_active() {
  if is_alpine; then rc-service "$1" status >/dev/null 2>&1; else systemctl is-active --quiet "$1"; fi
}

service_wait_active() {
  local attempt
  # OpenSSH briefly removes its pidfile while re-executing after SIGHUP.
  for ((attempt=0; attempt<5; attempt++)); do
    if service_active "$1"; then return 0; fi
    sleep 1
  done
  return 1
}

prepare_sftp_password() {
  if ! is_alpine; then
    run passwd -l "$SFTP_USER"
  elif is_dry_run; then
    log "DRY-RUN: set an unknown random SFTP password hash; ssh-final disables password authentication"
  else
    # Non-PAM OpenSSH rejects *all* login methods for locked accounts. Use a
    # discarded random password; the SFTP Match policy permits public keys only.
    local password_hash
    password_hash="$(openssl rand -base64 48 | openssl passwd -6 -stdin)"
    usermod -p "$password_hash" "$SFTP_USER" >>"$LOG_FILE" 2>&1
  fi
}

service_exists() {
  if is_alpine; then
    [[ -x "/etc/init.d/$1" ]]
  else
    [[ "$(systemctl show "$1" --property=LoadState --value 2>/dev/null)" == loaded ]]
  fi
}

service_disable() {
  if is_alpine; then
    if service_active "$1"; then run rc-service "$1" stop; fi
    run rc-update del "$1" default
  else
    run systemctl disable --now "$1"
  fi
}

service_reload() {
  if is_alpine; then
    run rc-service "$1" reload || service_restart "$1"
  else
    run systemctl reload "$1" || service_restart "$1"
  fi
}

service_daemon_reload() {
  if ! is_alpine; then run systemctl daemon-reload; fi
}

install_openrc_service() {
  local name="$1" executable="$2" workdir="$3" check_config="$4"
  shift 4
  backup_path "/etc/init.d/$name"
  backup_path "/etc/conf.d/$name"
  run mkdir -p /etc/init.d /etc/conf.d
  # shlex.quote protects both the conf.d assignments and OpenRC's argument parser.
  python3 - "$executable" "$workdir" "$check_config" "$@" <<'PY' | write_root_file "/etc/conf.d/$name" 0600
import shlex
import sys
values = dict(command=sys.argv[1], directory=sys.argv[2], rrb_check_config=sys.argv[3],
              command_args=shlex.join(sys.argv[4:]))
for key, value in values.items():
    print(f"{key}={shlex.quote(value)}")
PY
  write_root_file "/etc/init.d/$name" 0755 <"$RRB_PROJECT_DIR/templates/rrb-service.openrc"
}

resolve_platform_config() {
  if [[ "$INSTALL_SINGBOX_METHOD" == auto ]]; then
    if is_alpine; then INSTALL_SINGBOX_METHOD=release; else INSTALL_SINGBOX_METHOD=apt; fi
  fi
  if [[ "$NGINX_FALLBACK_CONF" == auto ]]; then
    if is_alpine; then
      NGINX_FALLBACK_CONF=/etc/nginx/http.d/reality-relay-bootstrap.conf
    else
      NGINX_FALLBACK_CONF=/etc/nginx/sites-available/reality-relay-bootstrap.conf
    fi
  fi
}

check_platform_install_method() {
  if is_alpine; then
    [[ "$INSTALL_SINGBOX_METHOD" == release && "$USE_233BOY_INSTALLER" != true ]] \
      || die 'Alpine 请设置 INSTALL_SINGBOX_METHOD="auto" 或 "release"，并关闭 USE_233BOY_INSTALLER；APT/233boy 仅用于 Debian/Ubuntu。'
    release_arch >/dev/null
  elif [[ "$INSTALL_SINGBOX_METHOD" == release ]]; then
    die 'release 安装方式当前仅用于 Alpine；Debian/Ubuntu 请使用 auto、apt 或 233boy。'
  fi
}

# Official musl archives, pinned with GitHub release asset SHA256 digests.
# Keep the version and both digests together when upgrading, then test on musl.
release_arch() {
  case "$(uname -m)" in
    x86_64) printf 'amd64\n' ;;
    aarch64|arm64) printf 'arm64\n' ;;
    *) die "Alpine 二进制安装目前只支持 x86_64/aarch64，当前：$(uname -m)" ;;
  esac
}

install_singbox_release() (
  local version=1.14.3 arch archive checksum temp binary
  arch="$(release_arch)"
  case "$arch" in
    amd64) checksum=41fc81fd041134d4d7d850a928d7d2cde17a7d6563953f406b22d32a68bfab1f ;;
    arm64) checksum=12d6d67a4edf0dd54590865b1f4f79cd09ae043a02d648e9d1b5fccc8446ee07 ;;
  esac
  archive="sing-box-${version}-linux-${arch}-musl"
  if is_dry_run; then
    log "DRY-RUN: download official $archive.tar.gz, verify SHA256, install /usr/local/bin/sing-box"
    return 0
  fi
  temp="$(mktemp -d)"
  trap 'rm -rf -- "$temp"' EXIT
  curl --fail --show-error --silent --location --retry 2 --connect-timeout 15 --max-time 180 \
    "https://github.com/SagerNet/sing-box/releases/download/v${version}/${archive}.tar.gz" -o "$temp/archive.tar.gz"
  printf '%s  %s\n' "$checksum" "$temp/archive.tar.gz" | sha256sum -c - >>"$LOG_FILE" \
    || die "sing-box 下载校验失败；未替换现有程序。"
  tar -xzf "$temp/archive.tar.gz" -C "$temp" "$archive/sing-box"
  binary="$temp/$archive/sing-box"
  "$binary" version >>"$LOG_FILE" 2>&1 || die "官方 sing-box 无法在当前系统运行。"
  # Check the existing configuration before replacing a running installation.
  if [[ -s "$SINGBOX_CONFIG_PATH" ]]; then
    "$binary" check -c "$SINGBOX_CONFIG_PATH" >>"$LOG_FILE" 2>&1 \
      || die "现有 sing-box 配置与固定版本不兼容；未替换程序，请检查日志。"
  fi
  backup_path /usr/local/bin/sing-box
  install -d -m 0755 /usr/local/bin
  install -m 0755 "$binary" /usr/local/bin/sing-box.rrb-new
  mv -f /usr/local/bin/sing-box.rrb-new /usr/local/bin/sing-box
)

prepare_ssh_log() {
  # Alpine has no journal. Verify an actual syslog delivery before configuring a
  # file jail; an empty, manually-created log file is not proof of SSH logging.
  : "${FAIL2BAN_SSH_LOG:=/var/log/messages}"
  if ! service_active syslog && ! service_active rsyslog && ! service_active syslog-ng; then
    pkg_install busybox-openrc
    service_enable syslog
    service_restart syslog
  fi
  if is_dry_run; then
    log "DRY-RUN: verify authpriv delivery to $FAIL2BAN_SSH_LOG"
    return
  fi
  local marker="rrb-sshd-log-check-$$-$RANDOM" attempt
  logger -p authpriv.notice -t sshd "$marker"
  for ((attempt=0; attempt<5; attempt++)); do
    if [[ -r "$FAIL2BAN_SSH_LOG" ]] && grep -Fq "$marker" "$FAIL2BAN_SSH_LOG"; then return 0; fi
    sleep 1
  done
  die "SSH authpriv 日志未写入 $FAIL2BAN_SSH_LOG；请按 syslog 配置设置 FAIL2BAN_SSH_LOG 后重试。"
}

ensure_ufw_ipv6() {
  [[ "$ENABLE_IPV6_LISTEN" == true ]] || return 0
  local defaults=/etc/default/ufw
  backup_path "$defaults"
  if is_dry_run; then
    log "DRY-RUN: set IPV6=yes in $defaults"
  else
    [[ -f "$defaults" ]] || die "找不到 UFW 配置 $defaults"
    if grep -Eq '^[[:space:]]*IPV6=' "$defaults"; then
      sed -i -E 's/^[[:space:]]*IPV6=.*/IPV6=yes/' "$defaults"
    else
      printf '\nIPV6=yes\n' >>"$defaults"
    fi
  fi
}

install_certbot() {
  if is_alpine; then
    pkg_install python3 py3-virtualenv
    CERTBOT_BIN=/opt/reality-relay-certbot/bin/certbot
    if [[ ! -x "$CERTBOT_BIN" ]]; then
      run python3 -m virtualenv /opt/reality-relay-certbot
    fi
    # Version-index parsing can exhaust a 256 MiB VPS before any wheel installs.
    # Direct hashed artifacts bound resolver memory; native dependencies use musl
    # wheels. Only the small pure-Python Cloudflare source needs a local build.
    run /opt/reality-relay-certbot/bin/pip install --no-input --disable-pip-version-check \
      --no-index --no-deps --no-build-isolation --no-cache-dir --no-compile --require-hashes \
      -r "$RRB_PROJECT_DIR/requirements/alpine-certbot.txt"
    run /opt/reality-relay-certbot/bin/pip check
  else
    pkg_install certbot python3-certbot-dns-cloudflare
    CERTBOT_BIN=certbot
  fi
}
