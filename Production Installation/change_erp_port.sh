#!/bin/bash

# SSPL ERP — move the running ERPNext stack to a different external HTTP port.
#
# Run by the admin panel's "Change port" button (installation suite, ERPNext
# row), or by hand:  sudo HTTP_PORT=8080 bash change_erp_port.sh
#
# What it changes, in order:
#   1. docker-compose.yml  — the frontend's published port, and the
#      configurator's FRAPPE_HOST_NAME (host.docker.internal:<port>, used by
#      wkhtmltopdf to reach the site) when present. Edited in place: the file
#      is the fully resolved output of `docker compose config`, and it is what
#      the update/rollback scripts run, so it must stay the source of truth.
#   2. frappe_docker/.env  — HTTP_PUBLISH_PORT, so a later regeneration of the
#      compose file keeps the new port.
#   3. `docker compose up -d --pull never` — recreates only what changed
#      (frontend, configurator). Never pulls: PULL_POLICY=always would
#      otherwise turn a port change into an unplanned image update.
#   4. host_name in common_site_config.json and the site's site_config.json —
#      rewritten when it still carries the old port.
#   5. socket.io Origin check — on any port other than 80, nginx must send
#      Origin with the port ($http_host) or realtime auth calls port 80 and
#      fails. Images built by sspl-erp-docker after 2026-11-06 carry the fix;
#      for an older image the running container is hot-patched (see below).
#
# socketio_port is deliberately NOT touched: it is the listen port of the
# realtime server inside the websocket container and must stay 9000 —
# nginx proxies /socket.io to websocket:9000 whatever the external port is.
#
# Any failure before the new port answers restores both files and brings the
# stack back up on the old port.
#
# Required env:
#   HTTP_PORT        the new external HTTP port
# Optional env:
#   SSPL_ERP_DIR     deployment directory (default /opt/sspl-erp)   [testing]

set -e

ERP_DIR="${SSPL_ERP_DIR:-/opt/sspl-erp}"
FD_DIR="$ERP_DIR/frappe_docker"
COMPOSE_FILE="$ERP_DIR/docker-compose.yml"
ENV_FILE="$FD_DIR/.env"
NEW="${HTTP_PORT:-}"
# Ports already used on this server: SSH, AdGuard DNS + dashboard, OpenWA, admin panel
RESERVED="22 53 2785 3000 8090"

err()  { echo "❌ $*" >&2; exit 1; }
step() { echo ""; echo "──────── $* ────────"; }
dc()   { sudo docker compose -f "$COMPOSE_FILE" "$@"; }
url_for() { echo "http://$1$( [ "$2" != 80 ] && echo ":$2" )"; }

# ──────────────────────────────────────────────────────────── 0. checks
[[ "$NEW" =~ ^[0-9]+$ ]] && [ "$NEW" -ge 1 ] && [ "$NEW" -le 65535 ] \
    || err "HTTP_PORT must be a number between 1 and 65535 (got '$NEW')"
