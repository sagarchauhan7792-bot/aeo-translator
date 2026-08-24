"""Launcher.

  python -m studio                          localhost only, no sign-in
  python -m studio --set-password           set the shared team password
  python -m studio --share                  public HTTPS, random address, dies on restart
  python -m studio --domain studio.x.com    public HTTPS, FIXED address, survives a restart
  python -m studio --host 0.0.0.0           reachable on your LAN (needs a password)
  python -m studio --no-auth --domain ...   fixed address, NO LOGIN -- only ever pass this
                                            because you specifically want it; see serve()
"""
from __future__ import annotations

import argparse
import getpass
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import log                                  # noqa: E402
from studio import auth                                # noqa: E402
from studio.server import serve                        # noqa: E402


def _free_port(preferred: int, host: str = "127.0.0.1") -> int:
    for port in range(preferred, preferred + 20):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return preferred


def _set_password() -> int:
    print("\nSet the shared Blog Studio password.")
    print("Everyone at Revnox who signs in shares one workspace: the same crawls,")
    print("drafts and reports, and the same API quota.\n")
    first = getpass.getpass("New password (min 8 chars): ")
    again = getpass.getpass("Again: ")
    if first != again:
        print("\nThey do not match. Nothing was changed.")
        return 1
    try:
        auth.set_password(first)
    except ValueError as exc:
        print(f"\n{exc}")
        return 1
    print("\nSaved to auth.json (gitignored, and it stores a hash, not the password).")
    print("Share it with your team over something better than email.\n")
    return 0


def _cloudflared() -> str | None:
    """Find cloudflared, wherever this machine put it."""
    import shutil
    found = shutil.which("cloudflared")
    if found:
        return found
    import os
    for cand in (Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "cloudflared" / "cloudflared.exe",
                 Path(os.environ.get("LOCALAPPDATA", "")) / "cloudflared" / "cloudflared.exe",
                 Path("C:/Program Files (x86)/cloudflared/cloudflared.exe"),
                 Path("C:/Program Files/cloudflared/cloudflared.exe")):
        if cand.exists():
            return str(cand)
    return None


