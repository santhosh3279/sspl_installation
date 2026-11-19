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

import json
import os
import pty
import re
import shutil
import subprocess
import termios
import threading
import time
import zipfile
from datetime import datetime
from functools import wraps
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
PANEL_VERSION = "2026-11-19.mt1"
FEATURES = ("Multi-tenant panel: sites list with per-site backup, DB backup, "
            "migrate, uninstall app and drop; Create site with app picker; "
            "per-site backup browser and guarded per-site restore (also from "
            "uploads and other sites); all-site backup, verify, migrate, update "
            "and rollback; stack + tools installer; rclone destinations in "
            "sspl-mt.conf; multi-tenant cron schedule; one-click panel and "
            "tools update from git")

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
    "IMAGE": "ghcr.io/santhosh3279/sspl-erpnext",
    "IMAGE_TAG": "latest",
    "BACKUP_ROOT": "/opt/backups/frappe",
    "RETENTION_DAYS": "30",
    "DB_RETENTION_DAYS": "14",
    "LOCAL_KEEP_MIN": "20",
    "CLOUD_KEEP": "10",
    "RCLONE_REMOTE": "",
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
}

# Component installers, run from the repo checkout. Only wired up when
# repo_dir is configured; the suite also checks each script is on disk.
STACK_SETUP_SCRIPT = _repo("multi-tenant/install/panel-install.sh")
TOOLS_SETUP_SCRIPT = _repo("multi-tenant/install/install-tools.sh")
RCLONE_SETUP_SCRIPT = _repo("Backup/frappe_backup_system/install_rclone.sh")
ADGUARD_SETUP_SCRIPT = _repo("Production Installation/install_adguard.sh")
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
    "install_adguard": {"label": "Install AdGuard Home",          "cmd": ["bash", ADGUARD_SETUP_SCRIPT or ""]},
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

# Ports this server already uses: SSH, AdGuard DNS + dashboard, OpenWA, this
# panel.
RESERVED_PORTS = {22, 53, 2785, 3000, 8090}

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
        out.append({"name": s, "url": site_url(s), "is_ip": bool(IPV4_RE.match(s)),
                    "host_name": cfg.get("host_name"),
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


def is_adguard_running():
    try:
        out = subprocess.run(["systemctl", "is-active", "AdGuardHome"], capture_output=True, text=True, timeout=5)
        return out.stdout.strip() == "active"
    except (OSError, subprocess.SubprocessError):
        return False


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
            "adguard": {
                "installed": os.path.isfile("/opt/AdGuardHome/AdGuardHome"),
                "running": is_adguard_running(),
            },
            "openwa": openwa_status(),
        },
        "scripts": {
            "rclone": _script_ok(RCLONE_SETUP_SCRIPT),
            "adguard": _script_ok(ADGUARD_SETUP_SCRIPT),
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
        port = str(data.get("http_port", "80")).strip() or "80"
        image = str(data.get("image", "")).strip() or erp_image()
        db_pw = str(data.get("db_password", ""))
        if os.path.isfile(compose_file()):
            return None, None, "a stack is already installed on this server"
        if not (port.isdigit() and 1 <= int(port) <= 65535):
            return None, None, "HTTP port must be a number between 1 and 65535"
        if int(port) in RESERVED_PORTS:
            return None, None, f"port {port} is already used on this server (SSH, AdGuard, OpenWA or this panel)"
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
        return {"HTTP_PORT": port, "IMAGE": image, "DB_PASSWORD": db_pw,
                "SSPL_INSTALL_CRON": "yes" if data.get("schedule_cron", True) else "no"}, None, None

    if name == "install_tools":
        return {}, ["--cron" if data.get("schedule_cron", True) else "--no-cron"], None

    if name in ("install_rclone", "install_adguard"):
        return {}, None, None
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
                                     "run 'sudo rclone config' over SSH first"}), 400
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
                                         "configure it with 'sudo rclone config' over SSH"}), 400
            if n not in seen:
                seen.append(n)
        target = " ".join(n + path for n in seen)

    err = _set_conf_value("RCLONE_REMOTE", target)
    if err:
        return jsonify({"error": err}), 500
    return jsonify({"ok": True, "wired": target})


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
/* The panel updater sits in the top bar beside the suite link, so it reads as
   chrome rather than as one of the server actions further down the page. */
