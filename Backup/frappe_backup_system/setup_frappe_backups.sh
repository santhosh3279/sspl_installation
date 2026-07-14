#!/bin/bash

# Frappe Backup Setup Installation Script
# Run this to set up automated backups on your server
#
# Installs side-by-side with any older backup scripts:
# new scripts go to /opt/scripts/v2/ and the old ones in
# /opt/scripts/ are left untouched, so you can fall back to
# them if needed.

set -e

INSTALL_DIR="/opt/scripts/v2"

echo "=== Frappe Backup Setup Installation (v2) ==="

# 1. Create directories
echo "Creating backup directories..."
sudo mkdir -p /opt/backups/frappe
sudo mkdir -p "$INSTALL_DIR"
sudo mkdir -p /var/log

# 2. Copy scripts
echo "Installing backup scripts to $INSTALL_DIR..."
sudo cp frappe_backup.sh "$INSTALL_DIR/"
sudo cp frappe_db_backup.sh "$INSTALL_DIR/"
sudo cp frappe_restore.sh "$INSTALL_DIR/"
sudo cp frappe_backup_verify.sh "$INSTALL_DIR/"

# 3. Make scripts executable
sudo chmod +x "$INSTALL_DIR"/frappe_*.sh

# 4. Configure site name
echo ""
echo "Please enter your Frappe site name:"
echo "To find it, run: docker compose -f /opt/sspl-erp/docker-compose.yml exec backend ls sites/"
read -p "Site name: " SITE_NAME

if [ ! -z "$SITE_NAME" ]; then
    sudo sed -i "s/your-site-name/$SITE_NAME/g" "$INSTALL_DIR/frappe_backup.sh"
    sudo sed -i "s/your-site-name/$SITE_NAME/g" "$INSTALL_DIR/frappe_db_backup.sh"
    sudo sed -i "s/your-site-name/$SITE_NAME/g" "$INSTALL_DIR/frappe_restore.sh"
    echo "Site name configured: $SITE_NAME"
fi

# 5. Set ownership and restrict access (backups contain credentials)
sudo chown -R root:root "$INSTALL_DIR"
sudo chown -R root:root /opt/backups/
sudo chmod 700 /opt/backups/frappe

# 6. Set up cron jobs
echo ""
echo "Setting up cron jobs..."
echo "NOTE: If your old backup scripts already run from cron (/opt/scripts/*.sh),"
echo "installing these jobs too will run BOTH backups. Recommended: answer 'no',"
echo "test the v2 scripts manually first, then switch the paths in 'sudo crontab -e'"
echo "from /opt/scripts/ to /opt/scripts/v2/ when you are happy with them."
echo ""
echo "Do you want to install automated backups via cron? (yes/no)"
read -p "Install cron jobs: " INSTALL_CRON

if [ "$INSTALL_CRON" = "yes" ]; then
    # Add to root crontab
    (sudo crontab -l 2>/dev/null || true; echo "") | sudo tee /tmp/crontab.tmp > /dev/null
    echo "# Frappe Backup Jobs (v2)" | sudo tee -a /tmp/crontab.tmp > /dev/null
    echo "0 2 * * * $INSTALL_DIR/frappe_backup.sh >> /var/log/frappe_backup_v2.log 2>&1" | sudo tee -a /tmp/crontab.tmp > /dev/null
    echo "0 */6 * * * $INSTALL_DIR/frappe_db_backup.sh >> /var/log/frappe_db_backup_v2.log 2>&1" | sudo tee -a /tmp/crontab.tmp > /dev/null
    echo "0 3 * * 0 $INSTALL_DIR/frappe_backup_verify.sh >> /var/log/frappe_backup_verify_v2.log 2>&1" | sudo tee -a /tmp/crontab.tmp > /dev/null

    sudo crontab /tmp/crontab.tmp
    sudo rm /tmp/crontab.tmp

    echo "Cron jobs installed successfully!"
    echo "- Full backup: Daily at 2:00 AM"
    echo "- DB backup: Every 6 hours"
    echo "- Verification: Weekly on Sunday at 3:00 AM"
    echo ""
    echo "REMINDER: remove or comment out any old /opt/scripts/ backup lines"
    echo "in 'sudo crontab -e' so backups do not run twice."
fi

# 7. Test backup
echo ""
echo "Do you want to run a test backup now? (yes/no)"
read -p "Run test backup: " RUN_TEST

if [ "$RUN_TEST" = "yes" ]; then
    echo "Running test backup..."
    sudo "$INSTALL_DIR/frappe_backup.sh"
    echo ""
    echo "Test backup completed! Check /opt/backups/frappe/"
fi

echo ""
echo "=== Installation Complete ==="
echo ""
echo "Backup scripts installed in: $INSTALL_DIR/"
echo "Old scripts (if any) remain untouched in: /opt/scripts/"
echo "Backups will be stored in: /opt/backups/frappe/ (shared with old scripts)"
echo "Logs available in: /var/log/frappe_*_v2.log"
echo ""
echo "Manual backup commands:"
echo "  Full backup:     sudo $INSTALL_DIR/frappe_backup.sh"
echo "  DB only:         sudo $INSTALL_DIR/frappe_db_backup.sh"
echo "  Verify backups:  sudo $INSTALL_DIR/frappe_backup_verify.sh"
echo "  Restore:         sudo $INSTALL_DIR/frappe_restore.sh /path/to/backup/folder"
echo ""
echo "View cron jobs:    sudo crontab -l"
echo "Edit cron jobs:    sudo crontab -e"
echo ""
