#!/bin/bash
# Install (or refresh) the multi-tenant tools to $DEST and set up cron.
#
# Usage: install-tools.sh [--cron | --no-cron]
#
# Code only: the config file (SSPL_CONF) is never touched, so re-running this
# after 'git pull' is the whole tooling update. Cron is (re)written as one
# marked block in root's crontab; other lines are left alone.
#
# Env:  SSPL_TOOLS_DIR   destination (default /opt/sspl-erp/tools)
#       SSPL_CONF        config path baked into the cron lines
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${SSPL_TOOLS_DIR:-/opt/sspl-erp/tools}"
CONF="${SSPL_CONF:-/opt/sspl-erp/sspl-mt.conf}"
CRON=ask
case "${1:-}" in
    --cron) CRON=yes ;;
    --no-cron) CRON=no ;;
    "") ;;
    *) echo "Usage: $0 [--cron|--no-cron]" >&2; exit 2 ;;
esac

[ "$(id -u)" -eq 0 ] || { echo "❌ Run as root (sudo)." >&2; exit 1; }

mkdir -p "$DEST"
for d in lib sites backup update; do
    rm -rf "${DEST:?}/$d.new"
    cp -r "$SRC/$d" "$DEST/$d.new"
    rm -rf "${DEST:?}/$d"
    mv "$DEST/$d.new" "$DEST/$d"
done
chown -R root:root "$DEST"
chmod 755 "$DEST"/*/
find "$DEST" -type f \( -name '*.sh' -o -name '*.py' \) -exec chmod 755 {} +
chmod 644 "$DEST/lib/common.sh"
echo "✓ Tools installed to $DEST"

if [ "$CRON" = ask ]; then
    if [ -t 0 ]; then
        read -rp "Schedule automatic backups for all sites (cron)? [Y/n]: " A
        [[ "${A:-y}" =~ ^[Yy] ]] && CRON=yes || CRON=no
    else
        CRON=no
    fi
fi
if [ "$CRON" = yes ]; then
    BEGIN="# >>> SSPL multi-tenant backups >>>"
    END="# <<< SSPL multi-tenant backups <<<"
    ENVP="SSPL_CONF=$CONF"
    { crontab -l 2>/dev/null | sed "/^$BEGIN\$/,/^$END\$/d" || true
      echo "$BEGIN"
      echo "0 2 * * *   $ENVP $DEST/backup/backup.sh --all    >> /var/log/sspl-backup.log 2>&1"
      echo "0 */6 * * * $ENVP $DEST/backup/db-backup.sh --all >> /var/log/sspl-db-backup.log 2>&1"
      echo "0 3 * * 0   $ENVP $DEST/backup/verify.sh --all    >> /var/log/sspl-backup-verify.log 2>&1"
      echo "$END"
    } | crontab -
    echo "✓ Cron: full backup 02:00 daily, DB dump every 6h, verify Sundays 03:00 (all sites)"
fi
