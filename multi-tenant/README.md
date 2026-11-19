# SSPL ERP — multi-tenant

One server, one Docker stack, one image, **many ERPNext sites**: one site per
tenant, each with its own database, files, users and apps. A host nginx
picks the site from the domain in the browser's address bar, and one site can
answer on several domains.

> This is the `Multi-Site` branch. The single-site tooling on `main`
> (`Admin Panel/`, `Backup/`, `Production Installation/`) is untouched and
> keeps serving existing servers. Use this folder on **fresh servers only**.
>
> The web admin panel for these servers is **`multi-tenant/admin-panel/`**,
> not `Admin Panel/` (that one assumes one site and the v2 script layout).

## How it works

| Shared by all tenants | Per tenant (site) |
|---|---|
| Image, containers (backend, workers, scheduler, nginx) | `sites/<host>/` folder, `site_config.json`, encryption key |
| MariaDB and Redis | Its own database and DB user |
| Port (default 80), host nginx | Site name, e.g. `acme.erp.lan`, plus any extra domains |
| App **versions** | Which apps are installed |

- **Routing.** A host nginx listens on `HTTP_PORT` and looks the request's
  domain up in `/opt/sspl-erp/site-domains` (`<domain> <site>` per line). It
  passes the site name on as the `Host` header to the stack's own nginx,
  which is published on `127.0.0.1:FRONTEND_PORT` (default 18080) only.
  `FRAPPE_SITE_NAME_HEADER` is not set, so frappe_docker routes on `$host`.
  A domain that isn't in the table gets its connection closed. See
  [Domains](#domains).
- **DNS is still yours.** nginx only routes. Every domain must resolve to the
  server: a public DNS A record, the router's DNS, or a hosts-file line on
  each PC. An IP works as a site name too, but only one site can use it.
- **PDFs.** Each site gets `host_name = http://<host>[:port]`. The containers
  resolve every tenant hostname to the host (a generated
  `compose.sspl-tenants.yaml`), so wkhtmltopdf can fetch images and letterheads
  with the right Host header. Adding or removing a non-IP site restarts
  backend, workers and scheduler for a few seconds.
- **One image** means an update upgrades every tenant at once. If a tenant
  needs different app *versions*, it needs its own stack.

## Install from the web panel (fresh server)

```bash
git clone -b Multi-Site https://github.com/santhosh3279/sspl_installation.git
cd sspl_installation/multi-tenant/admin-panel
./setup_admin_panel.sh
```

Then open `https://<server-ip>:8090`:

1. **ERP Next Installation suite → Install stack**: port, image, MariaDB root
   password, and whether to schedule backups. Runs `install/panel-install.sh`
   (`install-stack.sh` then `install-tools.sh`). No site is created.
   The stack install also sets up the host nginx (`install/install-proxy.sh`).
2. Optional, same page: rclone (cloud backups for every site, stored as
   `RCLONE_REMOTE` in `sspl-mt.conf`) and OpenWA. The **nginx domain
   routing** row shows whether the proxy is running, and repairs it.
3. **Dashboard → Create site**, once per tenant: hostname, Administrator
   password, which apps.

The dashboard lists every site with per-site Backup, DB backup, Migrate,
Apps… (uninstall) and Drop; all-site backup, verify, migrate, **Update all
sites**, image download and rollback; a per-site backup browser with Restore
(into the same site, another site with `--allow-other-site`, or from an upload
folder); uploads; and license files per site. Restore and Drop ask for the
site name and the panel password, then run the tool with `--yes`.
**Update panel & tools** (top bar) runs `admin-panel/update_panel.sh`: `git
pull`, `install-tools.sh` (cron and `sspl-mt.conf` untouched), redeploy the
panel. Changing the HTTP port of a running multi-tenant stack is not in the
panel yet.

## Install from the terminal (fresh server)

```bash
git clone -b Multi-Site https://github.com/santhosh3279/sspl_installation.git
cd sspl_installation/multi-tenant/install
./install.sh
```

It asks for the port, the MariaDB root password, the first tenant's hostname
and Administrator password, and whether to schedule backups. Then it:

1. starts the stack: `/opt/sspl-erp/` (compose project `sspl-erp`), and the
   host nginx that routes domains to sites
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

## Domains

Each site answers on its own name. Add more domains with `sites/domains.sh`,
or with **Domains…** in the site's row on the dashboard:

```bash
sudo $T/sites/domains.sh add acme.erp.lan erp.acme.com --primary
sudo $T/sites/domains.sh add acme.erp.lan acme.lan
sudo $T/sites/domains.sh list
sudo $T/sites/domains.sh primary acme.erp.lan acme.lan
sudo $T/sites/domains.sh remove acme.lan
```

- The table is `/opt/sspl-erp/site-domains`. The tools generate
  `/etc/nginx/conf.d/sspl-erp.conf` from it, run `nginx -t`, and reload. If
  nginx rejects a config, the previous one stays.
- `--primary` (or `primary`) sets the site's `host_name` to that domain. Emails
  link to that URL, and PDFs load images from it. Removing the primary domain
  sets `host_name` back to the site name.
- Adding or removing a domain also adds it to the containers' host list, so
  PDFs can fetch it. That restarts backend and workers for a few seconds, for
  every site.
- `new-site.sh` adds the site's own name. `drop-site.sh` removes every domain
  of the site.

### IP + port per site

A site can also get its own port on the server's IP address, with no DNS at
all. nginx listens on that port and sends everything that arrives there to
that one site:

```bash
sudo $T/sites/domains.sh port acme.erp.lan 8001 --primary   # http://<server-ip>:8001
sudo $T/sites/domains.sh port beta.erp.lan 8002
sudo $T/sites/domains.sh primary beta.erp.lan 8002           # a number = a port
sudo $T/sites/domains.sh unport 8002
```

The panel does the same with **Add IP + port** in the site's Domains… dialog.

- One site can have domains *and* ports, and each works on its own:
  `erp.acme.com` on 80/443 and `http://192.168.1.50:8001` both reach
  `acme.erp.lan`.
- The table is `/opt/sspl-erp/site-ports` (`<port> <site>` per line). Ports
  used by HTTP/HTTPS, the stack's nginx, SSH, OpenWA or the panel are refused,
  and so is a port another program holds. If nginx can't bind a port, the
  change is undone. With ufw active, the port is opened (and closed again on
  `unport`).
- `--primary` sets `host_name` to `http://<server-ip>:<port>`. The IP is
  `SERVER_IP` in `sspl-mt.conf`, or the first address from `hostname -I`.
  Set `SERVER_IP` if that isn't the address users type.
- Ports are HTTP only.
- **Cookies:** browsers keep cookies per IP address, not per port. If one
  browser logs in to two sites on the same IP (`:8001` and `:8002`), logging
  in to one logs it out of the other. Give each site a domain name, or use a
  separate browser or profile per site.

### HTTPS

The host nginx also listens on `HTTPS_PORT` (default 443; set it to `""` in
`sspl-mt.conf` for HTTP only).

- **Every domain** works on HTTPS at once, with a self-signed default
  certificate (`/etc/sspl-erp/ssl/default.crt`, made by `install-proxy.sh`).
  Browsers warn about it, but the traffic is encrypted. That is what LAN names
  like `acme.erp.lan` get.
- **Public domains** get a trusted Let's Encrypt certificate:

  ```bash
  sudo $T/sites/domains.sh cert erp.acme.com
  ```

  You can also use **Get certificate** in the site's Domains… dialog. The
  domain must be in the table, its DNS must point at this server's public IP,
  and port 80 must be open from the internet (`HTTP_PORT` must be 80). certbot
  uses the webroot method (`/var/www/sspl-acme`) and never edits the nginx
  config. `certbot.timer` renews the certificate and reloads nginx.
  `LETSENCRYPT_EMAIL` in `sspl-mt.conf` gets expiry notices.
- After that, `http://` for that domain redirects to HTTPS
  (`HTTPS_REDIRECT=yes`). If the domain is a site's primary, its `host_name`
  becomes `https://…`. Removing the domain, or dropping its site, deletes the
  certificate.
- Realtime: `install-proxy.sh` sets `webserver_host=http://backend` and
  `webserver_port=8000` in `common_site_config.json`. The websocket server then
  checks browser sessions against the backend directly, so it doesn't need to
  resolve tenant domains or trust the self-signed certificate.

**Stacks installed before nginx routing, or before HTTPS,** need one
`install-proxy.sh` run. Before routing, the frontend still holds the public
port. Before HTTPS, there's no default certificate. Run `sudo SSPL_CONF=/opt/sspl-erp/sspl-mt.conf
install/install-proxy.sh` from the checkout, or **Repair nginx routing** on
the panel's installation suite. It moves the frontend to `127.0.0.1`, adds
every existing site to the domain table, creates the default certificate, and
starts nginx. It is safe to re-run. Every site is down
for a few seconds while the frontend moves.

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
  admin-panel/  the web panel: app.py, setup_admin_panel.sh, update_panel.sh
  install/   install.sh (fresh server), panel-install.sh (the panel's),
             install-stack.sh, install-proxy.sh (host nginx), install-tools.sh
  lib/       common.sh — config, site validation, domain table + nginx config,
             maintenance helpers, rclone
  sites/     new-site, drop-site, list-sites, domains, apply-hosts
  backup/    backup, db-backup, verify, restore, restore-with-backup, migrate,
             rclone_trash_cleanup (from main, unchanged)
  update/    update, rollback, download-images, uninstall-app,
             pull-progress.py, image-backup-manager.sh (from main)
```

Mutating tools (update, restore, new/drop site, domains, uninstall) take one lock
(`LOCK_FILE`), so they can never interleave.
