#!/bin/bash
# Shared configuration and helpers for the SSPL ERP update/rollback scripts.
# Source this file from the same directory; do not run it directly.

SITE_NAME="192.168.225.135"
COMPOSE_FILE="docker-compose.yml"
BACKUP_DIR="${BACKUP_DIR:-/opt/sspl-erp/image-backups}"
SERVICE_WAIT_TIMEOUT=180

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
