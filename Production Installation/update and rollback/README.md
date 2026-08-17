# SSPL ERP Update & Rollback System

This system provides safe Docker image updates with automatic backup and rollback capability.

## 📁 Files

- `sspl-erp-common.sh` - Shared configuration (site name, paths) and helpers, sourced by the other scripts
- `sspl-erp-update-with-rollback.sh` - Main update script with automatic backup
- `sspl-erp-rollback.sh` - Rollback to previous version
- `sspl-erp-backup-manager.sh` - Manage backup files

## 🚀 Installation

The new scripts install **side-by-side** with the old ones: they go into
`/opt/sspl-erp/v2/`, and any older `sspl-erp-*.sh` scripts directly in
`/opt/sspl-erp/` are left untouched as a fallback. Both generations share the
same image backup directory (`/opt/sspl-erp/image-backups/`), so a snapshot
made by one can be rolled back with the other.

1. Copy the scripts to your server:
```bash
sudo mkdir -p /opt/sspl-erp/v2
# Upload the four scripts into /opt/sspl-erp/v2/
```

2. Set your site name in `/opt/sspl-erp/v2/sspl-erp-common.sh` (the `SITE_NAME` variable at the top).

3. Make scripts executable:
```bash
cd /opt/sspl-erp/v2
chmod +x sspl-erp-update-with-rollback.sh
chmod +x sspl-erp-rollback.sh
chmod +x sspl-erp-backup-manager.sh
```

The scripts can be run from anywhere (they switch to `/opt/sspl-erp`
internally), so `cd /opt/sspl-erp/v2` first or use full paths — both work.

## 📖 Usage

### Update System (with automatic backup)

```bash
cd /opt/sspl-erp/v2
./sspl-erp-update-with-rollback.sh
```

**What it does:**
1. Runs a full Frappe backup (database + files) via `/opt/scripts/v2/frappe_backup.sh`
2. Backs up current Docker images to `/opt/sspl-erp/image-backups/backup_TIMESTAMP.tar`
3. Prunes unused Docker resources and pulls the latest images **with the site still
   running** — the download is the longest step, so it is kept out of the downtime
   window. A failed pull changes nothing and leaves the site up on current images.
4. Stops all services
5. Starts services and waits until the database and backend are actually ready
6. Fixes DB grants
7. Takes the user-facing services (frontend, websocket, queues, scheduler)
   offline and runs migrations via `/opt/scripts/v2/frappe_migrate.sh`.
   `up -d` in step 5 started everything, so without this window users would be
   on a site whose code is new and whose schema is still moving.
8. Installs any app the new image added that the site does not have yet —
   `bench migrate` only touches apps already installed, so an app added to the
   image reaches an existing site only here. Already-installed apps are skipped.
9. Clears cache, and brings the site back online

If any step fails, the script stops and prints rollback instructions. The site
is brought back online whatever happens — the offline window is closed from a
trap, so a failure, a Ctrl-C or a kill cannot leave the site dark.

### If the update fails at the migration step

The migration is the one step that changes the database, and a patch belonging
to any app in the new image can fail there. The script stops, puts the site
back online, and does **not** install new apps or clear caches on top of a
part-migrated database. You are then running the new images against a database
whose patches did not finish, so expect errors until you do one of:

1. **Fix the app that failed** (the traceback names it), then re-run only the
   migration, leaving the new images in place:
   `sudo /opt/scripts/v2/frappe_migrate.sh`
2. **Roll the images back**: `/opt/sspl-erp/v2/sspl-erp-rollback.sh`

Rollback is second, not first, because it is not a clean undo here. `bench
migrate` synchronises the schema *before* it runs the post-sync patches, so by
the time a patch fails the tables have already been altered — and the rollback
restores images only. Old code against a moved schema usually works, because
Frappe tolerates columns it does not know about, but it is not the matching
pair it looks like. If the old code cannot cope, restore the data backup the
update took in step 1.

Restoring a data backup is not the first thing to reach for either: the data is
intact, the patches are what is missing. See the restore section of
`BACKUP_GUIDE.md` for the `--skip-failing` and `bypass-patch` escape hatches.

Note that step 8 writes to the database, and `sspl-erp-rollback.sh` restores
images only — it cannot uninstall an app. If an update fails while installing
one, restore the data backup from step 1 with `frappe_restore.sh` — this is a
genuine data change, unlike a failed migration, which restoring does not fix.
Rehearse the
first update after an image gains an app on a test server.

### Rollback to Previous Version

```bash
cd /opt/sspl-erp/v2
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
BACKUP_FILE=/opt/sspl-erp/image-backups/backup_20240421_143000.tar /opt/sspl-erp/v2/sspl-erp-rollback.sh
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
BACKUP_FILE=/opt/sspl-erp/image-backups/backup_20240421_143000.tar /opt/sspl-erp/v2/sspl-erp-rollback.sh

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
0 2 * * 0 /opt/sspl-erp/v2/sspl-erp-backup-manager.sh clean 5 --yes >> /var/log/sspl-erp-backup-clean.log 2>&1
```

This runs every Sunday at 2 AM, keeping the 5 most recent backups.