def _share(port: int, *, no_auth: bool = False) -> None:
    """Start a Cloudflare quick tunnel and print the public URL when it appears."""
    exe = _cloudflared()
    if not exe:
        print("\n  cloudflared is not installed, so --share cannot open a tunnel.")
        print("  Install it per-user (no admin needed):")
        print("    winget install --id Cloudflare.cloudflared")
        print("  or download cloudflared.exe from")
        print("    https://github.com/cloudflare/cloudflared/releases")
        print("  and put it on PATH or in %LOCALAPPDATA%\\Programs\\cloudflared\\\n")
        return

    # cloudflared logs to stderr and buffers when it is not attached to a
    # terminal, so reading its pipe on Windows can sit silent for minutes and
    # then deliver everything at once. Writing to a file and tailing it is
    # duller and works, and leaves the tunnel log somewhere you can read.
    import re
    from common import ROOT
    logfile = ROOT / "cloudflared.log"

    def run() -> None:
        time.sleep(1.5)                      # let the server bind first
        with logfile.open("w", encoding="utf-8", errors="replace") as fh:
            proc = subprocess.Popen(
                [exe, "tunnel", "--url", f"http://127.0.0.1:{port}", "--no-autoupdate"],
                stdout=fh, stderr=subprocess.STDOUT)

        rx = re.compile(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com")
        deadline = time.time() + 90
        while time.time() < deadline:
            if proc.poll() is not None:
                print(f"\n  cloudflared exited early. See {logfile}\n")
                return
            try:
                m = rx.search(logfile.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                m = None
            if m:
                url = m.group(0)
                print("\n" + "=" * 68)
                print("  PUBLIC URL:  " + url, flush=True)
                print("=" * 68, flush=True)
                if no_auth:
                    print("  *** NO LOGIN *** -- anyone with this link has full access:", flush=True)
                    print("  your API quota, the crawler, drafts and reports.", flush=True)
                else:
                    print("  Anyone with this link reaches the sign-in page and needs", flush=True)
                    print("  the shared password to get past it.", flush=True)
                print("  The address changes every restart, and the tunnel dies when", flush=True)
                print("  this process stops or the machine sleeps.", flush=True)
                print("  For a fixed address, use a named tunnel -- see SETUP.md.\n")
                return
            time.sleep(1.0)
        print(f"\n  cloudflared did not report a URL within 90s. See {logfile}\n")

    threading.Thread(target=run, daemon=True, name="cloudflared").start()


TUNNEL_NAME = "blog-studio"


def _run(exe: str, *args: str) -> tuple[int, str]:
    r = subprocess.run([exe, *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60)
    return r.returncode, (r.stdout + r.stderr)


def _named_tunnel(port: int, domain: str) -> None:
    """Persistent tunnel: same URL every run, survives a restart.

    Unlike --share's quick tunnel, this needs a one-time `cloudflared tunnel
    login` (the user authorising this machine against their own Cloudflare
    account and picking the zone -- not something that can be done headlessly)
    before it exists. Every step here is idempotent: re-running this after the
    tunnel and DNS record already exist just confirms they're there and starts
    it, so the same command works for first-time setup and every run after.
    """
    exe = _cloudflared()
    if not exe:
        print("\n  cloudflared is not installed. See --share's message for how "
              "to install it.\n")
        return

    import os
    cert = Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".cloudflared" / "cert.pem"
    if not cert.exists():
        print("\n  This machine has not authorised a Cloudflare account yet.")
        print("  Run this once, complete the login in the browser it opens, then")
        print("  run this same command again:\n")
        print(f"    cloudflared tunnel login\n")
        return

    # Idempotent create: "already exists" from a prior run is fine, anything
    # else is a real failure worth stopping for.
    code, out = _run(exe, "tunnel", "create", TUNNEL_NAME)
    if code != 0 and "already exists" not in out.lower():
        print(f"\n  Could not create the tunnel:\n{out}\n")
        return
    log(f"tunnel '{TUNNEL_NAME}' ready" + (" (created)" if code == 0 else " (existing)"))

    code, out = _run(exe, "tunnel", "route", "dns", TUNNEL_NAME, domain)
    if code != 0 and "already configured" not in out.lower() and "already exists" not in out.lower():
        print(f"\n  Could not point {domain} at the tunnel:\n{out}\n")
        print("  Check that revnox.in's DNS is managed through this Cloudflare "
              "account.\n")
        return
    log(f"{domain} -> tunnel '{TUNNEL_NAME}'")

    from common import ROOT
    logfile = ROOT / "cloudflared.log"

    def run() -> None:
        time.sleep(1.5)
        with logfile.open("w", encoding="utf-8", errors="replace") as fh:
            proc = subprocess.Popen(
                [exe, "tunnel", "--url", f"http://127.0.0.1:{port}",
                 "--no-autoupdate", "run", TUNNEL_NAME],
                stdout=fh, stderr=subprocess.STDOUT)
        time.sleep(6)
        if proc.poll() is not None:
            print(f"\n  cloudflared exited early. See {logfile}\n")
            return
        print("\n" + "=" * 68)
        print(f"  PUBLIC URL:  https://{domain}", flush=True)
        print("=" * 68, flush=True)
        print("  Fixed address -- stays the same across restarts. It stops", flush=True)
        print("  working only if this process or cloudflared itself is stopped.", flush=True)
        print(f"  Tunnel log: {logfile}\n", flush=True)

    threading.Thread(target=run, daemon=True, name="cloudflared").start()


def main() -> int:
    ap = argparse.ArgumentParser(description="Blog Studio")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1",
                    help="0.0.0.0 to allow other machines (requires a password)")
    ap.add_argument("--share", action="store_true",
                    help="expose a public HTTPS URL via Cloudflare Tunnel "
                        "(random address, changes every restart)")
    ap.add_argument("--domain", default="",
                    help="expose a public HTTPS URL at this FIXED domain via a "
                        "named Cloudflare Tunnel (e.g. studio.revnox.in) -- "
                        "needs `cloudflared tunnel login` done once first")
    ap.add_argument("--no-auth", action="store_true",
                    help="disable the sign-in page entirely. Only meaningful "
                        "with --share/--domain/--host, and only pass this "
                        "because you specifically want no login on a public "
                        "URL -- see the warning this prints on startup.")
    ap.add_argument("--set-password", action="store_true",
                    help="set or change the shared team password")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    if args.set_password:
        return _set_password()

    public = args.share or args.domain or args.host not in ("127.0.0.1", "localhost", "::1")
    if public and not args.no_auth and not auth.is_configured():
        print("\nRefusing to expose this without a password.\n")
        print("A public URL with no login hands your API keys, your quota and")
        print("your Drive access to anyone who finds it. Set one first:\n")
        print("  python -m studio --set-password\n")
        print("...or pass --no-auth if you deliberately want no login at all.\n")
        return 2

    port = _free_port(args.port, args.host)
    if args.domain:
        _named_tunnel(port, args.domain)
    elif args.share:
        _share(port, no_auth=args.no_auth)
    elif not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()

    serve(args.host, port, behind_tls=bool(args.share or args.domain),
          no_auth=args.no_auth)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
