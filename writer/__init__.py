"""Writer backends: transcreation, humanising, AEO restructuring, review.

Bhashini translates. It cannot rewrite for tone, restructure a document for
answer engines, or take rubric feedback and improve -- which is exactly what the
"rewrite until under 10%" requirement needs. That work goes to a writer backend.

Backends, resolved in the order configured in config.json:

  claude_local   Writes work packets to packets/ for the /aeo-rewrite slash
                 command to process inside Claude Code. Free, no API key, but it
                 needs a session open, so it cannot run unattended.
  gemini_free    Google AI Studio free tier. Needed for scheduled runs.
  openai_compat  Any OpenAI-shaped endpoint. Somewhere to go when Gemini's free
                 quota is spent mid-run.

All implement the same three calls, so run.py never knows which is active.
"""
from __future__ import annotations

import os

from common import config, log, warn

from .base import Writer, WriterUnavailable, PACKET_STAGES  # noqa: F401


def headless() -> bool:
    """True where nobody can service a work packet.

    claude_local queues a request and waits for the /aeo-rewrite slash command
    to be run by a human in an open Claude Code session. On a deployed host
    there is no such session and never will be, so selecting it there is not a
    fallback -- it is a job that queues forever and a visitor watching a
    progress bar that will never move.
    """
    return bool(os.environ.get("AEO_HEADLESS") or os.environ.get("RENDER"))


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
                if headless() and not name:
                    errors.append(
                        "claude_local: needs an open Claude Code session to "
                        "answer its work packets, and this host has none")
                    continue
                from .claude_local import ClaudeLocalWriter
                return ClaudeLocalWriter()
            if backend == "gemini_free":
                from .gemini_free import GeminiFreeWriter
                return GeminiFreeWriter()
            if backend in ("openai_compat", "openai_free", "openai"):
                from .openai_free import OpenAICompatWriter
                return OpenAICompatWriter()
            errors.append(f"{backend}: unknown backend")
        except WriterUnavailable as exc:
            errors.append(f"{backend}: {exc}")
            warn(f"writer backend '{backend}' unavailable: {exc}")

    raise WriterUnavailable(
        "no writer backend is available.\n  " + "\n  ".join(errors)
        + "\n  Set GEMINI_API_KEY for the free AI Studio tier, or OPENAI_API_KEY "
          "for any OpenAI-compatible endpoint"
        + ("." if headless() else
           ", or run inside Claude Code for the 'claude_local' backend."))
