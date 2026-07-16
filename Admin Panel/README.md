# SSPL ERP Admin Panel

A small web interface for managing the SSPL ERP server from a browser on the LAN.

## Layout

Two pages, linked from the header:

- **Dashboard** (`/`) — the day-to-day view: server health, actions, backups,
  upload.
- **ERP Next Installation suite** (`/install`) — installing the components onto
  a new server. A one-off job, so it stays out of the way of the daily view.

Each page is one column of cards, then a full-width **terminal** at the foot
showing the live output of whatever is running. The panel runs one job at a
time, so a job started on either page is readable from both. Starting a job
scrolls the terminal into view; drag its bottom edge to make it taller.

The terminal behaves like a console: you type into the terminal itself, not
into a box beside it, and Enter submits. Output is rendered the way a
terminal renders it — ANSI colour/cursor escapes are obeyed as control codes
rather than printed, and `\r` overwrites its line, so docker's progress
redraws read cleanly instead of as escape-code soup.

## Features

- **ERP Next Installation suite** — install the whole system from the browser:
  ERPNext stack, backup system, and update/rollback scripts, each with a live
  install log. The panel is the only thing you install by hand; everything
  else is a click. See [Panel-first setup](#panel-first-setup).
- **Cloud backup (rclone)** — install rclone and point backups at a cloud
  remote from the suite, with the three stages reported separately so a
  half-finished setup can't look finished. See [Cloud backup](#cloud-backup).
- **Server health** — CPU usage, load average, memory/swap/disk meters, uptime,
  live ERP container status
- **Clear RAM caches** button (`sync` + drop_caches — safe, caches rebuild automatically)
- **One-click actions** — full backup, DB-only backup, backup verification,
  system update, image rollback (with snapshot picker)
- **Guarded restore** — restore a full backup or an uploaded backup folder:
  re-enter your admin password, type the site name to confirm, and a safety
  backup is taken first. The MariaDB root password is typed into the live
  terminal, never stored. See [Restoring](#restoring-from-the-panel).
- **Live terminal** — watch the script output while it runs, full width at
  the foot of the page; type into it directly when a job asks a question;
  only one job can run at a time
- **Backup browser** — full backups (with DB/Files/Private completeness badges),
  DB-only dumps, Docker image snapshots, uploads — all downloadable
- **Upload backup files** to the server (stored under
  `/opt/backups/frappe/uploads/`, optionally in a named subfolder), and
  **delete** them again — files or whole folders. Deleting is confined to
  `uploads/`: real backups and image snapshots have no delete button and
  cannot be reached by the endpoint.
- **Admin login** — single admin user, hashed password, session cookie
- **HTTPS** — self-signed certificate generated at install; all traffic
  (passwords, backups) is encrypted on the LAN

## Requirements

- Docker + Docker Compose on the server (the panel installs ERPNext for you)
- Python 3 with `venv` (`sudo apt install python3-venv` if missing)
- **This repository stays checked out on the server.** The installation suite
  runs the installer scripts from the git checkout; its path is recorded as
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

Once logged in, follow **ERP Next Installation suite** in the header. That page
shows what is installed and lets you install the rest, in order:

1. **ERPNext stack** — fill in the server IP, HTTP port, MariaDB root
   password, and Administrator password, then click *Install ERPNext*. This
   pulls the image, starts the containers, and creates the site (10–20 min).
   Watch progress in the terminal at the foot of the page.
2. **Backup system** and **Update & rollback scripts** — enabled once ERPNext
   is installed (they reuse the deployed site name automatically). One click
   each.

Each row turns into an "installed ✓" status once done. After that the
installation suite has served its purpose, and the dashboard is the page you
use day-to-day: run backups, run updates, roll back, clear RAM, and watch
server health.

## Cloud backup

By default a backup stays on the server: `frappe_backup.sh` only uploads when
its `RCLONE_REMOTE` is set, and it ships empty. The **Cloud backup (rclone)**
row in the installation suite sets that up, and reports the three stages
separately — all three must be green before a backup leaves the machine:

1. **rclone installed** — the *Install rclone* button. Uses the official
   installer rather than apt, because a distro rclone can be old enough to
   still use Google's withdrawn out-of-band auth flow, which no longer
   completes.
2. **Cloud account connected** — done over SSH with `sudo rclone config`, not
   from the panel (see below). The panel lists whatever remotes it finds.
3. **Backups upload to it** — pick the remote and folder, and the panel writes
   `RCLONE_REMOTE` into the deployed `frappe_backup.sh`.

Step 2 is deliberately not a button. It is an OAuth flow needing a browser on
another machine, and `rclone config` prints the account's long-lived refresh
token as it goes — run as a panel job, that token would land in the job log and
render in the terminal for anyone signed in to the panel. The suite's built-in
guide walks through it, and the full `Rclone_Configuration_Guide.docx`
(S3, Dropbox, testing, encrypting the config) downloads from that page.

**Run `rclone config` as root.** Backups run as root, and rclone only reads the
config of the user that created it — a remote configured as `erpdev` is
invisible to the backup job, which then uploads nothing.

Two things worth knowing about how this fails:

- A failed upload is only a **warning**. `frappe_backup.sh` still exits 0, so
  the panel reports the backup as successful. If cloud copies matter, check the
  job log for `WARNING: Cloud upload failed`. The suite guards the usual cause
  from both ends: it refuses to wire a remote rclone doesn't know, and stage 3
  turns red if the deployed script already points at one that doesn't exist
  (easy to inherit — `gdrive:frappe-backups` is the guide's example string).
- Re-running `setup_frappe_backups.sh` copies the script over and **clears
  stage 3**, silently returning backups to local-only. `update_tooling.sh`
  preserves it. Re-check the suite after reinstalling backups.

The passwords you type into the ERP form are sent to the installer as
environment variables over HTTPS and are **never written to the job log**.
The `bench new-site` step still passes them on its in-container command line
(visible to `ps` inside the container only) — acceptable on the trusted LAN.

## Restoring from the panel

Restore is the one action that destroys data, so it is deliberately not a
plain button.

1. Find the backup in the **Backups** card — either a full backup, or an
   uploaded folder under the **Uploads** tab. Only folders containing a
   `*-database.sql.gz` get a **Restore** button.
2. A warning names the exact site about to be overwritten. Type the **site
   name** in full and re-enter your **admin panel password**.
3. The job takes a **full safety backup first**, then starts the restore.
   If the backup fails, the restore does not run.
4. The restore asks for the **MariaDB root password** in the terminal at the
   foot of the page. A prompt appears inside the terminal — type there and
   press Enter, then answer `yes` to the final confirmation.

Note the prompt is an ordinary text input, so **what you type is visible on
your screen** as you type it (unlike a console, which hides the password) —
mind your shoulder. It is not echoed into the job log at any point.

To abort a restore that is waiting at a prompt, answer `no` at the
confirmation. If nobody is there to answer, `sudo systemctl restart
sspl-admin` ends it: the script's next read hits end-of-file and it exits.
Until it ends, the one-job-at-a-time rule blocks the panel's other buttons
(cron backups are unaffected — they don't go through the panel).

To restore an **uploaded** backup, upload it into its own named folder (the
*Folder* box on the upload form) so its database and files stay together. A
folder is what gets restored, not a loose file: pointing the restore at a
directory of unrelated uploads would mix backups from different dates.

The console path still works and is unchanged:
`sudo /opt/scripts/v2/frappe_restore.sh /opt/backups/frappe/<TIMESTAMP>`.

### About the HTTPS warning

The certificate is self-signed (a LAN IP cannot get a public certificate), so
each browser shows a security warning the **first** time: click
*Advanced → Proceed*. The connection is fully encrypted either way. To make the
warning disappear on a particular PC, import
`/opt/sspl-admin/certs/sspl-admin.crt` into that machine's trusted certificate
store. To regenerate the certificate (e.g. after changing the server IP),
delete `/opt/sspl-admin/certs/` and re-run the installer.

## Which version am I running?

The panel header shows a version badge, e.g. `v2026-07-15.4`. That is baked
into the `app.py` that is **actually running**, so it is the honest answer to
"did my update take effect?".

Updating is two steps, and missing the second is the usual reason a new
feature doesn't appear:

1. `git pull && ./update_tooling.sh` — copies the new `app.py` **and**
   restarts the service. It prints the version it installed.
2. **Hard-refresh the browser** (Ctrl+Shift+R). The page's HTML, CSS and JS
   are served inline, so a cached page looks identical to old code.

If the badge still shows an old version after both, the service didn't
restart: `sudo systemctl restart sspl-admin`, then
`sudo journalctl -u sspl-admin -n 20` — the panel prints its version and
feature list on startup.

| Version | Should show |
|---|---|
| `2026-07-15.4` | Terminal full-width at the foot of the page, typed into directly |
| `2026-07-15.3` | Delete buttons on uploads |
| `2026-07-15.2` | Restore buttons on backups/uploads, interactive terminal |
| `2026-07-15.1` | Two-column layout, no restore |

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
(the git checkout the installation suite installs from). These optional keys
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
installation suite says so and the install buttons are hidden — the dashboard's
operational buttons still work.

## Security notes

- Intended for the **trusted LAN only** — do not expose the port to the internet.
- The service runs as root (required for docker, drop_caches, and reading
  `/opt/backups`). All actions — including the suite's installs — map to fixed
  scripts in the repo and the v2 directories; no arbitrary commands can be
  run from the web.
- Install actions receive their inputs (site IP, passwords) as validated
  environment variables passed to the script, never as shell arguments and
  never echoed to the job log.
- Don't restart the `sspl-admin` service (or run `update_tooling.sh`) while
  an install or backup job is running: the panel tracks the running job in
  memory, so a restart loses the handle and the terminal will show "no
  job" even though the underlying script keeps running to completion. Check
  `/opt/sspl-admin/jobs/*.log` if in doubt.
- **Restore** overwrites live data, so it is gated three ways, all checked
  server-side: your panel password is re-entered, the live site name is
  typed out in full, and the source must resolve to a real backup folder
  inside the backup roots. A full safety backup runs first, welded into the
  same script (`restore_with_backup.sh`) so it cannot be skipped — if the
  backup fails, the restore never starts.
- The **terminal input channel** (`/api/job/input`) can only type into the
  job that is already running — it cannot start a process, and only the
  restore action accepts input. It is not a shell: no arbitrary command can
  be run from the browser. The MariaDB root password is typed straight into
  the running script's terminal, so it is never an argument and never stored.
- Interactive jobs run on a pty with **echo disabled for the whole job**, not
  just during `read -s`. A pty echoes input into the output stream, i.e. into
  the job log; bash only suppresses that while a `read -s` is waiting, so a
  password typed at any other moment (early, pasted, typed ahead during the
  safety backup) would otherwise be logged in clear — and the restore would
  still succeed, hiding the leak. Tested at both timings.
- Job logs in `/opt/sspl-admin/jobs/` are created `0600`.
- Uploads accept only backup-type files (`.sql.gz`, `.tar`, `.tgz`, `.json`,
  `.yml`) and are stored under `/opt/backups/frappe/uploads/` — they are never
  executed or restored automatically.
- **Deleting is confined to `uploads/`.** The delete endpoint resolves the
  target and refuses anything that lands outside that tree — traversal
  (`../`), absolute paths, and symlinks pointing out of it are all rejected,
  so real backups, DB dumps and image snapshots cannot be deleted from the
  web. It also refuses while a restore is running, so a folder can't vanish
  from under the job reading it.
