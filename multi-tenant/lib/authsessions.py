#!/usr/bin/env python3
"""Remembered Authelia logins ("Remember me") of one site, read from Redis.

Usage (sites/auth.sh calls this; run as root). REDIS is the command that runs
redis-cli on the site's database and takes a Lua script on stdin:
  authsessions.py SITE_DIR list [--json] -- REDIS...
  authsessions.py SITE_DIR revoke ID     -- REDIS...

The Redis key is the browser's cookie, so it never leaves Redis: a session is
named by the SHA-1 of its key. Values are msgpack in AES-256-GCM, keyed with
SHA-256 of the site's session secret (checked with Authelia 4.39.28, AUTH_IMAGE).
Authelia also keeps anonymous sessions; those and the ones without "Remember
me" are left out. A session holds no address: it comes from the login log.
"""
import ctypes
import ctypes.util
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from urllib.request import pathname2url

ID_RE = re.compile(r"[0-9a-f]{40}")
SCAN = """
local cur = '0'
repeat
  local r = redis.call('SCAN', cur, 'MATCH', 'authelia-session:*', 'COUNT', 500)
  cur = r[1]
  for _, k in ipairs(r[2]) do %s end
until cur == '0'
"""
LIST = "local out = {}" + SCAN % """
    local v = redis.call('GET', k)
    if v then
      out[#out + 1] = redis.sha1hex(k) .. ' ' .. redis.call('PTTL', k) .. ' '
        .. v:gsub('.', function(c) return string.format('%02x', c:byte()) end)
    end""" + "return out"
REVOKE = "local n = 0" + SCAN % """
    if redis.sha1hex(k) == '{id}' then n = n + redis.call('DEL', k) end""" + "return 'deleted ' .. n"


def die(msg):
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(1)


