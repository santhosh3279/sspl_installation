# SSPL ERP Admin Panel

A small web interface for managing the SSPL ERP server from a browser on the LAN.

## Features

- **Server health** — CPU usage, load average, memory/swap/disk meters, uptime,
  live ERP container status
- **Clear RAM caches** button (`sync` + drop_caches — safe, caches rebuild automatically)
- **One-click actions** — full backup, DB-only backup, backup verification,
  system update, image rollback (with snapshot picker)
- **Live job console** — watch the script output while it runs; only one job
  can run at a time
- **Backup browser** — full backups (with DB/Files/Private completeness badges),
  DB-only dumps, Docker image snapshots, uploads — all downloadable
- **Upload backup files** to the server (stored under
  `/opt/backups/frappe/uploads/`, optionally in a named subfolder)
- **Admin login** — single admin user, hashed password, session cookie
- **HTTPS** — self-signed certificate generated at install; all traffic
  (passwords, backups) is encrypted on the LAN

## Requirements

- The v2 backup scripts installed in `/opt/scripts/v2/` (Part 1 of the guide)
- The v2 update/rollback scripts installed in `/opt/sspl-erp/v2/` (Part 2)
- Python 3 with `venv` (`sudo apt install python3-venv` if missing)

## Installation

```bash
cd "Admin Panel"
chmod +x setup_admin_panel.sh
./setup_admin_panel.sh
```

The installer asks for an admin username, password, and port (default **8090**),
generates a self-signed HTTPS certificate for the server IP (valid 10 years),
then installs everything to `/opt/sspl-admin/` and starts a systemd service.

Open `https://<server-ip>:8090` and log in.

### About the HTTPS warning

The certificate is self-signed (a LAN IP cannot get a public certificate), so
each browser shows a security warning the **first** time: click
*Advanced → Proceed*. The connection is fully encrypted either way. To make the
warning disappear on a particular PC, import
`/opt/sspl-admin/certs/sspl-admin.crt` into that machine's trusted certificate
store. To regenerate the certificate (e.g. after changing the server IP),
delete `/opt/sspl-admin/certs/` and re-run the installer.

## Managing the service

```bash
sudo systemctl status sspl-admin      # is it running?
sudo systemctl restart sspl-admin     # restart after config/app changes
sudo journalctl -u sspl-admin -f      # follow the logs
```

Job output is also kept in `/opt/sspl-admin/jobs/*.log`.

To change the admin password or port, simply re-run `./setup_admin_panel.sh`.

## Configuration

`/opt/sspl-admin/config.json` holds the credentials and port. These optional
keys override the default paths (useful for testing):

```json
{
  "backup_dir": "/opt/backups/frappe",
  "image_backup_dir": "/opt/sspl-erp/image-backups",
  "compose_file": "/opt/sspl-erp/docker-compose.yml",
  "scripts_dir": "/opt/scripts/v2",
  "update_dir": "/opt/sspl-erp/v2",
  "job_dir": "/opt/sspl-admin/jobs"
}
```

## Security notes

- Intended for the **trusted LAN only** — do not expose the port to the internet.
- The service runs as root (required for docker, drop_caches, and reading
  `/opt/backups`). All actions map to the fixed v2 scripts; no arbitrary
  commands can be run from the web.
- **Restore is deliberately not in the web UI**: `frappe_restore.sh` asks for
  the MariaDB root password and overwrites live data, so it stays a
  console-only operation.
- Uploads accept only backup-type files (`.sql.gz`, `.tar`, `.tgz`, `.json`,
  `.yml`) and are stored under `/opt/backups/frappe/uploads/` — they are never
  executed or restored automatically.
