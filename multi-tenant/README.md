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

## Authelia login (2FA) per site

An optional login gate in front of chosen sites: Authelia (login portal and
2FA), its Redis (sessions) and a Postfix mail relay that sends through Gmail.
They run as their own compose project in `/opt/sspl-auth`. Nothing is gated
until you link a site.

**Install:** installation page, **Authelia login (2FA) + mail relay** row. Or:

```bash
sudo SMTP_USER=you@gmail.com SMTP_PASSWORD='abcd efgh ijkl mnop' \
     ADMIN_USER=admin ADMIN_EMAIL=you@gmail.com AUTH_PASSWORD='…' \
     AUTH_TRUSTED_NETWORKS="192.168.1.0/24" \
     multi-tenant/install/install-auth.sh
```

- **Gmail:** turn on 2-Step Verification for the Google account, then create an
  **app password** (Google Account → Security → App passwords). Use that, not
  the account password. Gmail sends as that address, or a verified "Send mail
  as" alias only. Check delivery with `sites/auth.sh mail-test you@x.com`.
- The relay is not published on any host port: only containers on its Docker
  network can use it, so it can never be an open relay.
- Needs HTTPS on the host nginx (`install-proxy.sh`). Authelia only works on
  `https://` pages.

**Link a site:** **Domains…** → **Require Authelia login**, or
`sudo $T/sites/auth.sh link acme.erp.lan`. Then:

- Every domain of the site and every IP + port asks for the Authelia login
  first. The portal is at `https://<domain>/authelia/` on the site's own
  domain, so no extra DNS name is needed.
- The site becomes HTTPS-only. HTTP redirects to HTTPS, and its IP + port
  addresses switch to TLS. Without a Let's Encrypt certificate, browsers warn
  about the self-signed one.
- Only users who have the site in their list get in, or **admins**. Manage
  them under **Authelia users…** on the dashboard, or `auth.sh user-add`,
  `user-sites`, `user-passwd`, `user-reset-2fa` and the like. Passwords come
  from `AUTH_PASSWORD` or a prompt, never the command line.
- **First login:** the user registers 2FA (an authenticator app or a security
  key), confirmed by a code e-mailed through the relay.
- **Remember me:** that browser stays logged in for `AUTH_REMEMBER_ME` (1y)
  without asking again. The session is kept in Redis, on disk, so restarts and
  updates do not log anyone out. The inactivity timeout does not apply to such
  sessions. Without it ticked, the session ends after `AUTH_EXPIRATION` (12h)
  or `AUTH_INACTIVITY` (2h) idle.
- **Trusted networks:** from `AUTH_TRUSTED_NETWORKS` (the office LAN, say) the
  password is enough, with no 2FA. Set it with `auth.sh trusted "192.168.1.0/24"`
  or on the installation page. nginx passes Authelia the address of the
  connection, never a client-sent header, so the shortcut cannot be faked.
- **Where to log in on IP + port:** a site's IP + port shares one login per
  server IP, at `https://<server-ip>/authelia/`. Each port still only admits
  the users of its own site.
