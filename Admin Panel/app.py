#!/usr/bin/env python3
"""SSPL ERP Admin Panel.

Small LAN-only web interface to run the v2 backup / update / rollback
scripts, browse backup files, watch server health, clear RAM caches and
upload backup files to the server.

Configuration comes from /opt/sspl-admin/config.json (override the path
with the SSPL_ADMIN_CONFIG environment variable). Created by
setup_admin_panel.sh.
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
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import (Flask, abort, jsonify, redirect, render_template_string,
                   request, send_file, session, url_for)
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename

# Shown in the panel header and printed to the journal at startup, so you can
# tell at a glance whether the code running on the server is the code you
# think it is. Copying app.py is not enough — the service must be restarted
# for a new version to take effect. Bump this whenever app.py gains something
# visible; FEATURES lists what that version should show.
PANEL_VERSION = "2026-07-16.1"
FEATURES = ("ERP Next Installation suite page, console-style terminal, "
            "guarded restore, delete uploads")

CONFIG_FILE = os.environ.get("SSPL_ADMIN_CONFIG", "/opt/sspl-admin/config.json")
with open(CONFIG_FILE) as f:
    CONFIG = json.load(f)

# Paths are overridable in config.json for testing / non-standard layouts
BACKUP_DIR = Path(CONFIG.get("backup_dir", "/opt/backups/frappe"))
DB_ONLY_DIR = BACKUP_DIR / "db-only"
UPLOAD_DIR = BACKUP_DIR / "uploads"
IMAGE_BACKUP_DIR = Path(CONFIG.get("image_backup_dir", "/opt/sspl-erp/image-backups"))
COMPOSE_FILE = CONFIG.get("compose_file", "/opt/sspl-erp/docker-compose.yml")
ERP_DIR = CONFIG.get("erp_dir", "/opt/sspl-erp")
SCRIPTS_DIR = CONFIG.get("scripts_dir", "/opt/scripts/v2")
UPDATE_DIR = CONFIG.get("update_dir", "/opt/sspl-erp/v2")
JOB_DIR = Path(CONFIG.get("job_dir", "/opt/sspl-admin/jobs"))

# Where this repo is checked out on the server, recorded by
# setup_admin_panel.sh. The "Install" switches run the installers from here,
# so `git pull` then a panel restart picks up newer installers.
REPO_DIR = CONFIG.get("repo_dir")


def _repo(rel):
    return os.path.join(REPO_DIR, rel) if REPO_DIR else None


ACTIONS = {
    "backup":    {"label": "Full backup",   "cmd": [f"{SCRIPTS_DIR}/frappe_backup.sh"]},
    "db_backup": {"label": "DB-only backup", "cmd": [f"{SCRIPTS_DIR}/frappe_db_backup.sh"]},
    "verify":    {"label": "Verify backups", "cmd": [f"{SCRIPTS_DIR}/frappe_backup_verify.sh"]},
    "update":    {"label": "System update",  "cmd": [f"{UPDATE_DIR}/sspl-erp-update-with-rollback.sh"]},
    "rollback":  {"label": "Rollback",       "cmd": [f"{UPDATE_DIR}/sspl-erp-rollback.sh"], "stdin": "yes\n"},
    # Restore takes a safety backup first (see restore_with_backup.sh) and is
    # the one interactive action: the MariaDB root password and the final
    # confirmation are typed into the job's terminal, so they never become
    # arguments, env vars, or log lines. Guarded by _restore_request().
    "restore":   {"label": "Restore from backup",
                  "cmd": [f"{SCRIPTS_DIR}/restore_with_backup.sh"], "interactive": True},
}

# Component installers, driven from the panel's Setup switches. Only wired
# up when repo_dir is configured and the scripts are present on disk.
ERP_STACK_SCRIPT = _repo("Production Installation/install_erp_stack.sh")
BACKUP_SETUP_SCRIPT = _repo("Backup/frappe_backup_system/setup_frappe_backups.sh")
UPDATE_SETUP_SCRIPT = _repo("Production Installation/update and rollback/install_update_rollback.sh")

INSTALL_ACTIONS = {
    "install_erp":     {"label": "Install ERPNext stack",   "cmd": ["bash", ERP_STACK_SCRIPT or ""]},
    "install_backups": {"label": "Install backup system",   "cmd": ["bash", BACKUP_SETUP_SCRIPT or ""],
                        "cwd": _repo("Backup/frappe_backup_system")},
    "install_update":  {"label": "Install update/rollback", "cmd": ["bash", UPDATE_SETUP_SCRIPT or ""]},
}
if REPO_DIR:
    ACTIONS.update(INSTALL_ACTIONS)

IP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,60}$")

UPLOAD_EXTENSIONS = (".sql.gz", ".gz", ".tar", ".tgz", ".json", ".yml", ".yaml")
FULL_BACKUP_RE = re.compile(r"^20\d{6}_\d{6}$")
SNAPSHOT_RE = re.compile(r"^backup_\d{8}_\d{6}\.tar$")
SAFE_NAME_RE = re.compile(r"^[\w][\w.\-]*$")
SAFE_FOLDER_RE = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")

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


def start_job(name, extra_env=None, extra_args=None):
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
        env = {**os.environ, **(extra_env or {})}
        cmd = list(action["cmd"]) + [str(a) for a in (extra_args or [])]
        stdin_text = action.get("stdin")
        master = None
        try:
            if action.get("interactive"):
                # A real pty, not a pipe: shell scripts only print `read -p`
                # prompts when stdin is a terminal.
                master, slave = pty.openpty()
                # Echo OFF for the whole job, not just during `read -s`.
                # A pty echoes input back into the output stream — i.e. into
                # this job's log. Bash only turns that off while `read -s` is
                # waiting, so a password typed at any other moment (early,
                # pasted, type-ahead during the safety backup) would be logged
                # in clear, and the restore would still succeed, hiding it.
                # The user reads what they type in the browser input box; the
                # log never needs it.
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
        _job = {"name": name, "label": action["label"], "logfile": str(logfile),
                "proc": proc, "started": time.time(), "rc": None, "finished": None,
                "pty": master}
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
            "started": datetime.fromtimestamp(_job["started"]).strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed": int((_job["finished"] or time.time()) - _job["started"]),
            "log": log,
        }


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
            ["docker", "compose", "-f", COMPOSE_FILE, "ps", "--format", "json"],
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


# ---------------------------------------------------------------- setup / bootstrap

def deployed_site_name():
    """Read SITE_NAME from the deployed frappe_docker/.env, if present."""
    envp = os.path.join(ERP_DIR, "frappe_docker", ".env")
    try:
        with open(envp) as f:
            for line in f:
                if line.startswith("SITE_NAME="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None


def first_ip():
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=5)
        parts = out.stdout.split()
        return parts[0] if parts else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def setup_status():
    """What is installed on this server, to drive the Setup switches."""
    cs = container_status()
    running = any(str(s.get("state", "")).lower().startswith(("running", "up"))
                  for s in cs["services"])
    scripts_ok = bool(REPO_DIR) and bool(ERP_STACK_SCRIPT) and os.path.isfile(ERP_STACK_SCRIPT)
    return {
        "repo_dir": REPO_DIR,
        "repo_ok": scripts_ok,
        "server_ip": CONFIG.get("server_ip") or first_ip(),
        "components": {
            "erp": {
                "installed": os.path.isfile(COMPOSE_FILE),
                "running": running,
                "site": deployed_site_name(),
            },
            "backups": {"installed": os.path.isfile(os.path.join(SCRIPTS_DIR, "frappe_backup.sh"))},
            "update":  {"installed": os.path.isfile(os.path.join(UPDATE_DIR, "sspl-erp-common.sh"))},
        },
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


def backups_overview():
    full = []
    if BACKUP_DIR.is_dir():
        for d in sorted(BACKUP_DIR.iterdir(), reverse=True):
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
            })
    images, latest = [], None
    if IMAGE_BACKUP_DIR.is_dir():
        try:
            latest = Path((IMAGE_BACKUP_DIR / "latest_backup.txt").read_text().strip()).name
        except OSError:
            pass
        for it in _dir_listing(IMAGE_BACKUP_DIR, SNAPSHOT_RE):
            it["latest"] = it["name"] == latest
            images.append(it)
        images.sort(key=lambda x: x["name"], reverse=True)
    uploads, upload_folders = [], []
    if UPLOAD_DIR.is_dir():
        for entry in sorted(UPLOAD_DIR.iterdir()):
            if entry.is_file():
                uploads.extend(_dir_listing(UPLOAD_DIR, re.compile(re.escape(entry.name) + "$")))
            elif entry.is_dir() and SAFE_FOLDER_RE.match(entry.name):
                for it in _dir_listing(entry):
                    it["name"] = f"{entry.name}/{it['name']}"
                    uploads.append(it)
                # Only a folder holding a database dump can be restored from.
                # Loose files in uploads/ are not offered: restoring a whole
                # directory of unrelated uploads would mix backups together.
                if any(entry.glob("*-database.sql.gz")):
                    upload_folders.append(entry.name)
    dbonly = _dir_listing(DB_ONLY_DIR)
    dbonly.sort(key=lambda x: x["name"], reverse=True)
    return {"full": full, "db_only": dbonly, "images": images,
            "uploads": uploads, "upload_folders": upload_folders}


def _restore_source(kind, name):
    """Resolve a restore-from folder inside the backup roots.
    Returns (Path, None) or (None, error). Only whole backup folders are
    restorable — a full-backup timestamp folder or an upload subfolder."""
    roots = {"full": BACKUP_DIR, "upload": UPLOAD_DIR}
    root = roots.get(kind)
    if root is None:
        return None, "restore source must be a full backup or an upload folder"
    if kind == "full" and not FULL_BACKUP_RE.match(name):
        return None, "invalid backup name"
    if kind == "upload" and not SAFE_FOLDER_RE.match(name):
        return None, "invalid upload folder name"
    target = (root / name).resolve()
    if not str(target).startswith(str(root.resolve()) + os.sep) or not target.is_dir():
        return None, "backup folder not found"
    if not any(target.glob("*-database.sql.gz")):
        return None, "no *-database.sql.gz in that folder — nothing to restore from"
    return target, None


def _restore_request(data):
    """Validate a restore request. Returns (extra_env, extra_args, error, status).

    Three gates, all server-side: the panel admin password is re-entered, the
    live site name is typed out in full, and the source resolves to a real
    backup folder inside the backup roots."""
    site = deployed_site_name()
    if not site:
        return None, None, "no deployed site found — nothing to restore into", 400
    if not check_password_hash(CONFIG["password_hash"], str(data.get("admin_password", ""))):
        return None, None, "admin password is incorrect", 403
    if str(data.get("confirm_site", "")).strip() != site:
        return None, None, f"type the site name exactly — {site} — to confirm", 400
    src, err = _restore_source(str(data.get("kind", "")), str(data.get("name", "")))
    if err:
        return None, None, err, 400
    return {"SSPL_SITE_NAME": site, "SSPL_SCRIPTS_DIR": SCRIPTS_DIR}, [str(src)], None, None


def _safe_upload_target(name):
    """Resolve an upload file or folder for deletion.

    Deletion is confined to the uploads/ tree: real backups, db-only dumps
    and image snapshots are not reachable through this. Accepts 'file',
    'folder', or 'folder/file'."""
    parts = [p for p in name.split("/") if p]
    if not 1 <= len(parts) <= 2:
        return None, "invalid name"
    if len(parts) == 2:
        if not SAFE_FOLDER_RE.match(parts[0]) or not SAFE_NAME_RE.match(parts[1]):
            return None, "invalid name"
    elif not (SAFE_FOLDER_RE.match(parts[0]) or SAFE_NAME_RE.match(parts[0])):
        return None, "invalid name"
    root = UPLOAD_DIR.resolve()
    target = (UPLOAD_DIR / "/".join(parts)).resolve()
    if not str(target).startswith(str(root) + os.sep):
        return None, "outside the uploads folder"
    if not target.exists():
        return None, "not found — already deleted?"
    return target, None


def _safe_download_path(kind, name):
    roots = {"full": BACKUP_DIR, "db": DB_ONLY_DIR, "image": IMAGE_BACKUP_DIR, "upload": UPLOAD_DIR}
    root = roots.get(kind)
    if root is None:
        abort(400)
    parts = name.split("/")
    if kind == "full":
        if len(parts) != 2 or not FULL_BACKUP_RE.match(parts[0]) or not SAFE_NAME_RE.match(parts[1]):
            abort(400)
    elif kind == "upload" and len(parts) == 2:
        if not SAFE_FOLDER_RE.match(parts[0]) or not SAFE_NAME_RE.match(parts[1]):
            abort(400)
    elif len(parts) != 1 or not SAFE_NAME_RE.match(parts[0]):
        abort(400)
    target = (root / name).resolve()
    if not str(target).startswith(str(root.resolve()) + os.sep) or not target.is_file():
        abort(404)
    return target


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
    return render_template_string(DASH_HTML, user=session["user"], version=PANEL_VERSION)


@app.route("/install")
@login_required
def install_suite():
    return render_template_string(INSTALL_HTML, user=session["user"], version=PANEL_VERSION)


@app.route("/api/stats")
@login_required
def api_stats():
    return jsonify(system_stats())


@app.route("/api/backups")
@login_required
def api_backups():
    return jsonify(backups_overview())


@app.route("/api/job")
@login_required
def api_job():
    return jsonify(job_status())


@app.route("/api/setup-status")
@login_required
def api_setup_status():
    return jsonify(setup_status())


@app.route("/api/run/<name>", methods=["POST"])
@login_required
def api_run(name):
    if name not in ACTIONS:
        abort(404)
    data = request.get_json(silent=True) or {}
    extra_env = None
    extra_args = None

    if name == "restore":
        extra_env, extra_args, err, status = _restore_request(data)
        if err:
            return jsonify({"error": err}), status

    elif name == "rollback":
        snap = data.get("snapshot", "")
        if snap:
            if not SNAPSHOT_RE.match(snap):
                return jsonify({"error": "invalid snapshot name"}), 400
            extra_env = {"BACKUP_FILE": str(IMAGE_BACKUP_DIR / snap)}

    elif name in INSTALL_ACTIONS:
        if not REPO_DIR:
            return jsonify({"error": "repo_dir is not configured on this server"}), 400
        env, err = _install_env(name, data)
        if err:
            return jsonify({"error": err}), 400
        extra_env = env

    job, err = start_job(name, extra_env, extra_args)
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
    """Build (and validate) the environment for an install action.
    Returns (env_dict, None) or (None, error_message). Secrets stay in the
    returned dict and are passed to the child as env vars — never logged."""
    if name == "install_erp":
        ip = str(data.get("server_ip", "")).strip()
        port = str(data.get("http_port", "80")).strip() or "80"
        db_pw = str(data.get("db_password", ""))
        admin_pw = str(data.get("admin_password", ""))
        if not IP_RE.match(ip):
            return None, "enter a valid server IP / hostname"
        if not (port.isdigit() and 1 <= int(port) <= 65535):
            return None, "HTTP port must be a number between 1 and 65535"
        if not db_pw or not admin_pw:
            return None, "database root password and admin password are both required"
        return {"SERVER_IP": ip, "HTTP_PORT": port,
                "DB_PASSWORD": db_pw, "ADMIN_PASSWORD": admin_pw,
                "SSPL_ERP_DIR": ERP_DIR}, None

    # backups / update: derive the site name from the deployed ERP stack
    site = deployed_site_name()
    if not site:
        return None, "install the ERPNext stack first (no deployed site found)"
    if name == "install_backups":
        schedule = data.get("schedule_cron", True)
        return {"SSPL_SITE_NAME": site, "SSPL_RUN_TEST": "no",
                "SSPL_INSTALL_CRON": "yes" if schedule else "no"}, None
    return {"SERVER_IP": site, "SSPL_ERP_DIR": ERP_DIR}, None  # install_update


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


@app.route("/upload", methods=["POST"])
@login_required
def upload():
    folder = request.form.get("folder", "").strip()
    dest = UPLOAD_DIR
    if folder:
        if not SAFE_FOLDER_RE.match(folder):
            return jsonify({"error": "folder name may only use letters, digits, - and _"}), 400
        dest = UPLOAD_DIR / folder
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


@app.route("/api/uploads/delete", methods=["POST"])
@login_required
def api_uploads_delete():
    """Delete an uploaded file or folder. Uploads only — never a backup."""
    data = request.get_json(silent=True) or {}
    target, err = _safe_upload_target(str(data.get("name", "")))
    if err:
        return jsonify({"error": err}), 400
    # Don't delete the folder a restore is reading from underneath it.
    with _job_lock:
        if _job and _job["name"] == "restore" and _job["proc"].poll() is None:
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
  /* a definite height so the log scrolls inside it rather than growing the
     page forever; drag the bottom edge to resize, as you would a terminal */
  height:min(60vh,520px);min-height:220px;resize:vertical;overflow:hidden}
#console{color:#ddd;font:12.5px/1.45 ui-monospace,Menlo,Consolas,monospace;
  white-space:pre-wrap;overflow-wrap:anywhere;overflow:auto;flex:1;min-height:0}
#jobstate{font-size:13px;color:var(--ink-2);margin-bottom:8px}
#jobstate .live{color:var(--accent);font-weight:600}
#jobstate .okrc{color:var(--ok);font-weight:600} #jobstate .badrc{color:var(--crit);font-weight:600}
/* terminal input line — only shown while an interactive job is waiting.
   Borderless and transparent so typing happens *in* the terminal, not in a
   box below it; Enter submits, the way a console does. */
.term-in{display:none;gap:8px;align-items:baseline;flex:none}
.term-in.on{display:flex}
.term-in input{flex:1;min-width:0;font:12.5px/1.45 ui-monospace,Menlo,Consolas,monospace;
  background:transparent;color:#ddd;border:0;outline:none;padding:0;caret-color:#ddd}
.term-in input::placeholder{color:#555}
.term-in .ps1{color:var(--accent);font:12.5px/1.45 ui-monospace,monospace}
"""

