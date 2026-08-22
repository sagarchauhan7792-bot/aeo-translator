"""claude_local: run the writing work inside this Claude Code session, free.

The pipeline cannot call Claude directly without an API key, so it does the next
best thing: it writes a self-contained work packet to packets/ and stops. The
`/aeo-rewrite` slash command picks up every pending packet, does the writing, and
saves the answer next to the request. Re-running the pipeline then resumes from
exactly where it stopped, because the ledger and the translation cache both
survive the pause.

The cost of this design is that it needs a session open, so it cannot run on a
schedule. That is what gemini_free is for.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from common import ROOT, config, log, write_json
from .base import (Writer, WriterPending, transcreate_prompt, review_prompt,
                   rewrite_prompt)

PACKETS = ROOT / config()["writer"].get("packet_dir", "packets")


def _pid(stage: str, slug: str, lang: str, salt: str = "") -> str:
    tag = hashlib.sha1(f"{stage}{slug}{lang}{salt}".encode("utf-8")).hexdigest()[:6]
    return f"{slug[:40]}__{lang}__{stage}__{tag}"


class ClaudeLocalWriter(Writer):
    name = "claude_local"

    def __init__(self) -> None:
        PACKETS.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ packet io
    def _request(self, stage: str, slug: str, lang: str, prompt: str,
                 *, salt: str = "", meta: dict | None = None) -> dict:
        """Return the response if it exists, else queue the request and pause."""
        pid = _pid(stage, slug, lang, salt)
        resp_path = PACKETS / f"{pid}.response.json"
        req_path = PACKETS / f"{pid}.request.json"

        if resp_path.exists():
            data = json.loads(resp_path.read_text(encoding="utf-8"))
            log(f"writer: consumed {stage} packet for {lang}", indent=1)
            return data

        write_json(req_path, {
            "id": pid,
            "stage": stage,
            "slug": slug,
            "lang": lang,
            "meta": meta or {},
            "instructions": ("Read `prompt`, do what it asks, and write the JSON "
                             "answer to the sibling file named in `response_file`. "
                             "Reply with JSON only -- no prose, no code fences."),
            "response_file": resp_path.name,
            "prompt": prompt,
        })
        log(f"writer: queued {stage} packet for {lang} -> {req_path.name}", indent=1)
        raise WriterPending([req_path.name])

    # ------------------------------------------------------------- the calls
    def generate(self, prompt: str, *, stage: str, slug: str,
                 lang: str = "en", salt: str = "") -> dict:
        return self._request(stage, slug, lang, prompt, salt=salt)

    def transcreate(self, *, lang: str, slug: str, source_md: str, mt_md: str,
                    profile: dict, keywords: list[dict], aeo_cfg: dict) -> dict:
        return self._request(
            "transcreate", slug, lang,
            transcreate_prompt(lang=lang, source_md=source_md, mt_md=mt_md,
                               profile=profile, keywords=keywords, aeo_cfg=aeo_cfg),
            meta={"words_source": len(source_md.split())})

    def review(self, *, lang: str, slug: str, text: str, salt: str = "") -> dict:
        return self._request("review", slug, lang,
                             review_prompt(lang=lang, text=text), salt=salt)

    def rewrite(self, *, lang: str, slug: str, text_md: str, brief: list[str],
                profile: dict, ai_pct: float, target: float, attempt: int = 1) -> dict:
        return self._request(
            "rewrite", slug, lang,
            rewrite_prompt(lang=lang, text_md=text_md, brief=brief, profile=profile,
                           ai_pct=ai_pct, target=target),
            salt=f"pass{attempt}", meta={"attempt": attempt, "ai_pct": ai_pct})


# ------------------------------------------------------------------ helpers
# Used by the /aeo-rewrite command to find and report work.

def pending() -> list[Path]:
    if not PACKETS.exists():
        return []
    out = []
    for req in sorted(PACKETS.glob("*.request.json")):
        if not (PACKETS / req.name.replace(".request.json", ".response.json")).exists():
            out.append(req)
    return out


def response_path(req: Path) -> Path:
    return PACKETS / req.name.replace(".request.json", ".response.json")
