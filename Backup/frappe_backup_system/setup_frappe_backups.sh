#!/bin/bash

# Frappe Backup Setup Installation Script
# Run this to set up automated backups on your server

set -e

echo "=== Frappe Backup Setup Installation ==="

# 1. Create directories
echo "Creating backup directories..."
sudo mkdir -p /opt/backups/frappe
sudo mkdir -p /opt/scripts
sudo mkdir -p /var/log

# 2. Copy scripts
echo "Installing backup scripts..."
sudo cp frappe_backup.sh /opt/scripts/
sudo cp frappe_db_backup.sh /opt/scripts/
sudo cp frappe_restore.sh /opt/scripts/
sudo cp frappe_backup_verify.sh /opt/scripts/

# 3. Make scripts executable
sudo chmod +x /opt/scripts/frappe_*.sh

# 4. Configure site name
echo ""
echo "Please enter your Frappe site name:"
echo "To find it, run: docker compose -f /opt/sspl-erp/docker-compose.yml exec backend ls sites/"
read -p "Site name: " SITE_NAME

if [ ! -z "$SITE_NAME" ]; then
    sudo sed -i "s/your-site-name/$SITE_NAME/g" /opt/scripts/frappe_backup.sh
    sudo sed -i "s/your-site-name/$SITE_NAME/g" /opt/scripts/frappe_db_backup.sh
    sudo sed -i "s/your-site-name/$SITE_NAME/g" /opt/scripts/frappe_restore.sh
    echo "Site name configured: $SITE_NAME"
fi

# 5. Set ownership and restrict access (backups contain credentials)
sudo chown -R root:root /opt/scripts/
sudo chown -R root:root /opt/backups/
sudo chmod 700 /opt/backups/frappe

# 6. Set up cron jobs
echo ""
echo "Setting up cron jobs..."
echo "Do you want to install automated backups via cron? (yes/no)"
read -p "Install cron jobs: " INSTALL_CRON

if [ "$INSTALL_CRON" = "yes" ]; then
    # Add to root crontab
    (sudo crontab -l 2>/dev/null || true; echo "") | sudo tee /tmp/crontab.tmp > /dev/null
    echo "# Frappe Backup Jobs" | sudo tee -a /tmp/crontab.tmp > /dev/null
    echo "0 2 * * * /opt/scripts/frappe_backup.sh >> /var/log/frappe_backup.log 2>&1" | sudo tee -a /tmp/crontab.tmp > /dev/null
    echo "0 */6 * * * /opt/scripts/frappe_db_backup.sh >> /var/log/frappe_db_backup.log 2>&1" | sudo tee -a /tmp/crontab.tmp > /dev/null
    echo "0 3 * * 0 /opt/scripts/frappe_backup_verify.sh >> /var/log/frappe_backup_verify.log 2>&1" | sudo tee -a /tmp/crontab.tmp > /dev/null
    
    sudo crontab /tmp/crontab.tmp
    sudo rm /tmp/crontab.tmp
    
    echo "Cron jobs installed successfully!"
    echo "- Full backup: Daily at 2:00 AM"
    echo "- DB backup: Every 6 hours"
    echo "- Verification: Weekly on Sunday at 3:00 AM"
fi

# 7. Test backup
echo ""
echo "Do you want to run a test backup now? (yes/no)"
read -p "Run test backup: " RUN_TEST

if [ "$RUN_TEST" = "yes" ]; then
    echo "Running test backup..."
    sudo /opt/scripts/frappe_backup.sh
    echo ""
    echo "Test backup completed! Check /opt/backups/frappe/"
fi

echo ""
echo "=== Installation Complete ==="
echo ""
echo "Backup scripts installed in: /opt/scripts/"
echo "Backups will be stored in: /opt/backups/frappe/"
echo "Logs available in: /var/log/frappe_*.log"
echo ""
echo "Manual backup commands:"
echo "  Full backup:     sudo /opt/scripts/frappe_backup.sh"
echo "  DB only:         sudo /opt/scripts/frappe_db_backup.sh"
echo "  Verify backups:  sudo /opt/scripts/frappe_backup_verify.sh"
echo "  Restore:         sudo /opt/scripts/frappe_restore.sh /path/to/backup/folder"
echo ""
echo "View cron jobs:    sudo crontab -l"
echo "Edit cron jobs:    sudo crontab -e"
echo ""