TERM_HTML = """
<aside class="col-right">
<div class="card" id="job-card"><h2>Terminal — live output</h2>
  <div id="jobstate">No job has been run yet.</div>
  <div class="term" id="term">
    <div id="console"></div>
    <div class="term-in" id="term-in">
      <span class="ps1">&gt;</span>
      <input id="term-line" placeholder="type here, then press Enter" autocomplete="off"
             autocapitalize="off" autocorrect="off" spellcheck="false">
    </div>
  </div>
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
        + ` after ${j.elapsed}s (started ${esc(j.started)})`;
    $('#jobstate').innerHTML = st;
    const c = $('#console'), atEnd = c.scrollTop + c.clientHeight >= c.scrollHeight - 30;
    c.textContent = termText(j.log || '') || '(no output yet)';
    if (atEnd) c.scrollTop = c.scrollHeight;
    if (jobWasActive && !j.active) onJobFinished();
    jobWasActive = j.active;
  }catch(e){}
}

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
<h1>SSPL ERP Admin</h1>
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
"""

INSTALL_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ERP Next Installation suite</title><style>""" + BASE_CSS + LAYOUT_CSS + \
    TERM_CSS + SETUP_CSS + r"""
.lead{font-size:13px;color:var(--ink-2);margin:0 0 12px}
</style></head><body>
<div class="top">
  <h1>ERP Next Installation suite</h1><span id="clock" class="badge"></span>
  <span class="badge" title="panel code version — restart the service after updating">v{{ version }}</span>
  <a class="nav" href="/">← Dashboard</a>
  <span class="spacer"></span>
  <span style="color:var(--muted);font-size:13px">{{ user }}</span>
  <form method="post" action="/logout" style="margin:0"><button>Log out</button></form>
