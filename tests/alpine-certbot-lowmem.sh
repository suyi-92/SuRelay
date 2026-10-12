#!/usr/bin/env bash
# Run only in a disposable Alpine 3.19 container with a memory limit and no swap.
set -Eeuo pipefail
[[ "${RRB_TEST_DISPOSABLE:-}" == yes ]] || {
  echo 'Set RRB_TEST_DISPOSABLE=yes in a disposable Alpine container.' >&2
  exit 1
}
export RRB_PROJECT_DIR="${RRB_PROJECT_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}"
source "$RRB_PROJECT_DIR/scripts/00-lib.sh"
require_root
is_alpine || die 'This smoke test requires Alpine.'
require_supported_os
[[ ! -e /opt/reality-relay-certbot ]] || die 'Use a fresh container without a Certbot environment.'

oom_before="$(awk '$1 == "oom_kill" {print $2}' /sys/fs/cgroup/memory.events)"
install_certbot
"$CERTBOT_BIN" --version
"$CERTBOT_BIN" plugins --text | grep '^\* dns-cloudflare$'
"$CERTBOT_BIN" --help dns-cloudflare >/dev/null
# A repeated deployment must use the same complete, compatible environment.
install_certbot
"$CERTBOT_BIN" plugins --text | grep '^\* dns-cloudflare$'
# virtualenv may seed newer cached build tools than its bundled versions. Recreate
# the reported wheel dependency failure, then verify an existing env is repaired.
pip_bin=/opt/reality-relay-certbot/bin/pip
run "$pip_bin" install --no-input --disable-pip-version-check --no-index --no-deps \
  --no-cache-dir --no-compile --require-hashes -r "$RRB_PROJECT_DIR/tests/fixtures/alpine-certbot-seed.txt"
run "$pip_bin" uninstall --yes packaging
if seed_error="$("$pip_bin" check 2>&1)"; then
  die 'Expected the updated wheel seed to report missing packaging.'
fi
[[ "$seed_error" == *'wheel 0.48.0 requires packaging, which is not installed.'* ]] \
  || die "Unexpected seed failure: $seed_error"
install_certbot
"$CERTBOT_BIN" plugins --text | grep '^\* dns-cloudflare$'
oom_after="$(awk '$1 == "oom_kill" {print $2}' /sys/fs/cgroup/memory.events)"
[[ "$oom_before" == "$oom_after" ]] || die 'OOM kill occurred during Certbot installation.'
du -sh /opt/reality-relay-certbot
cat /sys/fs/cgroup/memory.max /sys/fs/cgroup/memory.events
printf 'ALPINE_CERTBOT_LOWMEM_OK\n'
