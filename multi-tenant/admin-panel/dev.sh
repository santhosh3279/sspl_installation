#!/bin/bash
# Run the multi-tenant panel from this checkout for development: live reload,
# 127.0.0.1 by default, fake sites. Buttons print what they would run; nothing
# touches Docker, nginx or /opt. See dev.py.
#
#   ./dev.sh [--host 0.0.0.0] [--port 8091] [--reset]     login: dev / dev
#
# Vue pages (frontend/) with hot reload: keep this running and, in a second
# terminal, cd frontend && npm ci && npm run dev    http://<this machine>:5173
#
# Vite listens on 0.0.0.0 and passes the panel through: trusted LAN only, since
# the login is dev / dev.
#
# --reset rebuilds the fake data. Over SSH: ssh -L 8091:127.0.0.1:8091 <server>
set -euo pipefail

case "${1:-}" in -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;; esac
[ "$(id -u)" -ne 0 ] || { echo "❌ Do not run dev.sh as root." >&2; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
export SSPL_DEV_HOME="${XDG_CACHE_HOME:-$HOME/.cache}/sspl-panel-dev"
VENV="$SSPL_DEV_HOME/venv"
if ! "$VENV/bin/python" -c 'import flask' >/dev/null 2>&1; then
    echo "Creating the dev virtualenv in $VENV ..."
    mkdir -p "$SSPL_DEV_HOME"
    python3 -m venv --clear "$VENV"
    "$VENV/bin/pip" install --quiet flask
fi
exec "$VENV/bin/python" -B "$HERE/dev.py" "$@"
