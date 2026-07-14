# Production Installation

## One-time install package — `install_sspl_erp.sh`

Sets up a **fresh server** end-to-end in one run, automating the full
`SSPL_ERP_Production_Deployment_Guide.docx` plus every tool in this repo:

1. **ERP stack** — clones `frappe_docker`, writes `.env` and the pdf-fix
   override, generates `/opt/sspl-erp/docker-compose.yml`, pulls the custom
   image from GHCR, starts all containers, creates the ERPNext site with
   every app shipped in the image, sets it as default, fixes DB grants,
   and enables Docker auto-start on reboot
2. **Backup system** → `/opt/scripts/v2/` with cron jobs (daily full,
   6-hourly DB, weekly verify)
3. **Update/rollback scripts** → `/opt/sspl-erp/v2/` (site name pre-filled)
4. **Web admin panel** → `/opt/sspl-admin/`, HTTPS on port 8090
5. **Terminal shortcuts** — passwordless `sudo` for clear-RAM and manual
   backup (the "Santhosh additions" from the guide)

### How to run (from the admin desk, over SSH)

```bash
ssh youruser@<server-ip>
git clone https://github.com/santhosh3279/sspl_installation.git
cd "sspl_installation/Production Installation"
chmod +x install_sspl_erp.sh
./install_sspl_erp.sh
```

All questions (server IP, HTTP port, passwords, which components to
install) are asked **up front** — after the final "Proceed?" the install
runs unattended for 10–20 minutes.

### Prerequisites

- Ubuntu Server 22.04/24.04 with a static LAN IP
- Docker installed and running (the compose plugin and `python3-venv`
  are installed automatically if missing)
- The `sspl-erpnext` image built and public on GHCR

### Notes

- **Already installed? It refuses to run.** If ERPNext is already
  deployed and running on the server, the installer says so and exits
  without changing anything (pointing you to the update tools instead).
  To deliberately re-run — e.g. to finish a half-completed install —
  use `SSPL_FORCE=1 ./install_sspl_erp.sh`. A forced re-run is safe:
  the existing site is detected and never re-created, and existing
  `.env` files are backed up before being rewritten.
- **Won't double-schedule backups.** Before installing the v2 backup
  cron jobs, the installer checks for an existing cron entry pointing
  at `/opt/scripts/` outside `/opt/scripts/v2/` (e.g. an older backup
  script wired up by hand). If found, it still installs the v2 scripts
  but skips scheduling their cron jobs automatically, and prints the
  conflicting line so you can switch over manually
  (`sudo crontab -e`).
- Default HTTP port is **80** (matching the live server); the guide's
  examples use 8080 — enter `8080` at the port prompt if you want that.
- After installation, keep everything current with:
  `git pull && ./update_tooling.sh` (repo root).
- The docx guide remains the reference for doing the steps manually.
