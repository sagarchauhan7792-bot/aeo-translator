"""openai_compat: any OpenAI-shaped chat endpoint, as a second writer backend.

One honest correction up front, because the request that produced this file
asked for "the free ChatGPT API": **OpenAI has no free API tier.** api.openai.com
requires a funded account, and no amount of configuration here changes that.
What OpenAI does have is a request format that a dozen other providers copied
exactly -- several of which do have free tiers -- so this backend speaks that
format and lets the endpoint be pointed anywhere:

  openai_api_key.txt    the key (required)
  openai_base_url.txt   default https://api.openai.com/v1
  openai_model.txt      default gpt-4o-mini

Point base_url at OpenAI and it is ChatGPT's model behind a paid key. Point it
at any other OpenAI-compatible host and it is that provider, free tier or not,
with no code change. Either way its job here is the same: somewhere to go when
Gemini's free quota is spent mid-run, so a translation or a rewrite does not die
at 429 with half the languages done.

Same three calls as every other backend, so run.py cannot tell which is active.
"""
from __future__ import annotations

import json
import re
import time

import requests

from common import log, secret, warn
from .base import (Writer, WriterUnavailable, transcreate_prompt, review_prompt,
                   rewrite_prompt)

DEFAULT_BASE = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"


class OpenAICompatWriter(Writer):
    name = "openai_compat"

    def __init__(self, model: str | None = None):
        self.key = secret("OPENAI_API_KEY", "openai_api_key.txt")
        if not self.key:
            raise WriterUnavailable(
                "OPENAI_API_KEY is not set. Note that OpenAI's API is paid -- "
                "there is no free ChatGPT tier. Either fund a key at "
                "https://platform.openai.com/api-keys, or point this backend at "
                "an OpenAI-compatible provider that does have a free tier by "
                "writing its base URL to openai_base_url.txt and its model name "
                "to openai_model.txt.")
        self.base = (secret("OPENAI_BASE_URL", "openai_base_url.txt")
                     or DEFAULT_BASE).rstrip("/")
        self.model = (model or secret("OPENAI_MODEL", "openai_model.txt")
                      or DEFAULT_MODEL).strip()

    # -------------------------------------------------------------- transport
    def _call(self, prompt: str, *, retries: int = 3) -> dict:
        url = f"{self.base}/chat/completions"
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.85,
            # Providers that support it return strict JSON; the ones that do not
            # ignore the field, and _parse_json handles the fenced reply anyway.
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {self.key}"}

        last = ""
        for attempt in range(retries):
            try:
                resp = requests.post(url, headers=headers, json=body, timeout=180)
                # 429 is quota or rate, 5xx is the provider. Both are the
                # endpoint's problem rather than this request's, so back off.
                if resp.status_code == 429 or resp.status_code >= 500:
                    last = f"HTTP {resp.status_code}"
                    if attempt == retries - 1:
                        break
                    wait = min(45, 4 * (2 ** attempt))
                    warn(f"{self.model}: {last}, waiting {wait}s")
                    time.sleep(wait)
                    continue
                if resp.status_code == 400 and "response_format" in resp.text:
                    # Older or partial implementations reject the JSON mode field
                    # rather than ignoring it. Drop it and let _parse_json cope.
                    body.pop("response_format", None)
                    continue
                resp.raise_for_status()
                text = resp.json()["choices"][0]["message"]["content"]
                return _parse_json(text)
            except Exception as exc:
                last = f"{exc.__class__.__name__}: {exc}"
                if attempt == retries - 1:
                    break
                time.sleep(2 ** attempt)
        raise RuntimeError(
            f"{self.base} refused the request after {retries} tries ({last}). "
            "If this is api.openai.com, check the key is funded -- there is no "
            "free tier.")

    # -------------------------------------------------------------- the calls
    def generate(self, prompt: str, *, stage: str, slug: str,
                 lang: str = "en", salt: str = "") -> dict:
        log(f"openai({self.model}): {stage} {lang}", indent=1)
        return self._call(prompt)

    def transcreate(self, *, lang: str, slug: str, source_md: str, mt_md: str,
                    profile: dict, keywords: list[dict], aeo_cfg: dict) -> dict:
        log(f"openai({self.model}): transcreate {lang}", indent=1)
        return self._call(transcreate_prompt(
            lang=lang, source_md=source_md, mt_md=mt_md, profile=profile,
            keywords=keywords, aeo_cfg=aeo_cfg))

    def review(self, *, lang: str, slug: str, text: str, salt: str = "") -> dict:
        log(f"openai({self.model}): review {lang}", indent=1)
        return self._call(review_prompt(lang=lang, text=text))

    def rewrite(self, *, lang: str, slug: str, text_md: str, brief: list[str],
                profile: dict, ai_pct: float, target: float, attempt: int = 1) -> dict:
        log(f"openai({self.model}): rewrite {lang} pass {attempt}", indent=1)
        return self._call(rewrite_prompt(
            lang=lang, text_md=text_md, brief=brief, profile=profile,
            ai_pct=ai_pct, target=target))


def _parse_json(text: str) -> dict:
    """Models wrap JSON in fences even when told not to."""
    text = (text or "").strip()
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