- **Not gated:** the ERP containers themselves (PDF rendering fetches the
  site's own URL), plus `AUTH_BYPASS_NETWORKS`.
- **Stopped by the login:** API-token clients, the Frappe mobile app,
  webhooks, payment callbacks and public web forms. Give those an unlinked
  domain, or put their fixed addresses in `AUTH_BYPASS_NETWORKS`.
- **Domain names:** Authelia needs domains with a dot (or IP addresses).
  Single-label names like `acmelan` cannot be linked.

**Back up** `/opt/sspl-auth/secrets/` and `/opt/sspl-auth/config/`. The site
backups do not include them. The `storage` key encrypts the 2FA registrations:
without it every user registers 2FA again.

## WireGuard to the print servers

Lets the ERP containers reach CUPS print servers in the shops. WireGuard runs on
the host (`wg-quick@sspl-wg`); this server is the hub (`10.99.0.1`) and each CUPS
machine dials in, so it works behind a shop router.

1. Open UDP 51820 to this server (cloud firewall or router).
2. Installation page, **WireGuard (print servers)**, or:

   ```bash
   sudo WG_ENDPOINT=erp.example.com multi-tenant/install/install-wireguard.sh
   ```

   `WG_ENDPOINT` is the **public** IP or DNS name. After changing it, re-run each
   print server's setup script.
3. Add the print server and get its setup script (the panel has both too):

   ```bash
   sudo $T/sites/wireguard.sh add shop1 --site acme.erp.lan
   sudo $T/sites/wireguard.sh setup-script shop1 > sspl-wg-shop1.sh
   ```

   Run it as root on the CUPS machine (Debian/Ubuntu). It installs WireGuard and
   makes CUPS listen on the tunnel and allow `10.99.0.1` (keeping a copy of
   `cupsd.conf`). **It contains a private key**: delete it afterwards. Share each
   printer: `lpadmin -p <printer> -o printer-is-shared=true`.
4. In ERPNext, set the Printer Server host to the tunnel address, port 631.
   Check with `sudo $T/sites/wireguard.sh test shop1`, which asks CUPS from
   inside the backend container.

- **One direction.** Firewall rules let the host and the containers connect to the
  print servers, not the reverse, and print servers cannot reach each other.
- **Tenants share containers**, so every site can reach every print server.
  `--site` is only a label. Protect CUPS with a password if that matters.
- Addresses are never reused after `remove`, so an old Printer Server record
  cannot print on another tenant's printer.
- **Back up** `/etc/wireguard/` and `/opt/sspl-erp/wg-peers`; site backups do not
  include them.

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

## RAM monitor and database memory

The **RAM monitor →** link at the top of the dashboard opens `/ram`. It shows:

- alerts for low RAM, active swapping, memory pressure, containers near their memory
  limit, and a database reading too much from disk
- memory per container (`docker stats`) and the top processes by RAM and swap
- the MariaDB **InnoDB buffer pool**: its size now, the saved setting, the database
  size, a recommended size and the share of reads that went to disk

MariaDB's default pool is 128M. A database bigger than the pool reads from disk,
and every bill save slows down. **Apply size** changes the pool live, with no
restart, and saves it to `/opt/sspl-erp/mariadb/99-sspl.cnf`. The installer mounts
that folder on the db container's `/etc/mysql/conf.d`
(`frappe_docker/overrides/compose.sspl-db.yaml`) and sizes the pool at 25% of RAM,
or `DB_BUFFER_POOL=2G` if set.

A stack installed before this has no mount yet, so the page shows **Make permanent
(restarts DB)**. It adds the mount to `docker-compose.yml`, keeping a
`.bak-dbmem-<time>` copy, and recreates the db container. Every site loses its
database for 10–30 seconds, so do it when nobody is billing. Until then a live
change is lost on the next DB restart.

Keep the pool below the free RAM. A pool pushed into swap is slower than a small one.

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
             install-stack.sh, install-proxy.sh (host nginx), install-auth.sh
             (Authelia + Redis + mail relay), install-wireguard.sh,
             install-tools.sh
  lib/       common.sh — config, site validation, domain table + nginx config,
             Authelia config, maintenance helpers, rclone; authusers.py — the
             Authelia users file
  sites/     new-site, drop-site, list-sites, domains, auth (Authelia),
             wireguard (print servers), apply-hosts
  backup/    backup, db-backup, verify, restore, restore-with-backup, migrate,
             rclone_trash_cleanup (from main, unchanged)
  update/    update, rollback, download-images, uninstall-app,
             pull-progress.py, image-backup-manager.sh (from main)
```

Mutating tools (update, restore, new/drop site, domains, uninstall) take one lock
(`LOCK_FILE`), so they can never interleave.