</div>
<main>
<div class="col-left">

<div class="card" id="setup-card"><h2>Install components</h2>
  <p class="lead">Install the ERPNext stack and its tooling on this server. Each
  install runs as a job and reports into the terminal below — leave the page open
  until it finishes.</p>
  <div id="setup-body"><p style="color:var(--muted)">Loading…</p></div>
</div>

</div><!-- /col-left -->
""" + TERM_HTML + r"""
</main>
<script>
""" + TERM_JS + r"""
function pill(ok){ return ok ? '<span class="badge ok">installed ✓</span>'
                             : '<span class="badge miss">not installed</span>'; }

async function refreshSetup(){
  try{
    const r = await fetch('/api/setup-status'); if(r.status===401){location='/login';return;}
    const s = await r.json();
    if(!s.repo_ok){
      $('#setup-body').innerHTML = `<p style="color:var(--warn);margin:0">Installer scripts not found
        (${s.repo_dir?('repo_dir = <code>'+esc(s.repo_dir)+'</code>'):'repo_dir is not set in config.json'}).
        Component installs are unavailable. Clone the repo on this server, set <code>repo_dir</code> in
        <code>/opt/sspl-admin/config.json</code>, then restart the panel.</p>`;
      return;
    }
    const c = s.components, erpDone = c.erp.installed;
    let rows = '';
    // ERPNext stack
    rows += `<div class="setup-row"><div class="setup-h"><b>ERPNext stack</b> ${pill(c.erp.installed)}`
      + (c.erp.installed && c.erp.running ? ' <span class="badge ok">running</span>' : '')
      + (c.erp.site ? ` <span class="badge">site ${esc(c.erp.site)}</span>` : '') + `</div>`;
    if(!c.erp.installed){
      rows += `<div class="setup-form">
        <input type="text" id="erp-ip" placeholder="Server IP / hostname" value="${esc(s.server_ip||'')}" size="18">
        <input type="text" id="erp-port" placeholder="HTTP port" value="80" size="6">
        <input type="password" id="erp-db" placeholder="MariaDB root password" size="20">
        <input type="password" id="erp-admin" placeholder="Administrator password" size="20">
        <button class="primary setup-install" data-inst="install_erp"
          data-confirm="Install the ERPNext stack now? This pulls the image, starts the containers and creates the site — 10-20 minutes. Do not close the page.">Install ERPNext</button></div>`;
    }
    rows += `</div>`;
    // Backup system
    rows += `<div class="setup-row"><div class="setup-h"><b>Backup system</b> ${pill(c.backups.installed)}</div>`;
    if(!c.backups.installed){
      rows += `<div class="setup-form">
        <label style="font-size:13px"><input type="checkbox" id="bk-cron" checked> also schedule daily cron backups</label>
        <button class="primary setup-install" data-inst="install_backups" ${erpDone?'':'disabled'}
          data-confirm="Install the backup system now?">Install backups</button>`
        + (erpDone?'':'<span style="color:var(--muted);font-size:12px">install ERPNext first</span>') + `</div>`;
    }
    rows += `</div>`;
    // Update / rollback
    rows += `<div class="setup-row"><div class="setup-h"><b>Update &amp; rollback scripts</b> ${pill(c.update.installed)}</div>`;
    if(!c.update.installed){
      rows += `<div class="setup-form">
        <button class="primary setup-install" data-inst="install_update" ${erpDone?'':'disabled'}
          data-confirm="Install the update/rollback scripts now?">Install update/rollback</button>`
        + (erpDone?'':'<span style="color:var(--muted);font-size:12px">install ERPNext first</span>') + `</div>`;
    }
    rows += `</div>`;
    const allDone = erpDone && c.backups.installed && c.update.installed;
    $('#setup-body').innerHTML =
      (allDone ? '<p style="color:var(--ok);margin:0 0 6px">✓ All components are installed. ' +
                 'Run the server from the <a href="/">dashboard</a>.</p>' : '') + rows;
    document.querySelectorAll('.setup-install').forEach(btn => btn.onclick = () => installComponent(btn));
    // A job started from the dashboard (or a second browser tab) still owns the
    // panel: re-disable the buttons the render above has just recreated.
    if (jobWasActive) document.querySelectorAll('.setup-install').forEach(b => b.disabled = true);
  }catch(e){}
}

