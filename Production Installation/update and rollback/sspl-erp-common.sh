#!/bin/bash
# Shared configuration and helpers for the SSPL ERP update/rollback scripts.
# Source this file from the same directory; do not run it directly.

SITE_NAME="192.168.225.135"
COMPOSE_FILE="docker-compose.yml"
BACKUP_DIR="${BACKUP_DIR:-/opt/sspl-erp/image-backups}"
SERVICE_WAIT_TIMEOUT=180
# Installed by the backup setup, not by this one — same as TRASH_CLEANUP and
# the frappe_backup.sh this update already calls, hence the absolute path.
MIGRATE_SCRIPT="/opt/scripts/v2/frappe_migrate.sh"

# Read a value from .env; f2- keeps values that contain '='
get_env_value() {
    grep "^$1=" .env | head -1 | cut -d '=' -f2-
}

# Poll until MariaDB answers and the backend container accepts exec, or time out
wait_for_services() {
    local root_pw waited=0
    root_pw=$(get_env_value MARIADB_ROOT_PASSWORD)

    echo "→ Waiting for database to be ready..."
    until docker compose -f "$COMPOSE_FILE" exec -T -e MYSQL_PWD="$root_pw" db \
            mariadb-admin -uroot ping --silent >/dev/null 2>&1; do
        waited=$((waited + 5))
        if [ "$waited" -ge "$SERVICE_WAIT_TIMEOUT" ]; then
            echo "   ❌ Database not ready after ${SERVICE_WAIT_TIMEOUT}s"
            return 1
        fi
        sleep 5
    done

    echo "→ Waiting for backend to be ready..."
    until docker compose -f "$COMPOSE_FILE" exec -T backend true >/dev/null 2>&1; do
        waited=$((waited + 5))
        if [ "$waited" -ge "$SERVICE_WAIT_TIMEOUT" ]; then
            echo "   ❌ Backend not ready after ${SERVICE_WAIT_TIMEOUT}s"
            return 1
        fi
        sleep 5
    done
    echo "   ✓ Services are ready"
}

# ─────────────────────────────────────────────── the migration, and the window
#
# 'bench migrate' is the one step of an update that changes the database, and
# it is the step most likely to fail: a patch shipped by any app in the new
# image runs against data written by the old one. Two things follow.
#
# First, it must not run while users are on the site. Up to here the update
# only swapped images; from here the schema is moving under whatever is
# connected. backend/db/redis stay up — the migration runs through them.
#
# Second, its failure is not the caller's ERR trap's business. Rolling back
# images is the right answer for a failed migration, but only after the site
# has been put back on its feet, so run_migrate reports and returns non-zero
# rather than killing the script under 'set -e'.

MIGRATE_OFFLINE_WANT="frontend websocket queue-short queue-long scheduler"
MIGRATE_OFFLINE_SERVICES=""

# Stop the user-facing services, if this compose file has them.
migrate_window_open() {
    local have s
    have=$(docker compose -f "$COMPOSE_FILE" config --services 2>/dev/null || true)
    MIGRATE_OFFLINE_SERVICES=""
    for s in $MIGRATE_OFFLINE_WANT; do
        printf '%s\n' "$have" | grep -qx "$s" && \
            MIGRATE_OFFLINE_SERVICES="$MIGRATE_OFFLINE_SERVICES $s"
    done
    MIGRATE_OFFLINE_SERVICES="${MIGRATE_OFFLINE_SERVICES# }"

    if [ -z "$MIGRATE_OFFLINE_SERVICES" ]; then
        echo "   ⚠ No known user-facing services in $COMPOSE_FILE — migrating"
        echo "     with the site up. Users and jobs can write during it."
        return 0
    fi
    echo "→ Taking the site offline for the migration:$MIGRATE_OFFLINE_SERVICES"
    docker compose -f "$COMPOSE_FILE" stop $MIGRATE_OFFLINE_SERVICES || true
}

# Put them back. Safe to call twice, and safe to call if the window never
# opened — which is why the update can call it from a trap and inline both.
migrate_window_close() {
    [ -z "$MIGRATE_OFFLINE_SERVICES" ] && return 0
    echo "→ Bringing the site back online..."
    if docker compose -f "$COMPOSE_FILE" start $MIGRATE_OFFLINE_SERVICES; then
        MIGRATE_OFFLINE_SERVICES=""
    else
        echo "   ❌ Could not start:$MIGRATE_OFFLINE_SERVICES"
        echo "      The site is still OFFLINE. Start it by hand:"
        echo "      docker compose -f $COMPOSE_FILE start$MIGRATE_OFFLINE_SERVICES"
    fi
}

