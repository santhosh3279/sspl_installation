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

from flask import (Flask, Response, abort, jsonify, redirect, render_template,
                   request, send_file, session, url_for)
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename

# Shown in the panel header and printed to the journal at startup, so you can
# tell at a glance whether the code running on the server is the code you
# think it is. Copying the files is not enough — the service must be restarted
# for a new version to take effect. Bump this whenever app.py gains something
# visible; FEATURES lists what that version should show.
PANEL_VERSION = "2026-11-24.mt16"
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
            "WireGuard page: tunnel to the CUPS print servers (install, settings, add, remove, test, setup script; built with Vue)")

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


# root_path is explicit: dev.py loads this file by path, where Flask would guess
# the current folder and not find templates/ and static/.
app = Flask(__name__, root_path=os.path.dirname(os.path.abspath(__file__)))
app.secret_key = CONFIG["secret_key"]
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    MAX_CONTENT_LENGTH=32 * 1024 ** 3,
)


def vue_page(name):
    """A page built by Vite (frontend/, output committed in static/app/). The
    HTML names hashed files, so it must never be cached itself."""
    html = (Path(app.root_path) / "static" / "app" / f"{name}.html").read_text(encoding="utf-8")
    return Response(html, mimetype="text/html", headers={"Cache-Control": "no-store"})


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
    return render_template("ram.html", user=session["user"], version=PANEL_VERSION)


@app.route("/auth")
@login_required
def auth_page():
    return render_template("auth.html", user=session["user"], version=PANEL_VERSION)


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
    return vue_page("wireguard")


@app.route("/api/me")
@login_required
def api_me():
    return jsonify({"user": session["user"], "version": PANEL_VERSION})


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
    return render_template("login.html", error=error, version=PANEL_VERSION)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    # Gate the updater on the script being on disk, not just repo_dir being
    # set: an older checkout without it would render a button that fails.
    return render_template(
        "dashboard.html", user=session["user"], version=PANEL_VERSION,
        repo_ok=REPO_DIR is not None and _script_ok(UPDATE_PANEL_SCRIPT),
        tools_ok=os.path.isfile(_tool("lib/common.sh")))


@app.route("/install")
@login_required
def install_suite():
    return render_template("install.html", user=session["user"], version=PANEL_VERSION)


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
