# SSPL ERP Update & Rollback System

This system provides safe Docker image updates with automatic backup and rollback capability.

## 📁 Files

- `sspl-erp-common.sh` - Shared configuration (site name, paths) and helpers, sourced by the other scripts
- `sspl-erp-update-with-rollback.sh` - Main update script with automatic backup
- `sspl-erp-rollback.sh` - Rollback to previous version
- `sspl-erp-backup-manager.sh` - Manage backup files

## 🚀 Installation

1. Copy the scripts to your server:
```bash
cd /opt/sspl-erp
# Upload the four scripts here
```

2. Set your site name in `sspl-erp-common.sh` (the `SITE_NAME` variable at the top).

3. Make scripts executable:
```bash
chmod +x sspl-erp-update-with-rollback.sh
chmod +x sspl-erp-rollback.sh
chmod +x sspl-erp-backup-manager.sh
```

## 📖 Usage

### Update System (with automatic backup)

```bash
cd /opt/sspl-erp
./sspl-erp-update-with-rollback.sh
```

**What it does:**
1. Runs a full Frappe backup (database + files) via `/opt/scripts/frappe_backup.sh`
2. Backs up current Docker images to `/opt/sspl-erp/image-backups/backup_TIMESTAMP.tar`
3. Stops all services
4. Pulls latest images
5. Starts services and waits until the database and backend are actually ready
6. Fixes DB grants, runs migrations, and clears cache

If any step fails, the script stops and prints rollback instructions.

### Rollback to Previous Version

```bash
cd /opt/sspl-erp
./sspl-erp-rollback.sh
```

**What it does:**
1. Shows the latest backup information
2. Asks for confirmation
3. Stops all services
4. Restores the backed-up Docker images
5. Starts services with restored images

To roll back to a specific (non-latest) backup:
```bash
BACKUP_FILE=/opt/sspl-erp/image-backups/backup_20240421_143000.tar ./sspl-erp-rollback.sh
```

### Manage Backups

**List all backups:**
```bash
./sspl-erp-backup-manager.sh list
```

**Clean old backups (keep 3 most recent):**
```bash
./sspl-erp-backup-manager.sh clean
```

**Keep 5 most recent backups:**
```bash
./sspl-erp-backup-manager.sh clean 5
```

**Delete all backups:**
```bash
./sspl-erp-backup-manager.sh delete-all
```

## 💾 Backup Storage

- Backups are stored in: `/opt/sspl-erp/image-backups/`
- Each backup is named: `backup_YYYYMMDD_HHMMSS.tar`
- Backup sizes can be large (depends on your Docker images)

## ⚠️ Important Notes

1. **Disk Space**: Backups can be several GB in size. Monitor your disk space:
   ```bash
   df -h /opt/sspl-erp
   ```

2. **Database**: The rollback only restores Docker images, not database data. If migrations changed the database structure, you may need to restore a database backup separately.

3. **Regular Cleanup**: Clean old backups regularly to save disk space:
   ```bash
   ./sspl-erp-backup-manager.sh clean 3
   ```

4. **Testing**: Always test rollback in a staging environment first.

## 🔄 Typical Workflow

### Standard Update
```bash
# 1. Update with automatic backup
./sspl-erp-update-with-rollback.sh

# 2. Test the system
# ... verify everything works ...

# 3. Clean old backups (keep last 3)
./sspl-erp-backup-manager.sh clean 3
```

### Emergency Rollback
```bash
# 1. If something goes wrong after update
./sspl-erp-rollback.sh

# 2. Verify system is working
docker compose ps
docker compose logs
```

## 🛠️ Troubleshooting

### Rollback fails to load images
```bash
# Check if backup file exists and is valid
ls -lh /opt/sspl-erp/image-backups/
tar -tvf /opt/sspl-erp/image-backups/backup_*.tar | head
```

### Backup taking too much space
```bash
# Check backup sizes
du -sh /opt/sspl-erp/image-backups/*

# Clean old backups
./sspl-erp-backup-manager.sh clean 2
```

### Manual rollback to specific backup
```bash
# List available backups
./sspl-erp-backup-manager.sh list

# Roll back to a specific backup
BACKUP_FILE=/opt/sspl-erp/image-backups/backup_20240421_143000.tar ./sspl-erp-rollback.sh

# Or manually load:
docker load -i /opt/sspl-erp/image-backups/backup_20240421_143000.tar
docker compose up -d
```

## 📊 Monitoring

After update or rollback, verify the system:

```bash
# Check service status
docker compose ps

# Check logs
docker compose logs --tail=50

# Check version
docker compose exec backend bench version

# Check site status
docker compose exec backend bench --site 192.168.225.135 doctor
```

## 🔐 Security

- Backup files contain your Docker images (not database data)
- Keep backups in a secure location
- Limit access to the backup directory
- Consider encrypting backups for sensitive systems

## 📝 Cron Job (Optional)

To automatically clean old backups weekly:

```bash
# Add to crontab (--yes is required: without a terminal the script cannot ask for confirmation)
0 2 * * 0 /opt/sspl-erp/sspl-erp-backup-manager.sh clean 5 --yes >> /var/log/sspl-erp-backup-clean.log 2>&1
```

This runs every Sunday at 2 AM, keeping the 5 most recent backups.
