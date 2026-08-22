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
from pathlib import Path

import requests

from common import log, secret, warn
from .base import (Writer, WriterUnavailable, transcreate_prompt, review_prompt,
                   rewrite_prompt)

API = "https://generativelanguage.googleapis.com/v1beta"

# Concrete model ids before the "-latest" aliases. The aliases resolve to
# whatever Google is pointing them at, which is also what everyone else's
# untuned code calls, so they are the first to return 503 under load --
# measured: gemini-flash-latest returned 503 on half of four identical probes
# while gemini-3.6-flash answered all four. The aliases stay in the list as a
# backstop for when a pinned id is retired.
PREFERRED = ("gemini-3.6-flash", "gemini-3.1-flash-lite", "gemini-flash-latest",
             "gemini-flash-lite-latest")

# Retired models are still advertised by models.list but refuse generateContent
# with "no longer available to new users. Please update your code to use
# models/X". The replacement is in the error text, so it can be followed.
_REPLACEMENT = re.compile(r"use\s+models/([A-Za-z0-9._-]+)")

_MODEL_CACHE = Path(__file__).resolve().parent.parent / "cache" / "gemini_model.txt"


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

    # ------------------------------------------------------------- model pick
    def _probe(self, model: str) -> tuple[bool, str | None]:
        """Can this model actually be called? Returns (ok, suggested_replacement).

        Listing a model is not the same as being allowed to call it. models.list
        happily returns models that 404 on use, so the only reliable test is a
        one-token call.
        """
        try:
            r = requests.post(
                f"{API}/models/{model}:generateContent", params={"key": self.key},
                timeout=45,
                json={"contents": [{"parts": [{"text": "hi"}]}],
                      "generationConfig": {"maxOutputTokens": 1, "temperature": 0}})
            if r.status_code == 200:
                return True, None
            msg = str(r.json().get("error", {}).get("message", ""))
            hit = _REPLACEMENT.search(msg)
            if r.status_code == 429:
                warn(f"{model}: quota exhausted on this key")
            return False, (hit.group(1) if hit else None)
        except Exception as exc:
            warn(f"probing {model} failed: {exc.__class__.__name__}")
            return False, None

    def _pick_model(self) -> str:
        cached = None
        if _MODEL_CACHE.exists():
            cached = _MODEL_CACHE.read_text(encoding="utf-8").strip() or None

        tried: set[str] = set()
        queue = ([cached] if cached else []) + list(PREFERRED)

        while queue:
            model = queue.pop(0)
            if not model or model in tried:
                continue
            tried.add(model)
            ok, replacement = self._probe(model)
            if ok:
                if model != cached:
                    _MODEL_CACHE.parent.mkdir(parents=True, exist_ok=True)
                    _MODEL_CACHE.write_text(model, encoding="utf-8")
                    log(f"gemini: using {model}")
                return model
            if replacement and replacement not in tried:
                warn(f"{model} is retired; the API suggests {replacement}")
                queue.insert(0, replacement)

        # Last resort: ask what exists and try every flash-ish candidate.
        try:
            resp = requests.get(f"{API}/models", params={"key": self.key}, timeout=30)
            resp.raise_for_status()
            names = [m["name"].split("/")[-1] for m in resp.json().get("models", [])
                     if "generateContent" in m.get("supportedGenerationMethods", [])]
        except Exception as exc:
            raise WriterUnavailable(
                f"Gemini key set, but no model could be reached "
                f"({exc.__class__.__name__}). Check the key at "
                "https://aistudio.google.com/apikey") from exc

        for name in [n for n in names
                     if "flash" in n and not any(x in n for x in ("tts", "image", "thinking"))]:
            if name in tried:
                continue
            ok, _ = self._probe(name)
            if ok:
                _MODEL_CACHE.parent.mkdir(parents=True, exist_ok=True)
                _MODEL_CACHE.write_text(name, encoding="utf-8")
                warn(f"falling back to {name}")
                return name

        raise WriterUnavailable(
            "the Gemini key works but every model refused generateContent. "
            f"Tried: {', '.join(sorted(tried))}. The API listed: "
            f"{', '.join(names[:8])}")

    # -------------------------------------------------------------- transport
    def _post(self, model: str, prompt: str, *, retries: int = 3) -> dict | None:
        """One model, with backoff. Returns None if it is unavailable, not broken."""
        url = f"{API}/models/{model}:generateContent"
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.85,
                                 "responseMimeType": "application/json"},
        }
        for attempt in range(retries):
            try:
                resp = requests.post(url, params={"key": self.key}, json=body, timeout=180)
                # 429 is quota, 503 is a demand spike. Both are worth waiting out,
                # and both mean this model rather than this request is the problem.
                if resp.status_code in (429, 503):
                    if attempt == retries - 1:
                        warn(f"{model}: {resp.status_code} after {retries} tries")
                        return None
                    wait = min(45, 4 * (2 ** attempt))
                    warn(f"{model}: {resp.status_code}, waiting {wait}s")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
                return _parse_json(text)
            except Exception as exc:
                if attempt == retries - 1:
                    warn(f"{model}: {exc.__class__.__name__}")
                    return None
                time.sleep(2 ** attempt)
        return None

    def _call(self, prompt: str) -> dict:
        """Try the chosen model, then fail over to the others.

        A single model being overloaded should not fail the job when three
        equivalent ones are available on the same key.
        """
        order = [self.model] + [m for m in PREFERRED if m != self.model]
        for i, model in enumerate(order):
            result = self._post(model, prompt)
            if result is not None:
                if model != self.model:
                    warn(f"failed over from {self.model} to {model}")
                    self.model = model
                    _MODEL_CACHE.parent.mkdir(parents=True, exist_ok=True)
                    _MODEL_CACHE.write_text(model, encoding="utf-8")
                return result
        raise RuntimeError(
            "every Gemini model refused the request: " + ", ".join(order)
            + ". Usually a demand spike or the daily free quota — try again "
              "shortly, or switch the writer backend to claude_local.")

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
