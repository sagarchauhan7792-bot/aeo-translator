"""Writer backends: transcreation, humanising, AEO restructuring, review.

Bhashini translates. It cannot rewrite for tone, restructure a document for
answer engines, or take rubric feedback and improve -- which is exactly what the
"rewrite until under 10%" requirement needs. That work goes to a writer backend.

Backends, resolved in the order configured in config.json:

  claude_local   Writes work packets to packets/ for the /aeo-rewrite slash
                 command to process inside Claude Code. Free, no API key, but it
                 needs a session open, so it cannot run unattended.
  gemini_free    Google AI Studio free tier. Needed for scheduled runs.

Both implement the same three calls, so run.py never knows which is active.
"""
from __future__ import annotations

from common import config, log, warn

from .base import Writer, WriterUnavailable, PACKET_STAGES  # noqa: F401


def get_writer(name: str | None = None) -> "Writer":
    """Return the first backend that can actually run."""
    cfg = config()["writer"]
    order = [name] if name else [cfg["backend"], *cfg.get("fallback", [])]
    errors = []

    for backend in order:
        if not backend:
            continue
        try:
            if backend == "claude_local":
                from .claude_local import ClaudeLocalWriter
                return ClaudeLocalWriter()
            if backend == "gemini_free":
                from .gemini_free import GeminiFreeWriter
                return GeminiFreeWriter()
            errors.append(f"{backend}: unknown backend")
        except WriterUnavailable as exc:
            errors.append(f"{backend}: {exc}")
            warn(f"writer backend '{backend}' unavailable: {exc}")

    raise WriterUnavailable(
        "no writer backend is available.\n  " + "\n  ".join(errors)
        + "\n  Either run inside Claude Code (backend 'claude_local'), or set "
          "GEMINI_API_KEY for the free AI Studio tier.")