async function installComponent(btn){
  if(btn.disabled) return;
  if(btn.dataset.confirm && !confirm(btn.dataset.confirm)) return;
  const inst = btn.dataset.inst, body = {};
  if(inst==='install_erp'){
    body.server_ip = $('#erp-ip').value.trim();
    body.http_port = $('#erp-port').value.trim();
    body.db_password = $('#erp-db').value;
    body.admin_password = $('#erp-admin').value;
  } else if(inst==='install_backups' && $('#bk-cron')){
    body.schedule_cron = $('#bk-cron').checked;
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

// An install that has finished has flipped a pill from "not installed" to
// "installed ✓" — this page's whole job is to show that.
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
</style></head><body>
<div class="top">
  <h1>SSPL ERP Admin</h1><span id="clock" class="badge"></span>
  <span class="badge" title="panel code version — restart the service after updating">v{{ version }}</span>
  <a class="nav" href="/install">ERP Next Installation suite →</a>
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
  <div class="tile"><div class="k">Uptime</div><div class="v num" id="t-up">–</div>
    <div class="s">since last reboot</div></div>
</div>

<div class="card"><h2>Memory &amp; disk</h2><div class="meters" id="meters"></div></div>

<div class="card"><h2>ERP containers</h2>
  <div id="cont-err" style="color:var(--crit);font-size:13px;display:none"></div>
  <table><thead><tr><th>Service</th><th>State</th><th>Status</th></tr></thead>
  <tbody id="containers"><tr><td colspan="3" style="color:var(--muted)">Loading…</td></tr></tbody></table>
</div>

<div class="card"><h2>Actions</h2>
  <div class="actions">
    <button class="primary act" data-act="backup">Run full backup</button>
    <button class="act" data-act="db_backup">DB-only backup</button>
    <button class="act" data-act="verify">Verify backups</button>
    <span class="sep"></span>
    <button class="primary act" data-act="update"
      data-confirm="Update the ERP system now? Services will restart and users will be disconnected for a few minutes.">Update system</button>
    <select id="rb-snap" title="Image snapshot to roll back to"></select>
    <button class="danger act" data-act="rollback"
      data-confirm="Roll back Docker images to the selected snapshot? Services will restart.">Rollback</button>
    <span class="sep"></span>
    <button id="clear-ram"
      data-confirm="Clear RAM caches now? This is safe but may briefly slow the system while caches rebuild.">Clear RAM caches</button>
    <span id="ram-msg" style="font-size:13px;color:var(--ink-2)"></span>
  </div>
</div>

<div class="card"><h2>Backups on the server</h2>
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
</div>

<div class="card"><h2>Upload backup files to the server</h2>
  <p style="font-size:13px;color:var(--ink-2);margin-top:0">Files are stored under
  <code id="updest">uploads/</code> inside the backup directory. Allowed:
  .sql.gz, .tar, .tgz, .json, .yml.</p>
  <div class="uprow">
    <input type="file" id="upfiles" multiple>
    <input type="text" id="upfolder" placeholder="Folder (optional)" size="14">
    <button class="primary" id="upbtn">Upload</button>
    <span id="upmsg"></span>
  </div>
</div>

</div><!-- /col-left -->
""" + TERM_HTML + r"""
</main>

<div class="modal" id="rs-modal"><div class="card">
  <h1 style="font-size:17px;margin-bottom:10px">Restore from backup</h1>
  <div class="danger-box">
    <b>⚠ This overwrites the live site.</b> Everything currently in
    <b id="rs-site">the site</b> — every record entered since the backup was taken — is
    replaced by the contents of <b id="rs-src">the backup</b>.
    <br><br>A <b>full safety backup is taken first</b>, so you can get back to the
    current state if this turns out to be the wrong backup. Users will be
    disconnected while it runs.
  </div>
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
<script>
""" + TERM_JS + r"""
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

// ---- restore: modal gate, then a job you talk to in the terminal ----
let rsTarget = null;   // {kind, name} of the backup being restored from

function restoreBtn(hasDb, kind, name){
  if (!hasDb) return '';
  return `<button class="danger rs-btn" data-kind="${esc(kind)}" data-name="${esc(name)}"
    title="Overwrites the live site with this backup">Restore</button>`;
}

function openRestore(kind, name){
  rsTarget = {kind, name};
  const site = (window.setupSite || 'the live site');
  $('#rs-site').textContent = site;
  $('#rs-site2').textContent = site;
  $('#rs-src').textContent = name;
  $('#rs-confirm').value = ''; $('#rs-pw').value = ''; $('#rs-err').textContent = '';
  $('#rs-modal').classList.add('on');
  $('#rs-confirm').focus();
}
function closeRestore(){ $('#rs-modal').classList.remove('on'); rsTarget = null; }

document.addEventListener('click', e => {
  const b = e.target.closest('.rs-btn');
  if (b) openRestore(b.dataset.kind, b.dataset.name);
});
$('#rs-cancel').onclick = closeRestore;
$('#rs-modal').onclick = e => { if (e.target === $('#rs-modal')) closeRestore(); };
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && $('#rs-modal').classList.contains('on')) closeRestore();
});