NEW=$((10#$NEW))
for p in $RESERVED; do
    [ "$NEW" = "$p" ] && err "port $NEW is already used on this server (reserved: $RESERVED)"
done
sudo test -f "$COMPOSE_FILE" || err "$COMPOSE_FILE not found — is the ERPNext stack installed?"

# Reads (get) or rewrites (set) the frontend's published port. Refuses unless
# exactly one published port exists, so it can never edit the wrong service.
compose_port() {
    sudo python3 - "$COMPOSE_FILE" "$@" <<'PY'
import os, re, sys, tempfile
path, mode = sys.argv[1], sys.argv[2]
text = open(path).read()
pub = re.compile(r'^(\s*published:\s*)"?(\d+)"?[ \t]*$', re.M)
found = pub.findall(text)
if len(found) != 1:
    sys.exit(f"expected exactly one 'published:' port in {path}, found {len(found)} — edit it by hand")
old = found[0][1]
if mode == "get":
    print(old)
    sys.exit(0)
new = sys.argv[3]
text = pub.sub(lambda m: f'{m.group(1)}"{new}"', text)
# wkhtmltopdf's route back to the site, set by the pdf-fix override
host = re.compile(r'(FRAPPE_HOST_NAME:\s*"?https?://host\.docker\.internal:)' + old + r'\b')
text, n = host.subn(lambda m: m.group(1) + new, text)
print(f"✓ FRAPPE_HOST_NAME port updated" if n else
      "  (no FRAPPE_HOST_NAME in the compose file — host_name is checked below)")
st = os.stat(path)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".port-")
with os.fdopen(fd, "w") as f:
    f.write(text)
os.chmod(tmp, st.st_mode & 0o7777)
os.chown(tmp, st.st_uid, st.st_gid)
os.replace(tmp, path)
PY
}

OLD=$(compose_port get) || err "could not read the current port"
SITE=$(sudo grep -oP '^SITE_NAME=\K.*' "$ENV_FILE" 2>/dev/null || true)
[ -n "$SITE" ] || err "SITE_NAME not found in $ENV_FILE"

echo "Site:          $SITE"
echo "Current port:  $OLD   ($(url_for "$SITE" "$OLD"))"
echo "New port:      $NEW   ($(url_for "$SITE" "$NEW"))"

# socket.io Origin check / hot-patch. Also run on a same-port re-run, which
# is how a hot-patch lost to a container recreate is re-applied.
check_origin() {
    step "socket.io Origin header"
    ORIGIN_OK='://$http_host;'
    TEMPLATE=/templates/nginx/frappe.conf.template
    # The entrypoint re-renders frappe.conf a moment after the restart
    origin_patched() {
        for _ in $(seq 1 15); do
            dc exec -T frontend grep -qF "$ORIGIN_OK" /etc/nginx/conf.d/frappe.conf 2>/dev/null && return 0
            sleep 2
        done
        return 1
    }
    if dc exec -T frontend grep -qF "$ORIGIN_OK" /etc/nginx/conf.d/frappe.conf 2>/dev/null; then
        echo "✓ nginx already sends Origin with the port — realtime works on any port"
    elif [ "$NEW" = 80 ]; then
        echo "✓ Port 80 — the stock Origin header works, no patch needed"
    else
        echo "→ This image's nginx drops the port from Origin, which breaks realtime"
        echo "  (socket.io) on port $NEW. Hot-patching the running frontend container..."
        # Keep the scheme variable, swap the host part for $http_host (host:port).
        # Patch only the template: the entrypoint renders frappe.conf from it on
        # every start, and a restart (not a recreate) keeps the patched file.
        if dc exec -T -u root frontend sed -i \
                's|\(proxy_set_header Origin \$[a-z_]*://\)[^;]*;|\1$http_host;|' "$TEMPLATE" \
           && dc restart frontend >/dev/null \
           && origin_patched; then
            echo "✓ Patched — realtime works on port $NEW"
            echo ""
            echo "⚠⚠ TEMPORARY FIX ⚠⚠"
            echo "  The patch lives only in this container. It is lost the next time the"
            echo "  frontend container is recreated — e.g. by a System update that pulls an"
            echo "  image built without the fix. Rebuild the image from the sspl-erp-docker"
            echo "  repo (Actions → Build & Push SSPL ERPNext Image), then update, or simply"
            echo "  re-run Change port with the same port after an update to re-apply it."
        else
            echo "⚠ Hot-patch failed — pages load, but live updates (socket.io) will not"
            echo "  work on port $NEW until the image is rebuilt with the Origin fix."
        fi
    fi
}

if [ "$OLD" = "$NEW" ]; then
    echo ""
    echo "✓ The stack already listens on port $NEW — no port change"
    check_origin
    exit 0
fi
if sudo ss -Hltn "( sport = :$NEW )" 2>/dev/null | grep -q .; then
    err "port $NEW is already in use on this server: $(sudo ss -Hltnp "( sport = :$NEW )" | head -1)"
fi

# ─────────────────────────────────────────────────────────── 1. backups
step "Backing up the compose file and .env"
TS=$(date +%Y%m%d_%H%M%S)
sudo cp -p "$COMPOSE_FILE" "$COMPOSE_FILE.bak.port.$TS"
sudo cp -p "$ENV_FILE" "$ENV_FILE.bak.port.$TS"
echo "✓ $COMPOSE_FILE.bak.port.$TS"
echo "✓ $ENV_FILE.bak.port.$TS"

ROLLED_BACK=""
rollback() {
    [ -n "$ROLLED_BACK" ] && return
    ROLLED_BACK=1
    echo ""
    echo "↩ Restoring port $OLD..."
    sudo cp -p "$COMPOSE_FILE.bak.port.$TS" "$COMPOSE_FILE"
    sudo cp -p "$ENV_FILE.bak.port.$TS" "$ENV_FILE"
    if dc up -d --pull never --no-build; then
        echo "↩ Stack is back on port $OLD — $(url_for "$SITE" "$OLD")"
    else
        echo "↩ Files restored but 'docker compose up -d' failed — check: sudo docker compose -f $COMPOSE_FILE ps"
    fi
}
fail() { echo "❌ $*" >&2; rollback; exit 1; }
trap 'rc=$?; [ $rc -ne 0 ] && [ -z "$DONE" ] && rollback' EXIT

# ───────────────────────────────────────────────── 2. edit compose + .env
step "Updating configuration"
compose_port set "$NEW" || fail "could not update $COMPOSE_FILE"
echo "✓ docker-compose.yml: published port $OLD → $NEW"

if sudo grep -q '^HTTP_PUBLISH_PORT=' "$ENV_FILE"; then
    sudo sed -i "s/^HTTP_PUBLISH_PORT=.*/HTTP_PUBLISH_PORT=$NEW/" "$ENV_FILE"
else
    echo "HTTP_PUBLISH_PORT=$NEW" | sudo tee -a "$ENV_FILE" >/dev/null
fi
echo "✓ .env: HTTP_PUBLISH_PORT=$NEW"

# ──────────────────────────────────────────────── 3. recreate + verify
step "Recreating containers on port $NEW"
dc up -d --pull never --no-build || fail "docker compose up -d failed"

BOUND=""
for _ in $(seq 1 30); do
    if dc port frontend 8080 2>/dev/null | grep -q ":$NEW\$"; then BOUND=1; break; fi
    sleep 2
done
[ -n "$BOUND" ] || fail "frontend is not published on port $NEW — $(dc port frontend 8080 2>&1)"
echo "✓ frontend published on port $NEW"

if command -v curl >/dev/null 2>&1; then
    PONG=""
    for _ in $(seq 1 24); do
        if curl -fsS -m 5 "http://127.0.0.1:$NEW/api/method/ping" 2>/dev/null | grep -q pong; then
            PONG=1; break
        fi
        sleep 5
    done
    [ -n "$PONG" ] || fail "http://127.0.0.1:$NEW/api/method/ping did not answer within 2 minutes"
    echo "✓ ERPNext answers on port $NEW"
else
    echo "⚠ curl not installed — skipped the ping check"
fi
# From here on the new port works; later steps warn instead of rolling back.
DONE=1

# ───────────────────────────────────────────────────── 4. host_name
step "Site host_name"
# Rewrites the port of a host_name URL from OLD to NEW. A URL with no port
# counts as port 80 for http. Prints nothing when the URL needs no change.
new_host_name() {
    [[ "$1" =~ ^(https?)://([^/:]+)(:([0-9]+))?(/.*)?$ ]] || return 0
    local scheme=${BASH_REMATCH[1]} host=${BASH_REMATCH[2]} port=${BASH_REMATCH[4]} path=${BASH_REMATCH[5]}
    [ -z "$port" ] && [ "$scheme" = http ] && port=80
    [ "$port" = "$OLD" ] || return 0
    echo "$scheme://$host$( [ "$NEW" != 80 ] && echo ":$NEW" )$path"
}
read_host_name() {
    dc exec -T backend cat "$1" 2>/dev/null | grep -oP '"host_name":\s*"\K[^"]+' || true
}

GLOBAL_HN=$(read_host_name sites/common_site_config.json)
NEW_HN=$(new_host_name "$GLOBAL_HN")
if [ -n "$NEW_HN" ]; then
    dc exec -T backend bench set-config -g host_name "$NEW_HN" \
        && echo "✓ common_site_config host_name: $GLOBAL_HN → $NEW_HN" \
        || echo "⚠ could not update common_site_config host_name ($GLOBAL_HN)"
else
    echo "✓ common_site_config host_name: ${GLOBAL_HN:-(not set)}"
fi
SITE_HN=$(read_host_name "sites/$SITE/site_config.json")
NEW_HN=$(new_host_name "$SITE_HN")
if [ -n "$NEW_HN" ]; then
    dc exec -T backend bench --site "$SITE" set-config host_name "$NEW_HN" \
        && echo "✓ $SITE host_name: $SITE_HN → $NEW_HN" \
        || echo "⚠ could not update $SITE host_name ($SITE_HN)"
else
    echo "✓ $SITE host_name: ${SITE_HN:-(not set — common_site_config applies)}"
fi
# The configurator also rewrites host_name when FRAPPE_HOST_NAME changed, so
# restart the app processes so they all read the new value.
RESTART=$(dc config --services 2>/dev/null | grep -xE 'backend|queue-short|queue-long|scheduler' | xargs)
if [ -n "$RESTART" ]; then
    dc restart $RESTART >/dev/null && echo "✓ Restarted: $RESTART" || echo "⚠ restart of $RESTART failed"
fi

# ─────────────────────────────────────────────── 5. socket.io Origin
check_origin

if sudo ufw status 2>/dev/null | grep -q '^Status: active'; then
    echo ""
    echo "⚠ ufw is active — allow the new port if it is not open yet:  sudo ufw allow $NEW/tcp"
fi

echo ""
echo "✅ ERPNext moved from port $OLD to $NEW — $(url_for "$SITE" "$NEW")"
echo "   Backups of the old settings: *.bak.port.$TS"
