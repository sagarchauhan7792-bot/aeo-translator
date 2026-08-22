"""Launcher: python -m studio  (or blog.bat)."""
from __future__ import annotations

import argparse
import socket
import sys
import threading
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from studio.server import serve                      # noqa: E402


def _free_port(preferred: int, host: str = "127.0.0.1") -> int:
    """Use the preferred port, or the next free one if it is taken."""
    for port in range(preferred, preferred + 20):
        with socket.socket() as s:
            if s.connect_ex((host, port)) != 0:
                return port
    return preferred


def main() -> int:
    ap = argparse.ArgumentParser(description="Blog Studio")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    port = _free_port(args.port, args.host)
    url = f"http://{args.host}:{port}"
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    serve(args.host, port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
