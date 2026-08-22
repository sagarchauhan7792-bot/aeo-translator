"""gemini_free: Google AI Studio free tier, for unattended and scheduled runs.

claude_local is the default because it costs nothing and needs no key, but it
needs a session open. Anything on a schedule needs this backend.

The model id is discovered from the API rather than hard-coded, so a model being
retired degrades to "picks the next available flash model" instead of "every run
fails with 404".
"""
from __future__ import annotations

import json
import re
import time

import requests

from common import log, secret, warn
from .base import (Writer, WriterUnavailable, transcreate_prompt, review_prompt,
                   rewrite_prompt)

API = "https://generativelanguage.googleapis.com/v1beta"
PREFERRED = ("gemini-2.5-flash", "gemini-2.0-flash", "gemini-flash-latest")


class GeminiFreeWriter(Writer):
    name = "gemini_free"

    def __init__(self, model: str | None = None):
        self.key = secret("GEMINI_API_KEY", "gemini_api_key.txt")
        if not self.key:
            raise WriterUnavailable(
                "GEMINI_API_KEY is not set. Get a free key at "
                "https://aistudio.google.com/apikey, then set the environment "
                "variable or save it to aeo-translator/gemini_api_key.txt")
        self.model = model or self._pick_model()

    def _pick_model(self) -> str:
        try:
            resp = requests.get(f"{API}/models", params={"key": self.key}, timeout=30)
            resp.raise_for_status()
            names = [m["name"].split("/")[-1] for m in resp.json().get("models", [])
                     if "generateContent" in m.get("supportedGenerationMethods", [])]
        except Exception as exc:
            warn(f"could not list Gemini models ({exc.__class__.__name__}); "
                 f"defaulting to {PREFERRED[0]}")
            return PREFERRED[0]

        for want in PREFERRED:
            if want in names:
                return want
        flash = [n for n in names if "flash" in n and "thinking" not in n]
        if flash:
            warn(f"none of {PREFERRED} available; using {flash[0]}")
            return flash[0]
        raise WriterUnavailable(
            f"no usable Gemini model found. API offered: {', '.join(names[:10])}")

    # -------------------------------------------------------------- transport
    def _call(self, prompt: str, *, retries: int = 4) -> dict:
        url = f"{API}/models/{self.model}:generateContent"
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.85, "responseMimeType": "application/json"},
        }
        last: Exception | None = None
        for attempt in range(retries):
            try:
                resp = requests.post(url, params={"key": self.key}, json=body, timeout=180)
                if resp.status_code == 429:
                    wait = min(60, 5 * (2 ** attempt))
                    warn(f"gemini rate limited; waiting {wait}s")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                data = resp.json()
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                return _parse_json(text)
            except Exception as exc:
                last = exc
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
        raise RuntimeError(f"gemini call failed after {retries} attempts: {last}")

    # -------------------------------------------------------------- the calls
    def generate(self, prompt: str, *, stage: str, slug: str,
                 lang: str = "en", salt: str = "") -> dict:
        log(f"gemini({self.model}): {stage} {lang}", indent=1)
        return self._call(prompt)

    def transcreate(self, *, lang: str, slug: str, source_md: str, mt_md: str,
                    profile: dict, keywords: list[dict], aeo_cfg: dict) -> dict:
        log(f"gemini({self.model}): transcreate {lang}", indent=1)
        return self._call(transcreate_prompt(
            lang=lang, source_md=source_md, mt_md=mt_md, profile=profile,
            keywords=keywords, aeo_cfg=aeo_cfg))

    def review(self, *, lang: str, slug: str, text: str, salt: str = "") -> dict:
        log(f"gemini({self.model}): review {lang}", indent=1)
        return self._call(review_prompt(lang=lang, text=text))

    def rewrite(self, *, lang: str, slug: str, text_md: str, brief: list[str],
                profile: dict, ai_pct: float, target: float, attempt: int = 1) -> dict:
        log(f"gemini({self.model}): rewrite {lang} pass {attempt}", indent=1)
        return self._call(rewrite_prompt(
            lang=lang, text_md=text_md, brief=brief, profile=profile,
            ai_pct=ai_pct, target=target))


def _parse_json(text: str) -> dict:
    """Models wrap JSON in fences even when told not to."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise
