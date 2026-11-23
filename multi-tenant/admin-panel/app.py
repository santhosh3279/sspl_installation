#!/usr/bin/env python3
"""SSPL ERP Admin Panel — multi-tenant edition.

Small LAN-only web interface for a server that runs MANY ERPNext sites on one
Docker stack (see multi-tenant/README.md). It drives the multi-tenant tools in
/opt/sspl-erp/tools: create and drop sites, per-site and all-site backups,
restore one tenant, the all-tenant update and rollback, and the component
installers. It also watches server health and takes backup uploads.

This is not the single-site panel in "Admin Panel/": that one assumes one
site and the v2 script layout. A server runs one or the other.

Configuration comes from /opt/sspl-admin/config.json (override the path with
the SSPL_ADMIN_CONFIG environment variable), written by setup_admin_panel.sh.
Everything about the stack itself (paths, port, cloud remotes) is read from
the tools' own config file, sspl-mt.conf, so the panel and the tools can
never disagree about where things are.
"""

import ipaddress
import json
import os
import pty
import re
import shutil
import sqlite3
import subprocess
import tempfile
import termios
import threading
import time
import urllib.request
import zipfile
from datetime import datetime, timezone
from functools import wraps
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from flask import (Flask, Response, abort, jsonify, redirect, render_template_string,
                   request, send_file, session, url_for)
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename

# Shown in the panel header and printed to the journal at startup, so you can
# tell at a glance whether the code running on the server is the code you
# think it is. Copying app.py is not enough — the service must be restarted
# for a new version to take effect. Bump this whenever app.py gains something
# visible; FEATURES lists what that version should show.
PANEL_VERSION = "2026-11-23.mt15"
FEATURES = ("Multi-tenant panel: sites list with per-site backup, DB backup, "
            "migrate, uninstall app and drop; Create site with app picker; "
            "per-site backup browser and guarded per-site restore (also from "
            "uploads and other sites); all-site backup, verify, migrate, update "
            "and rollback; stack + tools installer; rclone install, Google Drive "
            "connect / reconnect and destinations in "
            "sspl-mt.conf; multi-tenant cron schedule; one-click panel and "
            "tools update from git; nginx domain → site routing with per-site "
            "Domains… (add, remove, primary); AdGuard removed; HTTPS: self-signed "
            "default + Let's Encrypt per domain (Get certificate); per-site "
            "IP + port (Add IP + port); Authelia login with 2FA per site "
            "(Domains… → Require Authelia login), Authelia page (per site: users, whitelist "
            "and no-2FA IPs, 2FA logins; "
            "password, admin, 2FA reset), Redis sessions (Remember me 1 year), "
            "no-2FA trusted networks, Gmail mail relay with test mail, "
            "RAM monitor page with alerts, per-container and per-process memory, "
            "and the MariaDB buffer pool size applied live from the panel, "
            "Tools dropdown in the dashboard top bar, "
            "WireGuard page: tunnel to the CUPS print servers (install, settings, add, remove, test, setup script)")

CONFIG_FILE = os.environ.get("SSPL_ADMIN_CONFIG", "/opt/sspl-admin/config.json")
with open(CONFIG_FILE) as f:
    CONFIG = json.load(f)

# The tools and their config. Both are fixed for the life of the process; what
# is IN the config is re-read on every use (see mt_conf), because the install
# suite writes it while the panel is running.
MT_CONF = CONFIG.get("mt_conf", "/opt/sspl-erp/sspl-mt.conf")
TOOLS_DIR = CONFIG.get("tools_dir", "/opt/sspl-erp/tools")
JOB_DIR = Path(CONFIG.get("job_dir", "/opt/sspl-admin/jobs"))

# Where this repo is checked out on the server, recorded by
# setup_admin_panel.sh. The install suite runs the installers from here.
REPO_DIR = CONFIG.get("repo_dir")


def _repo(rel):
    return os.path.join(REPO_DIR, rel) if REPO_DIR else None


# ---------------------------------------------------------------- sspl-mt.conf
#
# The same defaults as lib/common.sh, for any key the file leaves out. Kept in
# step by hand — the file is a bash script that the tools 'source', so the
# panel can only read the plain KEY=value lines, which is all the installer
# writes.
MT_DEFAULTS = {
    "ERP_DIR": "/opt/sspl-erp",
    "COMPOSE_PROJECT": "sspl-erp",
    "HTTP_PORT": "80",
    "FRONTEND_PORT": "18080",
    "HTTPS_PORT": "443",
    "SSL_DIR": "/etc/sspl-erp/ssl",
    "LE_LIVE": "/etc/letsencrypt/live",
    "IMAGE": "ghcr.io/santhosh3279/sspl-erpnext",
    "IMAGE_TAG": "latest",
    "BACKUP_ROOT": "/opt/backups/frappe",
    "RETENTION_DAYS": "30",
    "DB_RETENTION_DAYS": "14",
    "LOCAL_KEEP_MIN": "20",
    "CLOUD_KEEP": "10",
    "RCLONE_REMOTE": "",
    "AUTH_DIR": "/opt/sspl-auth",
    "AUTH_REMEMBER_ME": "1y",
    "AUTH_TRUSTED_NETWORKS": "",
    "AUTH_BYPASS_NETWORKS": "",
    "SMTP_USER": "",
    "MAIL_FROM": "",
    "SMTP_RELAY_HOST": "smtp.gmail.com",
    "WG_IFACE": "sspl-wg",
    "WG_PORT": "51820",
    "WG_SUBNET": "10.99.0.0/24",
    "WG_ENDPOINT": "",
}
CONF_LINE_RE = re.compile(
    r"""^([A-Z_][A-Z0-9_]*)=(?:"([^"]*)"|'([^']*)'|([^\s#]*))""", re.M)


def mt_conf():
    """sspl-mt.conf as a dict, defaults filled in. Never raises."""
    conf = dict(MT_DEFAULTS)
    try:
        with open(MT_CONF) as f:
            text = f.read()
    except OSError:
        return conf
    for m in CONF_LINE_RE.finditer(text):
        conf[m.group(1)] = next((g for g in m.groups()[1:] if g is not None), "")
    return conf


def erp_dir():
    return mt_conf()["ERP_DIR"]


def compose_file():
    return os.path.join(erp_dir(), "docker-compose.yml")


def backup_root():
    return Path(mt_conf()["BACKUP_ROOT"])


def sites_backup_dir():
    return backup_root() / "sites"


def upload_dir():
    # Beside sites/, not in it: the --all backup jobs prune only sites/<host>/,
    # so uploads can never be swept up by a site's retention.
    return backup_root() / "uploads"


def image_backup_dir():
    return Path(erp_dir()) / "image-backups"


def http_port():
    p = str(mt_conf().get("HTTP_PORT", "80"))
    return int(p) if p.isdigit() else 80


def site_url(site):
    port = http_port()
    return f"http://{site}" + ("" if port == 80 else f":{port}")


# The host nginx routes each domain to its site from this table, one
# "<domain> <site>" per line (lib/common.sh domain_lines; sites/domains.sh
# edits it). Read directly: it is a plain 0644 file, no container needed.
PROXY_CONF = "/etc/nginx/conf.d/sspl-erp.conf"


def domains_file():
    return Path(erp_dir()) / "site-domains"


def domain_map():
    """{domain: site} for every valid line of the domain table."""
    out = {}
    try:
        with open(domains_file()) as f:
            for line in f:
                parts = line.split()
                if (len(parts) >= 2 and not parts[0].startswith("#")
                        and valid_site_name(parts[0]) and valid_site_name(parts[1])):
                    out.setdefault(parts[0], parts[1])
    except OSError:
        pass
    return out


def site_domains(site):
    return sorted(d for d, s in domain_map().items() if s == site)


# HTTPS — the same rules as lib/common.sh (https_on, domain_has_cert,
# domain_url): self-signed default certificate for every domain, Let's
# Encrypt certificates per domain (sites/domains.sh cert).
def https_port():
    p = str(mt_conf().get("HTTPS_PORT", "443")).strip()
    return int(p) if p.isdigit() else None


def default_cert_ok():
    d = mt_conf()["SSL_DIR"]
    return all(os.path.isfile(os.path.join(d, f)) and os.path.getsize(os.path.join(d, f)) > 0
               for f in ("default.crt", "default.key"))


def https_on():
    """HTTPS is live: configured, default certificate there, and the
    generated nginx config really has the TLS server (apply_proxy leaves it
    out while another program holds HTTPS_PORT)."""
    p = https_port()
    if p is None or not default_cert_ok():
        return False
    try:
        with open(PROXY_CONF) as f:
            return f"listen {p} ssl" in f.read()
    except OSError:
        return False


def https_off_reason():
    """Why https_on() is False, for the panel ("" = HTTPS is live)."""
    if https_on():
        return ""
    p = https_port()
    if p is None:
        return (f"HTTPS is turned off: HTTPS_PORT is empty or not a number in {MT_CONF}. "
                "Set it (e.g. HTTPS_PORT=443), then run Repair nginx routing.")
    if not os.path.isfile(PROXY_CONF):
        return "nginx domain routing is not set up yet. Run Install / Repair nginx routing."
    if not default_cert_ok():
        return (f"HTTPS is not set up yet: no default certificate. Run Repair nginx routing; if it "
                f"does not help, its log shows which program holds port {p}.")
    return (f"nginx is not serving HTTPS on port {p}; usually another program holds that port. "
            "Run Repair nginx routing: its log names the program. Free the port, then repair again.")


def domain_has_cert(domain):
    d = os.path.join(mt_conf()["LE_LIVE"], domain)
    return all(os.path.isfile(os.path.join(d, f)) and os.path.getsize(os.path.join(d, f)) > 0
               for f in ("fullchain.pem", "privkey.pem"))


# Authelia login gate — the same rules as lib/common.sh (auth_sites,
# auth_active): a linked site is HTTPS-only (self-signed until it has a
# certificate) and its IP:port addresses switch to TLS.
AUTH_USER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
CIDR_RE = re.compile(r"^(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:/(?:3[0-2]|[12]?\d))?$")


def auth_dir():
    return mt_conf()["AUTH_DIR"]


def auth_installed():
    d = auth_dir()
    return (os.path.isfile(os.path.join(d, "compose.yaml"))
            and os.path.isfile(os.path.join(d, "secrets", "redis")))


def auth_sites():
    out = []
    try:
        with open(Path(erp_dir()) / "site-auth") as f:
            for line in f:
                parts = line.split()
                if parts and not parts[0].startswith("#") and valid_site_name(parts[0]) and parts[0] not in out:
                    out.append(parts[0])
    except OSError:
        pass
    return out


def site_auth_linked(site):
    return auth_installed() and site in auth_sites()


def auth_domain_ok(d):
    return bool(IPV4_RE.match(d)) or "." in d


def auth_users(site):
    """One site's Authelia users, without password hashes (lib/authusers.py's file)."""
    try:
        with open(os.path.join(auth_dir(), "sites", site, "config", "users_database.yml")) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    return [{"user": name, "name": u.get("displayname", ""), "email": u.get("email", ""),
             "disabled": bool(u.get("disabled"))}
            for name, u in sorted((data.get("users") or {}).items())]


def auth_nets(site):
    """The site's own networks: {"bypass": [...], "trusted": [...]}."""
    out = {"bypass": [], "trusted": []}
    try:
        with open(Path(erp_dir()) / "site-auth-ips") as f:
            for line in f:
                p = line.split()
                if len(p) >= 3 and p[0] == site and p[1] in out and CIDR_RE.match(p[2]) and p[2] not in out[p[1]]:
                    out[p[1]].append(p[2])
    except OSError:
        pass
    return out


def _local_time(text):
    """Authelia's timestamp (UTC unless it carries an offset) in server time."""
    try:
        t = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return str(text)[:19]
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def auth_logins(site):
    """The site's login log from its Authelia's SQLite, opened read-only:
    2FA results per user + IP, and the newest attempts."""
    out = {"error": None, "twofa": [], "recent": []}
    db = os.path.join(auth_dir(), "sites", site, "config", "db.sqlite3")
    if not os.path.isfile(db):
        return out      # its Authelia never ran
    try:
        con = sqlite3.connect(f"file:{urllib.request.pathname2url(db)}?mode=ro", uri=True, timeout=3)
        try:
            # Not MAX(time): the text carries a zone offset. Ids only grow.
            rows = con.execute(
                "SELECT g.username, g.remote_ip, g.auth_type, g.good, g.bad, l.time FROM ("
                " SELECT username, remote_ip, auth_type, SUM(successful) AS good,"
                " SUM(NOT successful) AS bad, MAX(CASE WHEN successful THEN id END) AS lid,"
                " MAX(id) AS mid FROM authentication_logs WHERE auth_type <> '1FA'"
                " GROUP BY username, remote_ip, auth_type) g"
                " LEFT JOIN authentication_logs l ON l.id = g.lid ORDER BY g.mid DESC LIMIT 200").fetchall()
            out["twofa"] = [{"user": u, "ip": ip or "", "type": t, "good": g, "bad": b,
                             "last": _local_time(ts) if ts else None} for u, ip, t, g, b, ts in rows]
            rows = con.execute("SELECT time, username, remote_ip, auth_type, successful, banned"
                               " FROM authentication_logs ORDER BY id DESC LIMIT 100").fetchall()
            out["recent"] = [{"time": _local_time(ts), "user": u, "ip": ip or "", "type": t,
                              "ok": bool(s), "banned": bool(b)} for ts, u, ip, t, s, b in rows]
        finally:
            con.close()
    except sqlite3.Error as e:
        out["error"] = f"could not read the login log: {e}"
    return out


def auth_ports():
    """{site: port of its Authelia}."""
    out = {}
    try:
        with open(os.path.join(auth_dir(), "sites", ".ports")) as f:
            for line in f:
                p = line.split()
                if len(p) == 2 and p[1].isdigit():
                    out[p[0]] = int(p[1])
    except OSError:
        pass
    return out


def auth_site_up(port):
    if not port:
        return False
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/authelia/api/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def auth_status():
    c = mt_conf()
    linked = auth_sites()
    st = {"installed": auth_installed(), "linked": linked, "down": [], "legacy": False,
          "mail": c.get("SMTP_USER", ""),
          "mail_from": c.get("MAIL_FROM", "") or c.get("SMTP_USER", ""),
          "relay": c.get("SMTP_RELAY_HOST", ""), "trusted": c.get("AUTH_TRUSTED_NETWORKS", ""),
          "remember": c.get("AUTH_REMEMBER_ME", "1y")}
    if st["installed"]:
        ports = auth_ports()
        if linked:
            with ThreadPoolExecutor(max_workers=16) as ex:
                up = list(ex.map(lambda s: auth_site_up(ports.get(s)), linked))
            st["down"] = [s for s, ok in zip(linked, up) if not ok]
        # Still the one shared Authelia from before: "Save settings" converts it.
        st["legacy"] = os.path.isfile(os.path.join(auth_dir(), "config", "configuration.yml"))
    return st


# WireGuard to the CUPS print servers (sites/wireguard.sh). The peer table
# holds public keys only; private keys stay in /etc/wireguard.
WG_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,30}")
WG_ENDPOINT_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")
WG_SUBNET_RE = re.compile(r"(?:10|172|192)(?:\.[0-9]{1,3}){2}\.0/24")


def wg_peers():
    rows = []
    try:
        with open(Path(erp_dir()) / "wg-peers") as f:
            for line in f:
                p = line.split()
                if len(p) >= 3 and WG_NAME_RE.fullmatch(p[0]):
                    rows.append({"name": p[0], "ip": p[1], "key": p[2],
                                 "site": p[3] if len(p) > 3 else ""})
    except OSError:
        pass
    return rows


def wg_status():
    c = mt_conf()
    iface = c["WG_IFACE"]
    st = {"installed": os.path.isfile(f"/etc/wireguard/{iface}.conf"),
          "running": os.path.isdir(f"/sys/class/net/{iface}"),
          "endpoint": c["WG_ENDPOINT"], "port": c["WG_PORT"], "subnet": c["WG_SUBNET"], "peers": []}
    seen = {}
    if st["running"]:
        try:
            r = subprocess.run(["wg", "show", iface, "latest-handshakes"],
                               capture_output=True, text=True, timeout=5)
            for line in r.stdout.splitlines():
                key, _, ts = line.partition("\t")
                seen[key] = int(ts) if ts.isdigit() else 0
        except (OSError, subprocess.SubprocessError):
            pass
    now = int(time.time())
    for p in wg_peers():
        ts = seen.get(p.pop("key"), 0)
        # Keepalives renew the handshake about every 2 minutes.
        st["peers"].append({**p, "age": max(0, now - ts) if ts else None,
                            "up": bool(ts) and now - ts < 200})
    return st


def domain_url(domain):
    if https_on() and (domain_has_cert(domain) or site_auth_linked(domain_map().get(domain, ""))):
        p = https_port()
        return f"https://{domain}" + ("" if p == 443 else f":{p}")
    return site_url(domain)


def port_map():
    """{port: site} from the per-site port table (sites/domains.sh port)."""
    out = {}
    try:
        with open(Path(erp_dir()) / "site-ports") as f:
            for line in f:
                parts = line.split()
                if (len(parts) >= 2 and parts[0].isdigit() and 1 <= int(parts[0]) <= 65535
                        and valid_site_name(parts[1])):
                    out.setdefault(int(parts[0]), parts[1])
    except OSError:
        pass
    return out


def server_ip():
    return (str(mt_conf().get("SERVER_IP", "")).strip() or CONFIG.get("server_ip")
            or first_ip() or "127.0.0.1")


def domain_info(site):
    """The site's own name first, then its other domains, each with the URL
    users type and whether it has a Let's Encrypt certificate; then the
    site's own ports (http://<server-ip>:<port>)."""
    names = [site] + [d for d in site_domains(site) if d != site]
    out = [{"name": d, "url": domain_url(d), "cert": domain_has_cert(d),
            "is_ip": bool(IPV4_RE.match(d))} for d in names]
    ip = server_ip()
    scheme = "https" if https_on() and site_auth_linked(site) else "http"
    for p, s in sorted(port_map().items()):
        if s == site:
            out.append({"name": f"{ip}:{p}", "url": f"{scheme}://{ip}:{p}", "cert": False,
                        "is_ip": True, "port": p})
    return out


def _tool(rel):
    return os.path.join(TOOLS_DIR, rel)


# ---------------------------------------------------------------- site names
#
# The exact check lib/common.sh applies (valid_site_name). Site names end up
# in paths, in container 'bash -c' strings and in rclone paths, so the panel
# refuses anything the tools would refuse before it ever starts a job.
SITE_RE = re.compile(
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")
RESERVED_SITES = {"assets", "archived_sites", "logs", "apps.txt"}
IPV4_RE = re.compile(r"^\d+\.\d+\.\d+\.\d+$")
APP_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,60}$")
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,190}\.[A-Za-z]{2,24}$")


def valid_site_name(s):
    return (bool(s) and len(s) <= 253 and bool(SITE_RE.match(s))
            and s not in RESERVED_SITES)


# ---------------------------------------------------------------- actions
#
# Every action runs one of the deployed tools. The tools read MT_CONF, which
# is passed explicitly so a panel configured for a non-default path still
# drives the right stack. Arguments are built per request in api_run; secrets
# (admin / DB passwords) only ever travel as environment variables.
ACTIONS = {
    "backup":         {"label": "Full backup",            "cmd": [_tool("backup/backup.sh")]},
    "db_backup":      {"label": "DB-only backup",         "cmd": [_tool("backup/db-backup.sh")]},
    "verify":         {"label": "Verify backups",         "cmd": [_tool("backup/verify.sh")]},
    "migrate":        {"label": "Run migrations",         "cmd": [_tool("backup/migrate.sh")]},
    # No --yes: without a terminal update.sh then refuses to go on when the
    # pre-update backup failed, instead of updating with no way back.
    "update":         {"label": "Update all sites",       "cmd": [_tool("update/update.sh")]},
    "download_image": {"label": "Download update images", "cmd": [_tool("update/download-images.sh")]},
    "rollback":       {"label": "Rollback (all sites)",   "cmd": [_tool("update/rollback.sh"), "--yes"]},
    "restore":        {"label": "Restore site",           "cmd": [_tool("backup/restore-with-backup.sh")]},
    "uninstall_app":  {"label": "Uninstall app",          "cmd": [_tool("update/uninstall-app.sh")]},
    "new_site":       {"label": "Create site",            "cmd": [_tool("sites/new-site.sh")]},
    "drop_site":      {"label": "Drop site",              "cmd": [_tool("sites/drop-site.sh")]},
    "apply_hosts":    {"label": "Apply tenant hostnames", "cmd": [_tool("sites/apply-hosts.sh")]},
    "domain_add":     {"label": "Add domain",             "cmd": [_tool("sites/domains.sh"), "add"]},
    "domain_remove":  {"label": "Remove domain",          "cmd": [_tool("sites/domains.sh"), "remove"]},
    "domain_primary": {"label": "Set primary domain",     "cmd": [_tool("sites/domains.sh"), "primary"]},
    "domain_cert":    {"label": "Get HTTPS certificate",  "cmd": [_tool("sites/domains.sh"), "cert"]},
    "port_add":       {"label": "Add site port",          "cmd": [_tool("sites/domains.sh"), "port"]},
    "port_remove":    {"label": "Remove site port",       "cmd": [_tool("sites/domains.sh"), "unport"]},
    # Authelia login gate (sites/auth.sh). Passwords travel as AUTH_PASSWORD.
    "auth_link":           {"label": "Link site to Authelia",     "cmd": [_tool("sites/auth.sh"), "link"]},
    "auth_unlink":         {"label": "Unlink site from Authelia", "cmd": [_tool("sites/auth.sh"), "unlink"]},
    "auth_user_add":       {"label": "Add Authelia user",         "cmd": [_tool("sites/auth.sh"), "user-add"]},
    "auth_user_passwd":    {"label": "Set user password",         "cmd": [_tool("sites/auth.sh"), "user-passwd"]},
    "auth_user_disable":   {"label": "Enable/disable user",       "cmd": [_tool("sites/auth.sh"), "user-disable"]},
    "auth_user_remove":    {"label": "Remove Authelia user",      "cmd": [_tool("sites/auth.sh"), "user-remove"]},
    "auth_user_reset_2fa": {"label": "Reset user 2FA",            "cmd": [_tool("sites/auth.sh"), "user-reset-2fa"]},
    "auth_trusted":        {"label": "Set no-2FA networks",       "cmd": [_tool("sites/auth.sh"), "trusted"]},
    "auth_ip_add":         {"label": "Add site IP rule",          "cmd": [_tool("sites/auth.sh"), "ip-add"]},
    "auth_ip_remove":      {"label": "Remove site IP rule",       "cmd": [_tool("sites/auth.sh"), "ip-remove"]},
    "auth_mail_test":      {"label": "Send test mail",            "cmd": [_tool("sites/auth.sh"), "mail-test"]},
    # WireGuard peers (CUPS print servers)
    "wg_add":    {"label": "Add print server",    "cmd": [_tool("sites/wireguard.sh"), "add"]},
    "wg_remove": {"label": "Remove print server", "cmd": [_tool("sites/wireguard.sh"), "remove"]},
    "wg_test":   {"label": "Test print server",   "cmd": [_tool("sites/wireguard.sh"), "test"]},
}

# Component installers, run from the repo checkout. Only wired up when
# repo_dir is configured; the suite also checks each script is on disk.
STACK_SETUP_SCRIPT = _repo("multi-tenant/install/panel-install.sh")
TOOLS_SETUP_SCRIPT = _repo("multi-tenant/install/install-tools.sh")
RCLONE_SETUP_SCRIPT = _repo("Backup/frappe_backup_system/install_rclone.sh")
PROXY_SETUP_SCRIPT = _repo("multi-tenant/install/install-proxy.sh")
PORTS_SETUP_SCRIPT = _repo("multi-tenant/install/use-standard-ports.sh")
AUTH_SETUP_SCRIPT = _repo("multi-tenant/install/install-auth.sh")
WG_SETUP_SCRIPT = _repo("multi-tenant/install/install-wireguard.sh")
OPENWA_SETUP_SCRIPT = _repo("Production Installation/install_openwa.sh")
RCLONE_GUIDE = _repo("Backup/Rclone_Configuration_Guide.docx")
# The multi-tenant updater. NOT the repo-root update_panel.sh/update_tooling.sh:
# those deploy "Admin Panel/app.py" (the single-site panel) over this one and
# install the single-site v2 scripts.
UPDATE_PANEL_SCRIPT = _repo("multi-tenant/admin-panel/update_panel.sh")
PANEL_SOURCE = _repo("multi-tenant/admin-panel/app.py")

INSTALL_ACTIONS = {
    "install_stack":   {"label": "Install ERPNext stack + tools", "cmd": ["bash", STACK_SETUP_SCRIPT or ""]},
    "install_tools":   {"label": "Install / refresh tools",       "cmd": ["bash", TOOLS_SETUP_SCRIPT or ""]},
    "install_rclone":  {"label": "Install rclone",                "cmd": ["bash", RCLONE_SETUP_SCRIPT or ""]},
    # Host nginx: domain → site routing. Also moves an older stack's frontend
    # off the public port.
    "install_proxy":   {"label": "Install / repair nginx proxy",  "cmd": ["bash", PROXY_SETUP_SCRIPT or ""]},
    # HTTP 80 / HTTPS 443, and every site's host_name without a port.
    "install_ports":   {"label": "Move to ports 80 / 443",       "cmd": ["bash", PORTS_SETUP_SCRIPT or ""]},
    # Authelia + Redis + Postfix relay (Gmail). Secrets go as env vars.
    "install_auth":    {"label": "Install / update Authelia",     "cmd": ["bash", AUTH_SETUP_SCRIPT or ""]},
    "install_wireguard": {"label": "Install / update WireGuard", "cmd": ["bash", WG_SETUP_SCRIPT or ""]},
    # OpenWA WhatsApp API gateway in Docker. Its admin API key never goes
    # through the job log: /api/openwa/key reads it from the container.
    "install_openwa":  {"label": "Install OpenWA",                "cmd": ["bash", OPENWA_SETUP_SCRIPT or ""]},
    # git pull the checkout, refresh the tools, deploy this panel.
    "update_panel":    {"label": "Update panel & tools",          "cmd": ["bash", UPDATE_PANEL_SCRIPT or ""]},
}
if REPO_DIR:
    ACTIONS.update(INSTALL_ACTIONS)

# Actions that take "--site <host>" or "--all"
SITE_OR_ALL = {"backup", "db_backup", "verify", "migrate"}

RCLONE_LINE_RE = re.compile(r"^RCLONE_REMOTE=.*$", re.M)
RCLONE_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,40}:$")
RCLONE_PATH_RE = re.compile(r"^[A-Za-z0-9_.\-][A-Za-z0-9_.\-/]{0,120}$")

DEFAULT_ERP_IMAGE = MT_DEFAULTS["IMAGE"]
ERP_IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._\-]*(?::[0-9]{1,5})?(?:/[a-z0-9][a-z0-9._\-]*)+$")

# Ports this server already uses: SSH, OpenWA, this panel, and the stack's
# own nginx behind the host nginx (FRONTEND_PORT, 127.0.0.1).
RESERVED_PORTS = {22, 443, 2785, 8090, int(MT_DEFAULTS["FRONTEND_PORT"])}

# OpenWA (WhatsApp API). Its compose file pins the container name, so the
# container is how the panel finds it, wherever it was installed from.
OPENWA_DIR = CONFIG.get("openwa_dir", "/opt/openwa")
OPENWA_PORT = int(CONFIG.get("openwa_port", 2785))
OPENWA_CONTAINER = "openwa-api"

# The cron block install-tools.sh --cron writes into root's crontab.
CRON_BEGIN = "# >>> SSPL multi-tenant backups >>>"
CRON_END = "# <<< SSPL multi-tenant backups <<<"

UPLOAD_EXTENSIONS = (".sql.gz", ".gz", ".tar", ".tgz", ".json", ".yml", ".yaml")
FULL_BACKUP_RE = re.compile(r"^20\d{6}_\d{6}$")
SNAPSHOT_RE = re.compile(r"^backup_\d{8}_\d{6}\.tar$")
SAFE_NAME_RE = re.compile(r"^[\w][\w.\-]*$")
SAFE_FOLDER_RE = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")

# The MT tools exit 75 when another SSPL job holds the lock (lib/common.sh
# take_lock) — usually the nightly backup. Worth a plain-words hint.
LOCK_EXIT = 75


def erp_image():
    return str(CONFIG.get("erp_image") or mt_conf().get("IMAGE") or DEFAULT_ERP_IMAGE)


def save_config_key(key, value):
    """Persist one key into config.json, atomically and preserving its mode
    (the file holds the panel's password hash and secret key — never loosen
    it). Also updates the in-memory CONFIG so the change is live at once."""
    CONFIG[key] = value
    mode = os.stat(CONFIG_FILE).st_mode & 0o7777
    tmp = CONFIG_FILE + ".panel-tmp"
    with open(tmp, "w") as f:
        json.dump(CONFIG, f, indent=2)
    os.chmod(tmp, mode)
    os.replace(tmp, CONFIG_FILE)


app = Flask(__name__)
app.secret_key = CONFIG["secret_key"]
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    MAX_CONTENT_LENGTH=32 * 1024 ** 3,
)


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("user"):
            if request.path.startswith("/api/") or request.path.startswith("/download"):
                return jsonify({"error": "not logged in"}), 401
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------- job runner

_job_lock = threading.Lock()
_job = None  # {"name","label","logfile","proc","started","rc","finished","pty"}


def _pump_pty(master, out):
    """Copy an interactive job's terminal output into its log file."""
    try:
        while True:
            try:
                data = os.read(master, 4096)
            except OSError:      # the child exited and closed the slave side
                break
            if not data:
                break
            out.write(data.decode("utf-8", "replace"))
            out.flush()
    finally:
        out.close()
        try:
            os.close(master)
        except OSError:
            pass


def start_job(name, extra_env=None, extra_args=None, label=None):
    """Start a script as a background job; refuse if one is running."""
    global _job
    action = ACTIONS[name]
    with _job_lock:
        if _job and _job["proc"].poll() is None:
            return None, f"'{_job['label']}' is still running"
        JOB_DIR.mkdir(parents=True, exist_ok=True)
        logfile = JOB_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_{name}.log"
        # 0600: job output can quote config and backup contents
        out = os.fdopen(os.open(logfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w")
        env = {**os.environ, "SSPL_CONF": MT_CONF, "SSPL_TOOLS_DIR": TOOLS_DIR,
               **(extra_env or {})}
        cmd = list(action["cmd"]) + [str(a) for a in (extra_args or [])]
        stdin_text = action.get("stdin")
        master = None
        try:
            if action.get("interactive"):
                # A real pty, not a pipe: shell scripts only print `read -p`
                # prompts when stdin is a terminal. Echo OFF for the whole job
                # so nothing typed is ever copied into the log.
                master, slave = pty.openpty()
                attrs = termios.tcgetattr(slave)
                attrs[3] &= ~termios.ECHO          # lflags
                termios.tcsetattr(slave, termios.TCSANOW, attrs)
                proc = subprocess.Popen(
                    cmd, stdin=slave, stdout=slave, stderr=slave,
                    env=env, cwd=action.get("cwd"), start_new_session=True)
                os.close(slave)
                threading.Thread(target=_pump_pty, args=(master, out), daemon=True).start()
            else:
                proc = subprocess.Popen(
                    cmd, stdout=out, stderr=subprocess.STDOUT,
                    stdin=subprocess.PIPE if stdin_text else subprocess.DEVNULL,
                    env=env, cwd=action.get("cwd"))
        except OSError as e:
            if master is not None:
                os.close(master)
            out.write(f"Failed to start {cmd[0]}: {e}\n")
            out.close()
            return None, f"could not start script: {e}"
        if stdin_text:
            try:
                proc.stdin.write(stdin_text.encode())
                proc.stdin.close()
            except OSError:
                pass
        _job = {"name": name, "label": label or action["label"], "logfile": str(logfile),
                "proc": proc, "started": time.time(), "rc": None, "finished": None,
                "pty": master}
        # Whatever the job does to sites (creates, drops, restores) shows up
        # on the next listing rather than after the cache expires.
        _sites_cache["ts"] = 0.0
        return _job, None


def send_job_input(line):
    """Type one line into the running job's terminal. Can only ever feed a
    job that is already running — it never starts anything."""
    with _job_lock:
        if not _job or _job["proc"].poll() is not None:
            return "no job is running"
        if _job.get("pty") is None:
            return f"'{_job['label']}' does not take keyboard input"
        try:
            os.write(_job["pty"], (line + "\n").encode())
        except OSError:
            return "the job is no longer accepting input"
        return None


def job_status():
    with _job_lock:
        if not _job:
            return {"active": False, "log": "", "label": None}
        rc = _job["proc"].poll()
        if rc is not None and _job["rc"] is None:
            _job["rc"] = rc
            _job["finished"] = time.time()
            _sites_cache["ts"] = 0.0
            _apps_cache["ts"] = 0.0
        log = ""
        try:
            with open(_job["logfile"], "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 64 * 1024))
                log = f.read().decode("utf-8", "replace")
        except OSError:
            pass
        return {
            "active": rc is None,
            "interactive": rc is None and _job.get("pty") is not None,
            "name": _job["name"],
            "label": _job["label"],
            "rc": _job["rc"],
            "hint": ("another SSPL job (often the scheduled backup) holds the lock — "
                     "try again when it finishes") if _job["rc"] == LOCK_EXIT else None,
            "started": datetime.fromtimestamp(_job["started"]).strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed": int((_job["finished"] or time.time()) - _job["started"]),
            "log": log,
            "logfile": Path(_job["logfile"]).name if _job.get("logfile") else None,
        }


def job_running(*names):
    with _job_lock:
        return bool(_job and _job["proc"].poll() is None
                    and (not names or _job["name"] in names))


# Job log names are '<YYYYmmdd>_<HHMMSS>_<action>.log' — see start_job.
JOB_LOG_RE = re.compile(r"^(\d{8})_(\d{6})_(\w+)\.log$")


def job_logs(limit=60):
    """Every past terminal run, newest first."""
    items = []
    if JOB_DIR.is_dir():
        for p in JOB_DIR.iterdir():
            m = JOB_LOG_RE.match(p.name)
            if not (m and p.is_file()):
                continue
            st = p.stat()
            action = ACTIONS.get(m.group(3))
            items.append({
                "name": p.name,
                "label": action["label"] if action else m.group(3),
                "size": human(st.st_size),
                "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
            })
    items.sort(key=lambda x: x["name"], reverse=True)
    return items[:limit]


# ---------------------------------------------------------------- system stats

def human(nbytes):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(nbytes) < 1024 or unit == "TB":
            return f"{nbytes:.1f} {unit}" if unit != "B" else f"{int(nbytes)} B"
        nbytes /= 1024


def _cpu_times():
    with open("/proc/stat") as f:
        nums = list(map(int, f.readline().split()[1:]))
    idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
    return idle, sum(nums)


def cpu_percent(interval=0.25):
    i1, t1 = _cpu_times()
    time.sleep(interval)
    i2, t2 = _cpu_times()
    dt = t2 - t1
    return round(100.0 * (1 - (i2 - i1) / dt), 1) if dt > 0 else 0.0


def meminfo():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, val = line.split(":", 1)
            info[key] = int(val.strip().split()[0]) * 1024  # kB -> bytes
    return info


_containers_cache = {"ts": 0.0, "data": None}


def container_status():
    now = time.time()
    if _containers_cache["data"] is not None and now - _containers_cache["ts"] < 10:
        return _containers_cache["data"]
    result = {"services": [], "error": None}
    try:
        out = subprocess.run(
            ["docker", "compose", "-f", compose_file(), "ps", "--format", "json"],
            capture_output=True, text=True, timeout=20)
        if out.returncode != 0:
            result["error"] = (out.stderr or "docker compose ps failed").strip()[:300]
        else:
            txt = out.stdout.strip()
            items = json.loads(txt) if txt.startswith("[") else [
                json.loads(line) for line in txt.splitlines() if line.strip()]
            for it in items:
                result["services"].append({
                    "name": it.get("Service") or it.get("Name", "?"),
                    "state": it.get("State", ""),
                    "status": it.get("Status", ""),
                })
    except Exception as e:  # docker missing, timeout, bad json
        result["error"] = str(e)[:300]
    _containers_cache.update(ts=now, data=result)
    return result


# ---------------------------------------------------------------- sites

_vol_cache = {"ts": 0.0, "key": None, "path": None}


def get_sites_volume_path():
    """The bench's sites volume on the host: <compose project>_sites.
    Cached for 60 s: the dashboard asks for it once per site per poll."""
    vol = f"{mt_conf()['COMPOSE_PROJECT']}_sites"
    now = time.time()
    if (_vol_cache["path"] and _vol_cache["key"] == vol
            and now - _vol_cache["ts"] < 60 and os.path.isdir(_vol_cache["path"])):
        return _vol_cache["path"]
    path = _find_sites_volume(vol)
    _vol_cache.update(ts=now, key=vol, path=path)
    return path


def _find_sites_volume(vol):
    try:
        res = subprocess.run(
            ["docker", "volume", "inspect", vol, "--format", "{{.Mountpoint}}"],
            capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL)
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    fallback = Path(f"/var/lib/docker/volumes/{vol}/_data")
    if fallback.is_dir():
        return str(fallback)
    return None


_sites_cache = {"ts": 0.0, "data": None}


def list_sites():
    """Every site on the bench: a folder in the sites volume holding a
    site_config.json, with a name the tools accept — the same rule as
    list_sites() in lib/common.sh. Read from the volume on the host rather
    than through 'docker compose exec', which costs a second or two per call
    and fails while the backend restarts. Cached for 15 s; a job start or end
    clears the cache."""
    now = time.time()
    if _sites_cache["data"] is not None and now - _sites_cache["ts"] < 15:
        return _sites_cache["data"]
    sites = []
    vol = get_sites_volume_path()
    if vol:
        try:
            for d in sorted(Path(vol).iterdir()):
                if d.is_dir() and valid_site_name(d.name) and (d / "site_config.json").is_file():
                    sites.append(d.name)
        except OSError:
            pass
    _sites_cache.update(ts=now, data=sites)
    return sites


def site_dir(site):
    """The site's folder in the sites volume, or None."""
    if not valid_site_name(site):
        return None
    vol = get_sites_volume_path()
    if not vol:
        return None
    root = Path(vol).resolve()
    d = (root / site).resolve()
    if not str(d).startswith(str(root) + os.sep) or not d.is_dir():
        return None
    return d


def site_public_config(site):
    """The harmless bits of a site's site_config.json. The file also holds
    the DB password and the encryption key: those never leave this function."""
    d = site_dir(site)
    try:
        with open(d / "site_config.json") as f:
            cfg = json.load(f)
    except (OSError, ValueError, TypeError):
        return {}
    return {"host_name": cfg.get("host_name") if isinstance(cfg.get("host_name"), str) else None,
            "maintenance_mode": bool(cfg.get("maintenance_mode")),
            "pause_scheduler": bool(cfg.get("pause_scheduler"))}


_apps_cache = {"ts": 0.0, "data": None}


def image_apps():
    """Apps the running image ships, frappe excluded — the same probe as
    image_apps() in lib/common.sh ('ls apps' in the backend), because that is
    what new-site.sh installs from when no --apps is given. sites/apps.txt
    is only a fallback for a stopped backend: it lives in the named volume,
    which keeps its first contents, so it can lag behind an updated image.
    Cached for 60 s; a finished job clears it."""
    now = time.time()
    if _apps_cache["data"] is not None and now - _apps_cache["ts"] < 60:
        return _apps_cache["data"]
    apps = None
    try:
        out = subprocess.run(
            ["docker", "compose", "-f", compose_file(), "exec", "-T", "backend",
             "ls", "-1", "/home/frappe/frappe-bench/apps"],
            capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
        if out.returncode == 0:
            apps = [a.strip() for a in out.stdout.splitlines()
                    if APP_NAME_RE.match(a.strip()) and a.strip() != "frappe"]
    except (OSError, subprocess.SubprocessError):
        pass
    if apps is None:
        vol = get_sites_volume_path()
        try:
            with open(os.path.join(vol, "apps.txt")) as f:
                apps = [a.strip() for a in f
                        if APP_NAME_RE.match(a.strip()) and a.strip() != "frappe"]
        except (OSError, TypeError):
            apps = []
        _apps_cache.update(ts=now, data=apps, live=False)
        return apps
    _apps_cache.update(ts=now, data=apps, live=True)
    return apps


def skip_apps(site):
    try:
        with open(os.path.join(erp_dir(), "site-skip-apps", site)) as f:
            return [a.strip() for a in f if APP_NAME_RE.match(a.strip())]
    except OSError:
        return []


def installed_apps(site):
    """Apps installed on the site, frappe excluded. Returns (apps, error).
    The same probe as site_installed_apps() in lib/common.sh, so the picker
    offers exactly what uninstall-app.sh accepts."""
    try:
        out = subprocess.run(
            ["docker", "compose", "-f", compose_file(), "exec", "-T", "backend",
             "bench", "--site", site, "execute", "frappe.get_installed_apps"],
            capture_output=True, text=True, timeout=90, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        return None, str(e)[:300]
    if out.returncode != 0:
        return None, (out.stderr or "bench execute failed").strip()[-300:]
    for line in reversed(out.stdout.splitlines()):
        try:
            apps = json.loads(line)
        except ValueError:
            continue
        if isinstance(apps, list):
            return [a for a in apps if isinstance(a, str) and a != "frappe"], None
    return None, "could not read the installed apps from bench's output"


def _site_backup_dirs(site):
    base = sites_backup_dir() / site
    return base / "full", base / "db-only"


def latest_full_backup(site):
    full, _ = _site_backup_dirs(site)
    try:
        names = sorted((d.name for d in full.iterdir()
                        if d.is_dir() and FULL_BACKUP_RE.match(d.name)), reverse=True)
    except OSError:
        return None, 0
    return (names[0] if names else None), len(names)


def sites_overview():
    out = []
    for s in list_sites():
        latest, count = latest_full_backup(s)
        cfg = site_public_config(s)
        out.append({"name": s, "url": cfg.get("host_name") or site_url(s),
                    "is_ip": bool(IPV4_RE.match(s)),
                    "host_name": cfg.get("host_name"),
                    "domains": [d for d in site_domains(s) if d != s],
                    "domain_info": domain_info(s),
                    "auth": site_auth_linked(s),
                    "maintenance": cfg.get("maintenance_mode", False),
                    "paused": cfg.get("pause_scheduler", False),
                    "latest_backup": latest, "backups": count,
                    "skip_apps": skip_apps(s)})
    return out


def backup_sites():
    """Every site that has backups on disk — live or since dropped."""
    try:
        return sorted(d.name for d in sites_backup_dir().iterdir()
                      if d.is_dir() and valid_site_name(d.name))
    except OSError:
        return []


# ---------------------------------------------------------------- setup / bootstrap

def first_ip():
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=5)
        parts = out.stdout.split()
        return parts[0] if parts else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def proxy_status():
    """The host nginx that routes domains to sites (install-proxy.sh)."""
    st = {"installed": bool(shutil.which("nginx")), "running": False,
          "configured": os.path.isfile(PROXY_CONF), "frontend_local": False,
          "domains": len(domain_map()), "https_port": https_port(),
          "default_cert": default_cert_ok(), "https_live": https_on(),
          "https_reason": https_off_reason(),
          "certs": sum(1 for d in domain_map() if domain_has_cert(d))}
    try:
        out = subprocess.run(["systemctl", "is-active", "nginx"], capture_output=True, text=True, timeout=5)
        st["running"] = out.stdout.strip() == "active"
    except (OSError, subprocess.SubprocessError):
        pass
    # A stack from before the proxy publishes its frontend on 0.0.0.0.
    try:
        with open(os.path.join(erp_dir(), "frappe_docker", ".env")) as f:
            st["frontend_local"] = any(l.startswith("HTTP_PUBLISH_PORT=127.0.0.1:") for l in f)
    except OSError:
        pass
    return st


def openwa_status():
    """Is the openwa-api container there / running, and on which host port."""
    st = {"installed": False, "running": False, "port": OPENWA_PORT}
    try:
        out = subprocess.run(
            ["docker", "inspect", OPENWA_CONTAINER, "--format",
             "{{.State.Running}} {{range $p, $b := .NetworkSettings.Ports}}"
             "{{if eq $p \"2785/tcp\"}}{{range $b}}{{.HostPort}} {{end}}{{end}}{{end}}"],
            capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return st
    if out.returncode != 0:
        return st
    parts = out.stdout.split()
    st["installed"] = True
    st["running"] = bool(parts) and parts[0] == "true"
    ports = [p for p in parts[1:] if p.isdigit()]
    if ports:
        st["port"] = int(ports[0])
    return st


def openwa_api_key():
    """Read the admin API key OpenWA generated on first boot.
    Returns (key, None) or (None, reason)."""
    try:
        out = subprocess.run(
            ["docker", "exec", OPENWA_CONTAINER, "cat", "/app/data/.api-key"],
            capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"docker exec failed: {e}"
    key = out.stdout.strip()
    if out.returncode != 0 or not key:
        return None, ("the API key has not been generated yet — wait for OpenWA "
                      "to finish starting, or check docker logs openwa-api")
    return key, None


def rclone_remotes():
    """Cloud remotes rclone can see, as root — the user the backup job runs as.
    stdin is closed: a password-protected config would otherwise prompt."""
    try:
        out = subprocess.run(["rclone", "listremotes"], capture_output=True,
                             text=True, timeout=15, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    return [ln.strip() for ln in out.stdout.splitlines()
            if RCLONE_NAME_RE.match(ln.strip())]


def rclone_version():
    try:
        out = subprocess.run(["rclone", "version"], capture_output=True,
                             text=True, timeout=15, stdin=subprocess.DEVNULL)
        return out.stdout.splitlines()[0].strip() if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, IndexError):
        return None


def expand_targets(spec, remotes):
    """The destinations an RCLONE_REMOTE value resolves to. Mirrors
    rclone_expand_targets() in lib/common.sh and must keep mirroring it."""
    spec = (spec or "").strip()
    if not spec:
        return []
    if spec.startswith("*:"):
        folder = spec[2:]
        return [r + folder for r in remotes]
    return spec.split()


def rclone_status():
    """The three stages of cloud backup, reported separately: the binary, a
    configured remote, and sspl-mt.conf's RCLONE_REMOTE pointing at it. Each
    fails quietly — an upload failure is only a warning in backup.sh."""
    installed = bool(shutil.which("rclone"))
    remotes = rclone_remotes() if installed else []
    conf_ok = os.path.isfile(MT_CONF)
    wired = mt_conf().get("RCLONE_REMOTE", "") if conf_ok else ""
    targets = expand_targets(wired, remotes)
    wired_ok = bool(targets) and all(
        any(t.startswith(r) for r in remotes) for t in targets)
    return {
        "installed": installed,
        "version": rclone_version() if installed else None,
        "remotes": remotes,
        "configured": bool(remotes),
        "conf_ok": conf_ok,
        "wired": wired or "",
        "wired_ok": wired_ok,
        "wired_targets": targets,
        "wired_all": (wired or "").strip().startswith("*:"),
        "ready": bool(installed and wired_ok),
    }


def crontab_text():
    """Root's crontab, raw. Empty when there is none."""
    try:
        out = subprocess.run(["crontab", "-l"], capture_output=True,
                             text=True, timeout=10)
        return out.stdout if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


CRON_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _cron_entry(ln):
    ln = ln.strip()
    if not ln or ln.startswith("#"):
        return None
    if ln.startswith("@"):
        parts = ln.split(None, 1)
        return {"schedule": parts[0], "command": parts[1] if len(parts) > 1 else ""}
    if CRON_ENV_RE.match(ln):
        return {"schedule": "", "command": ln}
    parts = ln.split(None, 5)
    if len(parts) == 6:
        return {"schedule": " ".join(parts[:5]), "command": parts[5]}
    return None


def crontab_entries():
    """Every entry in root's crontab, parsed (env lines and @-shortcuts kept)."""
    return [e for e in map(_cron_entry, crontab_text().splitlines()) if e]


def cron_status():
    """The multi-tenant backup block install-tools.sh writes. Anything that
    looks like a backup job outside it is flagged: a leftover single-site
    schedule would back up a site that is not there, nightly, and fail."""
    inside, block, other = False, [], []
    for ln in crontab_text().splitlines():
        s = ln.strip()
        if s == CRON_BEGIN:
            inside = True
            continue
        if s == CRON_END:
            inside = False
            continue
        e = _cron_entry(ln)
        if not e or not e["schedule"]:
            continue
        if inside:
            block.append(e)
        elif "frappe" in e["command"].lower() or "sspl" in e["command"].lower():
            other.append(e)
    return {"jobs": block, "scheduled": bool(block), "other": other}


def repo_panel_version():
    """PANEL_VERSION of the checkout's multi-tenant app.py — differs from the
    running one when a git pull brought a newer panel not yet deployed."""
    if not PANEL_SOURCE or not os.path.isfile(PANEL_SOURCE):
        return None
    try:
        with open(PANEL_SOURCE) as f:
            for ln in f:
                if ln.startswith("PANEL_VERSION"):
                    m = re.search(r'"([^"]+)"', ln)
                    return m.group(1) if m else None
    except OSError:
        pass
    return None


def _script_ok(path):
    return bool(path) and os.path.isfile(path)


def setup_status():
    """What is installed on this server, to drive the install suite."""
    cs = container_status()
    running = any(str(s.get("state", "")).lower().startswith(("running", "up"))
                  for s in cs["services"])
    stack_installed = os.path.isfile(compose_file())
    return {
        "repo_dir": REPO_DIR,
        "repo_ok": _script_ok(STACK_SETUP_SCRIPT) and _script_ok(TOOLS_SETUP_SCRIPT),
        "mt_conf": MT_CONF,
        "tools_dir": TOOLS_DIR,
        "server_ip": CONFIG.get("server_ip") or first_ip(),
        "erp_image": erp_image(),
        "components": {
            "erp": {
                "installed": stack_installed,
                "running": running,
                "http_port": http_port() if stack_installed else None,
                "sites": list_sites() if stack_installed else [],
            },
            "tools": {"installed": os.path.isfile(_tool("lib/common.sh")),
                      "conf": os.path.isfile(MT_CONF)},
            "rclone":  rclone_status(),
            "proxy": proxy_status(),
            "auth": auth_status(),
            "wireguard": wg_status(),
            "openwa": openwa_status(),
        },
        "scripts": {
            "rclone": _script_ok(RCLONE_SETUP_SCRIPT),
            "proxy": _script_ok(PROXY_SETUP_SCRIPT),
            "ports": _script_ok(PORTS_SETUP_SCRIPT),
            "auth": _script_ok(AUTH_SETUP_SCRIPT),
            "wireguard": _script_ok(WG_SETUP_SCRIPT),
            "openwa": _script_ok(OPENWA_SETUP_SCRIPT),
            "update_panel": _script_ok(UPDATE_PANEL_SCRIPT),
        },
        "cron": cron_status(),
        "panel_version": PANEL_VERSION,
        "repo_panel_version": repo_panel_version(),
        "guide_ok": _script_ok(RCLONE_GUIDE),
    }


def system_stats():
    mem = meminfo()
    total, avail = mem.get("MemTotal", 0), mem.get("MemAvailable", 0)
    used = total - avail
    swap_t, swap_f = mem.get("SwapTotal", 0), mem.get("SwapFree", 0)
    with open("/proc/uptime") as f:
        up = float(f.read().split()[0])
    disks = []
    seen = set()
    for label, path in (("Root (/)", "/"), ("Data (/opt)", "/opt")):
        try:
            du = shutil.disk_usage(path)
        except OSError:
            continue
        key = (du.total, du.free)
        if key in seen:  # /opt on the same filesystem as /
            continue
        seen.add(key)
        disks.append({
            "label": label, "total": human(du.total), "used": human(du.used),
            "free": human(du.free),
            "pct": round(100.0 * du.used / du.total, 1) if du.total else 0,
        })
    days, rem = divmod(int(up), 86400)
    hours, rem = divmod(rem, 3600)
    return {
        "load": [round(x, 2) for x in os.getloadavg()],
        "cores": os.cpu_count() or 1,
        "cpu_pct": cpu_percent(),
        "uptime": f"{days}d {hours}h {rem // 60}m",
        "mem": {"total": human(total), "used": human(used), "available": human(avail),
                "pct": round(100.0 * used / total, 1) if total else 0},
        "swap": {"total": human(swap_t), "used": human(swap_t - swap_f),
                 "pct": round(100.0 * (swap_t - swap_f) / swap_t, 1) if swap_t else 0},
        "disks": disks,
        "containers": container_status(),
    }


# ---------------------------------------------------------------- backups

def _dir_listing(path, pattern=None):
    """List files in a directory: name, size, mtime."""
    items = []
    if path.is_dir():
        for p in sorted(path.iterdir()):
            if p.is_file() and (pattern is None or pattern.match(p.name)):
                st = p.stat()
                items.append({"name": p.name, "size": human(st.st_size),
                              "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")})
    return items


def site_backups(site):
    """One site's full backups and DB-only dumps, newest first."""
    full_dir, db_dir = _site_backup_dirs(site)
    full = []
    if full_dir.is_dir():
        for d in sorted(full_dir.iterdir(), reverse=True):
            if not (d.is_dir() and FULL_BACKUP_RE.match(d.name)):
                continue
            files, size = [], 0
            for p in sorted(d.iterdir()):
                if p.is_file():
                    st = p.stat()
                    size += st.st_size
                    files.append({"name": p.name, "size": human(st.st_size)})
            names = " ".join(f["name"] for f in files)
            full.append({
                "name": d.name, "size": human(size), "files": files,
                "db": "-database.sql.gz" in names,
                "public": bool(re.search(r"-files\.(tar|tgz)", names.replace("-private-files.", ""))),
                "private": "-private-files." in names,
                "config": "site_config.json" in names,
            })
    dbonly = _dir_listing(db_dir)
    dbonly.sort(key=lambda x: x["name"], reverse=True)
    return full, dbonly


def backups_overview(site=None):
    live = list_sites()
    known = sorted(set(live) | set(backup_sites()))
    if site is None:
        site = live[0] if live else (known[0] if known else None)
    full, dbonly = site_backups(site) if site else ([], [])
    images, latest = [], None
    img_dir = image_backup_dir()
    if img_dir.is_dir():
        try:
            latest = Path((img_dir / "latest_backup.txt").read_text().strip()).name
        except OSError:
            pass
        for it in _dir_listing(img_dir, SNAPSHOT_RE):
            it["latest"] = it["name"] == latest
            images.append(it)
        images.sort(key=lambda x: x["name"], reverse=True)
    uploads, upload_folders = [], []
    up = upload_dir()
    if up.is_dir():
        for entry in sorted(up.iterdir()):
            if entry.is_file():
                uploads.extend(_dir_listing(up, re.compile(re.escape(entry.name) + "$")))
            elif entry.is_dir() and SAFE_FOLDER_RE.match(entry.name):
                for it in _dir_listing(entry):
                    it["name"] = f"{entry.name}/{it['name']}"
                    uploads.append(it)
                # Only a folder holding a database dump can be restored from.
                if any(entry.glob("*-database.sql.gz")):
                    upload_folders.append(entry.name)
    return {"site": site, "sites": known, "live": live,
            "full": full, "db_only": dbonly, "images": images,
            "uploads": uploads, "upload_folders": upload_folders}


def _restore_source(kind, src_site, name):
    """Resolve a restore-from folder inside the backup roots.
    Returns (Path, None) or (None, error). Only whole backup folders are
    restorable — a site's full-backup folder or an upload subfolder."""
    if kind == "full":
        if not valid_site_name(src_site):
            return None, "invalid backup site"
        if not FULL_BACKUP_RE.match(name):
            return None, "invalid backup name"
        root = sites_backup_dir() / src_site / "full"
    elif kind == "upload":
        if not SAFE_FOLDER_RE.match(name):
            return None, "invalid upload folder name"
        root = upload_dir()
    else:
        return None, "restore source must be a full backup or an upload folder"
    target = (root / name).resolve()
    if not str(target).startswith(str(root.resolve()) + os.sep) or not target.is_dir():
        return None, "backup folder not found"
    if not any(target.glob("*-database.sql.gz")):
        return None, "no *-database.sql.gz in that folder — nothing to restore from"
    return target, None


def _check_admin(data):
    return check_password_hash(CONFIG["password_hash"], str(data.get("admin_password", "")))


def _restore_request(data):
    """Validate a restore. Returns (extra_args, label, error, status).

    Three gates, all server-side: the panel admin password is re-entered, the
    target site's name is typed out in full, and the source resolves to a
    real backup folder. restore.sh then runs with --yes: its own "type the
    site name" prompt needs a terminal the panel does not give it, and the
    same check has just been made here."""
    site = str(data.get("site", "")).strip()
    if site not in list_sites():
        return None, None, "choose the site to restore into", 400
    if not _check_admin(data):
        return None, None, "admin password is incorrect", 403
    if str(data.get("confirm_site", "")).strip() != site:
        return None, None, f"type the site name exactly — {site} — to confirm", 400
    kind = str(data.get("kind", ""))
    src_site = str(data.get("src_site", "")).strip()
    src, err = _restore_source(kind, src_site, str(data.get("name", "")))
    if err:
        return None, None, err, 400
    other = bool(data.get("allow_other_site"))
    # A backup of another site is refused by restore.sh unless told; make the
    # checkbox mandatory here too, so the refusal comes before the safety
    # backup rather than after it.
    if kind == "full" and src_site != site and not other:
        return None, None, (f"that backup is from {src_site}, not {site} — tick "
                            "'backup is from another site' to restore it anyway"), 400
    args = ["--site", site, str(src), "--yes"]
    if other:
        args.append("--allow-other-site")
    return args, f"Restore {site}", None, None


def _safe_upload_target(name):
    """Resolve an upload file or folder for deletion — confined to uploads/."""
    parts = [p for p in name.split("/") if p]
    if not 1 <= len(parts) <= 2:
        return None, "invalid name"
    if len(parts) == 2:
        if not SAFE_FOLDER_RE.match(parts[0]) or not SAFE_NAME_RE.match(parts[1]):
            return None, "invalid name"
    elif not (SAFE_FOLDER_RE.match(parts[0]) or SAFE_NAME_RE.match(parts[0])):
        return None, "invalid name"
    root = upload_dir().resolve()
    target = (upload_dir() / "/".join(parts)).resolve()
    if not str(target).startswith(str(root) + os.sep):
        return None, "outside the uploads folder"
    if not target.exists():
        return None, "not found — already deleted?"
    return target, None


def _safe_download_path(kind, name):
    """Resolve a download. Every site segment is checked against SITE_RE and
    every result against its root (resolve + containment)."""
    parts = name.split("/")
    if kind == "full":      # <site>/<TS>/<file>
        if (len(parts) != 3 or not valid_site_name(parts[0])
                or not FULL_BACKUP_RE.match(parts[1]) or not SAFE_NAME_RE.match(parts[2])):
            abort(400)
        root = sites_backup_dir()
    elif kind == "db":      # <site>/<file>
        if len(parts) != 2 or not valid_site_name(parts[0]) or not SAFE_NAME_RE.match(parts[1]):
            abort(400)
        root = sites_backup_dir()
        name = f"{parts[0]}/db-only/{parts[1]}"
    elif kind == "image":
        if len(parts) != 1 or not SNAPSHOT_RE.match(parts[0]):
            abort(400)
        root = image_backup_dir()
    elif kind == "log":
        if len(parts) != 1 or not JOB_LOG_RE.match(parts[0]):
            abort(400)
        root = JOB_DIR
    elif kind == "upload":
        if len(parts) == 2:
            if not SAFE_FOLDER_RE.match(parts[0]) or not SAFE_NAME_RE.match(parts[1]):
                abort(400)
        elif len(parts) != 1 or not SAFE_NAME_RE.match(parts[0]):
            abort(400)
        root = upload_dir()
    else:
        abort(400)
    if kind == "full":
        name = f"{parts[0]}/full/{parts[1]}/{parts[2]}"
    target = (root / name).resolve()
    if not str(target).startswith(str(root.resolve()) + os.sep) or not target.is_file():
        abort(404)
    return target


def _safe_backup_folder(site, name):
    """Resolve one full-backup folder for download (a files-only backup is
    still worth downloading, so no database check)."""
    if not valid_site_name(site) or not FULL_BACKUP_RE.match(name):
        abort(400)
    root = sites_backup_dir() / site / "full"
    target = (root / name).resolve()
    if not str(target).startswith(str(root.resolve()) + os.sep) or not target.is_dir():
        abort(404)
    return target


def _zip_stream(folder, arc_root):
    """Stream a folder as a zip, built in a thread and read out through a
    pipe so a multi-GB backup never lands in RAM. Stored, not deflated: the
    contents are already compressed."""
    r_fd, w_fd = os.pipe()

    def build():
        try:
            with os.fdopen(w_fd, "wb") as pipe:
                with zipfile.ZipFile(pipe, "w", zipfile.ZIP_STORED,
                                     allowZip64=True) as zf:
                    for p in sorted(folder.rglob("*")):
                        if p.is_file():
                            zf.write(p, arcname=str(Path(arc_root) / p.relative_to(folder)))
        except OSError:
            pass

    threading.Thread(target=build, daemon=True).start()
    with os.fdopen(r_fd, "rb") as out:
        while True:
            chunk = out.read(64 * 1024)
            if not chunk:
                return
            yield chunk


# ---------------------------------------------------------------- routes

# ---------------------------------------------------------------- RAM monitor + database memory
# MariaDB ships with a 128M InnoDB buffer pool. An ERPNext database of a GB or more then reads
# most pages from disk, and every bill save (20+ queries per item row) waits on it. The pool size
# lives in <erp_dir>/mariadb/99-sspl.cnf; overrides/compose.sspl-db.yaml mounts that folder on the
# db container's /etc/mysql/conf.d. MariaDB 11.8 resizes the pool live, so a new size applies at
# once and the file keeps it across restarts. A stack installed before the mount existed gets it
# from "Make permanent", which recreates the db container.

DBM_CNF = "99-sspl.cnf"
DBM_TARGET = "/etc/mysql/conf.d"
DBM_OVERRIDE = "compose.sspl-db.yaml"
DBM_STEP = 128 << 20
DBM_MIN = 128 << 20
DBM_HOST_RESERVE = 2 << 30   # never handed to the pool: OS, Redis, workers, OpenWA
DBM_SIZE_RE = re.compile(r"^\s*(\d{1,7})\s*([MG])\s*$", re.I)


def _dbm_erp_dir():
    return erp_dir()


def _dbm_compose():
    return compose_file()


def _dbm_cnf_dir():
    return Path(_dbm_erp_dir()) / "mariadb"


def _dbm_override_path():
    return Path(_dbm_erp_dir()) / "frappe_docker" / "overrides" / DBM_OVERRIDE


def _fmt_pool(n):
    mb = int(n) // (1 << 20)
    return f"{mb // 1024}G" if mb and mb % 1024 == 0 else f"{mb}M"


def _parse_pool(text):
    m = DBM_SIZE_RE.match(str(text or ""))
    if not m:
        return None
    return int(m.group(1)) << (30 if m.group(2).upper() == "G" else 20)


def _round_pool(n, up=False):
    q, r = divmod(int(n), DBM_STEP)
    return (q + (1 if up and r else 0)) * DBM_STEP


def _db_exec(sql, timeout=30):
    """Run SQL as root inside the db container -> (ok, stdout or error text).

    The SQL travels as $1, never through shell interpolation, and the password is
    the container's own, the one MariaDB was initialised with.
    """
    cmd = ["docker", "compose", "-f", _dbm_compose(), "exec", "-T", "db", "sh", "-c",
           'MYSQL_PWD="${MARIADB_ROOT_PASSWORD:-$MYSQL_ROOT_PASSWORD}" '
           'exec mariadb -uroot -N -B -e "$1"', "sh", sql]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)[:300]
    if out.returncode != 0:
        return False, (out.stderr or "mariadb failed").strip()[:300]
    return True, out.stdout


def _db_container():
    """{'mounted': conf.d mount present, 'mem_limit': bytes or 0} for the db container."""
    info = {"mounted": False, "mem_limit": 0}
    try:
        ps = subprocess.run(["docker", "compose", "-f", _dbm_compose(), "ps", "-q", "db"],
                            capture_output=True, text=True, timeout=20)
        cid = (ps.stdout.split() or [""])[0]
        if not cid:
            return info
        out = subprocess.run(["docker", "inspect", cid], capture_output=True, text=True, timeout=20)
        data = json.loads(out.stdout)[0]
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return info
    info["mounted"] = any(m.get("Destination") == DBM_TARGET for m in data.get("Mounts") or [])
    info["mem_limit"] = int((data.get("HostConfig") or {}).get("Memory") or 0)
    return info


def _dbm_configured():
    try:
        text = (_dbm_cnf_dir() / DBM_CNF).read_text()
    except OSError:
        return None
    m = re.search(r"^\s*innodb_buffer_pool_size\s*=\s*(\S+)", text, re.M)
    if not m:
        return None
    return int(m.group(1)) if m.group(1).isdigit() else _parse_pool(m.group(1))


def _dbm_write_cnf(size):
    d = _dbm_cnf_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / DBM_CNF
    # Written in place: MariaDB skips world-writable files, hence the explicit 0644.
    with open(path, "w") as f:
        f.write("# InnoDB buffer pool for the ERPNext database. Set from the admin panel's\n"
                "# RAM monitor page, which also applies it live.\n"
                f"[mysqld]\ninnodb_buffer_pool_size = {_fmt_pool(size)}\n")
    os.chmod(path, 0o644)


def _dbm_write_override():
    ov = _dbm_override_path()
    if not ov.parent.is_dir():
        raise OSError(f"{ov.parent} not found")
    ov.write_text(
        f"# SSPL: MariaDB settings from {_dbm_cnf_dir()} (InnoDB buffer pool, see {DBM_CNF}).\n"
        "services:\n  db:\n    volumes:\n      - type: bind\n"
        f"        source: {_dbm_cnf_dir()}\n        target: {DBM_TARGET}\n"
        "        read_only: true\n")
    return ov


_dbm_cache = {"ts": 0.0, "data": None}
_dbm_raw = {}   # last (reads, read_requests) sample, for the recent miss rate


def dbm_state(force=False):
    now = time.time()
    if not force and _dbm_cache["data"] is not None and now - _dbm_cache["ts"] < 30:
        return _dbm_cache["data"]
    mem = meminfo()
    ram = mem.get("MemTotal", 0)
    st = {"error": None, "live": None, "data": None, "miss_pct": None, "recent_miss_pct": None,
          "configured": _dbm_configured()}
    ok, out = _db_exec(
        "SELECT @@innodb_buffer_pool_size;"
        " SELECT COALESCE(SUM(data_length + index_length), 0) FROM information_schema.tables"
        " WHERE engine = 'InnoDB';"
        " SELECT variable_value FROM information_schema.global_status"
        " WHERE variable_name = 'INNODB_BUFFER_POOL_READS';"
        " SELECT variable_value FROM information_schema.global_status"
        " WHERE variable_name = 'INNODB_BUFFER_POOL_READ_REQUESTS';", timeout=60)
    if ok:
        try:
            live, data, reads, reqs = (int(float(v)) for v in out.split()[:4])
            # Too few requests (a fresh or idle DB) make the ratio meaningless
            st.update(live=live, data=data,
                      miss_pct=round(min(100.0, 100.0 * reads / reqs), 2) if reqs >= 10000 else None)
            # The total counts since the DB started, so it barely moves after a resize;
            # the rate between two samples shows what the current pool is doing.
            prev = _dbm_raw.get("v")
            if prev and reqs - prev[1] > 1000:
                st["recent_miss_pct"] = round(
                    min(100.0, 100.0 * max(0, reads - prev[0]) / (reqs - prev[1])), 2)
            elif prev and "recent" in _dbm_raw:
                st["recent_miss_pct"] = _dbm_raw["recent"]
            if prev is None or reqs - prev[1] > 1000 or reqs < prev[1]:
                _dbm_raw["v"] = (reads, reqs)
                if "recent_miss_pct" in st:
                    _dbm_raw["recent"] = st["recent_miss_pct"]
        except ValueError:
            st["error"] = "unexpected answer from MariaDB"
    else:
        st["error"] = out
    st.update(_db_container())

    upper = ram - DBM_HOST_RESERVE
    if st["mem_limit"]:
        upper = min(upper, st["mem_limit"] - (512 << 20))
    upper = max(DBM_MIN, _round_pool(upper))
    # The whole database plus a quarter for growth, but never more than a quarter of RAM
    want = max(256 << 20, (st["data"] or 0) * 5 // 4)
    rec = min(max(DBM_MIN, _round_pool(min(ram // 4, want), up=True)), upper)
    st.update(ram=ram, available=mem.get("MemAvailable", 0), min=DBM_MIN, max=upper, recommended=rec,
              h={"live": _fmt_pool(st["live"]) if st["live"] else "–",
                 "configured": _fmt_pool(st["configured"]) if st["configured"] else "not set",
                 "data": human(st["data"]) if st["data"] is not None else "–",
                 "recommended": _fmt_pool(rec), "max": _fmt_pool(upper), "min": _fmt_pool(DBM_MIN),
                 "mem_limit": human(st["mem_limit"]) if st["mem_limit"] else "none",
                 "available": human(mem.get("MemAvailable", 0))})
    _dbm_cache.update(ts=now, data=st)
    return st


def _dbm_job_busy():
    with _job_lock:
        return bool(_job and _job["proc"].poll() is None)


def _parse_docker_bytes(text):
    m = re.match(r"\s*([\d.]+)\s*([KMGT]?i?B)", text or "")
    if not m:
        return 0
    mult = {"B": 1, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12,
            "KIB": 1 << 10, "MIB": 1 << 20, "GIB": 1 << 30, "TIB": 1 << 40}
    return int(float(m.group(1)) * mult.get(m.group(2).upper(), 1))


_dstats_cache = {"ts": 0.0, "data": None}


def docker_mem_stats():
    """Every running container's memory against its limit (docker stats takes ~2s: cached)."""
    now = time.time()
    if _dstats_cache["data"] is not None and now - _dstats_cache["ts"] < 15:
        return _dstats_cache["data"]
    res = {"rows": [], "error": None}
    try:
        out = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{json .}}"],
                             capture_output=True, text=True, timeout=40)
        if out.returncode != 0:
            res["error"] = (out.stderr or "docker stats failed").strip()[:300]
        host = meminfo().get("MemTotal", 0)
        for line in out.stdout.splitlines():
            if not line.strip():
                continue
            it = json.loads(line)
            used_s, _, limit_s = (it.get("MemUsage") or "").partition("/")
            used, limit = _parse_docker_bytes(used_s), _parse_docker_bytes(limit_s)
            # No limit set: docker reports the host's RAM as the limit
            limited = bool(limit) and host and limit < host * 0.97
            res["rows"].append({
                "name": it.get("Name", "?"), "used": used, "used_h": human(used),
                "limit_h": human(limit) if limited else "none",
                "pct": round(100.0 * used / limit, 1) if limited else None,
                "host_pct": round(100.0 * used / host, 1) if host else 0,
                "cpu": it.get("CPUPerc", ""),
            })
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        res["error"] = str(e)[:300]
    res["rows"].sort(key=lambda r: r["used"], reverse=True)
    _dstats_cache.update(ts=now, data=res)
    return res


def top_processes(limit=12):
    procs = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/status") as f:
                fields = dict(line.split(":", 1) for line in f if ":" in line)
            if "VmRSS" not in fields:   # kernel thread
                continue
            rss = int(fields["VmRSS"].split()[0]) * 1024
            swap = int(fields.get("VmSwap", "0 kB").split()[0]) * 1024
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode(errors="replace").strip()
        except (OSError, ValueError):
            continue
        procs.append({"pid": int(pid), "name": fields.get("Name", "").strip(), "rss": rss,
                      "swap": swap, "cmd": cmd[:140]})
    procs.sort(key=lambda p: p["rss"] + p["swap"], reverse=True)
    for p in procs:
        p["rss_h"], p["swap_h"] = human(p["rss"]), human(p["swap"])
    return procs[:limit]


_vm_last = {"ts": 0.0, "in": 0, "out": 0}


def swap_rates():
    """Bytes/s swapped in and out since the previous call (None on the first)."""
    vals = {}
    try:
        with open("/proc/vmstat") as f:
            for line in f:
                k, _, v = line.partition(" ")
                if k in ("pswpin", "pswpout"):
                    vals[k] = int(v)
    except OSError:
        return None, None
    now, page = time.time(), os.sysconf("SC_PAGE_SIZE")
    prev = dict(_vm_last)
    _vm_last.update(ts=now, **{"in": vals.get("pswpin", 0), "out": vals.get("pswpout", 0)})
    dt = now - prev["ts"]
    if not prev["ts"] or dt <= 0:
        return None, None
    return (max(0, _vm_last["in"] - prev["in"]) * page / dt,
            max(0, _vm_last["out"] - prev["out"]) * page / dt)


def mem_pressure():
    """Share of the last 10s some task stalled waiting for memory (PSI), or None."""
    try:
        with open("/proc/pressure/memory") as f:
            m = re.search(r"some avg10=([\d.]+)", f.read())
        return float(m.group(1)) if m else None
    except OSError:
        return None


def ram_report():
    mem = meminfo()
    total, avail = mem.get("MemTotal", 0), mem.get("MemAvailable", 0)
    swap_t, swap_f = mem.get("SwapTotal", 0), mem.get("SwapFree", 0)
    swap_used = swap_t - swap_f
    sin, sout = swap_rates()
    psi = mem_pressure()
    containers = docker_mem_stats()
    db = dbm_state()
    alerts = []

    def alert(level, text):
        alerts.append({"level": level, "text": text})

    avail_pct = 100.0 * avail / total if total else 100
    if avail_pct < 10:
        alert("crit", f"Only {human(avail)} RAM available ({avail_pct:.0f}%)")
    elif avail_pct < 20:
        alert("warn", f"RAM is getting low: {human(avail)} available ({avail_pct:.0f}%)")
    if sin is not None and sin > (1 << 20):
        alert("crit", f"Actively swapping in {human(sin)}/s — the server is out of RAM")
    elif swap_t and swap_used > swap_t * 0.25:
        alert("warn", f"{human(swap_used)} of swap in use")
    if psi is not None and psi >= 10:
        alert("crit" if psi >= 30 else "warn", f"Tasks stalled on memory {psi:.0f}% of the last 10s")
    for c in containers["rows"]:
        if c["pct"] is not None and c["pct"] >= 90:
            alert("warn", f"{c['name']} is at {c['pct']:.0f}% of its {c['limit_h']} memory limit")
    miss = db.get("recent_miss_pct")
    miss = db.get("miss_pct") if miss is None else miss
    if miss is not None and miss > 1:
        alert("warn", f"Database reads {miss:.1f}% of pages from disk — raise the "
                      f"buffer pool (recommended {db['h']['recommended']})")
    if db.get("live") and db["live"] < db["recommended"] // 2:
        alert("warn", f"Database buffer pool {db['h']['live']} is well below the recommended "
                      f"{db['h']['recommended']}")
    return {
        "mem": {"total": human(total), "available": human(avail),
                "used_pct": round(100.0 - avail_pct, 1),
                "cached": human(mem.get("Cached", 0) + mem.get("Buffers", 0))},
        "swap": {"total": human(swap_t), "used": human(swap_used),
                 "pct": round(100.0 * swap_used / swap_t, 1) if swap_t else 0,
                 "in": human(sin) + "/s" if sin is not None else "–",
                 "out": human(sout) + "/s" if sout is not None else "–"},
        "psi": psi, "alerts": alerts, "containers": containers, "processes": top_processes(),
    }


@app.route("/ram")
@login_required
def ram_page():
    return render_template_string(RAM_HTML, user=session["user"], version=PANEL_VERSION)


@app.route("/auth")
@login_required
def auth_page():
    return render_template_string(AUTH_HTML, user=session["user"], version=PANEL_VERSION)


@app.route("/api/auth")
@login_required
def api_auth():
    c = mt_conf()
    linked = auth_sites()
    return jsonify({"installed": auth_installed(), "https": https_on(),
                    "all_bypass": c.get("AUTH_BYPASS_NETWORKS", ""), "all_trusted": c.get("AUTH_TRUSTED_NETWORKS", ""),
                    "sites": [{"name": s, "linked": s in linked, "users": auth_users(s), "nets": auth_nets(s)}
                              for s in list_sites()]})


@app.route("/api/auth/<site>/logins")
@login_required
def api_auth_logins(site):
    if site not in list_sites():
        return jsonify({"error": "no such site"}), 404
    return jsonify(auth_logins(site))


@app.route("/wireguard")
@login_required
def wireguard_page():
    return render_template_string(WG_HTML, user=session["user"], version=PANEL_VERSION)


@app.route("/api/wireguard")
@login_required
def api_wireguard():
    st = wg_status()
    return jsonify({**st, "hub": st["subnet"].removesuffix(".0/24") + ".1",
                    "tools": os.path.isfile(_tool("sites/wireguard.sh")),
                    "stack": os.path.isfile(compose_file()) and os.path.isfile(MT_CONF),
                    "script": _script_ok(WG_SETUP_SCRIPT), "sites": list_sites()})


@app.route("/api/ram")
@login_required
def api_ram():
    return jsonify(ram_report())


@app.route("/api/db-memory")
@login_required
def api_db_memory():
    return jsonify(dbm_state(force=request.args.get("fresh") == "1"))


@app.route("/api/db-memory", methods=["POST"])
@login_required
def api_db_memory_set():
    data = request.get_json(silent=True) or {}
    st = dbm_state(force=True)
    raw = str(data.get("size", "")).strip()
    size = st["recommended"] if raw.lower() == "recommended" else _parse_pool(raw)
    if size is None:
        return jsonify({"error": "Size must look like 2G or 1536M"}), 400
    if not st["min"] <= size <= st["max"]:
        return jsonify({"error": f"Size must be between {st['h']['min']} and {st['h']['max']} "
                                 f"(RAM minus {_fmt_pool(DBM_HOST_RESERVE)} for everything else)"}), 400
    # Some MariaDB builds cap live growth (innodb_buffer_pool_size_max). Then the size is
    # only saved, and takes effect when the db container next starts.
    live_ok, out = _db_exec(f"SET GLOBAL innodb_buffer_pool_size = {int(size)}")
    try:
        _dbm_write_cnf(size)
    except OSError as e:
        return jsonify({"error": (f"Applied live, but could not save {DBM_CNF}: {e}" if live_ok
                                  else f"MariaDB refused the new size ({out}) and {DBM_CNF} "
                                       f"could not be saved: {e}")}), 500
    st = dbm_state(force=True)
    if not live_ok:
        return jsonify({"ok": True, "state": st, "message": (
            f"MariaDB would not resize live ({out}). Saved {_fmt_pool(size)} to the file: it "
            "applies when the db container restarts" + ("." if st["mounted"] else
            " — press 'Make permanent', which restarts it."))})
    msg = f"Buffer pool is now {st['h']['live']}."
    msg += (" Saved — it stays after a restart." if st["mounted"] else
            " Saved to the file, but the db container does not read it yet: press"
            " 'Make permanent' or it falls back to 128M on the next DB restart.")
    return jsonify({"ok": True, "message": msg, "state": st})


@app.route("/api/db-memory/persist", methods=["POST"])
@login_required
def api_db_memory_persist():
    """Add the conf.d mount to the compose file and recreate the db container."""
    if _dbm_job_busy():
        return jsonify({"error": "A job is running — wait for it to finish first"}), 409
    st = dbm_state(force=True)
    if st["mounted"]:
        return jsonify({"ok": True, "message": "Already permanent.", "state": st})
    compose = _dbm_compose()
    try:
        if _dbm_configured() is None:
            _dbm_write_cnf(st["live"] if st["live"] and st["live"] > DBM_MIN else st["recommended"])
        ov = _dbm_write_override()
        # Merging the generated file with the override is lossless (it is itself the
        # output of 'docker compose config'), so no installer re-run is needed.
        merged = subprocess.run(["docker", "compose", "-f", compose, "-f", str(ov), "config"],
                                capture_output=True, text=True, timeout=60,
                                cwd=os.path.dirname(compose))
    except (OSError, subprocess.SubprocessError) as e:
        return jsonify({"error": f"Could not prepare the compose change: {e}"}), 500
    if merged.returncode != 0 or DBM_TARGET not in merged.stdout:
        return jsonify({"error": "docker compose config failed: "
                                 + (merged.stderr or "mount missing from output").strip()[:300]}), 500
    backup = f"{compose}.bak-dbmem-{datetime.now():%Y%m%d_%H%M%S}"
    shutil.copy2(compose, backup)
    with open(compose, "w") as f:
        f.write(merged.stdout)

    def up_db():
        return subprocess.run(["docker", "compose", "-f", compose, "up", "-d", "db"],
                              capture_output=True, text=True, timeout=300)

    up = up_db()
    if up.returncode != 0:
        shutil.copy2(backup, compose)   # put the old definition back and bring db up on it
        up_db()
        return jsonify({"error": "Recreating the db container failed, the old compose file was "
                                 "restored: " + (up.stderr or "").strip()[-300:]}), 500
    for _ in range(60):
        if _db_exec("SELECT 1", timeout=10)[0]:
            break
        time.sleep(2)
    _containers_cache.update(ts=0.0, data=None)
    st = dbm_state(force=True)
    if not st["mounted"]:
        return jsonify({"error": "The db container restarted but the mount is not there — "
                                 f"check {compose} (backup: {backup})"}), 500
    return jsonify({"ok": True, "state": st,
                    "message": f"Permanent: the db container now reads {DBM_CNF} "
                               f"(buffer pool {st['h']['live']}). Old compose file: {backup}"})


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        user = request.form.get("username", "")
        pw = request.form.get("password", "")
        if user == CONFIG["username"] and check_password_hash(CONFIG["password_hash"], pw):
            session["user"] = user
            return redirect(url_for("index"))
        time.sleep(1)  # slow down guessing
        error = "Invalid username or password"
    return render_template_string(LOGIN_HTML, error=error)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    # Gate the updater on the script being on disk, not just repo_dir being
    # set: an older checkout without it would render a button that fails.
    return render_template_string(
        DASH_HTML, user=session["user"], version=PANEL_VERSION,
        repo_ok=REPO_DIR is not None and _script_ok(UPDATE_PANEL_SCRIPT),
        tools_ok=os.path.isfile(_tool("lib/common.sh")))


@app.route("/install")
@login_required
def install_suite():
    return render_template_string(INSTALL_HTML, user=session["user"], version=PANEL_VERSION)


@app.route("/api/stats")
@login_required
def api_stats():
    return jsonify(system_stats())


@app.route("/api/sites")
@login_required
def api_sites():
    """The tenant list for the dashboard. Installed apps are NOT here — they
    take a bench call per site; /api/sites/<site>/apps asks on demand."""
    apps = image_apps()
    wired = mt_conf().get("RCLONE_REMOTE", "").strip() if os.path.isfile(MT_CONF) else ""
    return jsonify({"sites": sites_overview(), "image_apps": apps,
                    # False = read from the volume's apps.txt (backend down),
                    # which can be out of date: the picker says so.
                    "apps_live": bool(_apps_cache.get("live")),
                    # Shared by every site: each uploads to <target>/sites/<host>/
                    "cloud": wired.split() if wired else [],
                    "http_port": http_port(),
                    "https": https_on(),
                    "https_reason": https_off_reason(),
                    "auth": {"installed": auth_installed(), "users": {s: auth_users(s) for s in list_sites()} if auth_installed() else {}},
                    "stack": os.path.isfile(compose_file()),
                    "tools": os.path.isfile(_tool("lib/common.sh"))})


@app.route("/api/sites/<site>/apps")
@login_required
def api_site_apps(site):
    if site not in list_sites():
        return jsonify({"error": "no such site"}), 404
    apps, err = installed_apps(site)
    return jsonify({"site": site, "apps": apps or [], "error": err,
                    "skip": skip_apps(site)})


@app.route("/api/backups")
@login_required
def api_backups():
    site = request.args.get("site") or None
    if site is not None and not valid_site_name(site):
        return jsonify({"error": "invalid site"}), 400
    return jsonify(backups_overview(site))


@app.route("/api/job")
@login_required
def api_job():
    return jsonify(job_status())


@app.route("/api/logs")
@login_required
def api_logs():
    return jsonify({"logs": job_logs()})


@app.route("/api/setup-status")
@login_required
def api_setup_status():
    return jsonify(setup_status())


@app.route("/api/openwa/key", methods=["POST"])
@login_required
def api_openwa_key():
    # POST, and never part of /api/setup-status: the key is only fetched when
    # someone presses Show API key, and is not cached by the browser.
    key, err = openwa_api_key()
    resp = jsonify({"error": err}) if err else jsonify({"key": key})
    resp.headers["Cache-Control"] = "no-store"
    return resp, (500 if err else 200)


@app.route("/api/cron")
@login_required
def api_cron():
    """Root's whole crontab, unfiltered."""
    return jsonify({"jobs": crontab_entries()})


def _site_arg(data):
    """--site <host> for a live site, or --all. Returns (args, label_suffix, error)."""
    if data.get("all"):
        return ["--all"], "all sites", None
    site = str(data.get("site", "")).strip()
    if site not in list_sites():
        return None, None, "choose a site (or all sites)"
    return ["--site", site], site, None


def _new_site_request(data):
    host = str(data.get("site", "")).strip().lower()
    pw = str(data.get("admin_password", ""))
    if not valid_site_name(host):
        return None, None, ("site name must be a lowercase hostname or IPv4 address, "
                            "e.g. acme.erp.lan")
    if host in list_sites():
        return None, None, f"{host} already exists"
    owner = domain_map().get(host)
    if owner:
        return None, None, f"{host} is already a domain of {owner} — remove it there first"
    if len(pw) < 4:
        return None, None, "set an Administrator password for the new site"
    apps = data.get("apps")
    args = [host, "--yes"]
    if apps is not None:
        if not isinstance(apps, list):
            return None, None, "apps must be a list"
        apps = [str(a) for a in apps]
        avail = image_apps()
        bad = [a for a in apps if not APP_NAME_RE.match(a) or (avail and a not in avail)]
        if bad:
            return None, None, f"not in the image: {', '.join(bad)}"
        # Every app ticked is the default: no --apps, so a later image app
        # still counts as wanted. Only a real subset is recorded as a choice.
        if not apps:
            return None, None, "tick at least one app (erpnext, normally)"
        if set(apps) != set(avail):
            args += ["--apps", ",".join(apps)]
    return args, {"ADMIN_PASSWORD": pw}, None


def _port_request(name, data):
    """Arguments for 'domains.sh port|unport'. (args, label, None) or error."""
    p = str(data.get("port", "")).strip()
    if not (p.isdigit() and 1 <= int(p) <= 65535):
        return None, None, "port must be a number between 1 and 65535"
    port = int(p)
    pmap = port_map()
    if name == "port_remove":
        if port not in pmap:
            return None, None, f"port {port} is not assigned to a site"
        return [str(port)], f"Remove port {port} from {pmap[port]}", None
    site = str(data.get("site", "")).strip().lower()
    if site not in list_sites():
        return None, None, "no such site"
    owner = pmap.get(port)
    if owner and owner != site:
        return None, None, f"port {port} already belongs to {owner}"
    # 29101-30099: the sites' Authelias (lib/common.sh AUTH_BASE_PORT).
    reserved = RESERVED_PORTS | set(range(29101, 30100)) | {http_port()} | ({https_port()} if https_port() else set())
    if port in reserved:
        return None, None, f"port {port} is reserved (HTTP/HTTPS, SSH, OpenWA, this panel or the stack's nginx)"
    args = [site, str(port)] + (["--primary"] if data.get("primary") else [])
    return args, f"Port {port} for {site}", None


def _domain_request(name, data):
    """Arguments for sites/domains.sh. Returns (args, label, None) or
    (None, None, error). Both names pass the tools' own site-name check."""
    site = str(data.get("site", "")).strip().lower()
    domain = str(data.get("domain", "")).strip().lower()
    if not valid_site_name(domain):
        return None, None, "domain must be a lowercase hostname or IPv4 address, e.g. erp.acme.com"
    dmap = domain_map()
    if name == "domain_cert":
        owner = dmap.get(domain)
        if not owner:
            return None, None, f"{domain} is not in the domain table — add it first"
        if IPV4_RE.match(domain):
            return None, None, "Let's Encrypt does not issue certificates for IP addresses"
        if http_port() != 80:
            return None, None, f"Let's Encrypt checks domains on port 80, but this stack's HTTP port is {http_port()}"
        if not https_on():
            return None, None, https_off_reason()
        return [domain], f"HTTPS certificate for {domain}", None
    if name == "domain_remove":
        owner = dmap.get(domain)
        if not owner:
            return None, None, f"{domain} is not in the domain table"
        if owner == domain:
            return None, None, f"{domain} is the site's own name — drop the site to remove it"
        return [domain], f"Remove domain {domain} from {owner}", None
    if site not in list_sites():
        return None, None, "no such site"
    if name == "domain_primary":
        if domain.isdigit():
            if port_map().get(int(domain)) != site:
                return None, None, f"port {domain} does not point at {site}"
            return [site, domain], f"Primary address of {site}: port {domain}", None
        if dmap.get(domain) != site:
            return None, None, f"{domain} does not point at {site}"
        return [site, domain], f"Primary domain of {site}: {domain}", None
    owner = dmap.get(domain)
    if owner and owner != site:
        return None, None, f"{domain} already points at {owner}"
    if domain != site and domain in list_sites():
        return None, None, f"{domain} is the name of another site"
    args = [site, domain] + (["--primary"] if data.get("primary") else [])
    return args, f"Add domain {domain} to {site}", None


def _auth_password(data):
    pw = str(data.get("password", ""))
    if len(pw) < 8:
        return None, "the password needs 8 characters or more"
    if len(pw) > 256 or "\n" in pw or "\r" in pw:
        return None, "the password is too long or has a line break"
    return pw, None


def _auth_request(name, data):
    """Arguments for sites/auth.sh. (args, env, label, None) or (None, None, None, error).
    Passwords go in env (AUTH_PASSWORD), never argv."""
    if not auth_installed():
        return None, None, None, "Authelia is not installed — install it from the installation suite"
    if name in ("auth_link", "auth_unlink"):
        site = str(data.get("site", "")).strip().lower()
        if site not in list_sites():
            return None, None, None, "no such site"
        if name == "auth_link":
            if not https_on():
                return None, None, None, "HTTPS is off on the host nginx — Authelia only works on https:// pages"
            if not (any(auth_domain_ok(d) for d in site_domains(site)) or site in port_map().values()):
                return None, None, None, (f"{site} has no domain with a dot and no IP + port, which Authelia "
                                          "needs: add one under Domains… first")
            if not auth_users(site):
                return None, None, None, f"add an Authelia user for {site} first, or nobody can enter"
            return [site], None, f"Put {site} behind Authelia", None
        return [site], None, f"Remove the Authelia login from {site}", None
    if name == "auth_trusted":
        nets = str(data.get("networks", "")).split()
        if len(nets) > 20 or any(not CIDR_RE.match(n) for n in nets):
            return None, None, None, "networks: IPv4 addresses or CIDRs (e.g. 192.168.1.0/24), space-separated"
        return [" ".join(nets)], None, "No-2FA networks: " + (" ".join(nets) or "none"), None
    if name == "auth_mail_test":
        to = str(data.get("to", "")).strip()
        if not EMAIL_RE.match(to):
            return None, None, None, "that e-mail address does not look right"
        return [to], None, f"Test mail to {to}", None

    site = str(data.get("site", "")).strip().lower()
    if site not in list_sites():
        return None, None, None, "no such site"
    if name in ("auth_ip_add", "auth_ip_remove"):
        kind = str(data.get("kind", ""))
        if kind not in ("bypass", "trusted"):
            return None, None, None, "unknown list"
        try:
            net = ipaddress.IPv4Network(str(data.get("ip", "")).strip(), strict=False)
        except ValueError:
            return None, None, None, "enter an IPv4 address or network, e.g. 203.0.113.7 or 192.168.1.0/24"
        if net.prefixlen < 8:
            return None, None, None, "that network is too wide (/8 or narrower)"
        verb = "Add" if name == "auth_ip_add" else "Remove"
        what = "whitelist" if kind == "bypass" else "no-2FA list"
        return [site, kind, str(net)], None, f"{verb} {net}: {what} of {site}", None
    user = str(data.get("user", "")).strip().lower()
    if not AUTH_USER_RE.match(user):
        return None, None, None, "user names: lowercase letters, digits, . _ - (start with a letter or digit)"
    exists = any(u["user"] == user for u in auth_users(site))
    if name == "auth_user_add":
        if exists:
            return None, None, None, f"user {user} already exists on {site}"
        email = str(data.get("email", "")).strip()
        if not EMAIL_RE.match(email):
            return None, None, None, "that e-mail address does not look right (2FA codes are sent there)"
        disp = str(data.get("name", "")).strip()[:100] or user
        if any(ord(c) < 32 for c in disp):
            return None, None, None, "the display name has control characters"
        pw, err = _auth_password(data)
        if err:
            return None, None, None, err
        return [site, user, email, disp], {"AUTH_PASSWORD": pw}, f"Add Authelia user {user} on {site}", None
    if not exists:
        return None, None, None, f"no user {user} on {site}"
    if name == "auth_user_passwd":
        pw, err = _auth_password(data)
        if err:
            return None, None, None, err
        return [site, user], {"AUTH_PASSWORD": pw}, f"New password for {user} on {site}", None
    if name == "auth_user_disable":
        on = "yes" if data.get("on") else "no"
        return [site, user, on], None, f"{user} on {site}: disabled = {on}", None
    if name == "auth_user_remove" and site_auth_linked(site) and len(auth_users(site)) == 1:
        return None, None, None, (f"{user} is the only user of {site} — Authelia does not start without one; "
                                  "add another or remove the login from the site first")
    if name == "auth_user_remove":
        return [site, user], None, f"Remove user {user} from {site}", None
    if name == "auth_user_reset_2fa":
        return [site, user], None, f"Reset 2FA of {user} on {site}", None
    return None, None, None, "unknown action"


def _wg_request(name, data):
    """Arguments for sites/wireguard.sh: (args, label, None) or (None, None, error)."""
    peer = str(data.get("name", "")).strip().lower()
    if not WG_NAME_RE.fullmatch(peer):
        return None, None, "name: lowercase letters, digits and - (31 at most)"
    exists = any(p["name"] == peer for p in wg_peers())
    if name == "wg_add":
        if exists:
            return None, None, f"print server {peer} already exists"
        site = str(data.get("site", "")).strip()
        if site and site not in list_sites():
            return None, None, "no such site"
        return [peer] + (["--site", site] if site else []), f"Add print server {peer}", None
    if not exists:
        return None, None, f"no print server {peer}"
    return [peer], f"{ACTIONS[name]['label']} {peer}", None


@app.route("/api/wireguard/setup/<peer>")
@login_required
def api_wireguard_setup(peer):
    # Holds the peer's private key: straight to the browser, never in a job log.
    if not any(p["name"] == peer for p in wg_peers()):
        return jsonify({"error": "no such print server"}), 404
    try:
        r = subprocess.run([_tool("sites/wireguard.sh"), "setup-script", peer], capture_output=True,
                           text=True, timeout=20, env={**os.environ, "SSPL_CONF": MT_CONF})
    except (OSError, subprocess.SubprocessError):
        return jsonify({"error": "could not run sites/wireguard.sh"}), 500
    if r.returncode != 0:
        return jsonify({"error": r.stderr.strip()[-300:] or "failed"}), 500
    return Response(r.stdout, mimetype="text/x-shellscript", headers={
        "Content-Disposition": f'attachment; filename="sspl-wg-{peer}.sh"', "Cache-Control": "no-store"})


@app.route("/api/run/<name>", methods=["POST"])
@login_required
def api_run(name):
    if name not in ACTIONS:
        abort(404)
    data = request.get_json(silent=True) or {}
    extra_env = None
    extra_args = None
    label = None

    if name in INSTALL_ACTIONS or name == "apply_hosts":
        pass
    elif not os.path.isfile(ACTIONS[name]["cmd"][0]):
        return jsonify({"error": f"{ACTIONS[name]['cmd'][0]} is missing — install the "
                                 "tools from the installation suite first"}), 400

    if name in SITE_OR_ALL:
        extra_args, which, err = _site_arg(data)
        if err:
            return jsonify({"error": err}), 400
        label = f"{ACTIONS[name]['label']} — {which}"

    elif name == "restore":
        extra_args, label, err, status = _restore_request(data)
        if err:
            return jsonify({"error": err}), status

    elif name == "new_site":
        extra_args, extra_env, err = _new_site_request(data)
        if err:
            return jsonify({"error": err}), 400
        label = f"Create site {extra_args[0]}"

    elif name == "drop_site":
        site = str(data.get("site", "")).strip()
        if site not in list_sites():
            return jsonify({"error": "no such site"}), 400
        if not _check_admin(data):
            return jsonify({"error": "admin password is incorrect"}), 403
        if str(data.get("confirm_site", "")).strip() != site:
            return jsonify({"error": f"type the site name exactly — {site} — to confirm"}), 400
        extra_args = [site, "--yes"]
        label = f"Drop site {site}"

    elif name == "uninstall_app":
        site = str(data.get("site", "")).strip()
        app_name = str(data.get("app", "")).strip()
        if site not in list_sites():
            return jsonify({"error": "choose a site"}), 400
        if not APP_NAME_RE.match(app_name) or app_name == "frappe":
            return jsonify({"error": "choose an app to uninstall"}), 400
        apps, err = installed_apps(site)
        if err:
            return jsonify({"error": f"could not list the installed apps: {err}"}), 500
        if app_name not in apps:
            return jsonify({"error": f"'{app_name}' is not installed on {site}"}), 400
        extra_args = ["--site", site, app_name]
        label = f"Uninstall {app_name} from {site}"

    elif name == "rollback":
        snap = data.get("snapshot", "")
        if snap:
            if not SNAPSHOT_RE.match(snap):
                return jsonify({"error": "invalid snapshot name"}), 400
            extra_env = {"BACKUP_FILE": str(image_backup_dir() / snap)}

    elif name == "apply_hosts":
        if not os.path.isfile(ACTIONS[name]["cmd"][0]):
            return jsonify({"error": "install the tools first"}), 400

    elif name in ("port_add", "port_remove"):
        extra_args, label, err = _port_request(name, data)
        if err:
            return jsonify({"error": err}), 400
        # The same address the Domains… dialog shows, for host_name.
        extra_env = {"SERVER_IP": server_ip()}

    elif name in ("domain_add", "domain_remove", "domain_primary", "domain_cert"):
        extra_args, label, err = _domain_request(name, data)
        if err:
            return jsonify({"error": err}), 400
        if name in ("domain_primary", "domain_remove"):
            extra_env = {"SERVER_IP": server_ip()}
        if name == "domain_cert":
            email = str(data.get("email", "")).strip()
            if email:
                if not EMAIL_RE.match(email):
                    return jsonify({"error": "that e-mail address does not look right"}), 400
                extra_env = {"LETSENCRYPT_EMAIL": email}

    elif name.startswith("auth_"):
        extra_args, extra_env, label, err = _auth_request(name, data)
        if err:
            return jsonify({"error": err}), 400

    elif name.startswith("wg_"):
        extra_args, label, err = _wg_request(name, data)
        if err:
            return jsonify({"error": err}), 400

    elif name in INSTALL_ACTIONS:
        if not REPO_DIR:
            return jsonify({"error": "repo_dir is not configured on this server"}), 400
        script = ACTIONS[name]["cmd"][1]
        if not os.path.isfile(script):
            return jsonify({"error": f"{script} is not in the checkout — git pull first"}), 400
        env, args, err = _install_env(name, data)
        if err:
            return jsonify({"error": err}), 400
        extra_env, extra_args = env, args

    job, err = start_job(name, extra_env, extra_args, label)
    if err:
        return jsonify({"error": err}), 409
    return jsonify({"ok": True, "label": job["label"]})


@app.route("/api/job/input", methods=["POST"])
@login_required
def api_job_input():
    """Type a line into the running job's terminal."""
    data = request.get_json(silent=True) or {}
    line = str(data.get("line", ""))
    if len(line) > 512 or "\n" in line or "\r" in line:
        return jsonify({"error": "one line at a time, 512 characters max"}), 400
    err = send_job_input(line)
    if err:
        return jsonify({"error": err}), 409
    return jsonify({"ok": True})


def _install_env(name, data):
    """Build (and validate) the environment and arguments for an install
    action. Returns (env, args, None) or (None, None, error). Secrets stay in
    the env dict and reach the child as env vars — never argv, never logged."""
    if name == "install_stack":
        image = str(data.get("image", "")).strip() or erp_image()
        db_pw = str(data.get("db_password", ""))
        if os.path.isfile(compose_file()):
            return None, None, "a stack is already installed on this server"
        if len(image) > 200 or not ERP_IMAGE_RE.match(image):
            return None, None, ("Docker image must look like registry/owner/name "
                                "(lowercase, no tag — e.g. ghcr.io/santhosh3279/sspl-erpnext)")
        if len(db_pw) < 6:
            return None, None, "choose a MariaDB root password (6 characters or more)"
        # install-stack.sh 'source's the config it writes, so a quote or
        # backslash in the password would break every tool. Refuse them here.
        if any(c in db_pw for c in "\"'\\$`\n\r ") :
            return None, None, "the database password may not contain quotes, $, `, \\ or spaces"
        if image != erp_image():
            try:
                save_config_key("erp_image", image)
            except OSError as e:
                return None, None, f"could not save the image to config.json: {e}"
        # Port 80, so domains open without a port in the URL.
        return {"HTTP_PORT": "80", "IMAGE": image, "DB_PASSWORD": db_pw,
                "SSPL_INSTALL_CRON": "yes" if data.get("schedule_cron", True) else "no"}, None, None

    if name == "install_tools":
        return {}, ["--cron" if data.get("schedule_cron", True) else "--no-cron"], None

    if name in ("install_rclone", "install_proxy", "install_ports"):
        return {}, None, None
    if name == "install_auth":
        env = {}
        user = str(data.get("smtp_user", "")).strip()
        pw = str(data.get("smtp_password", "")).replace(" ", "")
        mfrom = str(data.get("mail_from", "")).strip()
        if user and not EMAIL_RE.match(user):
            return None, None, "Gmail address: that does not look like an e-mail address"
        if mfrom and not EMAIL_RE.match(mfrom):
            return None, None, "sender: that does not look like an e-mail address"
        if pw and (len(pw) > 128 or any(ord(c) < 33 for c in pw)):
            return None, None, "the app password has odd characters"
        if user:
            env["SMTP_USER"] = user
        if pw:
            env["SMTP_PASSWORD"] = pw
        if mfrom:
            env["MAIL_FROM"] = mfrom
        if "trusted" in data:
            nets = str(data.get("trusted", "")).split()
            if len(nets) > 20 or any(not CIDR_RE.match(n) for n in nets):
                return None, None, "no-2FA networks: IPv4 addresses or CIDRs, space-separated"
            env["AUTH_TRUSTED_NETWORKS"] = " ".join(nets)
        if not auth_installed() and not (user and pw):
            return None, None, "enter the Gmail address and its app password (Authelia mails the 2FA codes)"
        return env, None, None
    if name == "install_wireguard":
        ep = str(data.get("endpoint", "")).strip()
        port = str(data.get("port", "")).strip() or "51820"
        subnet = str(data.get("subnet", "")).strip() or MT_DEFAULTS["WG_SUBNET"]
        if not WG_ENDPOINT_RE.fullmatch(ep):
            return None, None, "enter this server's public IP or DNS name"
        if not (port.isascii() and port.isdigit() and 1 <= int(port) <= 65535):
            return None, None, "UDP port must be a number between 1 and 65535"
        if not WG_SUBNET_RE.fullmatch(subnet):
            return None, None, "tunnel network: a private /24, e.g. 10.99.0.0/24"
        return {"WG_ENDPOINT": ep, "WG_PORT": port, "WG_SUBNET": subnet}, None, None
    if name == "install_openwa":
        return {"OPENWA_DIR": OPENWA_DIR, "OPENWA_PORT": str(OPENWA_PORT)}, None, None

    # The updater restarts the panel at the end, which would kill its own
    # job — the flag makes it defer the restart until after the job ends.
    if name == "update_panel":
        return {"SSPL_FROM_PANEL": "1"}, None, None
    return None, None, "unknown install action"


def _set_conf_value(key, value):
    """Rewrite (or append) one KEY="value" line in sspl-mt.conf, atomically,
    keeping its mode (0600: it is root's and the tools 'source' it). value is
    validated by the caller to contain no quote, $ or backslash."""
    try:
        with open(MT_CONF) as f:
            text = f.read()
        mode = os.stat(MT_CONF).st_mode & 0o7777
    except OSError:
        return "sspl-mt.conf is missing — install the stack first"
    line = f'{key}="{value}"  # set from the admin panel'
    pat = re.compile(rf"^{key}=.*$", re.M)
    new_text, n = pat.subn(line.replace("\\", "\\\\"), text, count=1)
    if n != 1:
        new_text = text.rstrip("\n") + "\n" + line + "\n"
    tmp = MT_CONF + ".panel-tmp"
    try:
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            f.write(new_text)
        os.chmod(tmp, mode)
        os.replace(tmp, MT_CONF)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return f"could not write sspl-mt.conf: {e}"
    return None


@app.route("/api/rclone/wire", methods=["POST"])
@login_required
def api_rclone_wire():
    """Point every backup (all sites) at one or more cloud remotes, by setting
    RCLONE_REMOTE in sspl-mt.conf. Each site then uploads to
    <remote><folder>/sites/<host>/.

    Every remote name must be one rclone reports: a typo would not fail the
    backup — the upload failure is only a warning — so it would show green
    while nothing reached the cloud.

    Accepts {"remotes": ["gdrive:", "mega:"], "path": "frappe-backups"},
    {"all": true, "path": ...} for the "*:" form, or {"off": true}."""
    data = request.get_json(silent=True) or {}
    if data.get("off"):
        err = _set_conf_value("RCLONE_REMOTE", "")
        if err:
            return jsonify({"error": err}), 500
        return jsonify({"ok": True, "wired": ""})
    path = str(data.get("path", "")).strip().strip("/")
    use_all = bool(data.get("all"))
    names = [str(n).strip() for n in (data.get("remotes") or []) if str(n).strip()]

    if path and (not RCLONE_PATH_RE.match(path) or ".." in path):
        return jsonify({"error": "folder may only use letters, digits, . - _ and /"}), 400

    known = rclone_remotes()
    if use_all:
        if not known:
            return jsonify({"error": "rclone has no remotes configured yet — "
                                     "connect a cloud account first"}), 400
        target = "*:" + path
    else:
        if not names:
            return jsonify({"error": "choose at least one remote"}), 400
        seen = []
        for n in names:
            if not RCLONE_NAME_RE.match(n):
                return jsonify({"error": f"'{n}' is not a valid remote name"}), 400
            if n not in known:
                return jsonify({"error": f"rclone does not know a remote called '{n}' — "
                                         "connect it first"}), 400
            if n not in seen:
                seen.append(n)
        target = " ".join(n + path for n in seen)

    err = _set_conf_value("RCLONE_REMOTE", target)
    if err:
        return jsonify({"error": err}), 500
    return jsonify({"ok": True, "wired": target})


_rclone_lock = threading.Lock()


def rclone_conf_path():
    """Root's rclone.conf, the one the backup jobs read. None if unknown."""
    try:
        out = subprocess.run(["rclone", "config", "file"], capture_output=True,
                             text=True, timeout=15, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = out.stdout.split("\n")
    path = next((ln.strip() for ln in reversed(lines) if ln.strip()), "")
    return os.path.realpath(path) if out.returncode == 0 and path.startswith("/") else None


def rclone_section(text, name):
    """Options of one remote in rclone.conf text, or None."""
    opts = None
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("[") and s.endswith("]"):
            if opts is not None:
                break
            opts = {} if s[1:-1] == name else None
        elif opts is not None and "=" in s and s[0] not in "#;":
            k, v = s.split("=", 1)
            opts[k.strip()] = v.strip()
    return opts


def rclone_put_section(text, name, opts):
    """rclone.conf text with that remote replaced, or added at the end."""
    out, skip = [], False
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("[") and s.endswith("]"):
            skip = s[1:-1] == name
        if not skip:
            out.append(ln)
    body = "\n".join(out).strip("\n")
    return (body + "\n\n" if body else "") + f"[{name}]\n" \
        + "".join(f"{k} = {v}\n" for k, v in opts.items())


def rclone_token(raw):
    """The OAuth token in what `rclone authorize` printed, as one JSON line, or None."""
    a, b = raw.find("{"), raw.rfind("}")
    try:
        tok = json.loads(raw[a:b + 1]) if 0 <= a < b else None
    except ValueError:
        return None
    if not isinstance(tok, dict):
        return None
    tok = {k: tok[k] for k in ("access_token", "token_type", "refresh_token", "expiry", "expires_in") if k in tok}
    if not all(isinstance(tok.get(k), str) and tok[k] for k in ("access_token", "refresh_token")):
        return None
    line = json.dumps(tok, separators=(",", ":"))
    # rclone's config reader expands %(x)s
    return None if "%(" in line else line


@app.route("/api/rclone/connect", methods=["POST"])
@login_required
def api_rclone_connect():
    """Connect a Google Drive account: {"name", "token", "replace"}. The token
    is written straight to rclone.conf: never argv, a job log or a response.
    Nothing is saved until Google has accepted it."""
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip().rstrip(":")
    if not RCLONE_NAME_RE.fullmatch(name + ":"):
        return jsonify({"error": "name: letters, digits, _ . and - only, 41 at most"}), 400
    raw = str(data.get("token", ""))
    token = rclone_token(raw) if len(raw) <= 8192 else None
    if not token:
        return jsonify({"error": "that is not the token rclone printed — copy everything from { to }"}), 400
    if not shutil.which("rclone"):
        return jsonify({"error": "install rclone first"}), 400
    with _rclone_lock:
        path = rclone_conf_path()
        if not path:
            return jsonify({"error": "could not find rclone's config file"}), 500
        try:
            text = ""
            if os.path.exists(path):
                with open(path) as f:
                    text = f.read()
        except (OSError, ValueError) as e:
            return jsonify({"error": f"could not read {path}: {type(e).__name__}"}), 500
        if "RCLONE_ENCRYPT_V" in text:
            return jsonify({"error": "rclone.conf is password-protected — run 'sudo rclone config' over SSH"}), 400
        old = rclone_section(text, name)
        if old is not None and old.get("type") != "drive":
            return jsonify({"error": f"'{name}' is already used by another kind of remote — choose another name"}), 400
        if old is not None and data.get("replace") is not True:
            return jsonify({"exists": True, "error": f"'{name}' is already connected"}), 409
        # Reconnecting keeps e.g. a shared drive. The token belongs to the
        # OAuth client that asked for it: rclone's own, as in the guide.
        opts = {k: v for k, v in (old or {}).items()
                if k not in ("client_id", "client_secret", "token")}
        opts.update(type="drive", scope=opts.get("scope") or "drive", token=token)
        tmpdir = tempfile.mkdtemp(prefix="sspl-rclone-")        # 0700
        try:
            trial = os.path.join(tmpdir, "rclone.conf")
            with os.fdopen(os.open(trial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
                f.write(rclone_put_section("", name, opts))
            try:
                r = subprocess.run(["rclone", "--config", trial, "lsd", name + ":", "--contimeout", "10s",
                                    "--timeout", "20s", "--retries", "1", "--low-level-retries", "2"],
                                   capture_output=True, text=True, timeout=45, stdin=subprocess.DEVNULL)
            except subprocess.TimeoutExpired:
                return jsonify({"error": "no answer from Google — nothing was saved"}), 502
            if r.returncode != 0:
                why = (r.stderr.strip().splitlines() or ["unknown error"])[-1]
                for secret in json.loads(token).values():
                    if isinstance(secret, str) and len(secret) > 6:
                        why = why.replace(secret, "…")
                return jsonify({"error": "Google did not accept it, nothing was saved: " + why[-300:]}), 400
            # rclone may have renewed the token during the test
            with open(trial) as f:
                opts["token"] = (rclone_section(f.read(), name) or {}).get("token") or token
            # Read again: a backup may have renewed another remote's token.
            text, mode = "", 0o600
            if os.path.exists(path):
                with open(path) as f:
                    text = f.read()
                mode = os.stat(path).st_mode & 0o600
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            fd, new = tempfile.mkstemp(prefix=".rclone.conf.", dir=os.path.dirname(path))     # 0600
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(rclone_put_section(text, name, opts))
                    f.flush()
                    os.fsync(f.fileno())
                os.chmod(new, mode)
                os.replace(new, path)
            except OSError:
                os.unlink(new)
                raise
        except (OSError, ValueError) as e:
            return jsonify({"error": f"could not save the remote: {type(e).__name__}"}), 500
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    return jsonify({"ok": True, "remote": name + ":"})


@app.route("/guide/rclone")
@login_required
def rclone_guide():
    if not _script_ok(RCLONE_GUIDE):
        abort(404)
    return send_file(RCLONE_GUIDE, as_attachment=True)


@app.route("/api/clear-ram", methods=["POST"])
@login_required
def api_clear_ram():
    before = meminfo().get("MemAvailable", 0)
    try:
        subprocess.run(["sync"], timeout=60, check=True)
        with open("/proc/sys/vm/drop_caches", "w") as f:
            f.write("3\n")
    except (OSError, subprocess.SubprocessError) as e:
        return jsonify({"error": f"could not clear caches: {e}"}), 500
    time.sleep(0.5)
    after = meminfo().get("MemAvailable", 0)
    return jsonify({"ok": True, "freed": human(max(0, after - before)),
                    "available": human(after)})


CLEAR_OLD_DAYS = 30
CLEAR_KEEP_MIN = 20


def _prune(items, cutoff, size_of, remove, errors):
    deleted = freed = 0
    for p in items[CLEAR_KEEP_MIN:]:          # newest N per site always kept
        try:
            if p.stat().st_mtime >= cutoff:
                continue
            size = size_of(p)
            remove(p)
        except OSError as e:
            errors.append(f"{p.name}: {e}")
            continue
        deleted += 1
        freed += size
    return deleted, freed


@app.route("/api/backups/clear-old", methods=["POST"])
@login_required
def api_clear_old_backups():
    """Delete full and DB-only backups older than CLEAR_OLD_DAYS, per site.

    The newest CLEAR_KEEP_MIN of each kind survive per site regardless of
    age. Covers dropped sites' backups too — they are kept by the same rule,
    never removed outright. Uploads and image snapshots are untouched."""
    if job_running("restore"):
        return jsonify({"error": "a restore is running — wait for it to finish"}), 409
    cutoff = time.time() - CLEAR_OLD_DAYS * 86400
    deleted, freed, errors = 0, 0, []
    for site in backup_sites():
        full_dir, db_dir = _site_backup_dirs(site)
        full = sorted((d for d in full_dir.iterdir()
                       if d.is_dir() and FULL_BACKUP_RE.match(d.name)),
                      key=lambda d: d.name, reverse=True) if full_dir.is_dir() else []
        d1, f1 = _prune(full, cutoff,
                        lambda d: sum(p.stat().st_size for p in d.rglob("*") if p.is_file()),
                        shutil.rmtree, errors)
        dumps = sorted((p for p in db_dir.iterdir() if p.is_file()),
                       key=lambda p: p.stat().st_mtime,
                       reverse=True) if db_dir.is_dir() else []
        d2, f2 = _prune(dumps, cutoff, lambda p: p.stat().st_size, Path.unlink, errors)
        deleted += d1 + d2
        freed += f1 + f2
    if errors:
        return jsonify({"error": f"deleted {deleted}, but some could not be "
                                 f"removed: {'; '.join(errors[:3])}"}), 500
    return jsonify({"ok": True, "deleted": deleted, "freed": human(freed)})


@app.route("/upload", methods=["POST"])
@login_required
def upload():
    folder = request.form.get("folder", "").strip()
    dest = upload_dir()
    if folder:
        if not SAFE_FOLDER_RE.match(folder):
            return jsonify({"error": "folder name may only use letters, digits, - and _"}), 400
        dest = dest / folder
    dest.mkdir(parents=True, exist_ok=True)
    saved = []
    for f in request.files.getlist("files"):
        name = secure_filename(f.filename or "")
        if not name:
            continue
        if not name.lower().endswith(UPLOAD_EXTENSIONS):
            return jsonify({"error": f"'{name}': only {', '.join(UPLOAD_EXTENSIONS)} files are allowed"}), 400
        f.save(dest / name)
        saved.append(name)
    if not saved:
        return jsonify({"error": "no files received"}), 400
    return jsonify({"ok": True, "saved": saved, "dest": str(dest)})


LICENSE_EXTENSIONS = (".lic", ".txt", ".json", ".key")


@app.route("/upload-license", methods=["POST"])
@login_required
def upload_license():
    if "file" not in request.files:
        return jsonify({"error": "no file part"}), 400
    f = request.files["file"]
    name = secure_filename(f.filename or "")
    if not name:
        return jsonify({"error": "no file selected"}), 400
    if not name.lower().endswith(LICENSE_EXTENSIONS):
        return jsonify({"error": f"only {', '.join(LICENSE_EXTENSIONS)} files are allowed"}), 400
    low = name.lower()
    if low == "site_config.json" or low.endswith("_config.json"):
        return jsonify({"error": "that name is reserved for the site's own config"}), 400

    raw = f.read()
    try:
        site = json.loads(raw).get("site")
    except Exception:
        site = None
    if not site:
        return jsonify({"error": "license file must contain a \"site\" field naming the target site"}), 400
    site = str(site).strip()
    if site not in list_sites():
        return jsonify({"error": f"the license names site '{site[:80]}', which is not on this server"}), 400
    dest_dir = site_dir(site)
    if not dest_dir:
        return jsonify({"error": f"site folder for {site} not found"}), 500
    try:
        dest_path = dest_dir / name
        dest_path.write_bytes(raw)
        os.chmod(dest_path, 0o644)
    except OSError as e:
        return jsonify({"error": f"failed to save file: {e}"}), 500
    return jsonify({"ok": True, "saved": name, "site": site, "dest": str(dest_path)})


def _license_candidates(sdir):
    """License files directly in a site folder. site_config.json (DB password,
    encryption key) and any *_config.json are excluded by name."""
    out = []
    for f in sorted(sdir.iterdir()):
        if not f.is_file():
            continue
        low = f.name.lower()
        if low.endswith("_config.json") or low == "site_config.json":
            continue
        if low.endswith(LICENSE_EXTENSIONS):
            out.append(f.name)
    return out


def _license_site_dir(site):
    if site not in list_sites():
        return None, "choose a site", 400
    d = site_dir(site)
    if not d:
        return None, f"site folder for {site} not found", 500
    return d, None, None


@app.route("/api/licenses")
@login_required
def api_licenses():
    sdir, err, code = _license_site_dir(request.args.get("site", ""))
    if err:
        return jsonify({"error": err}), code
    return jsonify({"ok": True, "site": sdir.name, "files": _license_candidates(sdir)})


@app.route("/download-license")
@login_required
def download_license():
    sdir, err, code = _license_site_dir(request.args.get("site", ""))
    if err:
        return jsonify({"error": err}), code
    files = _license_candidates(sdir)
    name = request.args.get("name", "")
    if name:
        name = secure_filename(name)
        if name not in files:
            return jsonify({"error": "no such license file"}), 404
    elif len(files) == 1:
        name = files[0]
    elif not files:
        return jsonify({"error": "no license file found for this site"}), 404
    else:
        return jsonify({"error": "several license files — pick one"}), 409
    target = (sdir / name).resolve()
    if not str(target).startswith(str(sdir) + os.sep) or not target.is_file():
        return jsonify({"error": "no such license file"}), 404
    return send_file(target, as_attachment=True)


@app.route("/api/uploads/delete", methods=["POST"])
@login_required
def api_uploads_delete():
    """Delete an uploaded file or folder. Uploads only — never a backup."""
    data = request.get_json(silent=True) or {}
    target, err = _safe_upload_target(str(data.get("name", "")))
    if err:
        return jsonify({"error": err}), 400
    if job_running("restore"):
        return jsonify({"error": "a restore is running — wait for it to finish"}), 409
    try:
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    except OSError as e:
        return jsonify({"error": f"could not delete: {e}"}), 500
    return jsonify({"ok": True})


@app.route("/download/<kind>/<path:name>")
@login_required
def download(kind, name):
    return send_file(_safe_download_path(kind, name), as_attachment=True)


@app.route("/download-folder/full/<site>/<name>")
@login_required
def download_folder(site, name):
    """A whole full-backup folder as one streamed zip."""
    folder = _safe_backup_folder(site, name)
    fname = f"{site}_{folder.name}"
    return Response(_zip_stream(folder, fname), mimetype="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{fname}.zip"'})


# ---------------------------------------------------------------- templates

BASE_CSS = """
:root{
  --page:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink-2:#52514e;
  --muted:#898781; --hairline:#e1e0d9; --border:rgba(11,11,11,.10);
  --accent:#2a78d6; --accent-ink:#fff;
  --ok:#0ca30c; --warn:#ec835a; --crit:#d03b3b; --track:#f0efec;
}
@media (prefers-color-scheme: dark){
  :root{
    --page:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink-2:#c3c2b7;
    --muted:#898781; --hairline:#2c2c2a; --border:rgba(255,255,255,.10);
    --accent:#3987e5; --track:#383835;
  }
}
*{box-sizing:border-box} html,body{margin:0}
body{background:var(--page);color:var(--ink);
  font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
a{color:var(--accent)}
.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;
  padding:16px 18px;margin-bottom:16px}
h1{font-size:19px;margin:0} h2{font-size:15px;margin:0 0 12px;color:var(--ink-2)}
button{font:inherit;border:1px solid var(--border);border-radius:8px;cursor:pointer;
  padding:8px 14px;background:var(--surface);color:var(--ink)}
button:hover{border-color:var(--accent)}
button.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
button.danger{color:var(--crit);border-color:var(--crit)}
button:disabled{opacity:.45;cursor:not-allowed}
input,select{font:inherit;padding:8px 10px;border:1px solid var(--hairline);
  border-radius:8px;background:var(--surface);color:var(--ink)}
table{border-collapse:collapse;width:100%;font-size:14px}
th{color:var(--muted);font-weight:600;text-align:left;padding:6px 10px;
  border-bottom:1px solid var(--hairline);white-space:nowrap}
td{padding:6px 10px;border-bottom:1px solid var(--hairline)}
tr:last-child td{border-bottom:none}
.num{font-variant-numeric:tabular-nums}
.badge{font-size:11.5px;border:1px solid var(--hairline);border-radius:6px;padding:1px 6px;color:var(--muted)}
.badge.ok{color:var(--ok);border-color:var(--ok)}
.badge.miss{color:var(--crit);border-color:var(--crit)}
"""

# Page chrome shared by the dashboard and the installation suite.
LAYOUT_CSS = """
.top{display:flex;align-items:center;gap:14px;max-width:1500px;margin:0 auto;padding:18px 16px 6px}
.top .spacer{flex:1}
.top a.nav{font-size:13px;text-decoration:none;border:1px solid var(--border);
  border-radius:8px;padding:7px 12px;color:var(--ink)}
.top a.nav:hover{border-color:var(--accent);color:var(--accent)}
/* The dashboard's Tools dropdown: the other pages, then the panel updater. */
.tools{position:relative}
.tools summary{font-size:13px;border:1px solid var(--border);border-radius:8px;padding:7px 12px;
  background:var(--surface);cursor:pointer;list-style:none;white-space:nowrap;user-select:none}
.tools summary::-webkit-details-marker{display:none}
.tools summary::after{content:"▾";margin-left:6px;color:var(--muted)}
.tools summary:hover,.tools[open] summary{border-color:var(--accent)}
.tools .menu{position:absolute;top:calc(100% + 6px);left:0;z-index:5;min-width:230px;
  max-width:calc(100vw - 32px);padding:6px;background:var(--surface);border:1px solid var(--border);
  border-radius:10px;box-shadow:0 8px 24px rgba(0,0,0,.18)}
.tools .menu a,.tools .menu button{display:block;width:100%;text-align:left;font-size:13px;
  padding:8px 10px;border:0;border-radius:6px;background:none;color:var(--ink);text-decoration:none}
.tools .menu a:hover,.tools .menu button:hover:not(:disabled){background:var(--track)}
.tools .menu .sep{height:1px;margin:6px 4px;background:var(--hairline)}
@media (max-width:700px){
  .top{flex-wrap:wrap;position:relative}
  .tools{position:static}
  .tools .menu{left:16px;right:16px;top:100%}
}
/* One column: the page's controls, then the terminal full-width at the foot.
   Starting a job scrolls the terminal into view. */
main{max-width:1500px;margin:0 auto;padding:0 16px 40px}
.col-left{min-width:0}
.col-right{min-width:0}
#job-card{margin-bottom:0}
"""

# ---- the terminal ----------------------------------------------------------
# Both pages show the same terminal, watching the same server-side job: the
# panel runs one job at a time (_job is a module global), so an install started
# on the suite page is still readable from the dashboard, and vice versa. CSS,
# markup and JS live here once rather than being copied per page — the ANSI
# handling below is fiddly enough that two drifting copies would be a bug farm.

TERM_CSS = """
/* The terminal is one black pane. The log and the input line stay separate
   DOM nodes — the poll rewrites the log wholesale, which would eat the caret
   and any half-typed text — but they are styled as a single surface. */
.term{background:#111;border-radius:8px;padding:12px;display:flex;flex-direction:column;
  min-width:0;cursor:text;
  /* One knob for the whole pane: the log, the prompt and the input line must
     stay the same size or the input stops looking like part of the terminal.
     15.625px = 1.25x the original 12.5px. */
  --term-fs:15.625px;
  /* a definite height so the log scrolls inside it rather than growing the
     page forever; drag the bottom edge to resize, as you would a terminal */
  height:min(60vh,520px);min-height:220px;resize:vertical;overflow:hidden}
#console{color:#ddd;font:var(--term-fs)/1.45 ui-monospace,Menlo,Consolas,monospace;
  white-space:pre-wrap;overflow-wrap:anywhere;overflow:auto;flex:1;min-height:0}
#jobstate{font-size:13px;color:var(--ink-2);margin-bottom:8px}
#jobstate .live{color:var(--accent);font-weight:600}
#jobstate .okrc{color:var(--ok);font-weight:600} #jobstate .badrc{color:var(--crit);font-weight:600}
/* terminal input line — only shown while an interactive job is waiting.
   Borderless and transparent so typing happens *in* the terminal, not in a
   box below it; Enter submits, the way a console does. */
.term-in{display:none;gap:8px;align-items:baseline;flex:none}
.term-in.on{display:flex}
.term-in input{flex:1;min-width:0;font:var(--term-fs)/1.45 ui-monospace,Menlo,Consolas,monospace;
  background:transparent;color:#ddd;border:0;outline:none;padding:0;caret-color:#ddd}
.term-in input::placeholder{color:#555}
.term-in .ps1{color:var(--accent);font:var(--term-fs)/1.45 ui-monospace,monospace}
/* Log history: every past run's log file, folded away under the terminal so
   the live output stays the thing you see first. */
#log-history{margin-top:12px;font-size:13px}
#log-history summary{cursor:pointer;color:var(--ink-2)}
#log-history .rows{max-height:240px;overflow-y:auto;margin-top:8px}
#log-history td a{color:var(--accent)}
#log-history .right{text-align:right}
"""


TERM_HTML = """
<aside class="col-right">
<div class="card" id="job-card">
  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
    <h2 style="margin:0">Terminal — live output</h2>
    <a id="term-download" class="badge" style="display:none;text-decoration:none;cursor:pointer;font-size:12px;padding:3px 8px;border-color:var(--accent);color:var(--accent)" href="#" download>Download Log</a>
  </div>
  <div id="jobstate">No job has been run yet.</div>
  <div class="term" id="term">
    <div id="console"></div>
    <div class="term-in" id="term-in">
      <span class="ps1">&gt;</span>
      <input id="term-line" placeholder="type here, then press Enter" autocomplete="off"
             autocapitalize="off" autocorrect="off" spellcheck="false">
    </div>
  </div>
  <details id="log-history">
    <summary>Log history — past runs</summary>
    <div class="rows" id="log-rows"></div>
  </details>
</div>
</aside>
"""

# RAW string (r"""), like the page templates that embed it — see the note on
# DASH_HTML. The escapes below must reach the browser intact.
TERM_JS = r"""
const $ = s => document.querySelector(s);
const esc = t => (t??'').toString().replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let jobWasActive = false;
// What to refresh when a job finishes. Each page sets its own: the pages show
// different things, so they care about different halves of the aftermath.
let onJobFinished = () => {};

// ---- terminal input: only ever types into the job that is already running ----
async function sendTermLine(){
  const box = $('#term-line'), line = box.value;
  if (!line) return;
  box.value = '';
  try{
    const r = await fetch('/api/job/input', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({line})});
    const j = await r.json();
    if (j.error) alert(j.error);
  }catch(e){ alert('could not send input'); }
  refreshJob();
}
$('#term-line').addEventListener('keydown', e => { if (e.key === 'Enter') sendTermLine(); });

// Click anywhere in the terminal to type, like a real one — but never steal
// a text selection the user is making to copy an error out of the log.
$('#term').addEventListener('click', () => {
  if ($('#term-in').classList.contains('on') && !String(document.getSelection()))
    $('#term-line').focus();
});

// ---- render the log the way a terminal would -------------------------------
// Interactive jobs run on a pty, so docker/bench decide they are talking to a
// terminal and emit ANSI escapes and \r progress lines. Those are control
// codes, not text: printed verbatim they are garbage. Strip the escapes and
// let \r overwrite its line, as a console does. This is deliberately not a
// full emulator — cursor-up redraws just leave successive progress lines.
// OSC (window title) | CSI (colour, cursor) | nF, e.g. the ESC ( B that every
// `tput sgr0` emits | single-character escapes. CSI must be tried before nF.
const ANSI = /\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -\/]*[@-~]|\x1b[ -\/]+[0-~]|\x1b[@-Z\\-_]/g;
// The last sequence can be cut in half: we show the tail of a log that is
// still being written, so the fetch can land mid-escape.
const ANSI_CUT = /\x1b\][^\x07\x1b]*$|\x1b\[[0-?]*[ -\/]*$|\x1b[ -\/]*$/;
// Erase-in-line. Handled, not stripped: docker redraws progress with \r + \x1b[2K,
// and \r alone does not clear a row, so dropping the erase leaves the tail of
// the longer previous line behind ("Pull complete9MB/50MB").
const ERASE = /\x1b\[[012]?K/;

function applyCR(line){
  let out = '';
  for (const chunk of line.split('\r')){   // \r = back to column 0, no erase
    const parts = chunk.split(ERASE);
    if (parts.length > 1) out = '';        // the row was cleared before redrawing
    const text = parts[parts.length - 1].replace(ANSI, '').replace(/\x1b/g, '');
    out = text.length >= out.length ? text : text + out.slice(text.length);
  }
  return out;
}
function termText(s){
  return s.replace(ANSI_CUT, '').split('\n').map(applyCR).join('\n');
}

// The terminal is at the foot of the page, below the controls, so a job
// started from a button up top would otherwise run off-screen.
function revealConsole(){
  $('#job-card').scrollIntoView({behavior:'smooth', block:'end'});
}

async function refreshJob(){
  try{
    const r = await fetch('/api/job'); if(!r.ok) return;
    const j = await r.json();
    document.querySelectorAll('.act').forEach(btn => btn.disabled = j.active);
    document.querySelectorAll('.rs-btn').forEach(btn => btn.disabled = j.active);
    document.querySelectorAll('.setup-install').forEach(btn => btn.disabled = j.active);
    // The input line appears only while an interactive job is actually running.
    const ti = $('#term-in'), wantInput = !!j.interactive;
    if (wantInput !== ti.classList.contains('on')){
      ti.classList.toggle('on', wantInput);
      if (wantInput) $('#term-line').focus();
    }
    if (j.label === null) return;
    const st = j.active
      ? `<b>${esc(j.label)}</b> <span class="live">running…</span> (${j.elapsed}s)`
      : `<b>${esc(j.label)}</b> finished — ` + (j.rc === 0
          ? '<span class="okrc">success</span>' : `<span class="badrc">FAILED (exit ${j.rc})</span>`)
        + ` after ${j.elapsed}s (started ${esc(j.started)})`
        + (j.hint ? ` — ${esc(j.hint)}` : '');
    $('#jobstate').innerHTML = st;
    const dl = $('#term-download');
    if (j.logfile) {
      dl.href = `/download/log/${encodeURIComponent(j.logfile)}`;
      dl.style.display = 'inline-block';
    } else {
      dl.style.display = 'none';
    }
    const c = $('#console'), atEnd = c.scrollTop + c.clientHeight >= c.scrollHeight - 30;
    c.textContent = termText(j.log || '') || '(no output yet)';
    if (atEnd) c.scrollTop = c.scrollHeight;
    if (jobWasActive && !j.active) { refreshLogs(); onJobFinished(); }
    jobWasActive = j.active;
  }catch(e){}
}

// ---- log history -----------------------------------------------------------
// Read once on load and again when a job finishes — not on every poll tick,
// which would scan the jobs directory a few times a second for a list that
// only changes when a run starts or ends.
async function refreshLogs(){
  try{
    const r = await fetch('/api/logs'); if(!r.ok) return;
    const logs = (await r.json()).logs || [];
    $('#log-rows').innerHTML = logs.length
      ? `<table><thead><tr><th>Run</th><th>Date</th><th>Size</th><th></th></tr></thead><tbody>` +
        logs.map(l => `<tr><td>${esc(l.label)}</td><td class="num">${esc(l.mtime)}</td>
          <td class="num">${esc(l.size)}</td>
          <td class="right"><a href="/download/log/${encodeURIComponent(l.name)}">download</a></td></tr>`).join('') +
        '</tbody></table>'
      : '<p style="color:var(--muted)">No runs yet.</p>';
  }catch(e){}
}
refreshLogs();

// Poll the job faster while one is running, so prompts and output feel
// prompt to type against; idle back off when nothing is happening.
function pollJob(){
  const next = () => setTimeout(pollJob, jobWasActive ? 1000 : 3000);
  refreshJob().then(next, next);
}
"""

LOGIN_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SSPL ERP Admin — Login</title><style>""" + BASE_CSS + """
.wrap{min-height:100vh;display:flex;align-items:center;justify-content:center}
.card{width:320px}
label{display:block;margin:12px 0 4px;color:var(--ink-2);font-size:13px}
input{width:100%}
.err{color:var(--crit);font-size:13px;margin-top:10px}
button{width:100%;margin-top:16px}
</style></head><body><div class="wrap"><form class="card" method="post">
<h1>SSPL ERP Admin <span class="badge">multi-tenant</span></h1>
<label>Username</label><input name="username" autofocus autocomplete="username">
<label>Password</label><input name="password" type="password" autocomplete="current-password">
{% if error %}<div class="err">{{ error }}</div>{% endif %}
<button class="primary" type="submit">Sign in</button>
</form></div></body></html>"""

# ---- the installation suite ------------------------------------------------
# The component installers live on their own page: installing is a one-off job
# done when a server is first built, while the dashboard is the day-to-day
# view. Both pages carry the terminal, because an install has nowhere else to
# report to.

SETUP_CSS = """
.setup-row{padding:10px 0;border-bottom:1px solid var(--hairline)}
.setup-row:last-child{border-bottom:none}
.setup-h{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.setup-form{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:8px}
.setup-form input[type=text],.setup-form input[type=password],.setup-form input:not([type]){
  padding:6px 8px;border:1px solid var(--border);border-radius:6px;background:var(--surface);color:var(--ink)}
/* the rclone row: three stages, each with its own status line */
.stage{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;margin:6px 0 0 14px;font-size:13px}
.stage .n{color:var(--muted);font-variant-numeric:tabular-nums;min-width:14px}
.stage .t{color:var(--ink-2)}
.note{font-size:12.5px;color:var(--muted);margin:4px 0 0 36px}
.guide{margin:10px 0 0 14px;font-size:13px}
.guide summary{cursor:pointer;color:var(--accent)}
.guide ol{margin:8px 0;padding-left:20px;color:var(--ink-2);line-height:1.7}
.guide code{background:var(--track);border-radius:4px;padding:1px 5px;
  font:12px ui-monospace,Menlo,Consolas,monospace}
.guide .why{border-left:2px solid var(--hairline);padding-left:10px;margin:8px 0;color:var(--muted)}
"""

INSTALL_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ERP Next Installation suite (multi-tenant)</title><style>""" + BASE_CSS + LAYOUT_CSS + \
    TERM_CSS + SETUP_CSS + r"""
.lead{font-size:13px;color:var(--ink-2);margin:0 0 12px}
</style></head><body>
<div class="top">
  <h1>ERP Next Installation suite</h1><span class="badge">multi-tenant</span><span id="clock" class="badge"></span>
  <span class="badge" title="panel code version — restart the service after updating">v{{ version }}</span>
  <a class="nav" href="/">← Dashboard</a>
  <span class="spacer"></span>
  <span style="color:var(--muted);font-size:13px">{{ user }}</span>
  <form method="post" action="/logout" style="margin:0"><button>Log out</button></form>
</div>
<main>
<div class="col-left">

<div class="card" id="setup-card"><h2>Install components</h2>
  <p class="lead">Install the multi-tenant ERPNext stack (one stack, many sites) and
  its tooling on this server. Each install runs as a job and reports into the
  terminal below — leave the page open until it finishes.</p>
  <div id="setup-body"><p style="color:var(--muted)">Loading…</p></div>
</div>

</div><!-- /col-left -->
""" + TERM_HTML + r"""
</main>
<script>
""" + TERM_JS + r"""
// The schedule install-tools.sh --cron writes, in words.
function cronWords(sch){
  return {'0 2 * * *': 'daily at 02:00',
          '0 */6 * * *': 'every 6 hours',
          '0 3 * * 0': 'Sundays at 03:00'}[sch] || '';
}

function pill(ok){ return ok ? '<span class="badge ok">installed ✓</span>'
                             : '<span class="badge miss">not installed</span>'; }
function stagePill(ok, okText, missText){
  return ok ? `<span class="badge ok">${esc(okText)} ✓</span>`
            : `<span class="badge miss">${esc(missText)}</span>`; }

// Cloud backup is three separate things, and all three must be true before a
// backup leaves the server. Each fails silently: the upload is a warning in
// backup.sh, so a backup still reports success when nothing was uploaded.
function rcloneRow(s){
  const r = (s.components && s.components.rclone) || {};
  const wiredShow = (r.wired_all && (r.wired_targets||[]).length)
    ? r.wired + ' → ' + r.wired_targets.join(' ')
    : (r.wired || '');
  let h = `<div class="setup-row"><div class="setup-h"><b>Cloud backup (rclone)</b> `
    + (r.ready ? '<span class="badge ok">set up ✓</span>'
               : '<span class="badge miss">not set up</span>')
    + (r.ready ? ` <span class="badge">uploading to ${esc(wiredShow)}</span>` : '') + `</div>`;

  h += `<div class="stage"><span class="n">1.</span>
        <span class="t">rclone installed</span>
        ${stagePill(r.installed, 'installed', 'not installed')}`;
  if(!r.installed && s.scripts.rclone){
    h += `<button class="primary setup-install" data-inst="install_rclone"
           data-confirm="Install rclone on this server? It downloads the official rclone binary.">Install rclone</button>`;
  }
  h += `</div>`;
  if(r.installed && r.version) h += `<div class="note">${esc(r.version)}</div>`;

  h += `<div class="stage"><span class="n">2.</span>
        <span class="t">Cloud account connected</span>
        ${stagePill(r.configured, (r.remotes||[]).join(' ') || 'connected', 'no remote configured')}
        </div>`;
  if(r.installed){
    h += `<div class="setup-form" style="margin-left:14px;flex-wrap:wrap">
      <input type="text" id="rc-name" aria-label="Remote name" size="12" value="gdrive" autocomplete="off" autocapitalize="off" spellcheck="false">
      <input type="password" id="rc-token" aria-label="Google Drive token" size="34" placeholder="paste the token from rclone authorize" autocomplete="off">
      <button class="${r.configured ? '' : 'primary'}" id="rc-connect" data-all="${r.wired_all ? 1 : ''}">${r.configured ? 'Connect / reconnect Google Drive' : 'Connect Google Drive'}</button>
      <span id="rc-cmsg" style="font-size:13px;color:var(--ink-2)"></span></div>
      <div class="note">On your own computer, install rclone and run <code>rclone authorize "drive"</code>, sign in to Google,
      then paste what it prints. The token gives access to that whole Drive: it is tested, then stored only in root's
      rclone config.${location.protocol === 'https:' ? '' : ` <b style="color:var(--crit)">This page is not on HTTPS: the token
      would cross the network unencrypted. Use an SSH tunnel or VPN.</b>`}</div>`;
  }

  h += `<div class="stage"><span class="n">3.</span>
        <span class="t">Every site's backups upload to it</span>
        ${stagePill(r.wired_ok, wiredShow || 'wired', wiredShow || 'backups stay local only')}</div>`;
  if(r.wired && !r.wired_ok){
    const bad = (r.wired_targets||[]).filter(t => !(r.remotes||[]).some(x => t.startsWith(x)));
    h += `<div class="note" style="color:var(--crit)">RCLONE_REMOTE points at
      <code>${esc(bad.length ? bad.join(' ') : r.wired)}</code>, but rclone has
      no such remote${r.installed ? '' : ' (rclone is not installed)'} —
      ${bad.length && bad.length < (r.wired_targets||[]).length
        ? 'that copy is silently not being made' : 'uploads are failing silently'}.
      Pick your remotes below.</div>`;
  }
  if(!r.conf_ok){
    h += `<div class="note">Install the ERPNext stack first — the destination is stored in
      <code>${esc(s.mt_conf)}</code>.</div>`;
  } else if(r.configured){
    const targets = r.wired_targets || [];
    const first = (r.wired || '').split(/\s+/)[0] || '';
    const folder = first ? first.replace(/^[^:]*:/, '') : 'frappe-backups';
    h += `<div class="setup-form" style="margin-left:14px;flex-wrap:wrap">
      <div style="width:100%;display:flex;gap:14px;flex-wrap:wrap;margin-bottom:4px">`
      + (r.remotes||[]).map(x =>
        `<label style="font-size:13px"><input type="checkbox" class="rc-rem" value="${esc(x)}"
          ${!r.wired_all && targets.some(t => t.startsWith(x)) ? 'checked' : ''}> ${esc(x)}</label>`).join('')
      + `</div>
      <label style="width:100%;font-size:13px"><input type="checkbox" id="rc-all"
        ${r.wired_all ? 'checked' : ''}> Every remote rclone knows, including any added later</label>
      <div class="note" style="width:100%;margin-left:0">Each site uploads to
        <code>&lt;remote&gt;&lt;folder&gt;/sites/&lt;site&gt;/</code>. Backups contain every site's
        <code>site_config.json</code> — its database password and encryption key. Every remote
        you tick receives them; "every remote rclone knows" also hands them to remotes you add
        in future, without asking again.</div>
      <input type="text" id="rc-path" size="16" placeholder="folder" value="${esc(folder)}">
      <button class="primary" id="rc-wire">${r.wired ? 'Update destinations' : 'Upload backups here'}</button>
      ${r.wired ? '<button id="rc-off">Stop uploading</button>' : ''}
      <span id="rc-msg" style="font-size:13px;color:var(--ink-2)"></span></div>`;
  }

  h += `<details class="guide"><summary>Setup guide — connecting a Google account</summary>
    <ol>
      <li>On your own computer (it needs a browser), install rclone from
        <a href="https://rclone.org/downloads/" target="_blank" rel="noopener">rclone.org/downloads</a>.
        Use a recent version.</li>
      <li>Run <code>rclone authorize "drive"</code>, sign in to Google and allow access.</li>
      <li>Copy the result, from <code>{</code> to <code>}</code>, and paste it at step 2. Press
        <b>Connect Google Drive</b>: the account is tested before anything is saved.</li>
      <li>The remote appears at step 2, and step 3 points every site's backups at it.
        New sites are included automatically.</li>
      <li>If uploads start failing with an expired or revoked token, do the same again with the same name.</li>
    </ol>
    <div class="why">Other clouds (MEGA, S3, Dropbox), your own Google client ID, or a password-protected rclone
      config: use <code>sudo rclone config</code> over SSH. With <b>sudo</b>, because backups run as root.</div>
    ${s.guide_ok ? '<p><a href="/guide/rclone">Download the full guide (.docx)</a> — also covers '
      + 'AWS S3, Dropbox, testing a remote, and encrypting the rclone config.</p>' : ''}
  </details>`;
  return h + `</div>`;
}

async function connectRclone(){
  const btn = $('#rc-connect'), tok = $('#rc-token').value.trim(), name = $('#rc-name').value.trim();
  const say = m => { const el = $('#rc-cmsg'); if(el) el.textContent = m; };   // the row may be rebuilt meanwhile
  if(!tok){ $('#rc-token').focus(); return; }
  if(btn.dataset.all && !confirm('Backups go to EVERY remote rclone knows, so this account will receive every site\'s backups. Continue?')) return;
  const post = async replace => (await fetch('/api/rclone/connect', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({name, token: tok, replace})}));
  btn.disabled = true; say('Testing the account…');
  try{
    let r = await post(false);
    if(r.status === 401){ location = '/login'; return; }
    let j = await r.json();
    if(j.exists){
      if(!confirm(`Reconnect ${name} with this account? If it is a different Google account, backups go there from now on.`)){ say(''); return; }
      r = await post(true); j = await r.json();
    }
    if(j.error){ say(j.error); return; }
    $('#rc-token').value = '';
    await refreshSetup();
    say('Connected ' + j.remote + ' Now choose where backups upload (step 3).');
  }catch(e){ say('request failed'); }
  finally{ const b = $('#rc-connect'); if(b) b.disabled = false; }
}

function syncRcloneAll(){
  const all = $('#rc-all');
  if(!all) return;
  document.querySelectorAll('.rc-rem').forEach(c => {
    c.disabled = all.checked;
    c.parentElement.style.opacity = all.checked ? 0.45 : 1;
  });
}

async function postWire(body, msg){
  try{
    const r = await fetch('/api/rclone/wire', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
    const j = await r.json();
    if(j.error){ msg.textContent = j.error; return false; }
    msg.textContent = j.wired ? 'Saved — backups now upload to ' + j.wired
                              : 'Saved — backups stay on this server only';
    refreshSetup();
    return true;
  }catch(e){ msg.textContent = 'request failed'; return false; }
}

async function wireRclone(){
  const btn = $('#rc-wire'), msg = $('#rc-msg'), allBox = $('#rc-all');
  if(!btn || !allBox) return;
  const all = allBox.checked;
  const remotes = Array.from(document.querySelectorAll('.rc-rem:checked')).map(c => c.value);
  if(!all && !remotes.length){ msg.textContent = 'Tick at least one remote'; return; }
  if(all && !confirm('Upload every site\'s backups to EVERY remote rclone knows, now and in future?\n\n'
      + 'Backups contain each site\'s site_config.json — its database password and encryption key.')) return;
  btn.disabled = true; msg.textContent = 'Saving…';
  if(!await postWire({remotes, all, path: $('#rc-path').value.trim()}, msg)) btn.disabled = false;
}

async function rcloneOff(){
  if(!confirm('Stop uploading backups to the cloud? Backups already uploaded stay where they are.')) return;
  await postWire({off: true}, $('#rc-msg'));
}

function cronRow(s, toolsOk){
  const cr = s.cron || {}, cj = cr.jobs || [], other = cr.other || [];
  let h = `<div class="setup-row"><div class="setup-h"><b>Scheduled backups (cron)</b> `
    + (cr.scheduled ? `<span class="badge ok">${cj.length} job${cj.length===1?'':'s'} for all sites ✓</span>`
                    : '<span class="badge miss">nothing scheduled</span>')
    + (other.length ? ` <span class="badge miss">${other.length} other backup-like job${other.length===1?'':'s'}</span>` : '')
    + `</div>`;
  for(const j of cj){
    const w = cronWords(j.schedule);
    h += `<div class="note"><code>${esc(j.schedule)}</code>${w?' — '+w:''} — <code>${esc(j.command)}</code></div>`;
  }
  if(other.length){
    h += `<div class="note" style="color:var(--crit)">Outside the multi-tenant block — leftovers from a
      single-site setup would back up a site that is not here and fail nightly. Check them over SSH
      (<code>sudo crontab -e</code>):</div>`;
    for(const j of other) h += `<div class="note"><code>${esc(j.schedule)} ${esc(j.command)}</code></div>`;
  }
  if(!cr.scheduled){
    h += `<div class="setup-form">
      <button class="primary setup-install" data-inst="install_tools" data-cron="1" ${toolsOk?'':'disabled'}
        data-confirm="Schedule backups for every site? Full backup daily at 02:00, DB dump every 6 h, verify Sundays 03:00. Other lines in root's crontab are left alone. (Also refreshes the tools from the checkout.)">Schedule backups</button>`
      + (toolsOk ? '' : '<span style="color:var(--muted);font-size:12px">install the stack first</span>') + `</div>`;
  }
  return h + `<div class="note">This is root's crontab — <code>sudo crontab -l</code> over SSH.</div></div>`;
}

async function refreshSetup(){
  try{
    const r = await fetch('/api/setup-status'); if(r.status===401){location='/login';return;}
    const s = await r.json();
    if(!s.repo_ok){
      $('#setup-body').innerHTML = `<p style="color:var(--warn);margin:0">Multi-tenant installer scripts not found
        (${s.repo_dir?('repo_dir = <code>'+esc(s.repo_dir)+'</code>'):'repo_dir is not set in config.json'}).
        Component installs are unavailable. Check out the <code>Multi-Site</code> branch on this server,
        set <code>repo_dir</code> in <code>/opt/sspl-admin/config.json</code>, then restart the panel.</p>`;
      return;
    }
    const c = s.components, erpDone = c.erp.installed, toolsOk = c.tools.installed;
    let rows = '';
    // ERPNext stack (no site — sites are created from the dashboard)
    rows += `<div class="setup-row"><div class="setup-h"><b>ERPNext stack (multi-tenant)</b> ${pill(erpDone)}`
      + (erpDone && c.erp.running ? ' <span class="badge ok">running</span>' : '')
      + (erpDone ? ` <span class="badge">port ${esc(c.erp.http_port)}</span>
                   <span class="badge">${c.erp.sites.length} site${c.erp.sites.length===1?'':'s'}</span>` : '') + `</div>`;
    if(!erpDone){
      rows += `<div class="setup-form">
        <input type="text" id="erp-image" placeholder="Docker image" value="${esc(s.erp_image||'')}" size="34"
          title="ERPNext Docker image (registry/owner/name, no tag). One image serves every site.">
        <input type="password" id="erp-db" placeholder="MariaDB root password (new)" size="22">
        <label style="font-size:13px"><input type="checkbox" id="erp-cron" checked> schedule backups for all sites</label>
        <button class="primary setup-install" data-inst="install_stack"
          data-confirm="Install the multi-tenant ERPNext stack now? This pulls the image, starts the containers and installs the tools to ${esc(s.tools_dir)} — 5-15 minutes. No site is created: add sites from the dashboard afterwards.">Install stack</button></div>
        <div class="note">Creates the stack only. Then use <b>Create site</b> on the <a href="/">dashboard</a>
          for each tenant — each gets its own hostname, database and Administrator password.
          Ports 80 and 443 must be free, so every domain opens without a port.</div>`;
    } else {
      const sites = c.erp.sites;
      rows += sites.length
        ? `<div class="note">Sites: ${sites.map(x => `<code>${esc(x)}</code>`).join(' ')} — manage them on the <a href="/">dashboard</a>.</div>`
        : `<div class="note">No sites yet — create the first one on the <a href="/">dashboard</a>.</div>`;
      const hp = c.proxy.https_port;
      if(s.scripts.ports && (c.erp.http_port !== 80 || (hp !== null && hp !== 443))){
        rows += `<div class="setup-form">
          <button class="primary setup-install" data-inst="install_ports"
            data-confirm="Move every site to ports 80 and 443? Domains then open without a port. Links with the old port stop working, and both ports must be free.">Remove port from URLs (use 80 / 443)</button></div>
          <div class="note">Domains need a port in the URL because this stack is not on the standard ones
            (HTTP ${esc(String(c.erp.http_port))}${hp !== null ? `, HTTPS ${esc(String(hp))}` : ''}).</div>`;
      }
    }
    rows += `</div>`;
    // Tools
    rows += `<div class="setup-row"><div class="setup-h"><b>Multi-tenant tools</b> ${pill(toolsOk)}`
      + (c.tools.conf ? ' <span class="badge">config ✓</span>' : '') + `</div>
      <div class="note">Backup, restore, site and update scripts in <code>${esc(s.tools_dir)}</code>;
        settings in <code>${esc(s.mt_conf)}</code> (never overwritten by a refresh).</div>`;
    if(erpDone){
      rows += `<div class="setup-form">
        <button class="setup-install" data-inst="install_tools" data-cron="0"
          data-confirm="Copy the checkout's multi-tenant tools to ${esc(s.tools_dir)}? Settings and cron are not changed.">${toolsOk ? 'Refresh tools from checkout' : 'Install tools'}</button></div>`;
    }
    rows += `</div>`;
    // Host nginx: routes each domain to its site
    const px = c.proxy;
    const pxOk = px.installed && px.running && px.configured && px.frontend_local
      && (px.https_port === null || px.https_live);
    rows += `<div class="setup-row"><div class="setup-h"><b>nginx domain routing</b> ${pill(pxOk)}`
      + (px.installed ? (px.running ? ' <span class="badge ok">running</span>'
                                    : ' <span class="badge miss">stopped</span>') : '')
      + (px.configured ? ` <span class="badge">${px.domains} domain(s)</span>` : '')
      + (px.https_live ? ` <span class="badge ok">HTTPS :${px.https_port}</span>`
         : (px.installed ? ` <span class="badge miss" title="${esc(px.https_reason)}">HTTPS off</span>` : ''))
      + (px.certs ? ` <span class="badge">${px.certs} Let's Encrypt</span>` : '') + `</div>`;
    if(erpDone && s.scripts.proxy && !pxOk){
      rows += `<div class="setup-form">
        <button class="primary setup-install" data-inst="install_proxy"
          data-confirm="Install nginx on port ${esc(String(c.erp.http_port||80))} and route every site's domain through it? The stack's own nginx moves to 127.0.0.1 — every site is down for a few seconds.">${px.installed ? 'Repair nginx routing' : 'Install nginx routing'}</button></div>`;
    }
    rows += `<div class="note">nginx on the HTTP port maps each domain to its site, so one site can answer on
      several domains. Manage them per site with <b>Domains…</b> on the <a href="/">dashboard</a>. Each domain
      must still resolve to this server: a DNS A record (public DNS or the router) or a hosts-file line per PC.
      HTTPS works on every domain with a self-signed certificate (browsers warn); a public domain gets a trusted
      Let's Encrypt one with <b>Get certificate</b> in its Domains… dialog.</div>`;
    if(erpDone && px.installed && px.frontend_local && !px.https_live){
      rows += `<div class="note" style="color:var(--warn)">${esc(px.https_reason)}</div>`;
    }
    if(erpDone && px.installed && !px.frontend_local){
      rows += `<div class="note" style="color:var(--warn)">This stack was installed before nginx routing: its frontend
        still holds the public port. <b>Repair nginx routing</b> moves it.</div>`;
    }
    rows += `</div>`;
    // Authelia login gate + Redis + mail relay (Gmail)
    const au = c.auth || {};
    rows += `<div class="setup-row"><div class="setup-h"><b>Authelia login (2FA) + mail relay</b> ${pill(au.installed)}`
      + (au.installed ? (au.down||[]).map(x => ` <span class="badge miss">${esc(x)}: not answering</span>`).join('')
          + ` <span class="badge${(au.linked||[]).length && !(au.down||[]).length ? ' ok' : ''}">${(au.linked||[]).length} site(s) linked</span>`
          + (au.legacy ? ' <span class="badge miss">one login for all sites — Save settings gives each site its own</span>' : '') : '')
      + (au.mail ? ` <span class="badge ok" title="sends through ${esc(au.relay)}">mail: ${esc(au.mail)}</span>`
                 : (au.installed ? ' <span class="badge miss">no mail account</span>' : '')) + `</div>`;
    if(erpDone && s.scripts.auth){
      rows += `<div class="setup-form">
        <input type="email" id="au-smtp-user" placeholder="Gmail address" value="${esc(au.mail||'')}" size="26" autocomplete="off">
        <input type="password" id="au-smtp-pw" placeholder="${au.mail ? 'app password (unchanged)' : 'Gmail app password'}" size="20" autocomplete="new-password">
        <input type="email" id="au-from" placeholder="sender (default: the Gmail address)" value="${esc(au.mail_from && au.mail_from !== au.mail ? au.mail_from : '')}" size="28" autocomplete="off">
        <input type="text" id="au-trusted" placeholder="no-2FA networks, e.g. 192.168.1.0/24" value="${esc(au.trusted||'')}" size="30"
          title="From these networks the password is enough (no 2FA). Space-separated. Empty: 2FA everywhere.">`
        + `
        <button class="primary setup-install" data-inst="install_auth"
          data-confirm="${au.installed ? ('Apply these Authelia / mail settings? Authelia may restart for a few seconds.' + (au.legacy ? ' Every site gets its own users and 2FA: passwords carry over, everyone logs in and registers 2FA again.' : ' Nobody is logged out.')) : 'Install Authelia, Redis and the mail relay now? Nothing is gated until you link a site in its Domains… dialog.'}">${au.installed ? 'Save settings' : 'Install Authelia'}</button></div>`;
      if(au.installed && au.mail){
        rows += `<div class="setup-form"><input type="email" id="au-test-to" placeholder="send a test mail to…" size="26">
          <button id="au-test" onclick="authMailTest()">Send test mail</button></div>`;
      }
    }
    rows += `<div class="note">A linked site shows an Authelia login with 2FA (authenticator app or security key)
      before ERPNext. Every site has <b>its own users, passwords and 2FA</b>. <b>Remember me</b> keeps that browser
      logged in for ${esc(au.remember||'1y')} (survives restarts). Add users on the <a href="/auth">Authelia page</a>, linked from the
      <a href="/">dashboard</a>, then link the site in its <b>Domains…</b> dialog. Each linked site uses about
      30–90 MB of RAM. Mail goes through Gmail: turn on 2-Step Verification for the Google account, then
      create an <b>app password</b> (Google Account → Security → App passwords); Gmail sends as that address or a
      verified alias only. Needs HTTPS on the nginx routing above.</div>`;
    if(au.installed && !c.proxy.https_live){
      rows += `<div class="note" style="color:var(--warn)">HTTPS is off on the nginx routing — linked sites refuse every request until it works.</div>`;
    }
    rows += `</div>`;
    // WireGuard to the CUPS print servers
    const wg = c.wireguard || {peers: []};
    rows += `<div class="setup-row"><div class="setup-h"><b>WireGuard (print servers)</b> ${pill(wg.installed)}`
      + (wg.installed ? (wg.running ? ' <span class="badge ok">running</span>'
                                    : ' <span class="badge miss">down</span>') : '') + `</div>`;
    rows += `<div class="note">Lets the ERP containers print to CUPS servers behind shop routers. Install it, change its
      settings and manage print servers on the <a href="/wireguard">WireGuard page</a>.</div></div>`;
    // OpenWA WhatsApp API (Docker)
    const ow = c.openwa, owUrl = 'http://' + (s.server_ip || location.hostname) + ':' + ow.port;
    rows += `<div class="setup-row"><div class="setup-h"><b>OpenWA WhatsApp API</b> ${pill(ow.installed)}`
      + (ow.installed ? (ow.running ? ' <span class="badge ok">running</span>'
                                     : ' <span class="badge miss">stopped</span>') : '') + `</div>`;
    if(!ow.installed){
      rows += s.scripts.openwa ? `<div class="setup-form">
        <button class="primary setup-install" data-inst="install_openwa"
          data-confirm="Install OpenWA now? This clones OpenWA, pulls its Docker image and starts it on port ${ow.port}. A few minutes.">Install OpenWA</button></div>` : '';
    } else {
      rows += `<div class="setup-form">
        <span style="font-size:13px">Dashboard &amp; API: <a href="${esc(owUrl)}" target="_blank">${esc(owUrl)}</a>
          · <a href="${esc(owUrl)}/api/docs" target="_blank">Swagger</a></span>
        <button id="ow-show" onclick="showOpenwaKey()" ${ow.running?'':'disabled'}>Show API key</button>
        <input type="text" id="ow-key" readonly size="40" style="display:none;font-family:monospace">
        <button id="ow-copy" onclick="copyOpenwaKey()" style="display:none">Copy</button>
        <span id="ow-msg" style="color:var(--muted);font-size:12px"></span></div>
        <div class="note">Use the key to sign in to the dashboard and as the <code>X-API-Key</code> header on API calls.</div>`;
    }
    rows += `</div>`;
    rows += rcloneRow(s);
    rows += cronRow(s, toolsOk);
    // Panel + tools update straight from git
    const upd = s.repo_panel_version && s.repo_panel_version !== s.panel_version;
    rows += `<div class="setup-row"><div class="setup-h"><b>Panel &amp; tools updates</b> `
      + (upd ? `<span class="badge miss">checkout has v${esc(s.repo_panel_version)}</span>`
             : '<span class="badge ok">panel matches the checkout ✓</span>') + `</div>
      <div class="note">Panel running v${esc(s.panel_version||'')}. <b>Update panel &amp; tools</b> on the
        dashboard pulls <code>${esc(s.repo_dir||'')}</code> from git, refreshes the tools and redeploys
        this panel. Settings, cron and site data are not touched.</div></div>`;
    // The refresh rebuilds the forms: keep what was typed in them.
    const keep = {};
    document.querySelectorAll('#setup-body input[id]').forEach(i => {
      keep[i.id] = i.type === 'checkbox' ? i.checked : i.value; });
    const focused = document.activeElement && document.activeElement.id;
    $('#setup-body').innerHTML =
      (erpDone && toolsOk ? '<p style="color:var(--ok);margin:0 0 6px">✓ Stack and tools are installed. ' +
                 'Create and run sites from the <a href="/">dashboard</a>.</p>' : '') + rows;
    for (const [id, v] of Object.entries(keep)){
      const i = document.getElementById(id);
      if (!i || i.id === 'ow-key') continue;
      if (i.type === 'checkbox') i.checked = v; else i.value = v;
    }
    if (focused && document.getElementById(focused)) document.getElementById(focused).focus();
    document.querySelectorAll('.setup-install').forEach(btn => btn.onclick = () => installComponent(btn));
    if ($('#rc-connect')) $('#rc-connect').onclick = connectRclone;
    if ($('#rc-wire')) $('#rc-wire').onclick = wireRclone;
    if ($('#rc-off')) $('#rc-off').onclick = rcloneOff;
    if ($('#rc-all')) { $('#rc-all').onchange = syncRcloneAll; syncRcloneAll(); }
    if (jobWasActive) document.querySelectorAll('.setup-install').forEach(b => b.disabled = true);
  }catch(e){}
}

// The key is fetched on demand only, and the setup refresh every 30s
// rebuilds this row — which hides it again, so it is not left on screen.
async function showOpenwaKey(){
  const msg = $('#ow-msg'); msg.textContent = 'reading…';
  try{
    const r = await fetch('/api/openwa/key', {method:'POST'});
    if(r.status===401){location='/login';return;}
    const j = await r.json();
    if(j.error){ msg.textContent = j.error; return; }
    const k = $('#ow-key'); k.value = j.key; k.style.display = ''; $('#ow-copy').style.display = '';
    k.select(); msg.textContent = '';
  }catch(e){ msg.textContent = 'request failed'; }
}
async function copyOpenwaKey(){
  const k = $('#ow-key');
  try{ await navigator.clipboard.writeText(k.value); $('#ow-msg').textContent = 'copied'; }
  catch(e){ k.select(); document.execCommand('copy'); $('#ow-msg').textContent = 'copied'; }
}

async function authMailTest(){
  const to = $('#au-test-to').value.trim();
  if(!to) return;
  try{
    const r = await fetch('/api/run/auth_mail_test', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({to})});
    const j = await r.json();
    if(j.error) alert(j.error); else { jobWasActive = true; refreshJob(); revealConsole(); }
  }catch(e){ alert('request failed'); }
}

async function installComponent(btn){
  if(btn.disabled) return;
  if(btn.dataset.confirm && !confirm(btn.dataset.confirm)) return;
  const inst = btn.dataset.inst, body = {};
  if(inst==='install_stack'){
    body.image = $('#erp-image').value.trim();
    body.db_password = $('#erp-db').value;
    body.schedule_cron = $('#erp-cron').checked;
  } else if(inst==='install_tools'){
    body.schedule_cron = btn.dataset.cron === '1';
  } else if(inst==='install_auth'){
    body.smtp_user = $('#au-smtp-user').value.trim();
    body.smtp_password = $('#au-smtp-pw').value;
    body.mail_from = $('#au-from').value.trim();
    body.trusted = $('#au-trusted').value.trim();
  }
  btn.disabled = true;
  try{
    const r = await fetch('/api/run/'+inst, {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
    const j = await r.json();
    if(j.error){ alert(j.error); btn.disabled = false; }
    else { jobWasActive = true; refreshJob(); revealConsole(); }
  }catch(e){ alert('request failed'); btn.disabled = false; }
}

// An install that has finished has flipped a pill — this page's whole job.
onJobFinished = () => refreshSetup();

setInterval(() => $('#clock').textContent = new Date().toLocaleString(), 1000);
refreshSetup();
setInterval(refreshSetup, 30000);
pollJob();
</script></body></html>"""


# The CSS/JS half of this template is a RAW string (r"""). The JS needs to
# reach the browser with its own backslash escapes intact: in a normal string
# Python eats them, turning \x1b into a real ESC byte and split('\r') into a
# literal carriage return — which is a syntax error inside a JS string literal
# and kills the whole <script>. Keep the r prefix when editing.
DASH_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SSPL ERP Admin</title><style>""" + BASE_CSS + LAYOUT_CSS + TERM_CSS + r"""
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:16px}
.tile{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:12px 14px}
.tile .k{font-size:12px;color:var(--muted)} .tile .v{font-size:22px;margin-top:2px}
.tile .s{font-size:12px;color:var(--ink-2)}
.meters{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px 24px}
.meter .row{display:flex;justify-content:space-between;font-size:13px;margin-bottom:4px}
.meter .row b{font-weight:600}
.meter .flag{font-size:12px;font-weight:600}
.meter .flag.warn{color:var(--warn)} .meter .flag.crit{color:var(--crit)}
.bar{height:10px;border-radius:5px;background:var(--track);overflow:hidden}
.bar i{display:block;height:100%;border-radius:5px;background:var(--accent);width:0;
  transition:width .4s}
.bar i.warn{background:var(--warn)} .bar i.crit{background:var(--crit)}
.meter .sub{font-size:12px;color:var(--muted);margin-top:3px}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;vertical-align:baseline}
.dot.up{background:var(--ok)} .dot.down{background:var(--crit)}
.actions{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
.actions .sep{flex-basis:100%;height:0}
details{margin:2px 0} details summary{cursor:pointer}
.filelist{margin:6px 0 4px 18px;font-size:13px;color:var(--ink-2)}
.uprow{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
#upmsg{font-size:13px;color:var(--ink-2)}
.tabs{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap}
.tabs button.active{background:var(--accent);color:var(--accent-ink);border-color:var(--accent)}
.tabpane{display:none} .tabpane.active{display:block}
/* Backup tables show ~3 rows; the rest scroll inside the pane (capRows sets
   the exact height, since row height differs per tab). */
.scrollrows{overflow-y:auto}
.scrollrows thead th{position:sticky;top:0;background:var(--surface);z-index:1}
.right{text-align:right}
/* restore confirmation modal */
.modal{position:fixed;inset:0;background:rgba(0,0,0,.55);display:none;
  align-items:center;justify-content:center;padding:16px;z-index:10}
.modal.on{display:flex}
.modal .card{max-width:520px;width:100%;margin:0;max-height:90vh;overflow:auto}
.modal label{display:block;margin:10px 0 4px;color:var(--ink-2);font-size:13px}
.modal input{width:100%}
.danger-box{border:1px solid var(--crit);border-radius:8px;padding:10px 12px;
  font-size:13px;color:var(--ink-2);margin-bottom:4px}
.danger-box b{color:var(--crit)}
.modal .row{display:flex;gap:10px;justify-content:flex-end;margin-top:16px}
#rs-err{color:var(--crit);font-size:13px;margin-top:10px;min-height:0}
.sites td{vertical-align:top}
.sites .acts{display:flex;flex-wrap:wrap;gap:6px;justify-content:flex-end}
.sites .acts button{padding:5px 10px;font-size:13px}
.sites .apps{font-size:12.5px;color:var(--ink-2);margin-top:4px}
.newsite{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:10px}
.newsite .applist{width:100%;display:flex;flex-wrap:wrap;gap:12px;font-size:13px;color:var(--ink-2)}
.lead{font-size:13px;color:var(--ink-2);margin:0 0 10px}
.modal .chk{display:inline-flex;align-items:center;gap:5px;margin:4px 14px 4px 0}
.modal .chk input{width:auto}
#dm-auth{margin-top:12px;padding:10px 12px;border:1px solid var(--hairline);border-radius:8px;font-size:13px}
</style></head><body>
<div class="top">
  <h1>SSPL ERP Admin</h1><span class="badge">multi-tenant</span><span id="clock" class="badge"></span>
  <span class="badge" title="panel code version — restart the service after updating">v{{ version }}</span>
  <details class="tools" id="tools">
    <summary>Tools</summary>
    <div class="menu">
      <a href="/install">ERP Next Installation suite</a>
      <a id="au-open" href="/auth" style="display:none" title="Authelia users, whitelisted IPs and 2FA logins of each site">Authelia</a>
      <a href="/wireguard" title="Tunnel to the CUPS print servers">WireGuard</a>
      <a href="/ram" title="RAM, swap, containers and the database buffer pool">RAM monitor</a>
      {% if repo_ok %}
      <div class="sep"></div>
      <button class="act" data-act="update_panel"
        title="git pull the checkout, refresh the multi-tenant tools, then deploy this panel from it"
        data-confirm="Pull the latest code, refresh the multi-tenant tools and update this panel now?&#10;&#10;Settings, cron and site data are not touched. The panel restarts itself a few seconds after the job finishes — reload the page then.">Update panel &amp; tools</button>
      {% endif %}
    </div>
  </details>
  <span class="spacer"></span>
  <span style="color:var(--muted);font-size:13px">{{ user }}</span>
  <form method="post" action="/logout" style="margin:0"><button>Log out</button></form>
</div>
<main>
<div class="col-left">

<div class="tiles">
  <div class="tile"><div class="k">CPU usage</div><div class="v num" id="t-cpu">–</div>
    <div class="s" id="t-cores"></div></div>
  <div class="tile"><div class="k">Load average (1/5/15 min)</div><div class="v num" id="t-load">–</div>
    <div class="s">of <span id="t-cores2"></span> cores</div></div>
  <div class="tile"><div class="k">Memory available</div><div class="v num" id="t-avail">–</div>
    <div class="s" id="t-memtot"></div></div>
  <div class="tile"><div class="k">Sites</div><div class="v num" id="t-sites">–</div>
    <div class="s">on one stack</div></div>
  <div class="tile"><div class="k">Uptime</div><div class="v num" id="t-up">–</div>
    <div class="s">since last reboot</div></div>
</div>

<div class="card"><h2>Sites</h2>
  <p class="lead">One stack serves every site; nginx picks the site from the hostname in the
  browser's address bar. Per-site jobs touch only that site — the others stay up.</p>
  <div id="sites-body"><p style="color:var(--muted)">Loading…</p></div>
  <details id="newsite-box" style="margin-top:12px">
    <summary><b>Create site</b></summary>
    <div class="newsite">
      <input type="text" id="ns-host" placeholder="hostname, e.g. acme.erp.lan" size="26"
        autocomplete="off" autocapitalize="off" spellcheck="false">
      <input type="password" id="ns-pw" placeholder="Administrator password" size="20" autocomplete="new-password">
      <input type="password" id="ns-pw2" placeholder="confirm password" size="16" autocomplete="new-password">
      <div class="applist" id="ns-apps"></div>
      <button class="primary act" data-act="new_site">Create site</button>
      <span id="ns-msg" style="font-size:13px;color:var(--ink-2)"></span>
    </div>
    <p id="ns-cloud" style="font-size:12.5px;color:var(--ink-2);margin:8px 0 0"></p>
    <p style="font-size:12.5px;color:var(--muted)">Takes 5–10 minutes. Apps left unticked are recorded,
    so later updates never install them on this site. Adding a hostname briefly restarts the
    backend and workers (a few seconds, all sites). Afterwards make the hostname resolve to this
    server: a DNS record (public DNS or the router) or a hosts-file line on each PC. More domains
    for a site: <b>Domains…</b> in its row.</p>
  </details>
</div>

<div class="card"><h2>All sites</h2>
  <div class="actions">
    <button class="primary act" data-act="backup" data-all="1">Full backup — all sites</button>
    <button class="act" data-act="db_backup" data-all="1">DB-only backup — all</button>
    <button class="act" data-act="verify" data-all="1">Verify backups — all</button>
    <button class="act" data-act="migrate" data-all="1"
      data-confirm="Run database migrations on every site now? Use this to retry after an update failed at the migration step.">Migrate all</button>
    <span class="sep"></span>
    <button class="primary act" data-act="update"
      data-confirm="Update ERPNext for EVERY site now?&#10;&#10;1. full backup of every site (the update stops if one fails)&#10;2. image snapshot for rollback&#10;3. new images downloaded with every site up&#10;4. all sites offline for a few minutes while each one is migrated&#10;&#10;A site whose migration fails is left in maintenance mode; the others come back.">Update all sites</button>
    <button class="act" data-act="download_image"
      title="Download the latest Docker images now, with every site up; Update all sites will use them.">Download images</button>
    <select id="rb-snap" title="Image snapshot to roll back to"></select>
    <button class="danger act" data-act="rollback"
      data-confirm="Roll back the Docker images for EVERY site to the selected snapshot? All sites restart.&#10;&#10;Images only: a site the update already migrated then runs old code on a new schema — restore it from the backup the update took.">Rollback</button>
    <span class="sep"></span>
    <button id="clear-ram"
      data-confirm="Clear RAM caches now? This is safe but may briefly slow the system while caches rebuild.">Clear RAM caches</button>
    <span id="ram-msg" style="font-size:13px;color:var(--ink-2)"></span>
    <button id="cron-show" title="Root's crontab — what 'sudo crontab -l' shows over SSH">Show cron jobs</button>
  </div>
  <div id="cron-box" style="display:none;margin-top:12px">
    <table><thead><tr><th>Schedule</th><th>Command</th></tr></thead>
    <tbody id="cron-rows"></tbody></table>
    <div style="font-size:12.5px;color:var(--muted);margin-top:6px">Root's crontab —
      the same list <code>sudo crontab -l</code> shows over SSH.</div>
  </div>
</div>

<div class="card"><h2>ERP containers</h2>
  <div id="cont-err" style="color:var(--crit);font-size:13px;display:none"></div>
  <table><thead><tr><th>Service</th><th>State</th><th>Status</th></tr></thead>
  <tbody id="containers"><tr><td colspan="3" style="color:var(--muted)">Loading…</td></tr></tbody></table>
</div>

<div class="card"><h2>Memory &amp; disk</h2><div class="meters" id="meters"></div></div>

<div class="card"><h2>Backups on the server</h2>
  <div style="display:flex;gap:10px;align-items:center;margin-bottom:12px;flex-wrap:wrap">
    <label style="font-size:13px;color:var(--ink-2)">Site
      <select id="bk-site"></select></label>
    <span id="bk-note" style="font-size:12.5px;color:var(--muted)"></span>
  </div>
  <div class="tabs">
    <button class="active" data-tab="full">Full backups</button>
    <button data-tab="db">DB-only</button>
    <button data-tab="img">Image snapshots</button>
    <button data-tab="upl">Uploads</button>
  </div>
  <div class="tabpane active" id="tab-full"></div>
  <div class="tabpane" id="tab-db"></div>
  <div class="tabpane" id="tab-img"></div>
  <div class="tabpane" id="tab-upl"></div>
  <div style="display:flex;gap:10px;align-items:center;margin-top:12px">
    <button id="clear-old" class="danger"
      data-confirm="Delete full and DB-only backups older than 30 days, for every site? The newest 20 of each, per site, are always kept, however old. Uploads and image snapshots are not touched.">Clear backups older than 30 days</button>
    <span id="clear-old-msg" style="font-size:13px;color:var(--ink-2)"></span>
  </div>
</div>

<div class="card"><h2>Upload backup files to the server</h2>
  <p style="font-size:13px;color:var(--ink-2);margin-top:0">Files are stored under
  <code>uploads/</code> inside the backup directory. Put one backup per folder; the folder then
  gets a Restore button that restores it into a site you choose. Allowed:
  .sql.gz, .tar, .tgz, .json, .yml.</p>
  <div class="uprow">
    <label>Choose files <input type="file" id="upfiles" multiple></label>
    <label>Choose backup folder <input type="file" id="updirectory" webkitdirectory multiple></label>
    <input type="text" id="upfolder" aria-label="Destination folder" placeholder="Folder (optional)" size="14">
    <button class="primary" id="upbtn">Upload</button>
    <span id="upmsg"></span>
  </div>
  <p style="font-size:13px;color:var(--ink-2)">Selecting a folder fills in its destination name.
  Supported files directly inside it are uploaded together; subfolders and other file types are skipped.</p>
  <p id="upselection" style="font-size:13px" aria-live="polite"></p>
</div>

<div class="card"><h2>License files</h2>
  <p style="font-size:13px;color:var(--ink-2);margin-top:0">An uploaded license must contain a
  <code>"site"</code> field; it goes into that site's folder in the Docker <code>sites</code> volume.
  Allowed: .lic, .txt, .json, .key.</p>
  <div class="uprow">
    <input type="file" id="licfile">
    <button class="primary" id="licupbtn">Upload License</button>
    <select id="licsite" title="Site to download a license from"></select>
    <button id="licdlbtn">Download License</button>
    <select id="licdlpick" style="display:none"></select>
    <span id="licupmsg"></span>
  </div>
</div>

</div><!-- /col-left -->
""" + TERM_HTML + r"""
</main>

<div class="modal" id="rs-modal"><div class="card">
  <h1 style="font-size:17px;margin-bottom:10px">Restore a site</h1>
  <div class="danger-box">
    <b>⚠ This overwrites one live site.</b> Everything in <b id="rs-site">the site</b> — every record
    entered since the backup was taken — is replaced by <b id="rs-src">the backup</b>.
    <br><br>A <b>full safety backup of that site is taken first</b>. Only that site goes into
    maintenance mode; the other sites stay up, though their background jobs pause for the
    few minutes it takes.
  </div>
  <label>Restore into site</label>
  <select id="rs-target" style="width:100%"></select>
  <label style="display:flex;gap:8px;align-items:flex-start;font-size:13px">
    <input type="checkbox" id="rs-other" style="width:auto;margin-top:3px">
    <span>The backup is from another site (moving a tenant). Its encryption key is copied into
    the target too, so encrypted fields stay readable.</span></label>
  <label>Type the site name <b id="rs-site2"></b> to confirm</label>
  <input id="rs-confirm" autocomplete="off" placeholder="site name">
  <label>Your admin panel password</label>
  <input id="rs-pw" type="password" autocomplete="current-password">
  <div id="rs-err"></div>
  <div class="row">
    <button id="rs-cancel">Cancel</button>
    <button class="danger" id="rs-go">Back up, then restore</button>
  </div>
</div></div>

<div class="modal" id="dr-modal"><div class="card">
  <h1 style="font-size:17px;margin-bottom:10px">Drop site</h1>
  <div class="danger-box">
    <b>⚠ This deletes the database of <span id="dr-site"></span>.</b> Its users lose access.
    A full backup is taken first and must succeed, or nothing is dropped. Its backups stay on
    disk (and in the cloud) afterwards. Remove its DNS entry too.
  </div>
  <label>Type the site name <b id="dr-site2"></b> to confirm</label>
  <input id="dr-confirm" autocomplete="off" placeholder="site name">
  <label>Your admin panel password</label>
  <input id="dr-pw" type="password" autocomplete="current-password">
  <div id="dr-err" style="color:var(--crit);font-size:13px;margin-top:10px"></div>
  <div class="row">
    <button id="dr-cancel">Cancel</button>
    <button class="danger" id="dr-go">Back up, then drop</button>
  </div>
</div></div>

<div class="modal" id="dm-modal"><div class="card">
  <h1 style="font-size:17px;margin-bottom:10px">Domains of <span id="dm-site"></span></h1>
  <div id="dm-body" style="font-size:13px"></div>
  <div class="newsite" style="margin-top:10px">
    <input type="text" id="dm-new" placeholder="domain, e.g. erp.acme.com" size="28"
      autocomplete="off" autocapitalize="off" spellcheck="false">
    <label style="font-size:13px"><input type="checkbox" id="dm-primary"> primary</label>
    <button class="primary" id="dm-add">Add domain</button>
  </div>
  <div class="newsite" style="margin-top:8px">
    <input type="number" id="dm-port" placeholder="port, e.g. 8001" min="1" max="65535" style="width:140px">
    <label style="font-size:13px"><input type="checkbox" id="dm-port-primary"> primary</label>
    <button class="primary" id="dm-port-add">Add IP + port</button>
  </div>
  <p style="font-size:12.5px;color:var(--muted)"><b>IP + port</b>: everything on
    <code>http://&lt;server-ip&gt;:&lt;port&gt;</code> goes to this site, no DNS needed (HTTP; HTTPS once the
    site is behind the Authelia login). Browsers share
    login cookies across ports of one IP, so one browser stays logged in to only one IP + port site at a time.</p>
  <p style="font-size:12.5px;color:var(--muted)">nginx sends every request for these domains to this site.
    The <b>primary</b> domain becomes the site's <code>host_name</code>: the URL in emails and the one PDFs
    load images from. Adding or removing a domain briefly restarts the backend and workers (a few
    seconds, all sites). Point each domain at this server too: a DNS A record, or a hosts-file line.</p>
  <div id="dm-auth" style="display:none"></div>
  <div class="row"><button id="dm-close">Close</button></div>
</div></div>

<div class="modal" id="ap-modal"><div class="card">
  <h1 style="font-size:17px;margin-bottom:10px">Apps on <span id="ap-site"></span></h1>
  <div id="ap-body" style="font-size:13px">Reading installed apps…</div>
  <div class="row">
    <button id="ap-close">Close</button>
  </div>
</div></div>
<script>
""" + TERM_JS + r"""
let SITES = [], IMAGE_APPS = [], SITE_INFO = {}, HTTP_PORT = 80, PROXY_HTTPS = false, HTTPS_REASON = '',
    AUTH = {installed: false, users: {}};

async function runAction(act, body){
  try{
    const r = await fetch('/api/run/' + act, {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify(body || {})});
    const j = await r.json();
    if (j.error){ alert(j.error); return false; }
    jobWasActive = true; refreshJob(); revealConsole();
    return true;
  }catch(e){ alert('request failed'); return false; }
}

// ---- sites -----------------------------------------------------------------
function siteRow(s){
  const state = s.maintenance
    ? '<span class="badge miss" title="users see the maintenance page">maintenance</span>'
    : '<span class="badge ok">online</span>';
  const bk = s.latest_backup
    ? `<span class="num">${esc(s.latest_backup.replace(/^(\d{4})(\d\d)(\d\d)_(\d\d)(\d\d).*/, '$1-$2-$3 $4:$5'))}</span>
       <span style="color:var(--muted);font-size:12px">(${s.backups})</span>`
    : '<span class="badge miss">none</span>';
  const skip = s.skip_apps.length
    ? `<div class="apps">never auto-installed: ${s.skip_apps.map(esc).join(', ')}</div>` : '';
  const n = esc(s.name);
  const extra = (s.domain_info || []).slice(1);
  const doms = extra.length
    ? `<div class="apps">also: ${extra.map(d => `<a href="${esc(d.url)}" target="_blank">${esc(d.name)}</a>${d.cert ? ' 🔒' : ''}`).join(', ')}</div>` : '';
  const lock = s.auth ? ' <span class="badge ok" title="Authelia login (2FA) before ERPNext">🔐 login</span>' : '';
  return `<tr><td><a href="${esc(s.url)}" target="_blank"><b>${n}</b></a> ${state}${lock}${doms}${skip}</td>
    <td>${bk}</td>
    <td><div class="acts">
      <button class="act" data-act="backup" data-site="${n}">Backup</button>
      <button class="act" data-act="db_backup" data-site="${n}">DB backup</button>
      <button class="act" data-act="migrate" data-site="${n}"
        data-confirm="Run database migrations on ${n}?">Migrate</button>
      <button class="site-apps" data-site="${n}">Apps…</button>
      <button class="site-doms" data-site="${n}">Domains…</button>
      <button class="site-bk" data-site="${n}" title="Show this site's backups below">Backups</button>
      <button class="danger act-drop" data-site="${n}">Drop</button>
    </div></td></tr>`;
}

async function refreshSites(){
  try{
    const r = await fetch('/api/sites'); if(r.status===401){location='/login';return;}
    const j = await r.json();
    SITES = j.sites.map(s => s.name);
    SITE_INFO = Object.fromEntries(j.sites.map(s => [s.name, s]));
    HTTP_PORT = j.http_port || 80;
    PROXY_HTTPS = !!j.https; HTTPS_REASON = j.https_reason || '';
    AUTH = j.auth || {installed: false, users: {}};
    $('#au-open').style.display = AUTH.installed ? '' : 'none';
    IMAGE_APPS = j.image_apps || [];
    $('#t-sites').textContent = SITES.length;
    let h;
    if (!j.stack){
      h = `<p style="color:var(--warn);margin:0">No ERPNext stack on this server yet — install it from the
        <a href="/install">installation suite</a>.</p>`;
    } else if (!j.sites.length){
      h = '<p style="color:var(--muted);margin:0">No sites yet — create the first one below.</p>';
    } else {
      h = `<table class="sites"><thead><tr><th>Site</th><th>Last full backup</th><th></th></tr></thead><tbody>`
        + j.sites.map(siteRow).join('') + '</tbody></table>';
    }
    $('#sites-body').innerHTML = h;
    // The app picker for Create site: keep what the user has ticked.
    const box = $('#ns-apps'), ticked = new Set([...box.querySelectorAll('input:checked')].map(c => c.value));
    const fresh = !box.dataset.filled;
    box.innerHTML = IMAGE_APPS.length
      ? '<span>Apps:</span>' + IMAGE_APPS.map(a => `<label><input type="checkbox" class="ns-app" value="${esc(a)}"
          ${fresh || ticked.has(a) ? 'checked' : ''}> ${esc(a)}</label>`).join('')
      : '<span style="color:var(--muted)">Apps: every app in the image (list not readable yet)</span>';
    if (IMAGE_APPS.length) box.dataset.filled = '1';
    if (IMAGE_APPS.length && !j.apps_live)
      box.insertAdjacentHTML('beforeend', '<span style="color:var(--warn)">(backend not answering — list read from apps.txt, may be out of date)</span>');
    // Cloud backup is one setting for every site: say where a new site's backups go.
    $('#ns-cloud').innerHTML = j.cloud.length
      ? 'Cloud backup: the new site\'s backups also upload to '
        + j.cloud.map(x => `<code>${esc(x)}/sites/&lt;site&gt;/</code>`).join(' and ')
        + ' (the same destination as every site — change it in the <a href="/install">installation suite</a>).'
      : '<span style="color:var(--warn)">Cloud backup is not set up — backups stay on this server.</span> '
        + 'Set up rclone (e.g. Google Drive) in the <a href="/install">installation suite</a>; it then covers every site, including new ones.';
    $('#newsite-box').style.display = j.stack && j.tools ? '' : 'none';
    fillSiteSelects();
    if (jobWasActive) document.querySelectorAll('.act').forEach(b => b.disabled = true);
  }catch(e){}
}

function fillSelect(sel, values, placeholder){
  const cur = sel.value;
  sel.innerHTML = (placeholder ? `<option value="">${esc(placeholder)}</option>` : '')
    + values.map(v => `<option value="${esc(v)}">${esc(v)}</option>`).join('');
  if ([...sel.options].some(o => o.value === cur)) sel.value = cur;
}
function fillSiteSelects(){
  fillSelect($('#licsite'), SITES, SITES.length ? '' : 'no sites');
}

// Tools dropdown: close on a pick, a click elsewhere or Escape. Capture phase,
// so it is already closed when an item's confirm() blocks the page.
document.addEventListener('click', e => {
  if (!e.target.closest('#tools') || e.target.closest('#tools .menu a, #tools .menu button')) $('#tools').open = false;
}, true);
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && $('#tools').open) { $('#tools').open = false; $('#tools summary').focus(); }
});

// Every button with class .act starts a job. data-site / data-all say whom for.
document.addEventListener('click', async e => {
  const btn = e.target.closest('.act');
  if (!btn || btn.disabled) return;
  const act = btn.dataset.act, body = {};
  if (btn.dataset.site) body.site = btn.dataset.site;
  if (btn.dataset.all) body.all = true;
  if (act === 'new_site'){
    const host = $('#ns-host').value.trim().toLowerCase(), pw = $('#ns-pw').value;
    if (!/^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$/.test(host)){ $('#ns-msg').textContent = 'Enter a lowercase hostname, e.g. acme.erp.lan'; return; }
    if (!pw || pw !== $('#ns-pw2').value){ $('#ns-msg').textContent = 'Passwords are empty or do not match'; return; }
    body.site = host; body.admin_password = pw;
    if (IMAGE_APPS.length) body.apps = [...document.querySelectorAll('.ns-app:checked')].map(c => c.value);
    if (!confirm(`Create site ${host}?\n\nApps: ${(body.apps || ['every app in the image']).join(', ')}\n\n`
        + `5–10 minutes. Afterwards point ${host} at this server in DNS or the hosts file.`)) return;
    $('#ns-msg').textContent = '';
    if (await runAction(act, body)){ $('#ns-pw').value = ''; $('#ns-pw2').value = ''; }
    return;
  }
  if (btn.dataset.confirm && !confirm(btn.dataset.confirm)) return;
  if (act === 'rollback' && $('#rb-snap').value) body.snapshot = $('#rb-snap').value;
  await runAction(act, body);
});

// ---- drop site -------------------------------------------------------------
let drSite = null;
function closeDrop(){ $('#dr-modal').classList.remove('on'); drSite = null; }
document.addEventListener('click', e => {
  const b = e.target.closest('.act-drop');
  if (!b || b.disabled) return;
  drSite = b.dataset.site;
  $('#dr-site').textContent = drSite; $('#dr-site2').textContent = drSite;
  $('#dr-confirm').value = ''; $('#dr-pw').value = ''; $('#dr-err').textContent = '';
  $('#dr-modal').classList.add('on'); $('#dr-confirm').focus();
});
$('#dr-cancel').onclick = closeDrop;
$('#dr-modal').onclick = e => { if (e.target === $('#dr-modal')) closeDrop(); };
$('#dr-go').onclick = async () => {
  if (!drSite) return;
  const btn = $('#dr-go'); btn.disabled = true; $('#dr-err').textContent = '';
  try{
    const r = await fetch('/api/run/drop_site', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({site: drSite, confirm_site: $('#dr-confirm').value, admin_password: $('#dr-pw').value})});
    const j = await r.json();
    if (j.error){ $('#dr-err').textContent = j.error; }
    else { closeDrop(); jobWasActive = true; refreshJob(); revealConsole(); }
  }catch(e){ $('#dr-err').textContent = 'request failed'; }
  btn.disabled = false;
};

// ---- apps on a site: read on demand (one bench call), uninstall from here ----
let apSite = null;
function closeApps(){ $('#ap-modal').classList.remove('on'); apSite = null; }
$('#ap-close').onclick = closeApps;
$('#ap-modal').onclick = e => { if (e.target === $('#ap-modal')) closeApps(); };
document.addEventListener('click', async e => {
  const b = e.target.closest('.site-apps');
  if (!b) return;
  apSite = b.dataset.site;
  $('#ap-site').textContent = apSite;
  $('#ap-body').textContent = 'Reading installed apps…';
  $('#ap-modal').classList.add('on');
  try{
    const j = await (await fetch('/api/sites/' + encodeURIComponent(apSite) + '/apps')).json();
    if (j.site !== apSite) return;
    if (j.error){ $('#ap-body').innerHTML = `<span style="color:var(--crit)">${esc(j.error)}</span>`; return; }
    const notIn = IMAGE_APPS.filter(a => !j.apps.includes(a));
    $('#ap-body').innerHTML = `<table><thead><tr><th>Installed app</th><th></th></tr></thead><tbody>`
      + (j.apps.length ? j.apps.map(a => `<tr><td>${esc(a)}</td><td class="right">
          <button class="danger ap-un" data-app="${esc(a)}">Uninstall</button></td></tr>`).join('')
        : '<tr><td colspan="2" style="color:var(--muted)">No apps besides frappe</td></tr>')
      + '</tbody></table>'
      + (notIn.length ? `<p style="color:var(--muted)">In the image but not on this site: ${notIn.map(esc).join(', ')}`
          + (j.skip.length ? ` — never auto-installed: ${j.skip.map(esc).join(', ')}` : '') + '</p>' : '')
      + `<p style="color:var(--muted)">Uninstalling deletes that app's data on <b>${esc(apSite)}</b> only,
         after a full backup of it. Do this for every site that has an app before updating to an image
         without it — the update lists them.</p>`;
  }catch(err){ $('#ap-body').textContent = 'Could not read the apps'; }
});
document.addEventListener('click', async e => {
  const b = e.target.closest('.ap-un');
  if (!b || !apSite) return;
  const app = b.dataset.app, site = apSite;
  if (!confirm(`Uninstall ${app} from ${site}?\n\nThis DELETES ${app}'s DocTypes and all their data on ${site}. `
      + `A full backup of ${site} is taken first. ${site} is in maintenance mode while it runs; `
      + `other sites stay up.`)) return;
  closeApps();
  await runAction('uninstall_app', {site, app});
});

// ---- domains of a site: the host nginx's domain → site table ----
let dmSite = null;
function closeDomains(){ $('#dm-modal').classList.remove('on'); dmSite = null; }
$('#dm-close').onclick = closeDomains;
$('#dm-modal').onclick = e => { if (e.target === $('#dm-modal')) closeDomains(); };
function renderDomains(){
  const s = SITE_INFO[dmSite] || {name: dmSite, domain_info: [{name: dmSite, url: 'http://' + dmSite, cert: false}]};
  const hn = (s.host_name || '').replace(/\/$/, '');
  const hostOnly = hn.replace(/^https?:\/\//, '').replace(/[:\/].*$/, '');
  const portUrls = (s.domain_info || []).filter(d => d.port).map(d => d.url);
  const isPrimary = d => d.port ? hn === d.url : (hostOnly === d.name && !portUrls.includes(hn));
  const canCert = d => !d.cert && !d.is_ip && HTTP_PORT === 80 && PROXY_HTTPS;
  const row = (d, own) => `<tr><td><a href="${esc(d.url)}" target="_blank">${esc(d.name)}</a>
      ${isPrimary(d) ? '<span class="badge ok">primary</span>' : ''}
      ${d.port ? '<span class="badge" title="HTTP on this port goes to this site; no DNS needed">IP + port</span>' : ''}
      ${own ? '<span class="badge">site name</span>' : ''}
      ${d.port ? '' : d.cert ? '<span class="badge ok" title="Let\'s Encrypt, renewed automatically">HTTPS ✓</span>'
               : (PROXY_HTTPS ? '<span class="badge" title="self-signed: browsers warn">HTTPS self-signed</span>' : '')}</td>
    <td class="right">${isPrimary(d) ? '' : `<button class="dm-pri" data-d="${esc(d.port ? String(d.port) : d.name)}">Make primary</button>`}
      ${canCert(d) ? `<button class="dm-cert" data-d="${esc(d.name)}" title="Let's Encrypt: public domains only">Get certificate</button>` : ''}
      ${own ? '' : d.port ? `<button class="danger dm-unport" data-p="${d.port}">Remove</button>`
                          : `<button class="danger dm-del" data-d="${esc(d.name)}">Remove</button>`}</td></tr>`;
  const info = s.domain_info || [];
  $('#dm-body').innerHTML = `<table><thead><tr><th>Domain</th><th></th></tr></thead><tbody>`
    + info.map((d, i) => row(d, i === 0)).join('') + '</tbody></table>';
  // Say why "Get certificate" is missing.
  const why = !PROXY_HTTPS ? HTTPS_REASON
    : HTTP_PORT !== 80 ? `Let's Encrypt checks domains on port 80, but this stack's HTTP_PORT is ${HTTP_PORT}.` : '';
  if (why && info.some(d => !d.is_ip && !d.cert))
    $('#dm-body').innerHTML += `<p style="color:var(--warn)"><b>Get certificate</b> is unavailable. ${esc(why)}
      ${PROXY_HTTPS ? '' : '<a href="/install">Open the installation suite</a>'}</p>`;
  const box = $('#dm-auth');
  if (!AUTH.installed){ box.style.display = 'none'; return; }
  box.style.display = '';
  const who = siteUsers(dmSite).filter(u => !u.disabled);
  box.innerHTML = s.auth
    ? `<b>🔐 Authelia login: on</b> — every domain and IP + port of this site asks for a login with 2FA
       first (HTTPS only). ${who.length} user(s) may enter.
       <div class="row" style="margin-top:8px"><button class="danger" id="dm-unlink">Remove the login</button></div>`
    : `<b>Authelia login: off</b> — anyone who reaches this site sees the ERPNext login.
       <div style="color:var(--muted);font-size:12.5px;margin-top:4px">Turning it on makes the site HTTPS-only and puts
       a 2FA login in front of every domain and IP + port, with this site's own users (${who.length} so far). API clients, the mobile app, webhooks and payment
       callbacks are stopped by it too — give those an unlinked domain.</div>
       <div class="row" style="margin-top:8px"><button class="primary" id="dm-link" ${PROXY_HTTPS ? '' : 'disabled title="HTTPS is off"'}>Require Authelia login</button></div>`;
  if ($('#dm-link')) $('#dm-link').onclick = async () => {
    const site = dmSite;
    if (!who.length){ alert(`Add a user for ${site} on the Authelia page first, or nobody can enter.`); return; }
    closeDomains(); await runAction('auth_link', {site});
  };
  if ($('#dm-unlink')) $('#dm-unlink').onclick = async () => {
    const site = dmSite;
    if (!confirm(`Remove the Authelia login from ${site}?\n\nAnyone who reaches it sees the ERPNext login again.`)) return;
    closeDomains(); await runAction('auth_unlink', {site});
  };
}

function siteUsers(site){ return (AUTH.users || {})[site] || []; }
document.addEventListener('click', e => {
  const b = e.target.closest('.site-doms');
  if (!b) return;
  dmSite = b.dataset.site;
  $('#dm-site').textContent = dmSite;
  $('#dm-new').value = ''; $('#dm-primary').checked = false;
  $('#dm-port').value = ''; $('#dm-port-primary').checked = false;
  renderDomains();
  $('#dm-modal').classList.add('on');
});
$('#dm-port-add').onclick = async () => {
  if (!dmSite) return;
  const port = $('#dm-port').value.trim();
  if (!port) return;
  const site = dmSite;
  closeDomains();
  await runAction('port_add', {site, port, primary: $('#dm-port-primary').checked});
};
document.addEventListener('click', async e => {
  const b = e.target.closest('.dm-unport');
  if (!b || !dmSite) return;
  const site = dmSite, port = b.dataset.p;
  if (!confirm(`Remove port ${port} from ${site}?\n\nhttp://<server-ip>:${port} will stop working.`)) return;
  closeDomains();
  await runAction('port_remove', {port});
});
$('#dm-add').onclick = async () => {
  if (!dmSite) return;
  const domain = $('#dm-new').value.trim().toLowerCase();
  if (!domain) return;
  const site = dmSite;
  closeDomains();
  await runAction('domain_add', {site, domain, primary: $('#dm-primary').checked});
};
document.addEventListener('click', async e => {
  const del = e.target.closest('.dm-del'), pri = e.target.closest('.dm-pri'), crt = e.target.closest('.dm-cert');
  if ((!del && !pri && !crt) || !dmSite) return;
  const site = dmSite, domain = (del || pri || crt).dataset.d;
  if (del && !confirm(`Remove ${domain} from ${site}?\n\nRequests for ${domain} will no longer reach ${site}.`)) return;
  if (crt){
    const email = prompt(`Get a Let's Encrypt certificate for ${domain}?\n\n${domain} must be a public domain whose DNS points at `
      + `this server, with port 80 open from the internet. Renewal is automatic.\n\nE-mail for expiry notices (optional):`, '');
    if (email === null) return;
    closeDomains();
    await runAction('domain_cert', {site, domain, email: email.trim()});
    return;
  }
  closeDomains();
  await runAction(del ? 'domain_remove' : 'domain_primary', {site, domain});
});

document.addEventListener('click', e => {
  const b = e.target.closest('.site-bk');
  if (!b) return;
  $('#bk-site').value = b.dataset.site;
  refreshBackups();
  $('#bk-site').scrollIntoView({behavior:'smooth', block:'center'});
});

// ---- deleting uploads (uploads only — backups have no delete button) ----
function delBtn(name, what){
  return `<button class="danger del-btn" data-name="${esc(name)}" data-what="${esc(what)}"
    title="Delete this upload from the server">Delete</button>`;
}

document.addEventListener('click', async e => {
  const b = e.target.closest('.del-btn');
  if (!b) return;
  const name = b.dataset.name;
  const msg = b.dataset.what === 'folder'
    ? `Delete the uploaded folder "${name}" and everything in it?\n\nThis cannot be undone.`
    : `Delete the uploaded file "${name}"?\n\nThis cannot be undone.`;
  if (!confirm(msg)) return;
  b.disabled = true;
  try{
    const r = await fetch('/api/uploads/delete', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({name})});
    const j = await r.json();
    if (j.error) alert(j.error);
  }catch(err){ alert('request failed'); }
  refreshBackups();
});

// ---- restore: modal gate (target site, typed name, panel password) ----
let rsTarget = null;   // {kind, src_site, name}

function restoreBtn(hasDb, kind, srcSite, name){
  if (!hasDb) return '';
  return `<button class="danger rs-btn" data-kind="${esc(kind)}" data-src="${esc(srcSite)}" data-name="${esc(name)}"
    title="Restore this backup into a site">Restore</button>`;
}

function syncRestoreTarget(){
  const t = $('#rs-target').value || 'the site';
  $('#rs-site').textContent = t; $('#rs-site2').textContent = t;
  // A full backup of another site, or any upload (whose origin is unknown),
  // may need the cross-site switch; a site's own backup never does.
  if (rsTarget && rsTarget.kind === 'full' && rsTarget.src_site === $('#rs-target').value){
    $('#rs-other').checked = false; $('#rs-other').disabled = true;
  } else {
    $('#rs-other').disabled = false;
  }
}

function openRestore(kind, srcSite, name){
  if (!SITES.length){ alert('There is no site to restore into — create one first.'); return; }
  rsTarget = {kind, src_site: srcSite, name};
  fillSelect($('#rs-target'), SITES);
  if (SITES.includes(srcSite)) $('#rs-target').value = srcSite;
  $('#rs-src').textContent = kind === 'full' ? `${srcSite} / ${name}` : `uploads/${name}`;
  $('#rs-other').checked = false;
  $('#rs-confirm').value = ''; $('#rs-pw').value = ''; $('#rs-err').textContent = '';
  syncRestoreTarget();
  $('#rs-modal').classList.add('on');
  $('#rs-confirm').focus();
}
function closeRestore(){ $('#rs-modal').classList.remove('on'); rsTarget = null; }

$('#rs-target').onchange = syncRestoreTarget;
document.addEventListener('click', e => {
  const b = e.target.closest('.rs-btn');
  if (b && !b.disabled) openRestore(b.dataset.kind, b.dataset.src, b.dataset.name);
});
$('#rs-cancel').onclick = closeRestore;
$('#rs-modal').onclick = e => { if (e.target === $('#rs-modal')) closeRestore(); };
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  if ($('#rs-modal').classList.contains('on')) closeRestore();
  if ($('#dr-modal').classList.contains('on')) closeDrop();
  if ($('#ap-modal').classList.contains('on')) closeApps();
  if ($('#dm-modal').classList.contains('on')) closeDomains();
});

$('#rs-go').onclick = async () => {
  if (!rsTarget) return;
  const btn = $('#rs-go');
  btn.disabled = true; $('#rs-err').textContent = '';
  try{
    const r = await fetch('/api/run/restore', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({kind: rsTarget.kind, src_site: rsTarget.src_site, name: rsTarget.name,
        site: $('#rs-target').value, allow_other_site: $('#rs-other').checked,
        confirm_site: $('#rs-confirm').value, admin_password: $('#rs-pw').value})});
    const j = await r.json();
    if (j.error){ $('#rs-err').textContent = j.error; btn.disabled = false; return; }
    closeRestore();
    jobWasActive = true; refreshJob(); revealConsole();
  }catch(e){ $('#rs-err').textContent = 'request failed'; }
  btn.disabled = false;
};

function meterClass(p){ return p >= 92 ? 'crit' : p >= 80 ? 'warn' : ''; }
function meterFlag(p){ return p >= 92 ? '<span class="flag crit">critical</span>'
                     : p >= 80 ? '<span class="flag warn">high</span>' : ''; }
function meterHTML(label, pct, sub){
  return `<div class="meter"><div class="row"><span>${esc(label)}</span>
    <span><b class="num">${pct}%</b> ${meterFlag(pct)}</span></div>
    <div class="bar"><i class="${meterClass(pct)}" style="width:${Math.min(pct,100)}%"></i></div>
    <div class="sub">${esc(sub)}</div></div>`;
}

async function refreshStats(){
  try{
    const r = await fetch('/api/stats'); if(r.status===401){location='/login';return;}
    const s = await r.json();
    $('#t-cpu').textContent = s.cpu_pct + '%';
    $('#t-cores').textContent = s.cores + ' cores';
    $('#t-load').textContent = s.load.join(' / ');
    $('#t-cores2').textContent = s.cores;
    $('#t-avail').textContent = s.mem.available;
    $('#t-memtot').textContent = 'of ' + s.mem.total + ' total';
    $('#t-up').textContent = s.uptime;
    let m = meterHTML('Memory', s.mem.pct, s.mem.used + ' used of ' + s.mem.total);
    if (s.swap.total !== '0 B') m += meterHTML('Swap', s.swap.pct, s.swap.used + ' used of ' + s.swap.total);
    for (const d of s.disks) m += meterHTML(d.label, d.pct, d.used + ' used, ' + d.free + ' free of ' + d.total);
    $('#meters').innerHTML = m;
    const err = s.containers.error;
    $('#cont-err').style.display = err ? '' : 'none';
    if (err) $('#cont-err').textContent = err;
    $('#containers').innerHTML = s.containers.services.map(c =>
      `<tr><td>${esc(c.name)}</td><td><span class="dot ${c.state==='running'?'up':'down'}"></span>${esc(c.state)}</td>
       <td>${esc(c.status)}</td></tr>`).join('') ||
      '<tr><td colspan="3" style="color:var(--muted)">No containers reported.</td></tr>';
  }catch(e){}
}

function fileBadge(ok, label){ return `<span class="badge ${ok?'ok':'miss'}">${label}${ok?' ✓':' missing'}</span>`; }
function dl(kind, name){ return `<a href="/download/${kind}/${encodeURIComponent(name).replace(/%2F/g,'/')}">download</a>`; }
function folderDlBtn(site, name){
  return `<button onclick="location='/download-folder/full/${encodeURIComponent(site)}/${encodeURIComponent(name)}'"
    title="Download this whole backup folder as one .zip">Download</button>`;
}

async function refreshBackups(){
  try{
    const want = $('#bk-site').value;
    const r = await fetch('/api/backups' + (want ? '?site=' + encodeURIComponent(want) : '')); if(!r.ok) return;
    const b = await r.json();
    const sel = $('#bk-site');
    sel.innerHTML = b.sites.length
      ? b.sites.map(x => `<option value="${esc(x)}">${esc(x)}${b.live.includes(x) ? '' : ' (dropped)'}</option>`).join('')
      : '<option value="">no backups yet</option>';
    if (b.site) sel.value = b.site;
    $('#bk-note').textContent = b.site && !b.live.includes(b.site)
      ? 'This site is no longer on the bench; its backups are kept until you delete them over SSH.' : '';
    const site = b.site || '';
    $('#tab-full').innerHTML = b.full.length ? `<div class="scrollrows"><table><thead><tr><th>Backup</th><th>Size</th>
      <th>Contents</th><th class="right">Files</th><th></th></tr></thead><tbody>` +
      b.full.map(d => `<tr><td class="num">${esc(d.name)}</td><td class="num">${esc(d.size)}</td>
        <td>${fileBadge(d.db,'DB')} ${fileBadge(d.public,'Files')} ${fileBadge(d.private,'Private')}</td>
        <td class="right"><details><summary>${d.files.length} files</summary><div class="filelist">` +
        d.files.map(f => `${esc(f.name)} (${esc(f.size)}) — ${dl('full', site + '/' + d.name + '/' + f.name)}`).join('<br>') +
        `</div></details></td><td class="right">${folderDlBtn(site, d.name)} ${restoreBtn(d.db, 'full', site, d.name)}</td></tr>`).join('') +
        '</tbody></table></div>'
      : `<p style="color:var(--muted)">No full backups${site ? ' for ' + esc(site) : ''}.</p>`;
    const simpleTable = (rows, kind, prefix, deletable) => rows.length ? `<div class="scrollrows"><table><thead><tr><th>File</th><th>Size</th>
      <th>Date</th><th></th></tr></thead><tbody>` +
      rows.map(f => `<tr><td class="num">${esc(f.name)}${f.latest?' <span class="badge ok">latest</span>':''}</td>
        <td class="num">${esc(f.size)}</td><td class="num">${esc(f.mtime)}</td>
        <td class="right">${dl(kind, prefix + f.name)}${deletable ? ' ' + delBtn(f.name, 'file') : ''}</td></tr>`).join('') +
        '</tbody></table></div>'
      : '<p style="color:var(--muted)">Nothing here yet.</p>';
    $('#tab-db').innerHTML = simpleTable(b.db_only, 'db', site + '/');
    $('#tab-img').innerHTML = simpleTable(b.images, 'image', '');
    const uploadGroups = new Map();
    const looseUploads = [];
    for (const file of b.uploads) {
      const slash = file.name.indexOf('/');
      if (slash < 0) { looseUploads.push(file); continue; }
      const folder = file.name.slice(0, slash);
      if (!uploadGroups.has(folder)) uploadGroups.set(folder, []);
      uploadGroups.get(folder).push(file);
    }
    const restorable = new Set(b.upload_folders || []);
    $('#tab-upl').innerHTML = [...uploadGroups].map(([folder, files]) =>
      `<div class="upload-folder"><div class="row"><strong>${esc(folder)}/</strong> ` +
      (restorable.has(folder)
        ? restoreBtn(true, 'upload', '', folder)
        : '<button class="danger" disabled title="Upload a matching *-database.sql.gz file into this folder first">Restore</button> <span>Database backup missing</span>') +
      ` ${delBtn(folder, 'folder')}</div>` +
      `<details><summary>${files.length} files</summary>` +
      simpleTable(files, 'upload', '', true) + '</details></div>'
    ).join('') + (looseUploads.length
      ? '<p>Files uploaded without a folder:</p>' + simpleTable(looseUploads, 'upload', '', true)
      : '') + (!uploadGroups.size && !looseUploads.length
      ? '<p style="color:var(--muted)">No uploads yet. Enter a folder name below to keep a backup’s files together.</p>'
      : '');
    const rb = $('#rb-snap'), cur = rb.value;
    rb.innerHTML = '<option value="">Latest snapshot</option>' +
      b.images.map(i => `<option value="${esc(i.name)}">${esc(i.name)} (${esc(i.size)})</option>`).join('');
    if ([...rb.options].some(o => o.value === cur)) rb.value = cur;
    if (jobWasActive) document.querySelectorAll('.rs-btn').forEach(x => x.disabled = true);
    capRows();
  }catch(e){}
}
$('#bk-site').onchange = refreshBackups;

// Show 3 rows per backup table, the rest behind a scrollbar.
function capRows(){
  document.querySelectorAll('.tabpane.active .scrollrows').forEach(box => {
    const rows = box.querySelectorAll('tbody tr');
    box.style.maxHeight = (rows.length > 3 && rows[3].offsetTop > 0)
      ? rows[3].offsetTop + 'px' : '';
  });
}

$('#clear-old').onclick = async () => {
  if (!confirm($('#clear-old').dataset.confirm)) return;
  $('#clear-old-msg').textContent = 'Clearing…';
  const r = await fetch('/api/backups/clear-old', {method:'POST'});
  const j = await r.json();
  $('#clear-old-msg').textContent = j.error ? j.error
    : (j.deleted ? `Deleted ${j.deleted} old backup${j.deleted===1?'':'s'} — freed ${j.freed}`
                 : 'Nothing older than 30 days to delete');
  refreshBackups();
};

// ---- cron jobs: fetched fresh on every open ----
$('#cron-show').onclick = async () => {
  const box = $('#cron-box'), btn = $('#cron-show');
  if (box.style.display !== 'none'){
    box.style.display = 'none'; btn.textContent = 'Show cron jobs'; return;
  }
  btn.disabled = true;
  try{
    const r = await fetch('/api/cron');
    const j = await r.json();
    $('#cron-rows').innerHTML = (j.jobs || []).length
      ? j.jobs.map(c => `<tr>
          <td class="num" style="white-space:nowrap"><code>${esc(c.schedule)}</code></td>
          <td><code>${esc(c.command)}</code></td></tr>`).join('')
      : '<tr><td colspan="2" style="color:var(--muted)">The crontab is empty — nothing is scheduled.</td></tr>';
    box.style.display = ''; btn.textContent = 'Hide cron jobs';
  }catch(e){ alert('could not read the crontab'); }
  btn.disabled = false;
};

$('#clear-ram').onclick = async () => {
  if (!confirm($('#clear-ram').dataset.confirm)) return;
  $('#ram-msg').textContent = 'Clearing…';
  const r = await fetch('/api/clear-ram', {method:'POST'});
  const j = await r.json();
  $('#ram-msg').textContent = j.error ? j.error : `Freed ${j.freed} — ${j.available} now available`;
  refreshStats();
};

const backupUploadExtension = /\.(sql\.gz|gz|tar|tgz|json|ya?ml)$/i;
function selectedBackupFiles() {
  const directoryFiles = [...$('#updirectory').files];
  if (!directoryFiles.length) return [...$('#upfiles').files];
  return directoryFiles.filter(f => f.webkitRelativePath.split('/').length === 2 && backupUploadExtension.test(f.name));
}
$('#updirectory').onchange = () => {
  const files = [...$('#updirectory').files];
  if (!files.length) return;
  $('#upfiles').value = '';
  $('#upfolder').value = files[0].webkitRelativePath.split('/')[0];
  const selected = selectedBackupFiles();
  $('#upselection').textContent = `${selected.length} backup files selected; ${files.length - selected.length} unsupported or nested files skipped.`;
};
$('#upfiles').onchange = () => {
  if (!$('#upfiles').files.length) return;
  $('#updirectory').value = '';
  $('#upselection').textContent = `${$('#upfiles').files.length} files selected.`;
};
$('#upbtn').onclick = () => {
  const files = selectedBackupFiles();
  if (!files.length) { $('#upmsg').textContent = 'Choose backup files or a folder containing supported backup files first.'; return; }
  const folder = $('#upfolder').value.trim();
  if (($('#updirectory').files.length && !folder) || (folder && !/^[A-Za-z0-9_-]{1,40}$/.test(folder))) {
    $('#upmsg').textContent = 'Enter a destination folder of 1–40 letters, digits, hyphens or underscores.';
    return;
  }
  const fd = new FormData();
  for (const f of files) fd.append('files', f, f.name);
  fd.append('folder', folder);
  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/upload');
  xhr.upload.onprogress = e => { if (e.lengthComputable)
    $('#upmsg').textContent = 'Uploading… ' + Math.round(100*e.loaded/e.total) + '%'; };
  xhr.onload = () => {
    try{
      const j = JSON.parse(xhr.responseText);
      $('#upmsg').textContent = j.error ? j.error : 'Uploaded to ' + j.dest + ': ' + j.saved.join(', ');
    }catch(e){ $('#upmsg').textContent = 'Upload failed (' + xhr.status + ')'; }
    refreshBackups();
  };
  xhr.onerror = () => $('#upmsg').textContent = 'Upload failed — network error';
  xhr.send(fd);
  $('#upmsg').textContent = 'Uploading… 0%';
};

$('#licupbtn').onclick = () => {
  const file = $('#licfile').files[0];
  if (!file) { $('#licupmsg').textContent = 'Choose a license file first.'; return; }
  const fd = new FormData();
  fd.append('file', file);
  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/upload-license');
  xhr.upload.onprogress = e => { if (e.lengthComputable)
    $('#licupmsg').textContent = 'Uploading… ' + Math.round(100*e.loaded/e.total) + '%'; };
  xhr.onload = () => {
    try{
      const j = JSON.parse(xhr.responseText);
      $('#licupmsg').innerHTML = j.error
        ? `<span style="color:var(--crit)">${esc(j.error)}</span>`
        : `<span style="color:var(--ok)">Uploaded ${esc(j.saved)} to ${esc(j.site)}</span>`;
    }catch(e){ $('#licupmsg').innerHTML = '<span style="color:var(--crit)">Upload failed (' + xhr.status + ')</span>'; }
  };
  xhr.onerror = () => $('#licupmsg').innerHTML = '<span style="color:var(--crit)">Upload failed — network error</span>';
  xhr.send(fd);
  $('#licupmsg').textContent = 'Uploading… 0%';
};

$('#licdlbtn').onclick = async () => {
  const pick = $('#licdlpick'), site = $('#licsite').value;
  pick.style.display = 'none';
  if (!site) { $('#licupmsg').textContent = 'Choose a site first.'; return; }
  $('#licupmsg').textContent = 'Looking for a license file…';
  const q = 'site=' + encodeURIComponent(site);
  try{
    const r = await fetch('/api/licenses?' + q);
    const j = await r.json();
    if (j.error) { $('#licupmsg').innerHTML = `<span style="color:var(--crit)">${esc(j.error)}</span>`; return; }
    if (!j.files.length) {
      $('#licupmsg').innerHTML = `<span style="color:var(--crit)">No license file in ${esc(j.site)}.</span>`;
      return;
    }
    if (j.files.length > 1) {
      pick.innerHTML = '<option value="">Choose a license file…</option>' +
        j.files.map(f => `<option>${esc(f)}</option>`).join('');
      pick.style.display = '';
      pick.onchange = () => { if (pick.value)
        window.location = '/download-license?' + q + '&name=' + encodeURIComponent(pick.value); };
      $('#licupmsg').innerHTML = `<span>${j.files.length} license files — pick one.</span>`;
      return;
    }
    const name = j.files[0];
    $('#licupmsg').innerHTML = `<span style="color:var(--ok)">Downloading ${esc(name)}</span>`;
    window.location = '/download-license?' + q + '&name=' + encodeURIComponent(name);
  }catch(e){ $('#licupmsg').innerHTML = '<span style="color:var(--crit)">Could not reach the server</span>'; }
};

document.querySelectorAll('.tabs button').forEach(b => b.onclick = () => {
  document.querySelectorAll('.tabs button').forEach(x => x.classList.toggle('active', x === b));
  document.querySelectorAll('.tabpane').forEach(p =>
    p.classList.toggle('active', p.id === 'tab-' + b.dataset.tab));
  capRows();
});

// A job that has just finished may have created, dropped or restored a site
// and written backups — the things this page shows.
onJobFinished = () => { refreshSites(); refreshBackups(); refreshStats(); };

setInterval(() => $('#clock').textContent = new Date().toLocaleString(), 1000);
refreshSites(); refreshStats(); refreshBackups();
setInterval(refreshStats, 5000);
setInterval(refreshSites, 30000);
setInterval(refreshBackups, 60000);
pollJob();
</script></body></html>"""


AUTH_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Authelia</title><style>""" + BASE_CSS + LAYOUT_CSS + TERM_CSS + r"""
.lead{font-size:13px;color:var(--ink-2);margin:0 0 12px}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.acts{display:flex;flex-wrap:wrap;gap:6px;justify-content:flex-end}
.acts button{padding:4px 9px;font-size:13px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:8px;margin:8px 0}
.scroll{overflow-x:auto}
.sub{color:var(--muted);font-size:12.5px}
.edit{margin-top:10px;padding:10px 12px;border:1px solid var(--hairline);border-radius:8px}
.bad{color:var(--crit)}
</style></head><body>
<div class="top">
  <h1>Authelia</h1><span id="clock" class="badge"></span>
  <span class="badge" title="panel code version">v{{ version }}</span>
  <a class="nav" href="/">← Dashboard</a>
  <span class="spacer"></span>
  <span style="color:var(--muted);font-size:13px">{{ user }}</span>
  <form method="post" action="/logout" style="margin:0"><button>Log out</button></form>
</div>
<main>
<div class="col-left">

<div class="card"><div class="row"><label>Site <select id="site"></select></label>
  <span id="state"></span><button id="toggle" class="act"></button></div>
  <div id="msg" class="sub" style="margin-top:8px"></div></div>

<div class="card"><h2>Users</h2>
  <p class="lead">Every site has its own users, passwords and 2FA: the same name on two sites is two accounts.
    The first login asks for 2FA registration, confirmed by an e-mailed code.</p>
  <div id="users" class="scroll"></div>
  <div id="edit"></div>
  <h2 style="margin:16px 0 4px">Add a user</h2>
  <div class="grid">
    <input type="text" id="u-user" placeholder="user name, e.g. alex" autocomplete="off" autocapitalize="off" spellcheck="false">
    <input type="text" id="u-name" placeholder="display name" autocomplete="off">
    <input type="email" id="u-email" placeholder="e-mail (2FA codes)" autocomplete="off">
    <input type="password" id="u-pw" placeholder="password (8+)" autocomplete="new-password">
    <input type="password" id="u-pw2" placeholder="confirm" autocomplete="new-password">
  </div>
  <button class="primary act" id="u-add">Add user</button>
</div>

<div class="card"><h2>Whitelist IPs</h2>
  <p class="lead">No Authelia login from these addresses on this site. The ERPNext login still applies, and
    <b>everyone behind the address</b> gets it: use fixed addresses you control.</p>
  <div id="l-bypass" class="scroll"></div>
  <div class="row" style="margin-top:8px"><input type="text" id="n-bypass" placeholder="203.0.113.7 or 192.168.1.0/24" size="26" autocomplete="off">
    <button class="primary act ip-add" data-kind="bypass">Add to whitelist</button></div>
</div>

<div class="card"><h2>No-2FA IPs</h2>
  <p class="lead">From these addresses the password alone is enough on this site.</p>
  <div id="l-trusted" class="scroll"></div>
  <div class="row" style="margin-top:8px"><input type="text" id="n-trusted" placeholder="203.0.113.7 or 192.168.1.0/24" size="26" autocomplete="off">
    <button class="primary act ip-add" data-kind="trusted">Add to no-2FA</button></div>
</div>

<div class="card"><h2>2FA IPs</h2>
  <p class="lead">Where each user entered 2FA, from the site's Authelia login log. A remembered browser does
    not enter it again, so it is not listed each time.</p>
  <div id="twofa" class="scroll"></div>
  <details style="margin-top:12px"><summary><b>Last 100 login attempts</b></summary>
    <div id="recent" class="scroll" style="margin-top:8px"></div></details>
</div>

</div><!-- /col-left -->
""" + TERM_HTML + r"""
</main>
<script>
""" + TERM_JS + r"""
let A = {sites: []}, cur = '', loginSeq = 0;
try{ cur = localStorage.getItem('sspl-auth-site') || ''; }catch(e){}
const site = () => A.sites.find(s => s.name === cur);

async function run(act, body){
  try{
    const r = await fetch('/api/run/' + act, {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
    if (r.status === 401){ location = '/login'; return false; }
    const j = await r.json();
    if (j.error){ alert(j.error); return false; }
    jobWasActive = true; refreshJob(); revealConsole();
    return true;
  }catch(e){ alert('request failed'); return false; }
}

async function load(){
  try{
    const r = await fetch('/api/auth'); if (r.status === 401){ location = '/login'; return; }
    A = await r.json();
  }catch(e){ $('#msg').textContent = 'Could not load'; return; }
  if (!site()) cur = A.sites.length ? A.sites[0].name : '';
  $('#site').innerHTML = A.sites.map(s => `<option value="${esc(s.name)}"${s.name === cur ? ' selected' : ''}>${esc(s.name)}${s.linked ? ' 🔐' : ''}</option>`).join('');
  render(); loadLogins();
}

function netTable(kind, all){
  const nets = site() ? site().nets[kind] : [];
  const rows = all.split(/\s+/).filter(Boolean).map(n => `<tr><td><code>${esc(n)}</code> <span class="badge">all sites</span></td><td></td></tr>`)
    .concat(nets.map(n => `<tr><td><code>${esc(n)}</code></td><td><div class="acts">
      <button class="danger act ip-rm" data-kind="${kind}" data-ip="${esc(n)}">Remove</button></div></td></tr>`));
  $('#l-' + kind).innerHTML = rows.length ? `<table><tbody>${rows.join('')}</tbody></table>` : '<span class="sub">None.</span>';
}

function render(){
  const s = site(), off = !A.installed || !s;
  document.querySelectorAll('.act').forEach(b => b.disabled = off);
  $('#msg').innerHTML = !A.installed ? 'Authelia is not installed. <a href="/install">Open the installation suite</a>'
    : !s ? 'No sites yet.' : !s.linked ? 'The login is off for this site: users and IP lists take effect once it is on.' : '';
  $('#state').innerHTML = s ? (s.linked ? '<span class="badge ok">login on</span>' : '<span class="badge miss">login off</span>') : '';
  $('#toggle').style.display = off ? 'none' : '';
  if (s){
    $('#toggle').textContent = s.linked ? 'Remove the login' : 'Require Authelia login';
    $('#toggle').className = 'act ' + (s.linked ? 'danger' : 'primary');
    if (!s.linked && !A.https){ $('#toggle').disabled = true; $('#toggle').title = 'HTTPS is off'; } else $('#toggle').title = '';
  }
  const us = s ? s.users : [];
  $('#users').innerHTML = us.length ? '<table><tbody>' + us.map(u => `<tr>
      <td><b>${esc(u.user)}</b>${u.disabled ? ' <span class="badge miss">disabled</span>' : ''}
        <div class="sub">${esc(u.name)} · ${esc(u.email)}</div></td>
      <td><div class="acts">
        <button class="act u-act" data-a="pw" data-u="${esc(u.user)}">Password…</button>
        <button class="act u-act" data-a="2fa" data-u="${esc(u.user)}" title="Lost phone: register 2FA again at the next login">Reset 2FA</button>
        <button class="act u-act" data-a="dis" data-u="${esc(u.user)}" data-on="${u.disabled ? 0 : 1}">${u.disabled ? 'Enable' : 'Disable'}</button>
        <button class="danger act u-act" data-a="rm" data-u="${esc(u.user)}">Remove</button>
      </div></td></tr>`).join('') + '</tbody></table>' : '<span class="sub">No users on this site yet.</span>';
  netTable('bypass', A.all_bypass || ''); netTable('trusted', A.all_trusted || '');
}

async function loadLogins(){
  const seq = ++loginSeq;
  if (!A.installed || !cur){ $('#twofa').innerHTML = $('#recent').innerHTML = ''; return; }
  let j;
  try{ j = await (await fetch('/api/auth/' + encodeURIComponent(cur) + '/logins')).json(); }
  catch(e){ j = {error: 'could not load the login log'}; }
  if (seq !== loginSeq) return;      // another site was chosen meanwhile
  if (j.error){ $('#twofa').innerHTML = `<span class="bad">${esc(j.error)}</span>`; $('#recent').innerHTML = ''; return; }
  const v4 = ip => /^\d+\.\d+\.\d+\.\d+$/.test(ip);
  $('#twofa').innerHTML = j.twofa.length ? `<table><thead><tr><th>User</th><th>IP</th><th>Method</th><th>Last success</th><th>Passed / failed</th><th></th></tr></thead><tbody>`
    + j.twofa.map(x => `<tr><td>${esc(x.user)}</td><td><code>${esc(x.ip || '–')}</code></td><td>${esc(x.type)}</td>
      <td>${esc(x.last || 'never')}</td><td>${x.good} / <span class="${x.bad ? 'bad' : ''}">${x.bad}</span></td>
      <td><div class="acts">${x.good && v4(x.ip) ? `<button class="act ip-use" data-kind="bypass" data-ip="${esc(x.ip)}">Whitelist</button>
        <button class="act ip-use" data-kind="trusted" data-ip="${esc(x.ip)}">No 2FA</button>` : ''}</div></td></tr>`).join('') + '</tbody></table>'
    : '<span class="sub">No 2FA logins recorded.</span>';
  $('#recent').innerHTML = j.recent.length ? `<table><thead><tr><th>Time</th><th>User</th><th>IP</th><th>Step</th><th>Result</th></tr></thead><tbody>`
    + j.recent.map(x => `<tr><td>${esc(x.time)}</td><td>${esc(x.user)}</td><td><code>${esc(x.ip || '–')}</code></td><td>${esc(x.type)}</td>
      <td class="${x.ok ? '' : 'bad'}">${x.banned ? 'banned' : x.ok ? 'ok' : 'failed'}</td></tr>`).join('') + '</tbody></table>'
    : '<span class="sub">No logins recorded.</span>';
}

function addNet(kind, ip){
  const s = cur;
  if (!ip || !s) return;
  const q = kind === 'bypass' ? `Whitelist ${ip} on ${s}?\n\nEveryone at that address skips the Authelia login and sees the ERPNext login.`
                              : `Skip 2FA for ${ip} on ${s}?\n\nEveryone at that address logs in with the password alone.`;
  if (confirm(q)) return run('auth_ip_add', {site: s, kind, ip});
}

$('#site').onchange = () => {
  cur = $('#site').value; $('#edit').innerHTML = '';
  ['user','name','email','pw','pw2'].forEach(i => $('#u-' + i).value = '');
  try{ localStorage.setItem('sspl-auth-site', cur); }catch(e){}
  $('#twofa').innerHTML = $('#recent').innerHTML = '';
  render(); loadLogins();
};
$('#toggle').onclick = () => {
  const s = site();
  if (s.linked){ if (confirm(`Remove the Authelia login from ${s.name}?\n\nAnyone who reaches it sees the ERPNext login again.`)) run('auth_unlink', {site: s.name}); return; }
  if (!s.users.length){ alert(`Add a user for ${s.name} first, or nobody can enter.`); return; }
  if (confirm(`Require an Authelia login on ${s.name}?\n\nThe site becomes HTTPS-only. API clients, the mobile app and webhooks are stopped too, except from whitelisted IPs.`)) run('auth_link', {site: s.name});
};
$('#u-add').onclick = async () => {
  const v = i => $('#u-' + i).value;
  if (!v('user').trim()) return;
  if (v('pw') !== v('pw2')){ alert('The passwords do not match.'); return; }
  if (await run('auth_user_add', {site: cur, user: v('user').trim().toLowerCase(), name: v('name').trim(),
      email: v('email').trim(), password: v('pw')}))
    ['user','name','email','pw','pw2'].forEach(i => $('#u-' + i).value = '');
};
document.addEventListener('click', async e => {
  let b = e.target.closest('.ip-add');
  if (b){ const i = $('#n-' + b.dataset.kind); if (await addNet(b.dataset.kind, i.value.trim())) i.value = ''; return; }
  b = e.target.closest('.ip-use');
  if (b){ addNet(b.dataset.kind, b.dataset.ip); return; }
  b = e.target.closest('.ip-rm');
  if (b){ if (confirm(`Remove ${b.dataset.ip} from this list on ${cur}?`)) run('auth_ip_remove', {site: cur, kind: b.dataset.kind, ip: b.dataset.ip}); return; }
  b = e.target.closest('.u-act');
  if (!b) return;
  const user = b.dataset.u, s = cur, a = b.dataset.a;
  if (a === 'pw'){
    $('#edit').innerHTML = `<div class="edit"><b>New password for ${esc(user)} on ${esc(s)}</b><div class="grid">
      <input type="password" id="e-pw" placeholder="password (8+)" autocomplete="new-password">
      <input type="password" id="e-pw2" placeholder="confirm" autocomplete="new-password"></div>
      <div class="row"><button id="e-no">Cancel</button><button class="primary" id="e-ok">Set password</button></div></div>`;
    $('#e-pw').focus();
    $('#e-no').onclick = () => $('#edit').innerHTML = '';
    $('#e-ok').onclick = async () => {
      if ($('#e-pw').value !== $('#e-pw2').value){ alert('The passwords do not match.'); return; }
      if (await run('auth_user_passwd', {site: s, user, password: $('#e-pw').value})) $('#edit').innerHTML = '';
    };
  } else if (a === '2fa'){
    if (confirm(`Forget ${user}'s 2FA devices on ${s}? They register again at the next login.`)) run('auth_user_reset_2fa', {site: s, user});
  } else if (a === 'rm'){
    if (confirm(`Remove ${user} from ${s}? This cannot be undone.`)) run('auth_user_remove', {site: s, user});
  } else {
    const on = b.dataset.on === '1';
    if (!on || confirm(`Disable ${user} on ${s}? They cannot log in until enabled again.`)) run('auth_user_disable', {site: s, user, on});
  }
});

onJobFinished = load;
setInterval(() => $('#clock').textContent = new Date().toLocaleString(), 1000);
setInterval(() => { if (!document.hidden) loadLogins(); }, 60000);
load(); pollJob();
</script></body></html>"""


WG_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>WireGuard</title><style>""" + BASE_CSS + LAYOUT_CSS + TERM_CSS + r"""
.lead{font-size:13px;color:var(--ink-2);margin:0 0 12px}
.top{flex-wrap:wrap}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.row input,.row select{max-width:100%}
.acts{display:flex;flex-wrap:wrap;gap:6px;justify-content:flex-end;align-items:center}
.acts button{padding:4px 9px;font-size:13px}
.acts a{font-size:13px}
.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-top:12px}
.scroll{overflow-x:auto}
.sub{color:var(--muted);font-size:12.5px}
ol{margin:0;padding-left:20px;font-size:13.5px}
li{margin-bottom:4px}
</style></head><body>
<div class="top">
  <h1>WireGuard</h1><span id="clock" class="badge"></span>
  <span class="badge" title="panel code version">v{{ version }}</span>
  <a class="nav" href="/">← Dashboard</a>
  <span class="spacer"></span>
  <span style="color:var(--muted);font-size:13px">{{ user }}</span>
  <form method="post" action="/logout" style="margin:0"><button>Log out</button></form>
</div>
<main>
<div class="col-left">

<div class="card"><h2>Tunnel</h2>
  <p class="lead">Lets the ERP containers print to CUPS servers behind shop routers. Print servers cannot open
    connections to this server or to each other.</p>
  <div id="state">Loading…</div>
  <div id="facts" class="facts"></div>
  <div id="msg" class="sub" style="margin-top:8px"></div></div>

<div class="card" id="settings" style="display:none"><h2>Settings</h2>
  <div class="row" style="align-items:flex-end">
    <label class="sub">Public IP or DNS name of this server<br><input type="text" id="s-endpoint" placeholder="erp.example.com" size="28" autocomplete="off" autocapitalize="off" spellcheck="false"></label>
    <label class="sub">UDP port<br><input type="text" id="s-port" size="6" inputmode="numeric" autocomplete="off"></label>
    <label class="sub">Tunnel network (a /24)<br><input type="text" id="s-subnet" size="16" autocomplete="off"></label>
    <button class="primary act" id="s-save"></button></div>
  <div id="s-note" class="sub" style="margin-top:8px"></div>
</div>

<div id="manage" style="display:none">
<div class="card"><h2>Print servers</h2>
  <p class="lead">The site is a label only: every site on the stack can reach every print server.</p>
  <div id="peers" class="scroll"></div>
</div>

<div class="card"><h2>Add a print server</h2>
  <div class="row">
    <input type="text" id="n-name" aria-label="Print server name" placeholder="name, e.g. shop1" size="24" maxlength="31" autocomplete="off" autocapitalize="off" spellcheck="false">
    <select id="n-site" aria-label="Site label"><option value="">no site label</option></select>
    <button class="primary act" id="n-add">Add print server</button></div>
</div>

<div class="card"><h2>Connect it</h2>
  <ol>
    <li>Open <b>UDP <span id="h-port"></span></b> to this server in the cloud firewall or router.</li>
    <li>Download the <b>setup script</b> and run it as root on the CUPS machine (Debian/Ubuntu).
      It holds a <b>private key</b>: copy it over a safe channel and delete it afterwards.</li>
    <li>Share each printer: <code>lpadmin -p &lt;printer&gt; -o printer-is-shared=true</code></li>
    <li>In ERPNext, set the Printer Server host to the tunnel address above, port 631.</li>
    <li><b>Test CUPS</b> asks for a page from inside the backend container.</li>
  </ol>
</div>
</div>

</div><!-- /col-left -->
""" + TERM_HTML + r"""
</main>
<script>
""" + TERM_JS + r"""
let W = null, sitesShown = null, loadSeq = 0;
const SETTINGS = ['endpoint', 'port', 'subnet'];

async function run(act, body){
  try{
    const r = await fetch('/api/run/' + act, {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
    if (r.status === 401){ location = '/login'; return false; }
    const j = await r.json();
    if (j.error){ alert(j.error); return false; }
    jobWasActive = true; refreshJob(); revealConsole();
    return true;
  }catch(e){ alert('request failed'); return false; }
}

function ago(s){
  if (s === null) return 'never';
  if (s < 120) return s + ' s ago';
  if (s < 7200) return Math.floor(s / 60) + ' min ago';
  if (s < 172800) return Math.floor(s / 3600) + ' h ago';
  return Math.floor(s / 86400) + ' days ago';
}

async function load(){
  const seq = ++loadSeq;
  let w;
  try{
    const r = await fetch('/api/wireguard'); if (r.status === 401){ location = '/login'; return; }
    if (!r.ok) throw 0;
    w = await r.json();
  }catch(e){ if (seq === loadSeq) $('#msg').textContent = 'Could not load: the status shown may be old.'; return; }
  if (seq !== loadSeq) return;       // a newer answer is on its way
  // A field follows the server until it is edited.
  for (const k of SETTINGS){
    const i = $('#s-' + k);
    if (!W || i.value === (W[k] || '')) i.value = w[k] || '';
  }
  W = w;
  $('#settings').style.display = w.stack && w.script ? '' : 'none';
  $('#s-subnet').disabled = w.peers.length > 0;
  if (w.peers.length) $('#s-subnet').value = w.subnet;
  $('#s-save').textContent = w.installed ? 'Save / repair' : 'Install WireGuard';
  $('#s-note').textContent = w.peers.length
    ? 'The network is fixed while print servers exist. After changing the address or port, run each print server\'s setup script again.' : '';
  $('#state').innerHTML = !w.installed ? '<span class="badge miss">not installed</span>'
    : w.running ? '<span class="badge ok">running</span>' : '<span class="badge miss">down</span>';
  $('#manage').style.display = w.installed && w.tools ? '' : 'none';
  $('#msg').innerHTML = !w.stack ? 'Install the stack and tools first. <a href="/install">Open the installation suite</a>'
    : !w.script ? 'install-wireguard.sh is not in the checkout: update the panel &amp; tools first.'
    : !w.installed ? 'Fill in the settings below to install it.'
    : !w.tools ? 'The tools are missing. <a href="/install">Open the installation suite</a>'
    : !w.running ? 'The tunnel is down, so nothing prints through it. Use <b>Save / repair</b> below.' : '';
  $('#facts').innerHTML = !w.installed ? '' : [
    ['Print servers dial', w.endpoint ? w.endpoint + ':' + w.port : 'not set'], ['Tunnel network', w.subnet],
    ['This server', w.hub], ['Connected', w.peers.filter(p => p.up).length + ' of ' + w.peers.length]]
    .map(([k, v]) => `<div><div class="sub">${k}</div><b class="num">${esc(v)}</b></div>`).join('');
  $('#h-port').textContent = w.port;
  $('#peers').innerHTML = w.peers.length ? `<table><thead><tr><th>Name</th><th>Tunnel address</th><th>Site</th><th>Status</th><th>Last handshake</th><th></th></tr></thead><tbody>`
    + w.peers.map(p => `<tr><td><b>${esc(p.name)}</b></td><td><code>${esc(p.ip)}</code></td><td>${esc(p.site || '–')}</td>
      <td>${p.up ? '<span class="badge ok">connected</span>' : '<span class="badge miss">not connected</span>'}</td>
      <td class="num">${ago(p.age)}</td>
      <td><div class="acts"><a href="/api/wireguard/setup/${esc(p.name)}" title="Contains a private key">Setup script</a>
        <button class="act p-act" data-a="wg_test" data-n="${esc(p.name)}">Test CUPS</button>
        <button class="danger act p-act" data-a="wg_remove" data-n="${esc(p.name)}">Remove</button></div></td></tr>`).join('') + '</tbody></table>'
    : '<span class="sub">None yet. Add the first one below.</span>';
  if (jobWasActive) document.querySelectorAll('.act').forEach(b => b.disabled = true);
  const key = JSON.stringify(w.sites);
  if (key !== sitesShown){           // rebuilt only on a change: keeps the choice
    const sel = $('#n-site'), was = sel.value;
    sel.innerHTML = '<option value="">no site label</option>' + w.sites.map(s => `<option value="${esc(s)}">${esc(s)}</option>`).join('');
    if (w.sites.includes(was)) sel.value = was;
    sitesShown = key;
  }
}

$('#s-save').onclick = () => {
  if (!W) return;
  const body = {}, moved = W.installed && W.peers.length;
  for (const k of SETTINGS) body[k] = $('#s-' + k).value.trim();
  if (!body.endpoint){ $('#s-endpoint').focus(); return; }
  let q = W.installed ? 'Apply and restart the tunnel? Printing pauses for a few seconds.' : 'Install WireGuard on this server now?';
  if (moved && (body.endpoint !== W.endpoint || body.port !== W.port))
    q += '\n\nThe address or port changes: print servers may stay disconnected until you run their setup scripts again.';
  if (confirm(q)) run('install_wireguard', body);
};
$('#n-add').onclick = async () => {
  const name = $('#n-name').value.trim();
  if (!name){ $('#n-name').focus(); return; }
  if (await run('wg_add', {name, site: $('#n-site').value})) $('#n-name').value = '';
};
$('#n-name').addEventListener('keydown', e => { if (e.key === 'Enter' && !$('#n-add').disabled) $('#n-add').click(); });
document.addEventListener('click', e => {
  const b = e.target.closest('.p-act');
  if (!b) return;
  const name = b.dataset.n;
  if (b.dataset.a === 'wg_remove' && !confirm(`Remove ${name}?

Its key stops working, printing to it stops, and its address is not used again.`)) return;
  run(b.dataset.a, {name});
});

onJobFinished = load;
setInterval(() => $('#clock').textContent = new Date().toLocaleString(), 1000);
setInterval(() => { if (!document.hidden) load(); }, 15000);
load(); pollJob();
</script></body></html>"""


RAM_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RAM monitor</title><style>""" + BASE_CSS + LAYOUT_CSS + r"""
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:16px}
.tile{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:12px 14px}
.tile .k{font-size:12px;color:var(--muted)} .tile .v{font-size:22px;margin-top:2px}
.tile .s{font-size:12px;color:var(--ink-2)}
.alerts{display:flex;flex-direction:column;gap:6px}
.alert{border-radius:8px;padding:8px 12px;font-size:14px;border:1px solid var(--border)}
.alert.warn{border-color:var(--warn);color:var(--warn)}
.alert.crit{border-color:var(--crit);color:var(--crit);font-weight:600}
.alert.ok{border-color:var(--ok);color:var(--ok)}
.bar{height:8px;border-radius:4px;background:var(--track);overflow:hidden;min-width:80px}
.bar i{display:block;height:100%;background:var(--accent)}
.bar i.warn{background:var(--warn)} .bar i.crit{background:var(--crit)}
.right{text-align:right}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px 18px;margin-bottom:14px}
.kv .k{font-size:12px;color:var(--muted)} .kv .v{font-size:17px}
.dbform{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
.dbform input{width:120px}
.note{font-size:13px;color:var(--ink-2);margin:10px 0 0}
#db-msg{font-size:13px;margin-top:10px;min-height:1em}
#db-msg.err{color:var(--crit)} #db-msg.okm{color:var(--ok)}
.cmd{font:12px ui-monospace,Menlo,Consolas,monospace;color:var(--muted);
  max-width:520px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.scroll{overflow-x:auto}
</style></head><body>
<div class="top">
  <h1>RAM monitor</h1><span id="clock" class="badge"></span>
  <span class="badge" title="panel code version">v{{ version }}</span>
  <a class="nav" href="/">← Dashboard</a>
  <span class="spacer"></span>
  <span style="color:var(--muted);font-size:13px">{{ user }}</span>
  <form method="post" action="/logout" style="margin:0"><button>Log out</button></form>
</div>
<main>

<div class="card"><h2>Alerts</h2><div class="alerts" id="alerts">
  <div class="alert">Loading…</div></div></div>

<div class="tiles">
  <div class="tile"><div class="k">RAM available</div><div class="v num" id="t-avail">–</div>
    <div class="s" id="t-total"></div></div>
  <div class="tile"><div class="k">RAM used</div><div class="v num" id="t-used">–</div>
    <div class="s" id="t-cache"></div></div>
  <div class="tile"><div class="k">Swap used</div><div class="v num" id="t-swap">–</div>
    <div class="s" id="t-swaptot"></div></div>
  <div class="tile"><div class="k">Swapping in / out</div><div class="v num" id="t-swapio">–</div>
    <div class="s">since the last refresh</div></div>
  <div class="tile"><div class="k">Memory pressure</div><div class="v num" id="t-psi">–</div>
    <div class="s">time tasks waited on RAM (10s)</div></div>
</div>

<div class="card"><h2>Database memory (InnoDB buffer pool)</h2>
  <div class="kv">
    <div><div class="k">Pool size now</div><div class="v num" id="d-live">–</div></div>
    <div><div class="k">Saved setting</div><div class="v num" id="d-conf">–</div></div>
    <div><div class="k">Database size</div><div class="v num" id="d-data">–</div></div>
    <div><div class="k">Recommended</div><div class="v num" id="d-rec">–</div></div>
    <div><div class="k">Reads from disk (recent)</div><div class="v num" id="d-miss">–</div></div>
    <div><div class="k">Reads from disk (since DB start)</div><div class="v num" id="d-miss-all">–</div></div>
    <div><div class="k">Survives a DB restart</div><div class="v" id="d-perm">–</div></div>
    <div><div class="k">RAM free on the server</div><div class="v num" id="d-avail">–</div></div>
  </div>
  <div class="dbform">
    <input id="d-size" placeholder="e.g. 2G" autocomplete="off">
    <button class="primary" id="d-set">Apply size</button>
    <button id="d-userec">Use recommended</button>
    <button class="danger" id="d-persist" style="display:none"
      title="Adds the settings mount to the compose file and recreates the db container">Make permanent (restarts DB)</button>
  </div>
  <div id="db-msg"></div>
  <p class="note">The buffer pool is the RAM MariaDB keeps database pages in. When it is smaller than
    the database, queries read from disk and every bill save slows down. A new size applies at once,
    with no restart. The allowed range is <span id="d-range">–</span>: the server keeps 2G for
    everything else. Raise it only when there is free RAM, because a pool pushed into swap is slower than a small one.</p>
</div>

<div class="card"><h2>Containers by memory</h2>
  <div id="c-err" style="color:var(--crit);font-size:13px;display:none"></div>
  <div class="scroll"><table><thead><tr><th>Container</th><th class="right">Memory</th>
    <th class="right">Limit</th><th>Of limit</th><th class="right">Of host RAM</th><th class="right">CPU</th></tr></thead>
  <tbody id="containers"><tr><td colspan="6" style="color:var(--muted)">Loading…</td></tr></tbody></table></div>
</div>

<div class="card"><h2>Top processes (RAM + swap)</h2>
  <div class="scroll"><table><thead><tr><th class="right">PID</th><th>Name</th><th class="right">RAM</th>
    <th class="right">Swap</th><th>Command</th></tr></thead>
  <tbody id="procs"><tr><td colspan="5" style="color:var(--muted)">Loading…</td></tr></tbody></table></div>
  <div class="dbform" style="margin-top:12px">
    <button id="clear-ram">Clear RAM caches</button>
    <span id="ram-msg" style="font-size:13px;color:var(--ink-2)"></span>
  </div>
</div>

</main>
<script>
const $ = s => document.querySelector(s);
const esc = t => (t??'').toString().replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const cls = p => p >= 92 ? 'crit' : p >= 80 ? 'warn' : '';
let dbState = null;

async function getJSON(url, opts){
  const r = await fetch(url, opts);
  if (r.status === 401){ location = '/login'; throw new Error('logged out'); }
  return r.json();
}

async function refreshRam(){
  try{
    const s = await getJSON('/api/ram');
    $('#t-avail').textContent = s.mem.available;
    $('#t-total').textContent = 'of ' + s.mem.total;
    $('#t-used').textContent = s.mem.used_pct + '%';
    $('#t-cache').textContent = s.mem.cached + ' is reclaimable cache';
    $('#t-swap').textContent = s.swap.total === '0 B' ? 'no swap' : s.swap.used;
    $('#t-swaptot').textContent = s.swap.total === '0 B' ? '' : s.swap.pct + '% of ' + s.swap.total;
    $('#t-swapio').textContent = s.swap.in + ' / ' + s.swap.out;
    $('#t-psi').textContent = s.psi === null ? 'n/a' : s.psi.toFixed(1) + '%';
    $('#alerts').innerHTML = s.alerts.length
      ? s.alerts.map(a => `<div class="alert ${a.level}">${esc(a.text)}</div>`).join('')
      : '<div class="alert ok">All clear: RAM, swap, containers and the database are healthy.</div>';
    const ce = s.containers.error;
    $('#c-err').style.display = ce ? '' : 'none'; if (ce) $('#c-err').textContent = ce;
    $('#containers').innerHTML = s.containers.rows.map(c => `<tr><td>${esc(c.name)}</td>
      <td class="right num">${esc(c.used_h)}</td><td class="right num">${esc(c.limit_h)}</td>
      <td>${c.pct === null ? '<span style="color:var(--muted)">no limit</span>'
        : `<div class="bar" title="${c.pct}%"><i class="${cls(c.pct)}" style="width:${Math.min(c.pct,100)}%"></i></div>`}</td>
      <td class="right num">${c.host_pct}%</td><td class="right num">${esc(c.cpu)}</td></tr>`).join('')
      || '<tr><td colspan="6" style="color:var(--muted)">No running containers</td></tr>';
    $('#procs').innerHTML = s.processes.map(p => `<tr><td class="right num">${p.pid}</td>
      <td>${esc(p.name)}</td><td class="right num">${esc(p.rss_h)}</td>
      <td class="right num">${p.swap ? esc(p.swap_h) : '–'}</td>
      <td><div class="cmd" title="${esc(p.cmd)}">${esc(p.cmd)}</div></td></tr>`).join('');
  }catch(e){ console.error(e); }
}

function showDb(d){
  dbState = d;
  $('#d-live').textContent = d.h.live;
  $('#d-conf').textContent = d.h.configured;
  $('#d-data').textContent = d.h.data;
  $('#d-rec').textContent = d.h.recommended;
  $('#d-miss').textContent = d.recent_miss_pct === null ? 'measuring…' : d.recent_miss_pct + '%';
  $('#d-miss-all').textContent = d.miss_pct === null ? '–' : d.miss_pct + '%';
  $('#d-perm').innerHTML = d.mounted ? '<span class="badge ok">yes</span>'
    : '<span class="badge miss">no — falls back to 128M</span>';
  $('#d-persist').style.display = d.mounted ? 'none' : '';
  $('#d-range').textContent = d.h.min + ' – ' + d.h.max;
  $('#d-avail').textContent = d.h.available;
  if (!$('#d-size').value) $('#d-size').placeholder = 'e.g. ' + d.h.recommended;
  if (d.error) dbMsg(d.error, true);
}
function dbMsg(t, err){ const m = $('#db-msg'); m.textContent = t || ''; m.className = err ? 'err' : (t ? 'okm' : ''); }

async function refreshDb(fresh){
  try{ showDb(await getJSON('/api/db-memory' + (fresh ? '?fresh=1' : ''))); }catch(e){ console.error(e); }
}

const toBytes = v => parseInt(v, 10) * (v.toUpperCase().endsWith('G') ? 1073741824 : 1048576);
async function applySize(size){
  const live = dbState && dbState.h.live;
  let warn = '';
  if (dbState && dbState.live && toBytes(size) - dbState.live > dbState.available)
    warn = `\n\nWARNING: this needs ${size} but only ${dbState.h.available} of RAM is free. `
      + 'The pool would push other programs into swap and make the server slower. '
      + 'Free RAM first (e.g. restart OpenWA), or pick a smaller size.';
  if (!confirm(`Change the database buffer pool from ${live} to ${size}?\n\nIt applies at once, with no restart.${warn}`)) return;
  dbMsg('Applying…');
  for (const b of ['#d-set','#d-userec']) $(b).disabled = true;
  try{
    const j = await getJSON('/api/db-memory', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({size})});
    if (j.error) dbMsg(j.error, true); else { dbMsg(j.message); showDb(j.state); $('#d-size').value = ''; }
  }catch(e){ dbMsg('Request failed', true); }
  for (const b of ['#d-set','#d-userec']) $(b).disabled = false;
  refreshRam();
}
$('#d-set').onclick = () => { const v = $('#d-size').value.trim().toUpperCase();
  if (!/^\d+[MG]$/.test(v)) { dbMsg('Enter a size like 2G or 1536M', true); return; } applySize(v); };
$('#d-size').onkeydown = e => { if (e.key === 'Enter') $('#d-set').click(); };
$('#d-userec').onclick = () => dbState && applySize(dbState.h.recommended);

$('#d-persist').onclick = async () => {
  if (!confirm('Make the buffer pool size permanent?\n\nThis adds the MariaDB settings mount to the '
    + 'compose file and RECREATES THE DATABASE CONTAINER. Every site loses its database for about '
    + '10-30 seconds. Do this when nobody is billing.')) return;
  const b = $('#d-persist'); b.disabled = true; dbMsg('Recreating the db container… (up to a minute)');
  try{
    const j = await getJSON('/api/db-memory/persist', {method:'POST'});
    if (j.error) dbMsg(j.error, true); else { dbMsg(j.message); showDb(j.state); }
  }catch(e){ dbMsg('Request failed', true); }
  b.disabled = false;
};

$('#clear-ram').onclick = async () => {
  if (!confirm('Clear RAM caches now? This is safe but may briefly slow the system while caches rebuild.')) return;
  $('#ram-msg').textContent = 'Clearing…';
  const j = await getJSON('/api/clear-ram', {method:'POST'});
  $('#ram-msg').textContent = j.error ? j.error : `Freed ${j.freed} — ${j.available} now available`;
  refreshRam();
};

setInterval(() => $('#clock').textContent = new Date().toLocaleString(), 1000);
refreshRam(); refreshDb(true);
setInterval(refreshRam, 10000);
setInterval(() => refreshDb(false), 30000);
</script></body></html>"""


if __name__ == "__main__":
    print(f"SSPL ERP Admin Panel v{PANEL_VERSION} — {FEATURES}", flush=True)
    cert = CONFIG.get("tls_cert")
    key = CONFIG.get("tls_key")
    ssl_ctx = None
    if cert and key and os.path.isfile(cert) and os.path.isfile(key):
        ssl_ctx = (cert, key)
        app.config["SESSION_COOKIE_SECURE"] = True
    app.run(host="0.0.0.0", port=int(CONFIG.get("port", 8090)),
            threaded=True, ssl_context=ssl_ctx)
