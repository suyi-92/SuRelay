#!/usr/bin/env bash
# SuRelay command entry. Works directly or through /usr/local/bin/sr.
set -Eeuo pipefail
umask 077
entry="$(readlink -f -- "$0")"
exec python3 "$(dirname -- "$entry")/scripts/surelay.py" "$@"
