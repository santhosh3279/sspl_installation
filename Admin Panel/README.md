# SSPL ERP Admin Panel

A small web interface for managing the SSPL ERP server from a browser on the LAN.

## Features

- **Setup switches** — install the whole system from the browser: ERPNext
  stack, backup system, and update/rollback scripts, each with a live
  install log. The panel is the only thing you install by hand; everything
  else is a click. See [Panel-first setup](#panel-first-setup).
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

- Docker + Docker Compose on the server (the panel installs ERPNext for you)
- Python 3 with `venv` (`sudo apt install python3-venv` if missing)
- **This repository stays checked out on the server.** The Setup switches
  run the installer scripts from the git checkout; its path is recorded as
  `repo_dir` in `config.json`. Don't delete the clone after setup — and
  `git pull` there keeps the installers current.

The backup and update/rollback scripts do **not** need to be installed
first — the panel installs them (see below).

## Installation

```bash
git clone https://github.com/santhosh3279/sspl_installation.git
cd sspl_installation/"Admin Panel"
chmod +x setup_admin_panel.sh
./setup_admin_panel.sh
```

The installer asks for an admin username, password, and port (default **8090**),
generates a self-signed HTTPS certificate for the server IP (valid 10 years),
records the repo location, then installs everything to `/opt/sspl-admin/` and
starts a systemd service.

Open `https://<server-ip>:8090` and log in.

## Panel-first setup

Once logged in, the **Setup — install components** card at the top shows what
is installed and lets you install the rest, in order:

1. **ERPNext stack** — fill in the server IP, HTTP port, MariaDB root
   password, and Administrator password, then click *Install ERPNext*. This
   pulls the image, starts the containers, and creates the site (10–20 min).
   Watch progress in the Job console below.
2. **Backup system** and **Update & rollback scripts** — enabled once ERPNext
   is installed (they reuse the deployed site name automatically). One click
   each.

Each switch turns into an "installed ✓" status once done. After that you use
the same page day-to-day: run backups, run updates, roll back, clear RAM, and
watch server health.

The passwords you type into the ERP form are sent to the installer as
environment variables over HTTPS and are **never written to the job log**.
The `bench new-site` step still passes them on its in-container command line
(visible to `ps` inside the container only) — acceptable on the trusted LAN.

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

`/opt/sspl-admin/config.json` holds the credentials, port, and `repo_dir`
(the git checkout the Setup switches install from). These optional keys
override the default paths (useful for testing):

```json
{
  "repo_dir": "/home/erpdev/sspl_installation",
  "server_ip": "192.168.225.135",
  "erp_dir": "/opt/sspl-erp",
  "backup_dir": "/opt/backups/frappe",
  "image_backup_dir": "/opt/sspl-erp/image-backups",
  "compose_file": "/opt/sspl-erp/docker-compose.yml",
  "scripts_dir": "/opt/scripts/v2",
  "update_dir": "/opt/sspl-erp/v2",
  "job_dir": "/opt/sspl-admin/jobs"
}
```

If `repo_dir` is missing or the installer scripts aren't found there, the
Setup card says so and the install switches are hidden — the operational
buttons still work.

## Security notes

- Intended for the **trusted LAN only** — do not expose the port to the internet.
- The service runs as root (required for docker, drop_caches, and reading
  `/opt/backups`). All actions — including the Setup installs — map to fixed
  scripts in the repo and the v2 directories; no arbitrary commands can be
  run from the web.
- Install actions receive their inputs (site IP, passwords) as validated
  environment variables passed to the script, never as shell arguments and
  never echoed to the job log.
- Don't restart the `sspl-admin` service (or run `update_tooling.sh`) while
  an install or backup job is running: the panel tracks the running job in
  memory, so a restart loses the handle and the job console will show "no
  job" even though the underlying script keeps running to completion. Check
  `/opt/sspl-admin/jobs/*.log` if in doubt.
- **Restore is deliberately not in the web UI**: `frappe_restore.sh` asks for
  the MariaDB root password and overwrites live data, so it stays a
  console-only operation.
- Uploads accept only backup-type files (`.sql.gz`, `.tar`, `.tgz`, `.json`,
  `.yml`) and are stored under `/opt/backups/frappe/uploads/` — they are never
  executed or restored automatically.