.top .nav-act{font-size:13px;padding:7px 12px}
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
  if(r.installed && !r.configured){
    h += `<div class="note">Run <code>sudo rclone config</code> over SSH — see the setup guide below.</div>`;
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
    <div class="why">Step 2 is not a button. Connecting the account is an OAuth flow that
    needs a browser, and on the way through <code>rclone config</code> prints the account's
    long-lived token. Run as a panel job that token would be written to the job log and shown
    in the terminal to anyone signed in here — so it is done over SSH instead.</div>
    <ol>
      <li>SSH to the server and run <code>sudo rclone config</code>.
        <b>Use sudo</b> — backups run as root, and rclone only sees the config of the user
        that created it.</li>
      <li><code>n</code> for a new remote, name it <code>gdrive</code>, choose storage
        <code>drive</code> (Google Drive).</li>
      <li>Press Enter through client_id / client_secret, choose scope <code>1</code>
        (full access), and answer <code>n</code> to advanced config.</li>
      <li><b>Answer <code>n</code> to "Use auto config?"</b> — this server has no browser.</li>
      <li>rclone prints a command to run on your own computer
        (<code>rclone authorize "drive"</code>). Run it there, sign in to Google, then paste
        the result back into the SSH session.</li>
      <li>Confirm with <code>y</code>, then <code>q</code> to quit.</li>
      <li>Come back here — the remote appears at step 2, and step 3 points every site's
        backups at it. New sites are included automatically.</li>
    </ol>
    ${s.guide_ok ? '<p><a href="/guide/rclone">Download the full guide (.docx)</a> — also covers '
      + 'AWS S3, Dropbox, testing a remote, and encrypting the rclone config.</p>' : ''}
  </details>`;
  return h + `</div>`;
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
        <input type="text" id="erp-port" placeholder="HTTP port" value="80" size="6" title="every site is served on this port, picked by hostname">
        <input type="text" id="erp-image" placeholder="Docker image" value="${esc(s.erp_image||'')}" size="34"
          title="ERPNext Docker image (registry/owner/name, no tag). One image serves every site.">
        <input type="password" id="erp-db" placeholder="MariaDB root password (new)" size="22">
        <label style="font-size:13px"><input type="checkbox" id="erp-cron" checked> schedule backups for all sites</label>
        <button class="primary setup-install" data-inst="install_stack"
          data-confirm="Install the multi-tenant ERPNext stack now? This pulls the image, starts the containers and installs the tools to ${esc(s.tools_dir)} — 5-15 minutes. No site is created: add sites from the dashboard afterwards.">Install stack</button></div>
        <div class="note">Creates the stack only. Then use <b>Create site</b> on the <a href="/">dashboard</a>
          for each tenant — each gets its own hostname, database and Administrator password.</div>`;
    } else {
      const sites = c.erp.sites;
      rows += sites.length
        ? `<div class="note">Sites: ${sites.map(x => `<code>${esc(x)}</code>`).join(' ')} — manage them on the <a href="/">dashboard</a>.</div>`
        : `<div class="note">No sites yet — create the first one on the <a href="/">dashboard</a>.</div>`;
      rows += `<div class="note">Changing the HTTP port is not available on a multi-tenant stack from here yet
        (every site's <code>host_name</code> and the compose file must change together).</div>`;
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
    // AdGuard Home DNS — handy here: every tenant hostname needs a DNS rewrite
    rows += `<div class="setup-row"><div class="setup-h"><b>AdGuard Home DNS</b> ${pill(c.adguard.installed)}`
      + (c.adguard.installed && c.adguard.running ? ' <span class="badge ok">running</span>' : '') + `</div>`;
    if(!c.adguard.installed){
      rows += s.scripts.adguard ? `<div class="setup-form">
        <button class="primary setup-install" data-inst="install_adguard"
          data-confirm="Install AdGuard Home DNS service now? This will configure systemd-resolved to disable port 53 conflicts and install AdGuard Home.">Install AdGuard Home</button></div>` : '';
      rows += `<div class="note">Every site's hostname must resolve to this server. AdGuard Home can do
        that for the whole LAN with one DNS rewrite per site.</div>`;
    } else {
      const ag = 'http://' + (s.server_ip || location.hostname) + ':3000';
      rows += `<div class="note">Add a DNS rewrite per site (<i>Filters → DNS rewrites</i>: site → this server's IP):
        <a href="${esc(ag)}" target="_blank">${esc(ag)}</a></div>`;
    }
    rows += `</div>`;
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
    $('#setup-body').innerHTML =
      (erpDone && toolsOk ? '<p style="color:var(--ok);margin:0 0 6px">✓ Stack and tools are installed. ' +
                 'Create and run sites from the <a href="/">dashboard</a>.</p>' : '') + rows;
    document.querySelectorAll('.setup-install').forEach(btn => btn.onclick = () => installComponent(btn));
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

async function installComponent(btn){
  if(btn.disabled) return;
  if(btn.dataset.confirm && !confirm(btn.dataset.confirm)) return;
  const inst = btn.dataset.inst, body = {};
  if(inst==='install_stack'){
    body.http_port = $('#erp-port').value.trim();
    body.image = $('#erp-image').value.trim();
    body.db_password = $('#erp-db').value;
    body.schedule_cron = $('#erp-cron').checked;
  } else if(inst==='install_tools'){
    body.schedule_cron = btn.dataset.cron === '1';
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
</style></head><body>
<div class="top">
  <h1>SSPL ERP Admin</h1><span class="badge">multi-tenant</span><span id="clock" class="badge"></span>
  <span class="badge" title="panel code version — restart the service after updating">v{{ version }}</span>
  <a class="nav" href="/install">ERP Next Installation suite →</a>
  {% if repo_ok %}
  <button class="act nav-act" data-act="update_panel"
    title="git pull the checkout, refresh the multi-tenant tools, then deploy this panel from it"
    data-confirm="Pull the latest code, refresh the multi-tenant tools and update this panel now?&#10;&#10;Settings, cron and site data are not touched. The panel restarts itself a few seconds after the job finishes — reload the page then.">Update panel &amp; tools</button>
  {% endif %}
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
    server: a DNS rewrite in AdGuard Home / the router, or a hosts-file line on each PC.</p>
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

<div class="modal" id="ap-modal"><div class="card">
  <h1 style="font-size:17px;margin-bottom:10px">Apps on <span id="ap-site"></span></h1>
  <div id="ap-body" style="font-size:13px">Reading installed apps…</div>
  <div class="row">
    <button id="ap-close">Close</button>
  </div>
</div></div>
<script>
""" + TERM_JS + r"""
let SITES = [], IMAGE_APPS = [];

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
  return `<tr><td><a href="${esc(s.url)}" target="_blank"><b>${n}</b></a> ${state}${skip}</td>
    <td>${bk}</td>
    <td><div class="acts">
      <button class="act" data-act="backup" data-site="${n}">Backup</button>
      <button class="act" data-act="db_backup" data-site="${n}">DB backup</button>
      <button class="act" data-act="migrate" data-site="${n}"
        data-confirm="Run database migrations on ${n}?">Migrate</button>
      <button class="site-apps" data-site="${n}">Apps…</button>
      <button class="site-bk" data-site="${n}" title="Show this site's backups below">Backups</button>
      <button class="danger act-drop" data-site="${n}">Drop</button>
    </div></td></tr>`;
}

async function refreshSites(){
  try{
    const r = await fetch('/api/sites'); if(r.status===401){location='/login';return;}
    const j = await r.json();
    SITES = j.sites.map(s => s.name);
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