def redis(cmd, lua):
    try:
        r = subprocess.run(cmd, input=lua, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as e:
        die(f"could not reach Authelia's Redis: {type(e).__name__}")
    # redis-cli exits 0 on most errors and prints them instead.
    if r.returncode or re.match(r"\(error\)|ERR |NOAUTH|WRONGPASS|LOADING", r.stdout):
        die("could not reach Authelia's Redis: " + ((r.stderr or r.stdout).strip().splitlines() or ["no answer"])[-1][:200])
    return r.stdout


def decryptor(secret):
    """data -> plaintext, or None if it is not authentic. OpenSSL by ctypes: no pip package."""
    c = ctypes.CDLL(ctypes.util.find_library("crypto") or "libcrypto.so.3")
    p, i, b = ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p
    c.EVP_CIPHER_CTX_new.restype = c.EVP_aes_256_gcm.restype = p
    c.EVP_CIPHER_CTX_new.argtypes = c.EVP_aes_256_gcm.argtypes = []
    c.EVP_CIPHER_CTX_free.argtypes = [p]
    c.EVP_CIPHER_CTX_free.restype = None
    c.EVP_DecryptInit_ex.argtypes = [p, p, p, b, b]
    c.EVP_DecryptUpdate.argtypes = [p, b, ctypes.POINTER(i), b, i]
    c.EVP_CIPHER_CTX_ctrl.argtypes = [p, i, i, p]
    c.EVP_DecryptFinal_ex.argtypes = [p, b, ctypes.POINTER(i)]
    key = hashlib.sha256(secret).digest()

    def decrypt(data):
        if len(data) < 28:
            return None
        nonce, body, tag = data[:12], data[12:-16], ctypes.create_string_buffer(data[-16:], 16)
        out, n = ctypes.create_string_buffer(len(body) + 16), i()
        end, m = ctypes.create_string_buffer(32), i()
        ctx = c.EVP_CIPHER_CTX_new()
        try:
            ok = (c.EVP_DecryptInit_ex(ctx, c.EVP_aes_256_gcm(), None, key, nonce) == 1
                  and c.EVP_DecryptUpdate(ctx, out, ctypes.byref(n), body, len(body)) == 1
                  and c.EVP_CIPHER_CTX_ctrl(ctx, 0x11, 16, tag) == 1        # EVP_CTRL_GCM_SET_TAG
                  and c.EVP_DecryptFinal_ex(ctx, end, ctypes.byref(m)) == 1)
            return out.raw[:n.value] if ok else None
        finally:
            c.EVP_CIPHER_CTX_free(ctx)
    return decrypt


def user_session(plain):
    """The JSON inside {"KV": {"UserSession": <bytes>}}, or None."""
    at = plain.find(b"\xabUserSession")
    if at < 0:
        return None
    at += 12
    size = {0xc4: 1, 0xc5: 2, 0xc6: 4}.get(plain[at] if at < len(plain) else 0)
    if not size:
        return None
    at += 1
    length = int.from_bytes(plain[at:at + size], "big")
    try:
        s = json.loads(plain[at + size:at + size + length])
    except ValueError:
        return None
    return s if isinstance(s, dict) else None


def login_log(site_dir):
    """[(unix time, user, ip)] of the successful password logins."""
    db = os.path.join(site_dir, "config", "db.sqlite3")
    out = []
    if not os.path.isfile(db):
        return out
    try:
        con = sqlite3.connect(f"file:{pathname2url(db)}?mode=ro", uri=True, timeout=3)
        try:
            for t, user, ip in con.execute("SELECT time, username, remote_ip FROM authentication_logs"
                                           " WHERE successful AND auth_type = '1FA'"):
                # 9 decimals in the log; older Pythons parse 6 at most.
                t = re.sub(r"(\.\d{1,6})\d*", r"\1", str(t)).replace("Z", "+00:00")
                try:
                    d = datetime.fromisoformat(t)
                except ValueError:
                    continue
                if d.tzinfo is None:
                    d = d.replace(tzinfo=timezone.utc)
                out.append((d.timestamp(), user, ip or ""))
        finally:
            con.close()
    except sqlite3.Error:
        pass        # only the address is lost
    return out


def sessions(site_dir, cmd):
    try:
        with open(os.path.join(site_dir, "secrets", "session"), "rb") as f:
            decrypt = decryptor(f.read().strip())
    except (OSError, AttributeError) as e:
        die(f"could not read the sessions: {e}")
    lines = [ln.split() for ln in redis(cmd, LIST).splitlines()]
    log, found, bad = login_log(site_dir), [], 0
    for p in lines:
        if len(p) != 3:
            continue
        plain = decrypt(bytes.fromhex(p[2]))
        s = user_session(plain) if plain else None
        if s is None:
            bad += 1
            continue
        ttl = int(p[1]) // 1000
        if not s.get("Username") or not s.get("KeepMeLoggedIn") or ttl <= 0:
            continue
        began = s.get("FirstFactorAuthnTimestamp") or 0
        near = min((e for e in log if e[1] == s["Username"] and abs(e[0] - began) <= 3),
                   key=lambda e: abs(e[0] - began), default=None)
        found.append({"id": p[0], "user": s["Username"], "host": s.get("CookieDomain", ""),
                      "ip": near[2] if near else "", "twofa": bool(s.get("SecondFactorAuthnTimestamp")),
                      "login": began, "expires": int(time.time()) + ttl, "left": ttl})
    if bad and bad == len([p for p in lines if len(p) == 3]):
        die("the sessions could not be read: another Authelia version, or another session secret")
    return sorted(found, key=lambda x: -x["login"]), bad


def main():
    a = sys.argv[1:]
    if "--" not in a or len(a) < 4:
        die(__doc__)
    cut = a.index("--")
    (site_dir, what, *rest), cmd = a[:cut], a[cut + 1:]
    if what == "list":
        rows, bad = sessions(site_dir, cmd)
        if rest == ["--json"]:
            print(json.dumps({"sessions": rows, "unreadable": bad}))
        else:
            for x in rows:
                print(f"{x['id']}  {x['user']:<16} {x['ip'] or '-':<15} {'2FA' if x['twofa'] else 'password only':<13}"
                      f" since {datetime.fromtimestamp(x['login']):%Y-%m-%d %H:%M}  {x['left'] // 86400} days left")
            if not rows:
                print("no remembered logins")
    elif what == "revoke" and len(rest) == 1 and ID_RE.fullmatch(rest[0]):
        # Authelia re-creates a revoked key as an anonymous session when the
        # browser comes back, so the key alone does not mean it is logged in.
        if not any(x["id"] == rest[0] for x in sessions(site_dir, cmd)[0]):
            die("that login has already ended")
        redis(cmd, REVOKE.replace("{id}", rest[0]))
        # A request in flight can write the session back: look again.
        time.sleep(1)
        if any(x["id"] == rest[0] for x in sessions(site_dir, cmd)[0]):
            redis(cmd, REVOKE.replace("{id}", rest[0]))
            time.sleep(1)
            if any(x["id"] == rest[0] for x in sessions(site_dir, cmd)[0]):
                die("the login came back: it was in use at that moment; try again")
        print("   ✓ the remembered login was revoked")
    else:
        die("usage: list [--json] | revoke ID")


if __name__ == "__main__":
    main()
