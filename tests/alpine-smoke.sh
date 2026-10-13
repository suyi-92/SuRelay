#!/usr/bin/env bash
# Destructive integration fixture: only run inside a disposable Alpine container/VM.
set -Eeuo pipefail
[[ "${RRB_TEST_DISPOSABLE:-}" == yes && $EUID == 0 ]] || {
  echo 'Requires root and RRB_TEST_DISPOSABLE=yes in an isolated disposable system.' >&2
  exit 2
}
[[ "$(cat /etc/alpine-release)" == 3.19.* ]] || exit 2
cd "$(dirname "$0")/.."
[[ ! -e config.env ]] || { echo 'Fixture requires a fresh checkout without config.env.' >&2; exit 2; }
export RRB_SHOW_RESULT=false
ssh-keygen -q -t ed25519 -N '' -f /tmp/rrb-test-key
ssh-keygen -A
cp config.example.env config.env
cp home-proxies.example.csv home-proxies.csv
cp upstream-nodes.example.txt upstream-nodes.txt
python3 - <<'PY'
import re
import shlex
from pathlib import Path
path = Path('config.env')
text = path.read_text()
updates = dict(SERVER_ALIAS='alpine-smoke', SERVER_IP='127.0.0.1', SERVER_IP_IPV4='127.0.0.1',
               ADMIN_PUBKEY=Path('/tmp/rrb-test-key.pub').read_text().strip(),
               REALITY_HANDSHAKE_SERVER='127.0.0.1', REALITY_HANDSHAKE_PORT='18443',
               REALITY_SERVER_NAME='www.example.com')
for key, value in updates.items():
    text = re.sub(rf'^{key}=.*$', f'{key}={shlex.quote(value)}', text, flags=re.M)
path.write_text(text)
for file in ['home-proxies.csv', 'upstream-nodes.txt']:
    p = Path(file)
    p.write_text(p.read_text().splitlines()[0] + '\n')
PY
bash bootstrap.sh --phase preflight
# Minirootfs locks root; give this disposable fixture an unknown random password.
usermod -p "$(openssl rand -hex 32 | openssl passwd -6 -stdin)" root
rc-service sshd restart
bash bootstrap.sh --phase ssh-phase1
ssh -i /tmp/rrb-test-key -o StrictHostKeyChecking=accept-new -o BatchMode=yes root@127.0.0.1 'test "$(id -u)" = 0'
CONFIRM_ROOT_KEY_LOGIN=yes bash bootstrap.sh --phase ssh-final
ssh -i /tmp/rrb-test-key -o BatchMode=yes root@127.0.0.1 'test "$(id -u)" = 0'
bash bootstrap.sh --phase fail2ban
bash bootstrap.sh --phase singbox
bash bootstrap.sh --phase firewall
bash scripts/10-validate.sh --skip-proxy-tests

# Idempotent install must retain all node identity files.
sha256sum /etc/surelay/{vless-uuid.txt,reality-private.key,reality-public.key,reality-short-id.txt} >/tmp/rrb-identity.sha256
bash bootstrap.sh --phase singbox
sha256sum -c /tmp/rrb-identity.sha256
token="$(cat /etc/surelay/vless-uuid.txt)"
curl --noproxy '*' -fsS "http://127.0.0.1:51040/sub/$token?target=ClashMeta" -o /tmp/rrb-subscription.yaml
grep -q 'type: vless' /tmp/rrb-subscription.yaml

# Force a failure after -6 modifies config.env; the existing listener must survive.
cp config.env /tmp/rrb-before-ipv6.env
cp /etc/sing-box/config.json /tmp/rrb-before-ipv6.json
cp home-proxies.csv /tmp/rrb-home-proxies.csv
printf 'bad,unsupported,example.com,1080,user,pass,tcp,51043\n' >>home-proxies.csv
if bash scripts/16-add-ipv6-node.sh --address 2001:db8::10; then
  echo 'Expected invalid proxy failure' >&2
  exit 1
fi
cmp config.env /tmp/rrb-before-ipv6.env
cmp /etc/sing-box/config.json /tmp/rrb-before-ipv6.json
mv /tmp/rrb-home-proxies.csv home-proxies.csv
rc-service sing-box status
bash scripts/16-add-ipv6-node.sh --address 2001:db8::10
sha256sum -c /tmp/rrb-identity.sha256
curl --noproxy '*' -gfsS "http://[::1]:51040/sub/$token?target=ClashMeta" -o /tmp/rrb-subscription-v6.yaml
grep -q '2001:db8::10' /tmp/rrb-subscription-v6.yaml

# Real VLESS/Reality round trip to a local HTTP origin, without external DNS/egress.
openssl req -x509 -newkey rsa:2048 -nodes -keyout /tmp/rrb-tls.key -out /tmp/rrb-tls.crt \
  -days 1 -subj /CN=www.example.com >/dev/null 2>&1
openssl s_server -accept 18443 -cert /tmp/rrb-tls.crt -key /tmp/rrb-tls.key -tls1_3 -alpn h2 -www >/tmp/rrb-tls.log 2>&1 &
tls_pid=$!
python3 -m http.server 19080 --bind 127.0.0.1 --directory /tmp >/tmp/rrb-http.log 2>&1 &
http_pid=$!
client_pid=''
trap 'kill "$tls_pid" "$http_pid" ${client_pid:+"$client_pid"} 2>/dev/null || true' EXIT
python3 - <<'PY'
import json
from pathlib import Path
state = Path('/etc/surelay')
read = lambda name: (state / name).read_text().strip()
config = {'inbounds': [{'type': 'mixed', 'listen': '127.0.0.1', 'listen_port': 19090}],
          'outbounds': [{'type': 'vless', 'server': '127.0.0.1', 'server_port': 443,
                         'uuid': read('vless-uuid.txt'), 'flow': 'xtls-rprx-vision',
                         'tls': {'enabled': True, 'server_name': 'www.example.com',
                                 'utls': {'enabled': True, 'fingerprint': 'chrome'},
                                 'reality': {'enabled': True, 'public_key': read('reality-public.key'),
                                             'short_id': read('reality-short-id.txt')}}}]}
Path('/tmp/rrb-client.json').write_text(json.dumps(config))
Path('/tmp/rrb-health.txt').write_text('RRB_REALITY_OK\n')
PY
sing-box run -c /tmp/rrb-client.json >/tmp/rrb-client.log 2>&1 &
client_pid=$!
for ((attempt=0; attempt<15; attempt++)); do
  if curl --noproxy '' --socks5-hostname 127.0.0.1:19090 --max-time 4 -fsS \
      http://127.0.0.1:19080/rrb-health.txt >/tmp/rrb-roundtrip; then break; fi
  sleep 1
done
grep -qx RRB_REALITY_OK /tmp/rrb-roundtrip
printf 'ALPINE_SMOKE_OK\n'
