#!/usr/bin/env bash
# Follow-up to alpine-smoke.sh, only in the same disposable fixture.
set -Eeuo pipefail
[[ "${RRB_TEST_DISPOSABLE:-}" == yes && $EUID == 0 ]] || exit 2
[[ "$(cat /etc/alpine-release)" == 3.19.* && -f /tmp/rrb-test-key ]] || exit 2
cd "$(dirname "$0")/.."
export RRB_SHOW_RESULT=false

# Exercise non-PAM SFTP authentication, including user/group creation.
sed -i 's/^ENABLE_SFTP_USER=.*/ENABLE_SFTP_USER="true"/' config.env
bash bootstrap.sh --phase ssh-phase1
CONFIRM_ROOT_KEY_LOGIN=yes bash bootstrap.sh --phase ssh-final
printf 'pwd\n' >/tmp/rrb-sftp-batch
sftp -b /tmp/rrb-sftp-batch -i /tmp/rrb-test-key -o BatchMode=yes sftpuser@127.0.0.1 >/tmp/rrb-sftp-result
grep -q '/upload' /tmp/rrb-sftp-result

# Disable/re-enable the subscription using the same public entry point.
sed -i 's/^ENABLE_SUBSCRIPTION_SERVER=.*/ENABLE_SUBSCRIPTION_SERVER="false"/' config.env
bash bootstrap.sh --phase subscription
if rc-service surelay-subscription status; then exit 1; fi
sed -i 's/^ENABLE_SUBSCRIPTION_SERVER=.*/ENABLE_SUBSCRIPTION_SERVER="true"/' config.env
bash bootstrap.sh --phase subscription

# Exercise the normal rollback service adapter without changing production hosts.
backup="$(find /root/surelay-backups -maxdepth 1 -type d -name '*-singbox-install' | sort | tail -n 1)"
CONFIRM_ROLLBACK=yes ROLLBACK_BACKUP_DIR="$backup" bash bootstrap.sh --phase rollback
ssh -i /tmp/rrb-test-key -o BatchMode=yes root@127.0.0.1 'test "$(id -u)" = 0'
CONFIRM_ROOT_KEY_LOGIN=yes bash bootstrap.sh --phase ssh-final
bash bootstrap.sh --phase singbox

# Real Certbot/plugin install and Nginx/cron integration; use a local fixture
# certificate instead of contacting Cloudflare/ACME with fake credentials.
cp config.env /tmp/rrb-before-domain.env
python3 - <<'PY'
import re
import shlex
from pathlib import Path
p = Path('config.env')
text = p.read_text()
values = dict(ENABLE_CUSTOM_DOMAIN='true', CUSTOM_DOMAIN='edge.example.test',
              CLOUDFLARE_ZONE_NAME='example.test', LE_EMAIL='admin@example.test',
              REALITY_SERVER_NAME='edge.example.test', REALITY_HANDSHAKE_SERVER='127.0.0.1',
              REALITY_HANDSHAKE_PORT='8443')
for key, value in values.items():
    text = re.sub(rf'^{key}=.*$', f'{key}={shlex.quote(value)}', text, flags=re.M)
p.write_text(text)
source = Path('scripts/15-setup-custom-domain.sh').read_text()
functions = source.rsplit('\nensure_cloudflare_credentials\n', 1)[0]
Path('/tmp/rrb-domain-functions.sh').write_text(functions + '''
"$CERTBOT_BIN" plugins --text | grep dns-cloudflare
write_fallback_site
write_nginx_site
write_renew_hook
reload_nginx
verify_fallback
''')
PY
mkdir -p /etc/letsencrypt/live/edge.example.test
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout /etc/letsencrypt/live/edge.example.test/privkey.pem \
  -out /etc/letsencrypt/live/edge.example.test/fullchain.pem \
  -days 1 -subj /CN=edge.example.test >/dev/null 2>&1
bash bootstrap.sh --phase subscription
RRB_PROJECT_DIR="$PWD" bash /tmp/rrb-domain-functions.sh
token="$(cat /etc/surelay/vless-uuid.txt)"
curl --noproxy '*' -kfsS --resolve edge.example.test:8443:127.0.0.1 \
  "https://edge.example.test:8443/sub/$token?target=ClashMeta" -o /tmp/rrb-domain-sub.yaml
grep -q 'type: vless' /tmp/rrb-domain-sub.yaml
bash /etc/letsencrypt/renewal-hooks/deploy/surelay-nginx.sh
rc-service crond status
test -x /etc/periodic/daily/surelay-certbot
mv /tmp/rrb-before-domain.env config.env
bash bootstrap.sh --phase subscription
sha256sum -c /tmp/rrb-identity.sha256
printf 'ALPINE_MAINTENANCE_OK\n'
