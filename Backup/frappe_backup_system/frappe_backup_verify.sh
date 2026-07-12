#!/bin/bash

# Backup Verification Script
# Checks if recent backups exist and are valid

set -e

BACKUP_DIR="/opt/backups/frappe"
ALERT_EMAIL="your-email@example.com"  # Configure for alerts
MAX_AGE_HOURS=26  # Alert if no backup in last 26 hours

echo "=== Frappe Backup Verification at $(date) ==="

# Find most recent backup
LATEST_BACKUP=$(find "$BACKUP_DIR" -maxdepth 1 -type d -name "20*" | sort -r | head -1)

if [ -z "$LATEST_BACKUP" ]; then
    echo "ERROR: No backups found in $BACKUP_DIR"
    # Send alert email
    # echo "No Frappe backups found!" | mail -s "ALERT: Frappe Backup Missing" "$ALERT_EMAIL"
    exit 1
fi

# Check backup age
BACKUP_AGE=$(find "$LATEST_BACKUP" -maxdepth 0 -type d -mmin +$((MAX_AGE_HOURS * 60)) | wc -l)

if [ "$BACKUP_AGE" -gt 0 ]; then
    echo "WARNING: Latest backup is older than $MAX_AGE_HOURS hours"
    echo "Latest backup: $LATEST_BACKUP"
    # Send alert
    # echo "Frappe backup is stale: $LATEST_BACKUP" | mail -s "WARNING: Frappe Backup Stale" "$ALERT_EMAIL"
fi

# Check if backup contains expected files
echo "Latest backup: $LATEST_BACKUP"
echo "Checking backup contents..."

# Files archives are .tgz when created with --compress, .tar otherwise
DB_BACKUP=$(find "$LATEST_BACKUP" -name "*-database.sql.gz" | wc -l)
FILES_BACKUP=$(find "$LATEST_BACKUP" \( -name "*-files.tar" -o -name "*-files.tgz" \) ! -name "*-private-files.*" | wc -l)
PRIVATE_BACKUP=$(find "$LATEST_BACKUP" \( -name "*-private-files.tar" -o -name "*-private-files.tgz" \) | wc -l)
CONFIG_BACKUP=$(find "$LATEST_BACKUP" -name "site_config.json" | wc -l)

echo "Database backup:      $([[ $DB_BACKUP -gt 0 ]] && echo 'OK' || echo 'MISSING')"
echo "Public files backup:  $([[ $FILES_BACKUP -gt 0 ]] && echo 'OK' || echo 'MISSING')"
echo "Private files backup: $([[ $PRIVATE_BACKUP -gt 0 ]] && echo 'OK' || echo 'MISSING')"
echo "Config backup:        $([[ $CONFIG_BACKUP -gt 0 ]] && echo 'OK' || echo 'MISSING')"

# Check backup sizes
if [ $DB_BACKUP -gt 0 ]; then
    DB_FILE=$(find "$LATEST_BACKUP" -name "*-database.sql.gz" | sort | tail -1)
    DB_SIZE=$(du -h "$DB_FILE" | cut -f1)
    echo "Database backup size: $DB_SIZE"

    # Alert if backup is suspiciously small (< 1MB)
    DB_SIZE_KB=$(du -k "$DB_FILE" | cut -f1)
    if [ "$DB_SIZE_KB" -lt 1024 ]; then
        echo "WARNING: Database backup seems too small ($DB_SIZE)"
    fi
fi

# Calculate total backup size
TOTAL_SIZE=$(du -sh "$LATEST_BACKUP" | cut -f1)
echo "Total backup size: $TOTAL_SIZE"

# Count total backups
BACKUP_COUNT=$(find "$BACKUP_DIR" -maxdepth 1 -type d -name "20*" | wc -l)
echo "Total backups available: $BACKUP_COUNT"

# Exit non-zero if anything critical is missing so cron/monitoring can catch it
if [ "$DB_BACKUP" -eq 0 ] || [ "$FILES_BACKUP" -eq 0 ] || [ "$PRIVATE_BACKUP" -eq 0 ]; then
    echo "=== Verification FAILED: backup incomplete ==="
    exit 1
fi

echo "=== Verification completed ==="
