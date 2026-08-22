"""Shared-password authentication for a small team.

Blog Studio holds live API keys, can spend quota, crawls arbitrary sites and
(with Google credentials) writes to Drive. Exposing it beyond localhost without
a login would hand all of that to anyone who finds the URL, so `server.serve`
refuses to bind a non-local address unless a password has been set. That
interlock is the point of this module; the rest is plumbing.

Deliberately small: stdlib only, one shared password, signed cookies. A few
colleagues sharing one workspace do not need per-user accounts, but they do need
to know who ran what, so the login asks for a name and jobs are stamped with it.

What this is NOT: multi-tenant. Everyone who logs in sees the same crawls,
drafts and reports, and spends the same API quota. That is the design, not an
oversight -- see the README before pointing clients at it.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from http.cookies import SimpleCookie

from common import ROOT, log, warn

AUTH_FILE = ROOT / "auth.json"          # gitignored: hash + salt, never the password
COOKIE = "studio_session"
SESSION_HOURS = 24 * 14

# Login throttling, per client address.
MAX_ATTEMPTS = 6
LOCKOUT_SECONDS = 300

_attempts: dict[str, list[float]] = {}


# ------------------------------------------------------------------ storage

def _load() -> dict:
    if not AUTH_FILE.exists():
        return {}
    try:
        return json.loads(AUTH_FILE.read_text(encoding="utf-8"))
    except Exception:
        warn("auth.json is unreadable; treating as no password set")
        return {}


def is_configured() -> bool:
    d = _load()
    return bool(d.get("hash") and d.get("salt"))


def _hash(password: str, salt: bytes) -> str:
    # scrypt is in the stdlib and is memory-hard; plenty for a shared password.
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                        n=2 ** 14, r=8, p=1, dklen=32)
    return base64.b64encode(dk).decode("ascii")


def set_password(password: str) -> None:
    if len(password) < 8:
        raise ValueError("use at least 8 characters -- this is reachable from the internet")
    salt = secrets.token_bytes(16)
    AUTH_FILE.write_text(json.dumps({
        "hash": _hash(password, salt),
        "salt": base64.b64encode(salt).decode("ascii"),
        "secret": secrets.token_hex(32),        # signs session cookies
        "set_at": time.time(),
    }, indent=2), encoding="utf-8")
    try:
        os.chmod(AUTH_FILE, 0o600)
    except OSError:
        pass                                     # best effort on Windows
    log("auth: password set")


def check_password(password: str) -> bool:
    d = _load()
    if not d.get("hash"):
        return False
    salt = base64.b64decode(d["salt"])
    return hmac.compare_digest(_hash(password, salt), d["hash"])


# ----------------------------------------------------------------- sessions

def _secret() -> bytes:
    d = _load()
    return bytes.fromhex(d.get("secret", "")) if d.get("secret") else b""


def issue(name: str) -> str:
    """A signed, expiring token. No server-side session store to keep in sync."""
    payload = json.dumps({"n": (name or "someone")[:40],
                          "e": int(time.time() + SESSION_HOURS * 3600)},
                         separators=(",", ":")).encode("utf-8")
    body = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    sig = hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).hexdigest()[:32]
    return f"{body}.{sig}"


def verify(token: str) -> dict | None:
    if not token or "." not in token or not _secret():
        return None
    body, _, sig = token.rpartition(".")
    expect = hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expect):
        return None
    try:
        pad = "=" * (-len(body) % 4)
        data = json.loads(base64.urlsafe_b64decode(body + pad))
    except Exception:
        return None
    if data.get("e", 0) < time.time():
        return None
    return {"name": data.get("n", "someone")}


def session_from_headers(headers) -> dict | None:
    raw = headers.get("Cookie")
    if not raw:
        return None
    try:
        jar = SimpleCookie()
        jar.load(raw)
    except Exception:
        return None
    morsel = jar.get(COOKIE)
    return verify(morsel.value) if morsel else None


def cookie_header(token: str, *, secure: bool) -> str:
    bits = [f"{COOKIE}={token}", "Path=/", "HttpOnly", "SameSite=Lax",
            f"Max-Age={SESSION_HOURS * 3600}"]
    if secure:
        bits.append("Secure")
    return "; ".join(bits)


def clear_cookie() -> str:
    return f"{COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"


# ---------------------------------------------------------------- throttling

def throttled(addr: str) -> int:
    """Seconds still to wait, or 0. Keeps a brute force to a crawl."""
    hits = [t for t in _attempts.get(addr, []) if time.time() - t < LOCKOUT_SECONDS]
    _attempts[addr] = hits
    if len(hits) < MAX_ATTEMPTS:
        return 0
    return int(LOCKOUT_SECONDS - (time.time() - hits[0]))


def record_failure(addr: str) -> None:
    _attempts.setdefault(addr, []).append(time.time())


def clear_failures(addr: str) -> None:
    _attempts.pop(addr, None)


# ------------------------------------------------------------------ origin

def origin_ok(headers, host: str) -> bool:
    """Reject cross-site POSTs.

    The API is JSON over POST with a cookie, so a hostile page could otherwise
    make the browser fire authenticated requests. Same-origin is required; a
    missing Origin (curl, a same-origin fetch in some browsers) is allowed
    because it cannot have been forged by a page.
    """
    origin = headers.get("Origin")
    if not origin:
        return True
    try:
        from urllib.parse import urlparse
        return urlparse(origin).netloc.lower() == (host or "").lower()
    except Exception:
        return False


# ------------------------------------------------------------------ the page

LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Blog Studio</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif&family=Inter:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{--bg:#faf7f2;--surface:#fff;--ink:#1c1a17;--muted:#6b645c;--rule:#e2dad0;
  --rule2:#cfc4b6;--accent:#7a4bd0;--bad:#a8341f;--bad-soft:#fbe8e3}
@media(prefers-color-scheme:dark){:root{--bg:#131211;--surface:#1b1a18;--ink:#f0ece6;
  --muted:#a49b91;--rule:#302d2a;--rule2:#443f3a;--accent:#b494f5;--bad:#f0836a;--bad-soft:#361a14}}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--ink);font:400 15px/1.6 Inter,-apple-system,sans-serif;
  min-height:100vh;display:grid;place-items:center;padding:24px}
.box{background:var(--surface);border:1px solid var(--rule);border-radius:14px;
  padding:34px;width:100%;max-width:380px}
h1{font-family:"Instrument Serif",Georgia,serif;font-weight:400;font-size:30px;margin-bottom:6px}
p.sub{color:var(--muted);font-size:13.5px;margin-bottom:22px}
label{display:block;font-size:11.5px;letter-spacing:.05em;text-transform:uppercase;
  color:var(--muted);margin-bottom:5px;margin-top:14px}
input{width:100%;font:inherit;padding:10px 12px;border:1px solid var(--rule2);
  border-radius:8px;background:var(--bg);color:var(--ink)}
input:focus{outline:2px solid var(--accent);outline-offset:-1px;border-color:transparent}
button{width:100%;margin-top:20px;padding:11px;border-radius:8px;border:none;
  background:var(--ink);color:var(--bg);font:500 15px Inter,sans-serif;cursor:pointer}
button:hover{opacity:.88}
.err{background:var(--bad-soft);color:var(--bad);border-radius:8px;padding:10px 13px;
  font-size:13px;margin-top:16px}
.foot{color:var(--muted);font-size:12px;margin-top:20px;line-height:1.5}
</style></head><body>
<form class="box" method="POST" action="/login">
  <h1>Blog Studio</h1>
  <p class="sub">Revnox Media &middot; SEO and answer-engine workbench</p>
  __ERROR__
  <label for="name">Your name</label>
  <input id="name" name="name" autocomplete="nickname" placeholder="Sagar" required>
  <label for="password">Shared password</label>
  <input id="password" name="password" type="password" autocomplete="current-password" required autofocus>
  <button type="submit">Sign in</button>
  <p class="foot">Everyone signing in shares one workspace &mdash; the same crawls,
  drafts and reports, and the same API quota. Your name is only used to label
  what you run.</p>
</form></body></html>"""


def login_page(error: str = "") -> bytes:
    block = f'<div class="err">{error}</div>' if error else ""
    return LOGIN_PAGE.replace("__ERROR__", block).encode("utf-8")


SETUP_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Blog Studio &mdash; no password set</title>
<style>body{font:15px/1.6 -apple-system,sans-serif;max-width:620px;margin:60px auto;
padding:0 24px;color:#1c1a17}code{background:#f4efe7;padding:2px 6px;border-radius:4px;
font-family:ui-monospace,Consolas,monospace}h1{font-size:22px;margin-bottom:12px}
.warn{background:#f8eed6;border-left:3px solid #8a6212;padding:14px 16px;border-radius:0 8px 8px 0}</style>
</head><body>
<h1>No password is set</h1>
<div class="warn">Blog Studio is reachable from outside this machine but has no
login. It holds live API keys and can spend your quota, so it will not serve
anything until a password exists.</div>
<p>Set one, then restart:</p>
<p><code>python -m studio --set-password</code></p>
</body></html>"""
