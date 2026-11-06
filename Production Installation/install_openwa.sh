#!/bin/bash
# Install OpenWA (WhatsApp HTTP API gateway, github.com/rmyndharis/OpenWA)
# and run it in Docker with the published GHCR image.
#
# Env (all optional):
#   OPENWA_DIR   checkout / compose dir        (default /opt/openwa)
#   OPENWA_PORT  host port for API + dashboard (default 2785)
#
# The first boot generates the admin API key and stores it at
# /app/data/.api-key inside the openwa-api container. This script never
# prints it — the Admin Panel's "Show API key" button reads it on demand, so
# it does not land in the job log on disk.

set -e

err()  { echo "❌ $*" >&2; exit 1; }
step() { echo ""; echo "════════════════════════════════════════"; echo " $*"; echo "════════════════════════════════════════"; }

OPENWA_DIR="${OPENWA_DIR:-/opt/openwa}"
OPENWA_PORT="${OPENWA_PORT:-2785}"
OPENWA_REPO="https://github.com/rmyndharis/OpenWA.git"
OPENWA_IMAGE="ghcr.io/rmyndharis/openwa:latest"
CONTAINER="openwa-api"

step "Installing OpenWA (WhatsApp API) in Docker"

# ──────────────────────────────────────────────────────────── preflight checks
[ "$(id -u)" -eq 0 ] || err "This installer must be run as root (or with sudo)."
command -v docker >/dev/null || err "docker is not installed — install the ERPNext stack first."
docker compose version >/dev/null 2>&1 || err "docker compose plugin is missing."
command -v git >/dev/null || err "git is not installed."
case "$OPENWA_PORT" in ''|*[!0-9]*) err "OPENWA_PORT must be a number" ;; esac

# The compose file pins container_name openwa-api, so a second copy cannot
# run beside an existing one. If one is already running from another
# directory, leave it alone rather than recreating it on a different image.
if docker inspect "$CONTAINER" >/dev/null 2>&1; then
    existing_dir=$(docker inspect "$CONTAINER" \
        --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' 2>/dev/null || true)
    if [ -n "$existing_dir" ] && [ "$existing_dir" != "$OPENWA_DIR" ]; then
        echo "OpenWA is already installed on this server from $existing_dir."
        echo "Leaving it as it is. Use the panel's 'Show API key' button to read its key."
        exit 0
    fi
fi

# ──────────────────────────────────────────────────────────── source checkout
# The compose file forwards an explicit env list, so it must match the image:
# keep the checkout current on every (re)install.
step "Fetching OpenWA into $OPENWA_DIR"
if [ -d "$OPENWA_DIR/.git" ]; then
    git -C "$OPENWA_DIR" pull --ff-only
else
    [ -e "$OPENWA_DIR" ] && [ -n "$(ls -A "$OPENWA_DIR" 2>/dev/null)" ] \
        && err "$OPENWA_DIR exists and is not an OpenWA checkout — move it away first."
    git clone --depth 1 "$OPENWA_REPO" "$OPENWA_DIR"
fi

# ──────────────────────────────────────────────────────────── compose override
# Run the published image instead of building from source, expose it on the
# LAN, and allow the dashboard over plain HTTP.
step "Writing docker-compose.override.yml"
cat > "$OPENWA_DIR/docker-compose.override.yml" <<EOF
# Written by sspl_installation/Production Installation/install_openwa.sh
services:
  openwa-api:
    image: $OPENWA_IMAGE
    ports: !override
      - '0.0.0.0:${OPENWA_PORT}:2785'
    environment:
      - CSP_UPGRADE_INSECURE_REQUESTS=false
EOF

# ──────────────────────────────────────────────────────────── start
step "Pulling image and starting containers"
cd "$OPENWA_DIR"
docker compose pull openwa-api
# --no-build: a failed pull must fail here, not quietly build from source.
docker compose up -d --no-build

# ──────────────────────────────────────────────────────────── wait for key
step "Waiting for OpenWA to generate its API key"
for i in $(seq 1 90); do
    if docker exec "$CONTAINER" test -s /app/data/.api-key 2>/dev/null; then
        echo "API key generated."
        break
    fi
    [ "$i" -eq 90 ] && err "no API key after 3 minutes — check: docker logs $CONTAINER"
    sleep 2
done

step "OpenWA installed successfully!"
echo "Dashboard + API: http://<your-server-ip>:${OPENWA_PORT}"
echo "Swagger:         http://<your-server-ip>:${OPENWA_PORT}/api/docs"
echo "API key:         press 'Show API key' on the Installation page of the Admin Panel"