$('#rs-go').onclick = async () => {
  if (!rsTarget) return;
  const btn = $('#rs-go');
  btn.disabled = true; $('#rs-err').textContent = '';
  try{
    const r = await fetch('/api/run/restore', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({kind: rsTarget.kind, name: rsTarget.name,
        confirm_site: $('#rs-confirm').value,
        admin_password: $('#rs-pw').value})});
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

// The components themselves are installed from the installation suite. All the
// dashboard needs from that endpoint is the live site name, which the restore
// modal makes the user type out to confirm.
async function refreshSite(){
  try{
    const r = await fetch('/api/setup-status'); if(r.status===401){location='/login';return;}
    const s = await r.json();
    window.setupSite = (s.components && s.components.erp.site) || null;
  }catch(e){}
}

function fileBadge(ok, label){ return `<span class="badge ${ok?'ok':'miss'}">${label}${ok?' ✓':' missing'}</span>`; }
function dl(kind, name){ return `<a href="/download/${kind}/${encodeURIComponent(name).replace(/%2F/g,'/')}">download</a>`; }

async function refreshBackups(){
  try{
    const r = await fetch('/api/backups'); if(!r.ok) return;
    const b = await r.json();
    $('#tab-full').innerHTML = b.full.length ? `<table><thead><tr><th>Backup</th><th>Size</th>
      <th>Contents</th><th class="right">Files</th><th></th></tr></thead><tbody>` +
      b.full.map(d => `<tr><td class="num">${esc(d.name)}</td><td class="num">${esc(d.size)}</td>
        <td>${fileBadge(d.db,'DB')} ${fileBadge(d.public,'Files')} ${fileBadge(d.private,'Private')}</td>
        <td class="right"><details><summary>${d.files.length} files</summary><div class="filelist">` +
        d.files.map(f => `${esc(f.name)} (${esc(f.size)}) — ${dl('full', d.name + '/' + f.name)}`).join('<br>') +
        `</div></details></td><td class="right">${restoreBtn(d.db, 'full', d.name)}</td></tr>`).join('') +
        '</tbody></table>'
      : '<p style="color:var(--muted)">No full backups found.</p>';
    const simpleTable = (rows, kind, deletable) => rows.length ? `<table><thead><tr><th>File</th><th>Size</th>
      <th>Date</th><th></th></tr></thead><tbody>` +
      rows.map(f => `<tr><td class="num">${esc(f.name)}${f.latest?' <span class="badge ok">latest</span>':''}</td>
        <td class="num">${esc(f.size)}</td><td class="num">${esc(f.mtime)}</td>
        <td class="right">${dl(kind, f.name)}${deletable ? ' ' + delBtn(f.name, 'file') : ''}</td></tr>`).join('') +
        '</tbody></table>'
      : '<p style="color:var(--muted)">Nothing here yet.</p>';
    // Only uploads are deletable — real backups and snapshots are not.
    $('#tab-db').innerHTML = simpleTable(b.db_only, 'db');
    $('#tab-img').innerHTML = simpleTable(b.images, 'image');
    $('#tab-upl').innerHTML =
      ((b.upload_folders || []).length ? `<table><thead><tr><th>Restorable folder</th><th></th>
        </tr></thead><tbody>` + b.upload_folders.map(f =>
        `<tr><td class="num">${esc(f)} <span class="badge ok">DB</span></td>
         <td class="right">${restoreBtn(true, 'upload', f)} ${delBtn(f, 'folder')}</td></tr>`).join('') +
        '</tbody></table>'
        : '<p style="font-size:13px;color:var(--muted);margin-top:0">To restore an uploaded backup, ' +
          'upload it into its own named folder (the Folder box below) so its database and files stay ' +
          'together. Folders containing a <code>*-database.sql.gz</code> get a Restore button here.</p>') +
      simpleTable(b.uploads, 'upload', true);
    const sel = $('#rb-snap'), cur = sel.value;
    sel.innerHTML = '<option value="">Latest snapshot</option>' +
      b.images.map(i => `<option value="${esc(i.name)}">${esc(i.name)} (${esc(i.size)})</option>`).join('');
    if ([...sel.options].some(o => o.value === cur)) sel.value = cur;
  }catch(e){}
}

document.querySelectorAll('.act').forEach(btn => btn.onclick = async () => {
  const act = btn.dataset.act;
  if (btn.dataset.confirm && !confirm(btn.dataset.confirm)) return;
  const body = {};
  if (act === 'rollback' && $('#rb-snap').value) body.snapshot = $('#rb-snap').value;
  const r = await fetch('/api/run/' + act, {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  const j = await r.json();
  if (j.error) alert(j.error); else { jobWasActive = true; refreshJob(); revealConsole(); }
});

$('#clear-ram').onclick = async () => {
  if (!confirm($('#clear-ram').dataset.confirm)) return;
  $('#ram-msg').textContent = 'Clearing…';
  const r = await fetch('/api/clear-ram', {method:'POST'});
  const j = await r.json();
  $('#ram-msg').textContent = j.error ? j.error : `Freed ${j.freed} — ${j.available} now available`;
  refreshStats();
};

$('#upbtn').onclick = () => {
  const files = $('#upfiles').files;
  if (!files.length) { $('#upmsg').textContent = 'Choose one or more files first.'; return; }
  const fd = new FormData();
  for (const f of files) fd.append('files', f);
  fd.append('folder', $('#upfolder').value.trim());
  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/upload');
  xhr.upload.onprogress = e => { if (e.lengthComputable)
    $('#upmsg').textContent = 'Uploading… ' + Math.round(100*e.loaded/e.total) + '%'; };
  xhr.onload = () => {
    try{
      const j = JSON.parse(xhr.responseText);
      $('#upmsg').textContent = j.error ? j.error : 'Uploaded: ' + j.saved.join(', ');
    }catch(e){ $('#upmsg').textContent = 'Upload failed (' + xhr.status + ')'; }
    refreshBackups();
  };
  xhr.onerror = () => $('#upmsg').textContent = 'Upload failed — network error';
  xhr.send(fd);
  $('#upmsg').textContent = 'Uploading… 0%';
};

document.querySelectorAll('.tabs button').forEach(b => b.onclick = () => {
  document.querySelectorAll('.tabs button').forEach(x => x.classList.toggle('active', x === b));
  document.querySelectorAll('.tabpane').forEach(p =>
    p.classList.toggle('active', p.id === 'tab-' + b.dataset.tab));
});

// A backup, update or rollback that has just finished changes what is on disk
// and how the box is doing — the reasons this page exists.
onJobFinished = () => { refreshBackups(); refreshStats(); refreshSite(); };

setInterval(() => $('#clock').textContent = new Date().toLocaleString(), 1000);
refreshSite(); refreshStats(); refreshBackups();
setInterval(refreshStats, 5000);
setInterval(refreshBackups, 60000);
setInterval(refreshSite, 30000);
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
