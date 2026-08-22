"""Launcher.

  python -m studio                    localhost only, no sign-in
  python -m studio --set-password     set the shared team password
  python -m studio --share            public HTTPS via Cloudflare Tunnel
  python -m studio --host 0.0.0.0     reachable on your LAN (needs a password)
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


def _share(port: int) -> None:
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
                print("  Anyone with this link reaches the sign-in page and needs", flush=True)
                print("  the shared password to get past it.", flush=True)
                print("  The address changes every restart, and the tunnel dies when", flush=True)
                print("  this process stops or the machine sleeps.", flush=True)
                print("  For a fixed address, use a named tunnel -- see SETUP.md.\n")
                return
            time.sleep(1.0)
        print(f"\n  cloudflared did not report a URL within 90s. See {logfile}\n")

    threading.Thread(target=run, daemon=True, name="cloudflared").start()


def main() -> int:
    ap = argparse.ArgumentParser(description="Blog Studio")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1",
                    help="0.0.0.0 to allow other machines (requires a password)")
    ap.add_argument("--share", action="store_true",
                    help="expose a public HTTPS URL via Cloudflare Tunnel")
    ap.add_argument("--set-password", action="store_true",
                    help="set or change the shared team password")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    if args.set_password:
        return _set_password()

    if args.share and not auth.is_configured():
        print("\nRefusing to share without a password.\n")
        print("A public URL with no login hands your API keys, your quota and")
        print("your Drive access to anyone who finds it. Set one first:\n")
        print("  python -m studio --set-password\n")
        return 2

    port = _free_port(args.port, args.host)
    if args.share:
        _share(port)
    elif not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()

    serve(args.host, port, behind_tls=args.share)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
