"""Shared plumbing: config, UTF-8 safe IO, slugs, and the resumable job ledger.

Everything else in the pipeline imports from here so there is exactly one place
that knows where files live and how state is recorded.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent

# Windows consoles default to cp1252 and will crash on Devanagari the moment we
# print a translated line. Force UTF-8 on both streams before anything runs.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


# --------------------------------------------------------------------------- io

def read_json(path: str | Path, default: Any = None) -> Any:
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    if not p.exists():
        if default is not None:
            return default
        raise FileNotFoundError(f"missing file: {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def write_json(path: str | Path, data: Any, indent: int = 2) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=indent), encoding="utf-8")
    return p


def write_text(path: str | Path, text: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# ----------------------------------------------------------------------- config

_CONFIG: dict | None = None
_GLOSSARY: dict | None = None


def config() -> dict:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = read_json("config.json")
    return _CONFIG


def glossary() -> dict:
    global _GLOSSARY
    if _GLOSSARY is None:
        _GLOSSARY = read_json("glossary.json")
    return _GLOSSARY


def languages() -> list[dict]:
    return config()["languages"]


# English is the source language, not a translation target, so it is not in
# config.json's `languages` list -- but schema, slugs and the audit all handle
# English documents and legitimately look it up.
_ENGLISH = {"code": "en", "name": "English", "native": "English", "bhashini": "en",
            "script": "Latin", "range": ["0000", "007F"], "region": "India",
            "honorific": "formal"}


def lang_by_code(code: str) -> dict:
    if code == "en":
        return dict(_ENGLISH)
    for entry in languages():
        if entry["code"] == code:
            return entry
    known = ", ".join(e["code"] for e in languages())
    raise KeyError(f"unknown language {code!r}. configured: en, {known}")


def site_profile(url_or_host: str | None) -> dict:
    """Match a URL against config site_profiles, falling back to _default."""
    profiles = config()["site_profiles"]
    default = dict(profiles.get("_default", {}))
    if not url_or_host:
        return default
    host = url_or_host
    if "//" in host:
        host = host.split("//", 1)[1]
    host = host.split("/", 1)[0].lower()
    host = host[4:] if host.startswith("www.") else host
    for key, prof in profiles.items():
        if key.startswith("_"):
            continue
        if host == key or host.endswith("." + key):
            merged = dict(default)
            merged.update(prof)
            return merged
    return default


# ------------------------------------------------------------------ credentials

class MissingCredential(RuntimeError):
    """Raised with a human-readable fix, never a bare stack trace.

    Every external dependency in this pipeline is optional at build time and
    required at run time. When one is absent the operator needs to know which
    file to create and where to get it -- not which line threw a KeyError.
    """

    def __init__(self, what: str, where: str, how: str):
        super().__init__(f"MISSING CREDENTIAL: {what}\n  Needed for : {where}\n  How to fix : {how}")
        self.what, self.where, self.how = what, where, how


def secret(name: str, filename: str | None = None) -> str | None:
    """Read a secret from the environment, then from a bare file next to the code."""
    val = os.environ.get(name)
    if val:
        return val.strip()
    if filename:
        p = ROOT / filename
        if p.exists():
            return p.read_text(encoding="utf-8").strip()
    return None


# ------------------------------------------------------------------------ slugs

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(text: str, max_len: int = 60) -> str:
    """ASCII slug. Indic text is transliterated by aeo.py before it reaches here."""
    text = unicodedata.normalize("NFKD", text or "")
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = _SLUG_STRIP.sub("-", text).strip("-")
    if len(text) > max_len:
        text = text[:max_len].rsplit("-", 1)[0]
    return text or "untitled"


def word_count(text: str) -> int:
    """Count words in a way that works for both Latin and Indic scripts."""
    return len([t for t in re.split(r"\s+", (text or "").strip()) if t])


# ----------------------------------------------------------------------- ledger

LEDGER = ROOT / "state.jsonl"


@dataclass
class JobRecord:
    """One (source, language) pair. Append-only; latest record for a key wins."""
    key: str
    source: str
    slug: str
    lang: str
    status: str = "pending"          # pending|translated|scored|rewritten|published|failed|needs_human_review
    ai_pct: float | None = None
    fidelity: float | None = None
    grammar: float | None = None
    human_likeness: float | None = None
    aeo: float | None = None
    passes: int = 0
    doc_url: str | None = None
    words: int | None = None
    error: str | None = None
    ts: float = field(default_factory=time.time)

    def dict(self) -> dict:
        return asdict(self)


def job_key(source: str, lang: str) -> str:
    return f"{source}::{lang}"


def ledger_append(rec: JobRecord) -> None:
    rec.ts = time.time()
    with LEDGER.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec.dict(), ensure_ascii=False) + "\n")


def ledger_load() -> dict[str, dict]:
    """Collapse the append-only log into current state, last write wins."""
    state: dict[str, dict] = {}
    if not LEDGER.exists():
        return state
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # a torn final line from a killed run is not fatal
        state[rec["key"]] = rec
    return state


def ledger_done(source: str, lang: str) -> bool:
    rec = ledger_load().get(job_key(source, lang))
    return bool(rec and rec.get("status") == "published")


# ------------------------------------------------------------------------- logs

_T0 = time.time()


def log(msg: str, *, indent: int = 0) -> None:
    print(f"[{time.time() - _T0:7.1f}s] {'  ' * indent}{msg}", flush=True)


def warn(msg: str) -> None:
    log(f"WARN  {msg}")


def chunk_text(text: str, limit: int) -> list[str]:
    """Split on sentence boundaries, never mid-sentence, staying under `limit` chars.

    Indic sentences end in danda or purna viram as often as a full stop, so all
    three terminators count. A single sentence longer than the limit is emitted
    whole rather than cut -- a truncated sentence corrupts the translation far
    worse than an oversized request, which the API will simply reject and retry.
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    parts = re.split(r"(?<=[.!?।॥])\s+", text)
    out: list[str] = []
    buf = ""
    for part in parts:
        if buf and len(buf) + 1 + len(part) > limit:
            out.append(buf)
            buf = part
        else:
            buf = f"{buf} {part}".strip()
    if buf:
        out.append(buf)
    return out
