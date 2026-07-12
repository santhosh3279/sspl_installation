# Frappe Docker Backup Configuration Guide

## Overview
This backup system provides automated, reliable backups for your Frappe/ERPNext Docker deployment.

## What Gets Backed Up
1. **Database** - Complete MariaDB dump with all ERPNext data
2. **Files** - All uploaded files, private files, and attachments
3. **Configuration** - site_config.json and docker-compose.yml
4. **Metadata** - Backup manifest with timestamps and system info

## Directory Structure
```
/opt/backups/frappe/
├── 20250330_020000/          # Full backup (daily)
│   ├── *-database.sql.gz     # Compressed database
│   ├── *-files.tgz           # Public files archive
│   ├── *-private-files.tgz   # Private files archive (attachments)
│   ├── site_config.json      # Site configuration
│   ├── docker-compose.yml    # Docker config
│   └── backup_manifest.txt   # Backup metadata
├── 20250330_080000/          # Another backup
└── db-only/                  # Quick DB backups (every 6 hours)
    ├── 20250330_080000_*.sql.gz
    └── 20250330_140000_*.sql.gz
```

## Installation Steps

### 1. Download and Install
```bash
# Make scripts executable
chmod +x frappe_backup.sh frappe_db_backup.sh frappe_restore.sh
chmod +x frappe_backup_verify.sh setup_frappe_backups.sh

# Run installation script
sudo ./setup_frappe_backups.sh
```

### 2. Find Your Site Name
```bash
docker compose -f /opt/sspl-erp/docker-compose.yml exec backend ls sites/
```
The site name is usually something like: `erp.yourdomain.com` or `sspl.local`

### 3. Edit Configuration
Update the following in each script:
- `SITE_NAME="your-site-name"` - Replace with your actual site name
- `BACKUP_DIR="/opt/backups/frappe"` - Change if you want different location
- `RETENTION_DAYS=30` - Adjust how long to keep backups

### 4. Test Manual Backup
```bash
sudo /opt/scripts/frappe_backup.sh
```

Check if backup was created:
```bash
ls -lh /opt/backups/frappe/
```

## Automated Backup Schedule

### Default Cron Jobs:
- **Full Backup**: 2:00 AM daily (includes DB + files + config)
- **DB Backup**: Every 6 hours (quick database snapshots)
- **Verification**: 3:00 AM Sunday (checks backup integrity)

### View Cron Jobs:
```bash
sudo crontab -l
```

### Edit Cron Schedule:
```bash
sudo crontab -e
```

### Custom Schedule Examples:
```cron
# Hourly DB backups during business hours (9 AM - 6 PM)
0 9-18 * * * /opt/scripts/frappe_db_backup.sh >> /var/log/frappe_db_backup.log 2>&1

# Full backup twice daily (2 AM and 2 PM)
0 2,14 * * * /opt/scripts/frappe_backup.sh >> /var/log/frappe_backup.log 2>&1

# Weekly full backup only (Sunday 3 AM)
0 3 * * 0 /opt/scripts/frappe_backup.sh >> /var/log/frappe_backup.log 2>&1
```

## Backup Retention

Default: 30 days for full backups, 14 days for DB-only backups

### Adjust Retention:
Edit `RETENTION_DAYS` in scripts:
```bash
sudo nano /opt/scripts/frappe_backup.sh
# Change: RETENTION_DAYS=30  to your preferred value
```

### Manual Cleanup:
```bash
# Remove backups older than 60 days
find /opt/backups/frappe -type d -name "20*" -mtime +60 -exec rm -rf {} \;
```

## Restore Process

### 1. List Available Backups:
```bash
ls -lh /opt/backups/frappe/
```

### 2. Restore from Backup:
```bash
sudo /opt/scripts/frappe_restore.sh /opt/backups/frappe/20250330_020000
```

### 3. Manual Restore (if script fails):
```bash
# Copy backup to container
docker compose -f /opt/sspl-erp/docker-compose.yml cp \
    /opt/backups/frappe/20250330_020000/*-database.sql.gz \
    backend:/tmp/

# Enter container
docker compose -f /opt/sspl-erp/docker-compose.yml exec backend bash

# Inside container (use your actual MariaDB root password):
cd frappe-bench
bench --site your-site-name restore \
    --mariadb-root-password <your-root-password> \
    /tmp/*-database.sql.gz
```

