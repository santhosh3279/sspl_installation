#!/usr/bin/env python3
"""Authelia users for the SSPL multi-tenant stack.

Each linked site has its own users file (AUTH_DIR/sites/<site>/config/
users_database.yml), written as JSON, which is valid YAML; Authelia never
rewrites it (password reset and change are disabled in its config).

Usage (sites/auth.sh calls this; run as root):
  authusers.py FILE list [--json]
  authusers.py FILE add USER EMAIL NAME              password: env AUTH_PASSWORD
  authusers.py FILE passwd USER                      password: env AUTH_PASSWORD
  authusers.py FILE disable USER yes|no
  authusers.py FILE remove USER
  authusers.py FILE import OLDFILE SITE linked|unlinked
        from the old file shared by all sites: the users of SITE, and, if it
        is linked, the global admins. Users already in FILE are kept.

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
        rows = [{"user": n, "name": users[n].get("displayname", ""), "email": users[n].get("email", ""),
                 "disabled": bool(users[n].get("disabled"))} for n in sorted(users)]
        if args[:1] == ["--json"]:
            print(json.dumps(rows))
            return
        if not rows:
            print("(no users)")
        for r in rows:
            print(f"{r['user']:<20} {r['email']:<32}{' [disabled]' if r['disabled'] else ''}")
        return

    if cmd == "import":
        if len(args) != 3:
            die("import OLDFILE SITE linked|unlinked")
        site, linked = args[1], args[2] == "linked"
        n = 0
        for name, u in load(args[0])["users"].items():
            g = u.get("groups") or []
            if name not in users and (site in g or (linked and "admins" in g)):
                users[name] = {**u, "groups": []}
                n += 1
        if n:
            save(path, data)
            print(f"   ✓ {site}: {n} user(s) carried over")
        return

    if not args:
        die("which user?")
    name = args[0].strip().lower()

    if not USER_RE.match(name):
        die("user names: lowercase letters, digits, . _ - (start with a letter or digit)")

    if cmd == "add":
        if len(args) < 3:
            die("add USER EMAIL NAME")
        if name in users:
            die(f"user '{name}' already exists")
        email, display = args[1].strip(), args[2].strip()
        if not EMAIL_RE.match(email):
            die("that e-mail address does not look right")
        if not display or len(display) > 100 or any(c in display for c in "\n\r"):
            display = name
        users[name] = {"displayname": display, "email": email, "password": hash_password(password_from_env()),
                       "groups": [], "disabled": False}
        save(path, data)
        print(f"   ✓ user {name} added")
        return

    u = get_user(data, name)
    if cmd == "passwd":
        u["password"] = hash_password(password_from_env())
        save(path, data)
        print(f"   ✓ password of {name} changed")
    elif cmd == "disable":
        if len(args) < 2 or args[1] not in ("yes", "no"):
            die("disable USER yes|no")
        u["disabled"] = args[1] == "yes"
        save(path, data)
        print(f"   ✓ {name}: disabled = {args[1]}")
    elif cmd == "remove":
        del users[name]
        save(path, data)
        print(f"   ✓ user {name} removed")
    else:
        die(f"unknown command '{cmd}'", 2)


if __name__ == "__main__":
    main(sys.argv)
