# SSPL ERP — multi-tenant

One server, one Docker stack, one image, **many ERPNext sites**: one site per
tenant, each with its own database, files, users and apps. nginx picks the
site from the hostname in the browser's address bar.

> This is the `multi-tenant` branch. The single-site tooling on `main`
> (`Admin Panel/`, `Backup/`, `Production Installation/`) is untouched and
> keeps serving existing servers. Use this folder on **fresh servers only**.
>
> **Not yet multi-tenant:** the web admin panel. Its current version assumes
> one site and the old backup paths. Do not install it on a server set up
> from this folder. It is the next phase.

## How it works

| Shared by all tenants | Per tenant (site) |
|---|---|
| Image, containers (backend, workers, scheduler, nginx) | `sites/<host>/` folder, `site_config.json`, encryption key |
| MariaDB and Redis | Its own database and DB user |
| Port (default 80) | Hostname, e.g. `acme.erp.lan` |
| App **versions** | Which apps are installed |

- **Routing.** `FRAPPE_SITE_NAME_HEADER` is not set, so frappe_docker routes
  on `$host`. Every tenant hostname must resolve to the server, either through
  a DNS rewrite (AdGuard Home, router) or a hosts-file line on each PC. An IP
  works as a site name too, but only one site can use it.
- **PDFs.** Each site gets `host_name = http://<host>[:port]`. The containers
  resolve every tenant hostname to the host (a generated
  `compose.sspl-tenants.yaml`), so wkhtmltopdf can fetch images and letterheads
  with the right Host header. Adding or removing a non-IP site restarts
  backend, workers and scheduler for a few seconds.
- **One image** means an update upgrades every tenant at once. If a tenant
  needs different app *versions*, it needs its own stack.

## Install (fresh server)

```bash
git clone -b multi-tenant https://github.com/santhosh3279/sspl_installation.git
cd sspl_installation/multi-tenant/install
./install.sh
```

It asks for the port, the MariaDB root password, the first tenant's hostname
and Administrator password, and whether to schedule backups. Then it:

1. starts the stack: `/opt/sspl-erp/` (compose project `sspl-erp`)
2. installs the tools to `/opt/sspl-erp/tools/` and sets up cron
3. creates the first site

Configuration lives in **`/opt/sspl-erp/sspl-mt.conf`**: ports, retention,
rclone remotes, and so on. Every tool reads it, and a tooling update never
overwrites it. The MariaDB root password is in `/opt/sspl-erp/.env` (0600).

Refresh the tools after `git pull`:

```bash
sudo ./install-tools.sh --cron
```

## Tenants

```bash
T=/opt/sspl-erp/tools
sudo $T/sites/new-site.sh acme.erp.lan                     # all apps in the image
sudo $T/sites/new-site.sh beta.erp.lan --apps erpnext,hrms # only some
sudo $T/sites/list-sites.sh --apps
sudo $T/sites/drop-site.sh beta.erp.lan                    # backup first, then drop
```

Apps you leave out with `--apps` are recorded in
`/opt/sspl-erp/site-skip-apps/<host>`, so updates never install them on that
site.

## Backups

Every backup tool takes `--site <host>` (repeatable) or `--all`.

| Tool | What | Where |
|---|---|---|
| `backup/backup.sh` | DB + public + private files + `site_config.json` | `/opt/backups/frappe/sites/<host>/full/<TS>/` |
| `backup/db-backup.sh` | `mariadb-dump` as the site's DB user | `/opt/backups/frappe/sites/<host>/db-only/` |
| `backup/verify.sh` | newest full backup is recent and complete | (log) |

- Cron, with `--all`: full backup daily at 02:00, DB dump every 6 h, verify on
  Sundays at 03:00. Logs go to `/var/log/sspl-*.log`.
- Retention is **per site**. Files older than `RETENTION_DAYS`
  (`DB_RETENTION_DAYS` for dumps) are removed, but the newest `LOCAL_KEEP_MIN`
  are always kept. Each cloud remote keeps the newest `CLOUD_KEEP` per site,
  under `<remote>/sites/<host>/`.
- If one site fails, the others still run. The job exits non-zero and lists
  the failed sites.
- Unlike `main`, full backups no longer copy the stack `.env` (MariaDB root
  password). A site restore doesn't need it.

## Restore one tenant

```bash
sudo $T/backup/restore-with-backup.sh --site acme.erp.lan /opt/backups/frappe/sites/acme.erp.lan/full/20261031_020000
```

1. Takes a safety backup of the site. If that fails, nothing is restored.
2. Puts **only that site** into maintenance mode and pauses its scheduler.
   Every other tenant's web stays up. The shared job workers pause for the
   few minutes the restore takes: other tenants' background jobs wait in
   Redis and run afterwards.
   Frappe's web workers cache site settings for up to 60 s, so the tool waits
   65 s for maintenance mode to reach every worker before it touches data.
   After the restore, the maintenance page can also stay up to a minute.
3. Runs `bench restore` with the matching file archives, then migrates.
4. Brings the site back online, even if a step failed, and says which state
   it is in.

It refuses a backup taken from a **different** site unless you pass
`--allow-other-site`, which also copies that backup's encryption key so
encrypted fields stay readable. Use that flag only when moving a tenant.

## Update (all tenants)

```bash
sudo $T/update/download-images.sh   # optional, ahead of time; sites stay up
sudo $T/update/update.sh
```

1. Full backup of every site.
2. `docker save` of the current images (the rollback point).
3. Pull the new images. Sites stay up during the pull.
4. **Refuses** if any site still has an app the new image dropped. It lists
   `site:app` pairs; uninstall each with
   `update/uninstall-app.sh --site <host> <app>`.
5. Takes all tenants offline, switches images, and migrates **every** site.
6. Each site that migrated gets any new image apps (except its skip list), a
   cache clear, and comes back online. A site whose migration failed is left
   in **maintenance mode**, and the summary names it. The other sites are
   unaffected.

Rollback (`update/rollback.sh`) swaps images back for **every** site. Sites
that already migrated then run old code on a new schema, so after a rollback,
restore those sites from the backups the update took.

## Layout

```
multi-tenant/
  install/   install.sh (fresh server), install-stack.sh, install-tools.sh
  lib/       common.sh — config, site validation, maintenance helpers, rclone
  sites/     new-site, drop-site, list-sites, apply-hosts
  backup/    backup, db-backup, verify, restore, restore-with-backup, migrate,
             rclone_trash_cleanup (from main, unchanged)
  update/    update, rollback, download-images, uninstall-app,
             pull-progress.py, image-backup-manager.sh (from main)
```

Mutating tools (update, restore, new/drop site, uninstall) take one lock
(`LOCK_FILE`), so they can never interleave.
