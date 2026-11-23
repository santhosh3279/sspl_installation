#!/usr/bin/env python3
"""Authelia users for the SSPL multi-tenant stack.

The users file (AUTH_DIR/config/users_database.yml) is written as JSON, which
is valid YAML, and Authelia never rewrites it (password reset and change are
disabled in its config). A user may enter the linked sites named in their
groups; the group "admins" enters every linked site.

Usage (sites/auth.sh calls this; run as root):
  authusers.py FILE list [--json]
  authusers.py FILE add USER EMAIL NAME [SITE,SITE...] [--admin]   password: env AUTH_PASSWORD
  authusers.py FILE passwd USER                                     password: env AUTH_PASSWORD
  authusers.py FILE sites USER [SITE,SITE...]       replace the user's sites ("" = none)
  authusers.py FILE admin USER yes|no
  authusers.py FILE disable USER yes|no
  authusers.py FILE remove USER
  authusers.py FILE drop-site SITE                  remove SITE from every user

Passwords never travel in argv. They are stored as PBKDF2-SHA512 (310000
rounds), a format Authelia verifies natively.
"""
import base64
import hashlib
import json
import os
import re
import secrets
import sys
import tempfile

USER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,190}\.[A-Za-z]{2,24}$")
SITE_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")
ADMIN = "admins"
ROUNDS = 310000


def die(msg, code=1):
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(code)


def ab64(raw):
    """passlib's 'adapted base64': standard alphabet with '.' for '+', no padding."""
    return base64.b64encode(raw).decode().rstrip("=").replace("+", ".")


def hash_password(pw):
    salt = secrets.token_bytes(16)
    key = hashlib.pbkdf2_hmac("sha512", pw.encode(), salt, ROUNDS, 64)
    return f"$pbkdf2-sha512${ROUNDS}${ab64(salt)}${ab64(key)}"


def password_from_env():
    pw = os.environ.get("AUTH_PASSWORD", "")
    if len(pw) < 8:
        die("the password needs 8 characters or more (env AUTH_PASSWORD)")
    if len(pw) > 256 or "\n" in pw:
        die("the password is too long or has a line break")
    return pw


def load(path):
    try:
        with open(path) as f:
            text = f.read()
    except FileNotFoundError:
        return {"users": {}}
    if not text.strip():
        return {"users": {}}
    try:
        data = json.loads(text)
    except ValueError:
        die(f"{path} is not in the format this tool writes (JSON) — was it edited by hand?")
    if not isinstance(data.get("users"), dict):
        data["users"] = {}
    return data


def save(path, data):
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".users.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def parse_sites(arg):
    sites = [s.strip().lower() for s in (arg or "").split(",") if s.strip()]
    for s in sites:
        if not SITE_RE.match(s) or s == ADMIN:
            die(f"'{s}' is not a site name")
    return sorted(set(sites))


def get_user(data, name):
    u = data["users"].get(name)
    if u is None:
        die(f"no user '{name}'")
    return u


def main(argv):
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    path, cmd, args = argv[1], argv[2], argv[3:]
    data = load(path)
    users = data["users"]

    if cmd == "list":
        rows = []
        for name in sorted(users):
            u = users[name]
            groups = u.get("groups") or []
            rows.append({"user": name, "name": u.get("displayname", ""), "email": u.get("email", ""),
                         "admin": ADMIN in groups, "disabled": bool(u.get("disabled")),
                         "sites": sorted(g for g in groups if g != ADMIN)})
        if args[:1] == ["--json"]:
            print(json.dumps(rows))
            return
        if not rows:
            print("(no users)")
        for r in rows:
            flags = (" [admin]" if r["admin"] else "") + (" [disabled]" if r["disabled"] else "")
            print(f"{r['user']:<20} {r['email']:<32} {', '.join(r['sites']) or '-'}{flags}")
        return

    if not args:
        die("which user?")
    name = args[0].strip().lower()

    if cmd == "drop-site":
        site = name
        n = 0
        for u in users.values():
            g = u.get("groups") or []
            if site in g:
                u["groups"] = [x for x in g if x != site]
                n += 1
        save(path, data)
        print(f"   ✓ {site} removed from {n} user(s)")
        return

    if not USER_RE.match(name):
        die("user names: lowercase letters, digits, . _ - (start with a letter or digit)")

    if cmd == "add":
        if len(args) < 3:
            die("add USER EMAIL NAME [SITES] [--admin]")
        if name in users:
            die(f"user '{name}' already exists")
        email, display = args[1].strip(), args[2].strip()
        if not EMAIL_RE.match(email):
            die("that e-mail address does not look right")
        if not display or len(display) > 100 or any(c in display for c in "\n\r"):
            display = name
        rest = args[3:]
        admin = "--admin" in rest
        rest = [a for a in rest if a != "--admin"]
        groups = parse_sites(rest[0] if rest else "") + ([ADMIN] if admin else [])
        users[name] = {"displayname": display, "email": email, "password": hash_password(password_from_env()),
                       "groups": groups, "disabled": False}
        save(path, data)
        print(f"   ✓ user {name} added")
        return

    u = get_user(data, name)
    if cmd == "passwd":
        u["password"] = hash_password(password_from_env())
        save(path, data)
        print(f"   ✓ password of {name} changed")
    elif cmd == "sites":
        keep_admin = ADMIN in (u.get("groups") or [])
        u["groups"] = parse_sites(args[1] if len(args) > 1 else "") + ([ADMIN] if keep_admin else [])
        save(path, data)
        print(f"   ✓ {name} may enter: {', '.join(g for g in u['groups'] if g != ADMIN) or '(no site)'}"
              + (" + every site (admin)" if keep_admin else ""))
    elif cmd in ("admin", "disable"):
        if len(args) < 2 or args[1] not in ("yes", "no"):
            die(f"{cmd} USER yes|no")
        on = args[1] == "yes"
        if cmd == "admin":
            g = [x for x in (u.get("groups") or []) if x != ADMIN]
            u["groups"] = g + ([ADMIN] if on else [])
        else:
            u["disabled"] = on
        save(path, data)
        print(f"   ✓ {name}: {cmd} = {args[1]}")
    elif cmd == "remove":
        if len(users) == 1:
            die("this is the only user — Authelia does not start without one; add another first")
        del users[name]
        save(path, data)
        print(f"   ✓ user {name} removed")
    else:
        die(f"unknown command '{cmd}'", 2)


if __name__ == "__main__":
    main(sys.argv)