## Remote Backup Options

### Option 1: Rsync to Remote Server
Add to backup script:
```bash
# After local backup completes
rsync -avz --delete /opt/backups/frappe/ \
    user@backup-server:/backups/sspl-erp/
```

### Option 2: Rclone (Cloud Storage)
```bash
# Install rclone
curl https://rclone.org/install.sh | sudo bash

# Configure (Google Drive, S3, etc.)
rclone config

# Add to backup script:
rclone copy /opt/backups/frappe/ gdrive:frappe-backups/ \
    --include "20*/**" --max-age 7d
```

### Option 3: AWS S3
```bash
# Install AWS CLI
sudo apt install awscli

# Configure credentials
aws configure

# Add to backup script:
aws s3 sync /opt/backups/frappe/ \
    s3://your-bucket/frappe-backups/ \
    --exclude "*" --include "20*/*"
```

## Monitoring and Alerts

### Check Backup Logs:
```bash
tail -f /var/log/frappe_backup.log
tail -f /var/log/frappe_db_backup.log
```

### Email Alerts (configure in verify script):
```bash
# Install mail utilities
sudo apt install mailutils

# Edit verify script and set:
ALERT_EMAIL="your-email@example.com"

# Test email
echo "Test" | mail -s "Backup Test" your-email@example.com
```

### Disk Space Monitoring:
```bash
# Check backup disk usage
du -sh /opt/backups/frappe/

# Monitor in real-time
watch -n 60 'df -h | grep -E "(Filesystem|backups)"'
```

## Troubleshooting

### Backup Script Fails:
```bash
# Check Docker status
docker compose -f /opt/sspl-erp/docker-compose.yml ps

# Check permissions
ls -la /opt/backups/frappe/

# Run with verbose logging
bash -x /opt/scripts/frappe_backup.sh
```

### Container Not Found:
```bash
# Ensure containers are running
docker compose -f /opt/sspl-erp/docker-compose.yml up -d

# List container names
docker compose -f /opt/sspl-erp/docker-compose.yml ps --format "{{.Service}}"
```

### Database Connection Issues:
```bash
# Check database credentials
docker compose -f /opt/sspl-erp/docker-compose.yml exec backend \
    bench --site your-site-name console

# Inside console:
frappe.conf.db_password
```

### Restore Fails:
```bash
# Ensure site exists
docker compose -f /opt/sspl-erp/docker-compose.yml exec backend \
    bench --site your-site-name list-apps

# If site doesn't exist, create it first:
docker compose -f /opt/sspl-erp/docker-compose.yml exec backend \
    bench new-site your-site-name
```

## Security Best Practices

1. **Encrypt Backups**: Use GPG encryption for sensitive data
   ```bash
   gpg --symmetric --cipher-algo AES256 backup.sql.gz
   ```

2. **Secure Backup Directory**: Restrict permissions
   ```bash
   sudo chmod 700 /opt/backups/frappe
   sudo chown root:root /opt/backups/frappe
   ```

3. **Store Backups Off-Site**: Always maintain remote copies

4. **Test Restores Regularly**: Verify backups work before you need them

5. **Rotate Passwords**: Update MariaDB root password if needed
   ```bash
   docker compose -f /opt/sspl-erp/docker-compose.yml exec db \
       mariadb -u root -p
   # SET PASSWORD = PASSWORD('new-password');
   ```

## Quick Reference Commands

```bash
# Manual full backup
sudo /opt/scripts/frappe_backup.sh

# Manual DB backup
sudo /opt/scripts/frappe_db_backup.sh

# Verify backups
sudo /opt/scripts/frappe_backup_verify.sh

# Restore
sudo /opt/scripts/frappe_restore.sh /path/to/backup

# Check cron jobs
sudo crontab -l

# View logs
tail -f /var/log/frappe_backup.log

# Disk usage
du -sh /opt/backups/frappe/*

# Find site name
docker compose -f /opt/sspl-erp/docker-compose.yml exec backend ls sites/
```

## Support
For issues specific to your `ssplbilling` deployment, check:
- Container logs: `docker compose -f /opt/sspl-erp/docker-compose.yml logs`
- Frappe logs: Inside container at `/home/frappe/frappe-bench/logs/`
- Backup logs: `/var/log/frappe_*.log`
