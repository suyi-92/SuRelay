#!/usr/bin/env bash
# Run after rebooting the disposable alpine-maintenance-smoke.sh fixture.
set -Eeuo pipefail
[[ "${RRB_TEST_DISPOSABLE:-}" == yes && $EUID == 0 && -f /tmp/rrb-test-key ]] || exit 2
cd "$(dirname "$0")/.."
source scripts/00-lib.sh
load_config
for service in sshd sing-box surelay-subscription fail2ban syslog ufw nginx crond; do
  service_wait_active "$service" || die "重启后服务未就绪：$service"
done
rc-status default
LC_ALL=C ufw status | grep '^Status: active'
ufw status | grep '(v6)'
singbox_check
sha256sum -c /tmp/rrb-identity.sha256
ssh -i /tmp/rrb-test-key -o BatchMode=yes root@127.0.0.1 'test "$(id -u)" = 0'
sftp -b /tmp/rrb-sftp-batch -i /tmp/rrb-test-key -o BatchMode=yes sftpuser@127.0.0.1
token="$(cat /etc/surelay/vless-uuid.txt)"
curl --noproxy '*' -fsS "http://127.0.0.1:51040/sub/$token?target=ClashMeta" -o /tmp/rrb-boot-sub.yaml
curl --noproxy '*' -gfsS "http://[::1]:51040/sub/$token?target=ClashMeta" -o /tmp/rrb-boot-sub-v6.yaml
cmp /tmp/rrb-boot-sub.yaml /tmp/rrb-boot-sub-v6.yaml
curl --noproxy '*' -kfsS --resolve edge.example.test:8443:127.0.0.1 \
  https://edge.example.test:8443/healthz | grep '^ok'
fail2ban-client status sshd
test -x /etc/periodic/daily/surelay-certbot
printf 'ALPINE_BOOT_OK\n'
