#!/usr/bin/env python3
"""Development server for the multi-tenant panel. Start it with dev.sh.

Loads the real app.py against a throw-away state folder, and replaces the
`subprocess` and `shutil` names inside it: no command the panel asks for is
ever executed. Reads get canned answers; jobs run a small Python that prints
the command line. Anything unknown fails as "command not found".
"""
import argparse
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
HOME = Path(os.environ.get("SSPL_DEV_HOME") or Path.home() / ".cache" / "sspl-panel-dev")
STATE = HOME / "state"
ERP, VOLUME, BACKUPS, AUTH = STATE / "erp", STATE / "volume", STATE / "backups", STATE / "auth"
TOOLS, PROXY = STATE / "tools", STATE / "nginx.conf"
SITES = {"acme.example.test": "https://acme.example.test:8443",
         "beta.example.test": "http://beta.example.test:8080",
         "demo.erp.lan": "http://192.0.2.10:8001"}
APPS = ["erpnext", "hrms", "india_compliance"]


def put(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def seed():
    """Fake server: non-standard ports, so the 80/443 button shows."""
    from werkzeug.security import generate_password_hash
    put(STATE / "sspl-mt.conf", "\n".join([
        f"ERP_DIR={ERP}", "COMPOSE_PROJECT=sspl-dev", "HTTP_PORT=8080", "HTTPS_PORT=8443",
        f"SSL_DIR={STATE}/ssl", f"LE_LIVE={STATE}/letsencrypt", f"BACKUP_ROOT={BACKUPS}",
        f"AUTH_DIR={AUTH}", "SERVER_IP=192.0.2.10", "SMTP_USER=dev@example.test",
        "WG_IFACE=sspl-wg", "WG_ENDPOINT=vpn.example.test", 'RCLONE_REMOTE="gdrive:frappe-backups"', ""]))
    put(ERP / "docker-compose.yml", "services: {}\n")
    put(ERP / "frappe_docker" / ".env", "HTTP_PUBLISH_PORT=127.0.0.1:18080\n")
    (ERP / "frappe_docker" / "overrides").mkdir(parents=True, exist_ok=True)
    put(ERP / "site-domains", "".join(f"{s} {s}\n" for s in SITES) + "erp.acme.test acme.example.test\n")
    put(ERP / "site-ports", "8001 demo.erp.lan\n")
    put(ERP / "site-auth", "acme.example.test\n")
    put(ERP / "wg-peers", "shop1 10.99.0.2 devkey1 acme.example.test\nshop2 10.99.0.3 devkey2 beta.example.test\n")
    put(VOLUME / "apps.txt", "frappe\n" + "\n".join(APPS) + "\n")
    put(PROXY, "listen 8080;\nlisten 8443 ssl;\n")
    # The panel checks each tool exists before it starts a job.
    for d in ("lib", "sites", "backup", "update"):
        for src in (REPO / "multi-tenant" / d).glob("*.*"):
            put(TOOLS / d / src.name, "# dev placeholder, never run\n")
    for f in ("default.crt", "default.key"):
        put(STATE / "ssl" / f, "dev\n")
    for f in ("fullchain.pem", "privkey.pem"):
        put(STATE / "letsencrypt" / "erp.acme.test" / f, "dev\n")
    put(AUTH / "compose.yaml", "services: {}\n")
    put(AUTH / "secrets" / "redis", "dev\n")
    put(AUTH / "sites" / ".ports", "acme.example.test 29101\n")
    put(AUTH / "sites" / "acme.example.test" / "config" / "users_database.yml", json.dumps({"users": {
        "alex": {"displayname": "Alex", "email": "alex@example.test"},
        "sam": {"displayname": "Sam", "email": "sam@example.test", "disabled": True}}}))
    put(ERP / "site-auth-ips", "acme.example.test bypass 203.0.113.7\nacme.example.test trusted 192.168.1.0/24\n")
    con = sqlite3.connect(AUTH / "sites" / "acme.example.test" / "config" / "db.sqlite3")
    con.execute("CREATE TABLE authentication_logs (id INTEGER PRIMARY KEY, time TIMESTAMP, successful BOOLEAN,"
                " banned BOOLEAN, username TEXT, auth_type TEXT, remote_ip TEXT)")
    con.executemany("INSERT INTO authentication_logs (time, successful, banned, username, auth_type, remote_ip)"
                    " VALUES (datetime('now', ?), ?, 0, ?, ?, ?)", [
        ("-3 days", 1, "alex", "1FA", "203.0.113.7"), ("-3 days", 1, "alex", "TOTP", "203.0.113.7"),
        ("-1 days", 0, "sam", "1FA", "198.51.100.20"), ("-2 hours", 1, "sam", "1FA", "198.51.100.20"),
        ("-2 hours", 0, "sam", "TOTP", "198.51.100.20"), ("-1 hours", 1, "sam", "WebAuthn", "198.51.100.20")])
    con.commit()
    con.close()
    for i, (site, url) in enumerate(SITES.items()):
        put(VOLUME / site / "site_config.json",
            json.dumps({"host_name": url, "maintenance_mode": 1 if i == 1 else 0}))
        for n in range(3 - i):
            ts = f"{datetime.now() - timedelta(days=n, hours=i):%Y%m%d_%H%M%S}"
            put(BACKUPS / "sites" / site / "full" / ts / f"{ts}-database.sql.gz", "fake backup\n")
            put(BACKUPS / "sites" / site / "db-only" / f"{ts}-database.sql.gz", "fake dump\n")
    put(BACKUPS / "uploads" / "example.sql.gz", "fake upload\n")
    put(STATE / "config.json", json.dumps({
        "username": "dev", "password_hash": generate_password_hash("dev"),
        "secret_key": os.urandom(32).hex(),     # kept in the file: logins survive a reload
        "mt_conf": str(STATE / "sspl-mt.conf"), "tools_dir": str(TOOLS),
        "job_dir": str(STATE / "jobs"), "repo_dir": str(REPO), "server_ip": "192.0.2.10"}))
    (STATE / "config.json").chmod(0o600)
    put(STATE / ".seeded", "")                  # last: a half-made state is rebuilt


POOL = {"size": 512 << 20}


def answer(cmd):
    """Canned stdout for a command the panel reads, or None if unknown."""
    cmd = [str(c) for c in cmd]
    line = " ".join(cmd)
    if cmd[0] == "sync":
        raise OSError("dev mode: not clearing the real caches")
    if cmd[0] == "hostname":
        return "192.0.2.10\n"
    if cmd[0] == "systemctl":
        return "active\n"
    if cmd[0] == "crontab":
        return ("# >>> SSPL multi-tenant backups >>>\n"
                f"0 2 * * *   {TOOLS}/backup/backup.sh --all\n"
                f"0 */6 * * * {TOOLS}/backup/db-backup.sh --all\n"
                f"0 3 * * 0   {TOOLS}/backup/verify.sh --all\n"
                "# <<< SSPL multi-tenant backups <<<\n")
    if cmd[0] == "wg":
        return f"devkey1\t{int(time.time()) - 40}\ndevkey2\t0\n"
    if cmd[:2] == ["rclone", "listremotes"]:
        return "gdrive:\nmega:\n"
    if cmd[0] == "rclone":
        return "rclone v1.70.0 (dev)\n"
    if cmd[0] == str(TOOLS / "sites" / "wireguard.sh"):
        return "#!/bin/sh\necho 'dev mode: no real tunnel'\n"
    if cmd[0] != "docker":
        return None
    if "volume" in cmd:
        return f"{VOLUME}\n"
    if "stats" in cmd:
        return "".join(json.dumps({"Name": f"sspl-dev-{n}-1", "MemUsage": f"{m}MiB / 8GiB",
                                   "MemPerc": f"{m / 81.92:.2f}%", "CPUPerc": "1.50%"}) + "\n"
                       for n, m in (("backend", 900), ("db", 1400), ("frontend", 30), ("queue-long", 350)))
    if "frappe.get_installed_apps" in cmd:
        return json.dumps(["frappe"] + APPS) + "\n"
    if "ls" in cmd:
        return "frappe\n" + "\n".join(APPS) + "\n"
    if "mariadb" in line:
        sql = cmd[-1]
        if sql.startswith("SET GLOBAL"):
            POOL["size"] = int(sql.rsplit("=", 1)[1])
            return ""
        return "1\n" if sql == "SELECT 1" else f"{POOL['size']}\n{3 << 30}\n52000\n9000000\n"
    if cmd[1] == "inspect":
        if "--format" in cmd:
            return "true 2785\n"
        mounted = (ERP / "frappe_docker" / "overrides" / "compose.sspl-db.yaml").is_file()
        return json.dumps([{"Mounts": [{"Destination": "/etc/mysql/conf.d"}] if mounted else [],
                            "HostConfig": {"Memory": 0}}])
    if "/app/data/.api-key" in cmd:
        return "dev-not-a-real-key\n"
    if "ps" in cmd and "-q" in cmd:
        return "devdb\n"
    if "ps" in cmd:
        return "".join(json.dumps({"Service": s, "State": "running", "Status": "Up 3 days"}) + "\n"
                       for s in ("backend", "db", "frontend", "queue-long", "queue-short",
                                 "redis-cache", "redis-queue", "scheduler", "websocket"))
    if cmd[-1] == "config":
        return "services:\n  db:\n    volumes: [{target: /etc/mysql/conf.d}]\n"
    if cmd[-3:] == ["up", "-d", "db"]:
        return ""
    return None


def fake_run(cmd, *_a, **_k):
    out = answer(cmd)
    if out is None:
        raise FileNotFoundError(f"dev mode: {cmd[0]} is not run")
    return subprocess.CompletedProcess(cmd, 0, out, "")


JOB = """import shlex, sys, time
print("DEV MODE - nothing is executed. The real panel would run:", flush=True)
print("  " + shlex.join(sys.argv[1:]), flush=True)
for i in range(1, 4):
    time.sleep(1); print(f"fake step {i}/3", flush=True)
print("Done (no change made).", flush=True)
"""


def fake_popen(cmd, stdin=None, stdout=None, stderr=None, start_new_session=False, **_k):
    # The command is only printed. Its env (passwords) is dropped.
    return subprocess.Popen([sys.executable, "-I", "-c", JOB, *map(str, cmd)],
                            stdin=stdin, stdout=stdout, stderr=stderr,
                            start_new_session=start_new_session)


class Shutil:
    def __getattr__(self, name):
        return getattr(shutil, name)

    def which(self, name, *_a, **_k):
        return f"/dev-mode/{name}" if name in ("nginx", "rclone", "certbot", "wg", "docker") else None


def load_panel():
    os.environ["SSPL_ADMIN_CONFIG"] = str(STATE / "config.json")
    spec = importlib.util.spec_from_file_location("sspl_panel", HERE / "app.py")
    panel = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(panel)
    # A fixed list, not a pass-through: a new kind of call breaks loudly here
    # instead of reaching the real system.
    panel.subprocess = SimpleNamespace(
        run=fake_run, Popen=fake_popen, PIPE=subprocess.PIPE, STDOUT=subprocess.STDOUT,
        DEVNULL=subprocess.DEVNULL, SubprocessError=subprocess.SubprocessError,
        TimeoutExpired=subprocess.TimeoutExpired, CalledProcessError=subprocess.CalledProcessError)
    panel.shutil = Shutil()
    panel.PROXY_CONF = str(PROXY)
    panel.auth_site_up = lambda port: bool(port)
    real_wg = panel.wg_status

    def wg_status():
        st = real_wg()
        st.update(installed=True, running=True)
        for i, p in enumerate(st["peers"]):
            p.update(age=40 if i == 0 else None, up=i == 0)
        return st
    panel.wg_status = wg_status

    @panel.app.after_request
    def banner(resp):
        if resp.mimetype == "text/html" and not resp.direct_passthrough:
            resp.set_data(resp.get_data(as_text=True).replace(
                "</body>", '<div style="position:fixed;bottom:8px;left:8px;z-index:9999;background:#b45309;'
                'color:#fff;padding:4px 10px;border-radius:4px;font:12px sans-serif;pointer-events:none">'
                'DEV: fake data, no commands run</div></body>'))
        return resp
    return panel.app


def broken_app(text):
    def wsgi(_env, start):
        start("500 Internal Server Error", [("Content-Type", "text/plain; charset=utf-8")])
        return [text.encode()]
    return wsgi


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8091)
    ap.add_argument("--reset", action="store_true", help="rebuild the fake data")
    args = ap.parse_args()
    if os.geteuid() == 0:
        sys.exit("Do not run dev mode as root.")
    from werkzeug.serving import is_running_from_reloader, run_simple
    if not is_running_from_reloader():          # once, not on every reload
        if args.reset or not (STATE / ".seeded").is_file():
            shutil.rmtree(STATE, ignore_errors=True)
            seed()
        print(f"Panel (dev): http://127.0.0.1:{args.port}   login dev / dev   state: {STATE}", flush=True)
    try:
        wsgi = load_panel()
    except Exception:                           # keep watching: fix app.py and it reloads
        text = traceback.format_exc()
        print(text, file=sys.stderr, flush=True)
        wsgi = broken_app(text)
    # No Werkzeug debugger: it runs code typed in the browser.
    run_simple("127.0.0.1", args.port, wsgi, threaded=True, use_reloader=True,
               use_debugger=False, extra_files=[str(HERE / "app.py")])


if __name__ == "__main__":
    main()