# Run the migration. Returns bench's status; never aborts the caller.
#
# The real work lives in frappe_migrate.sh, in the backup package, because the
# restore needs the identical step and it is what you re-run by hand after a
# failure. If that package is not installed the migration still has to happen,
# so there is a fallback — kept to the bare command, with the guidance text
# left to the one place that owns it.
run_migrate() {
    if [ -x "$MIGRATE_SCRIPT" ]; then
        "$MIGRATE_SCRIPT" "$SITE_NAME"
        return $?
    fi
    echo "   ⚠ $MIGRATE_SCRIPT not found — running the migration directly."
    echo "     Install the backup scripts (setup_frappe_backups.sh) to get the"
    echo "     recovery guidance that goes with a failed migration."
    docker compose -f "$COMPOSE_FILE" exec -T backend \
        bench --site "$SITE_NAME" migrate
}

# Install every app the image ships that the site does not have yet.
#
# The image is the source of truth for which apps a site runs, but only at
# creation time: install_erp_stack.sh passes them all to 'bench new-site'.
# After that an app added to the image reaches an existing site only here,
# because 'bench migrate' walks the site's installed apps and a new one is
# not among them — it would ship in the image and silently never appear.
install_new_apps() {
    echo "→ Checking for apps added to the image..."
    local installed available app
    local missing=()

    # Advisory probes: neither failing is a reason to abort an update that
    # has otherwise succeeded, so keep them out of the caller's ERR trap.
    installed=$(docker compose -f "$COMPOSE_FILE" exec -T backend \
        bench --site "$SITE_NAME" list-apps 2>/dev/null) || true
    available=$(docker compose -f "$COMPOSE_FILE" exec -T backend ls apps 2>/dev/null) || true

    if [ -z "$installed" ] || [ -z "$available" ]; then
        echo "   ⚠ Could not list apps — skipping (install any new app by hand)"
        return 0
    fi

    for app in $available; do
        [ "$app" = "frappe" ] && continue
        # Matched against the whole of 'list-apps' rather than a parsed first
        # column, because its layout differs between bench versions (bare
        # names, or name + version + branch). -w keeps 'hrms' from matching
        # an 'hrms_extra' line. Do not loosen this match: a name matched
        # somewhere other than its own line reads as installed and is
        # skipped, which is the silent no-op this function exists to stop.
        grep -qw -- "$app" <<<"$installed" || missing+=("$app")
    done

    if [ ${#missing[@]} -eq 0 ]; then
        echo "   ✓ No new apps to install"
        return 0
    fi

    echo "   New apps in the image: ${missing[*]}"
    for app in "${missing[@]}"; do
        echo "→ Installing $app on $SITE_NAME (this may take a few minutes)..."
        # Unguarded on purpose. A half-installed app is a database change,
        # and sspl-erp-rollback.sh only rolls back images — so this must
        # reach the caller's ERR trap, which points at frappe_restore.sh.
        docker compose -f "$COMPOSE_FILE" exec -T backend \
            bench --site "$SITE_NAME" install-app "$app"
        echo "   ✓ $app installed"
    done
}

# Re-create the site DB user with host '%' so grants survive container IP changes
fix_db_grants() {
    echo "→ Fixing MariaDB user grants (handling IP changes)..."
    local root_pw site_config db_name db_pass
    root_pw=$(get_env_value MARIADB_ROOT_PASSWORD)
    site_config=$(docker compose -f "$COMPOSE_FILE" exec -T backend \
        bash -c "cat ~/frappe-bench/sites/${SITE_NAME}/site_config.json")
    db_name=$(echo "$site_config" | grep -oP '"db_name":\s*"\K[^"]+')
    db_pass=$(echo "$site_config" | grep -oP '"db_password":\s*"\K[^"]+')

    if [ -z "$db_name" ] || [ -z "$db_pass" ]; then
        echo "   ⚠ Could not extract DB credentials, skipping grant fix"
        return 0
    fi

    echo "   Granting access for user: $db_name on database: $db_name"
    if docker compose -f "$COMPOSE_FILE" exec -T -e MYSQL_PWD="$root_pw" db mariadb -uroot -e "
        DROP USER IF EXISTS '${db_name}'@'%';
        CREATE USER '${db_name}'@'%' IDENTIFIED BY '${db_pass}';
        GRANT ALL PRIVILEGES ON \`${db_name}\`.* TO '${db_name}'@'%';
        FLUSH PRIVILEGES;
    " 2>/dev/null; then
        echo "   ✓ Database grants updated"
    else
        echo "   ⚠ Grant update failed (may already be correct)"
    fi
}
