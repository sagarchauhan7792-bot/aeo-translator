"""Bhashini (ULCA / Dhruva) translation client.

Two-call protocol:
  1. Pipeline config  POST meity-auth .../getModelsPipeline   headers: userID, ulcaApiKey
     -> returns the inference callbackUrl, a per-session auth header, and the
        serviceId available for each language pair.
  2. Pipeline compute POST <callbackUrl>                       headers: <name>: <value>
     -> the actual NMT call, which accepts a batch of strings.

Everything is cached on disk keyed by (text, src, tgt, serviceId): a rerun of the
same article costs zero network calls, which matters because the rewrite loop can
re-translate the same document several times for back-translation checks.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

import requests

from common import (ROOT, MissingCredential, chunk_text, config, log, secret,
                    warn, write_json)
from extract import Article, Block
from patterns import normalise_digits

_CFG = config()["bhashini"]
CACHE_DIR = ROOT / _CFG.get("cache_dir", "cache")
CONFIG_CACHE = CACHE_DIR / "pipeline_config.json"
CONFIG_TTL = 6 * 3600            # the session auth value is short-lived

# Tokens that survive NMT. Purely ASCII-alphanumeric with no spaces: every
# subword tokenizer copies these through unchanged, whereas bracket characters
# get re-spaced and unicode sentinels sometimes get dropped entirely.
_TOKEN = "zq{}qz"
_TOKEN_RX = re.compile(r"zq\s*(\d+)\s*qz", re.I)

# Only mask things that must never be re-worded. Numbers stay visible so the
# engine can inflect around them correctly; they are verified after the fact by
# patterns.diff_protected instead.
_MASK_RX = [
    re.compile(r"https?://[^\s<>\"')]+"),
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
    re.compile(r"(?:\+91[\s-]?)?[6-9]\d{9}\b"),
]


class BhashiniError(RuntimeError):
    pass


def _mask(text: str) -> tuple[str, dict[str, str]]:
    mapping: dict[str, str] = {}
    idx = 0

    def sub(m: re.Match) -> str:
        nonlocal idx
        token = _TOKEN.format(idx)
        mapping[token] = m.group(0)
        idx += 1
        return token

    for rx in _MASK_RX:
        text = rx.sub(sub, text)
    return text, mapping


def _unmask(text: str, mapping: dict[str, str]) -> str:
    if not mapping:
        return text
    ordered = {int(re.search(r"\d+", k).group()): v for k, v in mapping.items()}

    def sub(m: re.Match) -> str:
        return ordered.get(int(m.group(1)), m.group(0))

    out = _TOKEN_RX.sub(sub, text)
    for token, val in mapping.items():           # belt and braces
        out = out.replace(token, val)
    return out


class Bhashini:
    def __init__(self, user_id: str | None = None, api_key: str | None = None):
        self.user_id = user_id or secret("BHASHINI_USER_ID", "bhashini_user_id.txt")
        self.api_key = api_key or secret("BHASHINI_API_KEY", "bhashini_api_key.txt")
        self.pipeline_id = _CFG["pipeline_id"]
        self._cfg: dict | None = None
        self.calls = 0
        self.cache_hits = 0
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (CACHE_DIR / "translations").mkdir(exist_ok=True)

    # ------------------------------------------------------------- credentials
    def require_creds(self) -> None:
        if not self.user_id or not self.api_key:
            raise MissingCredential(
                "Bhashini ULCA credentials",
                "translating and back-translating article text",
                "Register free at https://bhashini.gov.in (Sign Up -> ULCA -> "
                "My Profile -> API Key). Then either set BHASHINI_USER_ID and "
                "BHASHINI_API_KEY, or save each value in aeo-translator/"
                "bhashini_user_id.txt and bhashini_api_key.txt.")

    # ------------------------------------------------------------ pipeline cfg
    def pipeline_config(self, *, force: bool = False) -> dict:
        if self._cfg and not force:
            return self._cfg
        if not force and CONFIG_CACHE.exists():
            cached = json.loads(CONFIG_CACHE.read_text(encoding="utf-8"))
            if time.time() - cached.get("_fetched", 0) < CONFIG_TTL:
                self._cfg = cached
                return cached

        self.require_creds()
        log("bhashini: fetching pipeline config")
        resp = requests.post(
            _CFG["auth_url"],
            headers={"userID": self.user_id, "ulcaApiKey": self.api_key,
                     "Content-Type": "application/json"},
            json={"pipelineTasks": [{"taskType": "translation"}],
                  "pipelineRequestConfig": {"pipelineId": self.pipeline_id}},
            timeout=_CFG["timeout"],
        )
        if resp.status_code in (401, 403):
            raise BhashiniError(
                f"Bhashini rejected the credentials (HTTP {resp.status_code}). "
                "Check BHASHINI_USER_ID / BHASHINI_API_KEY against your ULCA profile.")
        resp.raise_for_status()
        data = resp.json()
        data["_fetched"] = time.time()
        write_json(CONFIG_CACHE, data)
        self._cfg = data
        return data

    def endpoint(self) -> tuple[str, dict[str, str]]:
        cfg = self.pipeline_config()
        ep = cfg.get("pipelineInferenceAPIEndPoint") or {}
        url = ep.get("callbackUrl")
        key = ep.get("inferenceApiKey") or {}
        name, value = key.get("name"), key.get("value")
        if not (url and name and value):
            raise BhashiniError(
                "pipeline config did not contain a usable inference endpoint; "
                f"got keys {sorted(ep)}. Re-run with --refresh-pipeline.")
        return url, {name: value, "Content-Type": "application/json"}

    def service_id(self, src: str, tgt: str) -> str:
        """Find the NMT serviceId that covers this language pair."""
        cfg = self.pipeline_config()
        fallback = None
        for entry in cfg.get("pipelineResponseConfig", []):
            if entry.get("taskType") not in (None, "translation"):
                continue
            for conf in entry.get("config", []):
                lang = conf.get("language", {})
                sid = conf.get("serviceId")
                if not sid:
                    continue
                fallback = fallback or sid
                if lang.get("sourceLanguage") == src and lang.get("targetLanguage") == tgt:
                    return sid
        if fallback:
            warn(f"no exact serviceId for {src}->{tgt}; using {fallback}")
            return fallback
        raise BhashiniError(f"no translation service advertised for {src} -> {tgt}")

    # ------------------------------------------------------------------ cache
    @staticmethod
    def _cache_path(text: str, src: str, tgt: str, sid: str) -> Path:
        digest = hashlib.sha256(f"{sid}|{src}|{tgt}|{text}".encode("utf-8")).hexdigest()[:32]
        return CACHE_DIR / "translations" / f"{digest}.json"

    # ------------------------------------------------------------- the call
    def _compute(self, texts: list[str], src: str, tgt: str, sid: str) -> list[str]:
        url, headers = self.endpoint()
        payload = {
            "pipelineTasks": [{
                "taskType": "translation",
                "config": {
                    "language": {"sourceLanguage": src, "targetLanguage": tgt},
                    "serviceId": sid,
                },
            }],
            "inputData": {"input": [{"source": t} for t in texts]},
        }

        last: Exception | None = None
        for attempt in range(_CFG["max_retries"]):
            try:
                resp = requests.post(url, headers=headers, json=payload,
                                     timeout=_CFG["timeout"])
                if resp.status_code == 401:
                    # session key expired mid-run: refetch config once and retry
                    self.pipeline_config(force=True)
                    url, headers = self.endpoint()
                    raise BhashiniError("inference key expired; refreshed")
                resp.raise_for_status()
                self.calls += 1
                data = resp.json()
                out = (data.get("pipelineResponse") or [{}])[0].get("output") or []
                results = [(o.get("target") or "").strip() for o in out]
                if len(results) != len(texts):
                    raise BhashiniError(
                        f"asked for {len(texts)} segments, got {len(results)}")
                return results
            except Exception as exc:
                last = exc
                if attempt < _CFG["max_retries"] - 1:
                    time.sleep(2 ** attempt)
        raise BhashiniError(f"translation failed after retries: {last}")

    # ------------------------------------------------------------------- api
    def translate_texts(self, texts: list[str], src: str, tgt: str,
                        *, batch_chars: int | None = None) -> list[str]:
        """Translate a list of strings, preserving order. Cached per string."""
        if src == tgt:
            return list(texts)

        sid = self.service_id(src, tgt)
        limit = _CFG["chunk_chars"]
        batch_chars = batch_chars or limit * 4

        results: list[str | None] = [None] * len(texts)
        pending: list[tuple[int, int, str, dict]] = []   # (idx, part_no, masked, mapping)
        part_counts: dict[int, int] = {}

        for i, raw in enumerate(texts):
            raw = (raw or "").strip()
            if not raw:
                results[i] = ""
                part_counts[i] = 0
                continue
            parts = chunk_text(raw, limit)
            part_counts[i] = len(parts)
            for j, part in enumerate(parts):
                masked, mapping = _mask(part)
                cached = self._cache_path(masked, src, tgt, sid)
                if cached.exists():
                    self.cache_hits += 1
                    continue
                pending.append((i, j, masked, mapping))

        # Send everything not already cached, batched by total character budget.
        batch: list[tuple[int, int, str, dict]] = []
        size = 0
        for item in pending:
            if batch and size + len(item[2]) > batch_chars:
                self._flush(batch, src, tgt, sid)
                batch, size = [], 0
            batch.append(item)
            size += len(item[2])
        if batch:
            self._flush(batch, src, tgt, sid)

        # Reassemble from cache in original order.
        for i, raw in enumerate(texts):
            if results[i] == "":
                continue
            parts = chunk_text((raw or "").strip(), limit)
            pieces = []
            for part in parts:
                masked, mapping = _mask(part)
                cached = self._cache_path(masked, src, tgt, sid)
                if cached.exists():
                    got = json.loads(cached.read_text(encoding="utf-8"))["target"]
                else:
                    warn(f"segment {i} missing from cache after flush; kept source")
                    got = part
                pieces.append(_unmask(got, mapping))
            results[i] = normalise_digits(" ".join(p for p in pieces if p).strip())

        return [r if r is not None else "" for r in results]

    def _flush(self, batch, src: str, tgt: str, sid: str) -> None:
        texts = [b[2] for b in batch]
        log(f"bhashini: {src}->{tgt}  {len(texts)} segments, {sum(map(len, texts))} chars", indent=1)
        out = self._compute(texts, src, tgt, sid)
        for (idx, part, masked, _mapping), target in zip(batch, out):
            self._cache_path(masked, src, tgt, sid).write_text(
                json.dumps({"source": masked, "target": target,
                            "src": src, "tgt": tgt, "serviceId": sid},
                           ensure_ascii=False), encoding="utf-8")

    # --------------------------------------------------------------- article
    def translate_article(self, art: Article, tgt: str, *, src: str = "en") -> Article:
        """Translate every human-visible string in an Article, keeping block order."""
        fields = [art.title, art.meta_description]
        block_texts = [b.text for b in art.blocks]
        alts = [im.get("alt", "") for im in art.images]
        faq_q = [f["q"] for f in art.faqs]
        faq_a = [f["a"] for f in art.faqs]

        payload = fields + block_texts + alts + faq_q + faq_a
        translated = self.translate_texts(payload, src, tgt)

        cut = 0
        out = Article.from_dict(art.dict())
        out.lang = tgt
        out.title = translated[cut]; cut += 1
        out.meta_description = translated[cut]; cut += 1
        out.blocks = [Block(type=b.type, text=t)
                      for b, t in zip(art.blocks, translated[cut:cut + len(block_texts)])]
        cut += len(block_texts)
        out.images = [{**im, "alt": t}
                      for im, t in zip(art.images, translated[cut:cut + len(alts)])]
        cut += len(alts)
        qs = translated[cut:cut + len(faq_q)]; cut += len(faq_q)
        as_ = translated[cut:cut + len(faq_a)]
        out.faqs = [{"q": q, "a": a} for q, a in zip(qs, as_)]
        out.meta = dict(art.meta or {})
        out.meta["translated_from"] = src
        out.meta["mt_engine"] = "bhashini"
        return out

    def back_translate(self, art: Article, *, src_lang: str, to: str = "en") -> Article:
        """Round-trip a translated article back to English for the fidelity check.

        This is the single most valuable quality signal in the pipeline: a fact
        that survives the round trip was genuinely carried across; one that does
        not was either dropped or invented.
        """
        return self.translate_article(art, to, src=src_lang)


class MyMemory(Bhashini):
    """Stand-in engine for testing the pipeline without Bhashini credentials.

    MyMemory's free public API needs no key. It is heavily rate limited (roughly
    1000 words/day anonymously) and its Indic quality is well below Bhashini's,
    so it is NOT a production path -- but it lets the whole pipeline be run and
    verified end to end before the ULCA registration comes through, and it is
    what produced the raw-MT class the scorer was calibrated against.

    Selected with `--engine mymemory`. Every article it touches is stamped so
    the origin is visible in the output and in the tracker.
    """

    ENDPOINT = "https://api.mymemory.translated.net/get"
    LIMIT = 450          # anonymous requests are capped at ~500 bytes

    def __init__(self, email: str | None = None):
        super().__init__(user_id="mymemory", api_key="none")
        self.email = email or secret("MYMEMORY_EMAIL")

    def require_creds(self) -> None:
        warn("engine=mymemory: test engine, not Bhashini. Rate limited and lower "
             "quality; use it to verify the pipeline, not to ship client work.")

    def service_id(self, src: str, tgt: str) -> str:
        return "mymemory"

    def _compute(self, texts: list[str], src: str, tgt: str, sid: str) -> list[str]:
        out = []
        for text in texts:
            params = {"q": text[:self.LIMIT], "langpair": f"{src}|{tgt}"}
            if self.email:
                params["de"] = self.email
            last: Exception | None = None
            for attempt in range(_CFG["max_retries"]):
                try:
                    resp = requests.get(self.ENDPOINT, params=params, timeout=60)
                    if resp.status_code == 429:
                        # Not worth retrying: the anonymous tier is a daily word
                        # budget, not a rate limit that clears in seconds.
                        raise BhashiniError(
                            "MyMemory's free daily quota is used up. This is the "
                            "TEST engine, not Bhashini -- its anonymous tier is "
                            "roughly 1000 words a day. Options: set MYMEMORY_EMAIL "
                            "to raise it, wait for the daily reset, or add Bhashini "
                            "credentials and switch the engine to bhashini.")
                    resp.raise_for_status()
                    data = resp.json()
                    if data.get("responseStatus") != 200:
                        raise BhashiniError(
                            f"mymemory: {data.get('responseDetails', 'error')} "
                            "(the anonymous daily quota is small; set MYMEMORY_EMAIL "
                            "to raise it, or supply Bhashini credentials)")
                    out.append((data["responseData"]["translatedText"] or "").strip())
                    self.calls += 1
                    break
                except BhashiniError:
                    raise                      # quota is terminal, not transient
                except Exception as exc:
                    last = exc
                    if attempt < _CFG["max_retries"] - 1:
                        time.sleep(2 ** attempt)
            else:
                raise BhashiniError(f"mymemory failed: {last}")
            time.sleep(1.2)                      # be polite to a free endpoint
        return out

    def translate_article(self, art: Article, tgt: str, *, src: str = "en") -> Article:
        out = super().translate_article(art, tgt, src=src)
        out.meta["mt_engine"] = "mymemory (TEST ENGINE -- not Bhashini)"
        return out


# ----------------------------------------------------------- LLM engines
#
# Two service ids, because the two directions are different jobs and must not
# share a cache. Bumping the version suffix is also how a prompt change
# invalidates everything translated by the previous one: entries keyed by the
# old id are simply never looked up again, and nothing has to be deleted.
_TONE_SID = "llm-tone-v1"
_LITERAL_SID = "llm-literal-v1"

_TOKEN_RULE = (
    "Some segments contain tokens shaped like zqNqz (the letters z, q, then "
    "digits, then q, z). Copy those through completely unchanged, in place -- "
    "they are not words, do not translate them, do not alter the digits inside."
)

_REPLY_RULE = (
    'Reply with JSON only, one entry per segment, keyed by its number as a '
    'string: {"translations": {"0": "...", "1": "...", ...}}'
)


def _literal_prompt(numbered: str, src_name: str, tgt_name: str) -> str:
    """Back-translation. This one MUST stay word-for-word.

    The English round trip is not a deliverable, it is the measuring
    instrument: quality.score_fidelity compares content words and protected
    spans between the source and this. A back-translation that improved the
    phrasing would report drift that is not in the target text, and hide drift
    that is.
    """
    return "\n".join([
        f"Translate each numbered segment from {src_name} to {tgt_name}.",
        "",
        "This is a fidelity check, not writing. Translate as literally as the "
        "grammar allows, preserve the sentence structure of the original, and "
        "do not improve, condense, expand or reorder anything. No commentary. "
        "Do not merge or split segments.",
        "", _TOKEN_RULE,
        "", f"SEGMENTS:\n{numbered}",
        "", _REPLY_RULE,
    ])


def _tone_prompt(numbered: str, src_name: str, tgt_name: str, tgt_code: str) -> str:
    """Forward translation, written the way the language is actually written.

    A word-for-word rendering is what an NMT box produces, and every measured
    difference between native prose and machine output in features.py is a
    consequence of it: prepositions carried across literally, English relative
    clauses stacked up, English comma rhythm, uniform sentence length. An LLM
    does not have to make those trades, so this asks it not to -- using the same
    calibrated rubric the transcreation pass is held to, rather than a vague
    instruction to sound natural.

    What it may not do is change the facts. Numbers, dosages, locked brand
    terms, claim strength and hedges are all checked after the reply lands, and
    a violation blocks publication, so they are stated as hard constraints
    rather than preferences.
    """
    from writer.base import language_rubric, glossary_rules
    return "\n".join([
        f"You are a {tgt_name} writer. Render each numbered segment from "
        f"{src_name} into {tgt_name} as it would have been written in "
        f"{tgt_name} in the first place -- not as a word-for-word conversion.",
        "",
        "Restructure sentences where the natural phrasing differs. Choose the "
        "idiom a reader of this language expects. Keep the register consistent "
        "across every segment: these are consecutive fragments of one article, "
        "not unrelated lines.",
        "",
        "HARD CONSTRAINTS -- checked automatically after you reply:",
        "  * Exactly one output per input, same numbering. Never merge, split, "
        "reorder, drop or add a segment.",
        "  * Keep each segment close to the source in length (roughly within a "
        "fifth). Condensing several sentences into one loses content the "
        "fidelity check will flag as truncation.",
        "  * Every number, dosage, percentage, price, phone number, email and "
        "URL appears byte-identical to the source.",
        "  * Translate meaning, never invent it. Do not add an example, a "
        "statistic or a claim that is not in the segment you were given.",
        "", _TOKEN_RULE,
        "", glossary_rules(tgt_code),
        "", language_rubric(tgt_code),
        "", f"SEGMENTS:\n{numbered}",
        "", _REPLY_RULE,
    ])


class LLMTranslate(Bhashini):
    """Translate with a writer backend instead of an NMT service.

    Bhashini remains the right engine for these languages when its credentials
    exist -- it is purpose-built, free and government-run. This is what runs
    until they do, and it has one advantage a translation API cannot have: an
    NMT model can only translate, so tone has to be repaired afterwards by a
    separate pass, whereas a model that can write can be asked for the right
    register the first time.

    Subclasses differ only in which backend they call. The cache is shared
    between them on purpose: when Gemini exhausts its quota half way through an
    article and the run falls through to another endpoint, the segments already
    paid for stay paid for.
    """

    engine_name = "llm"
    engine_label = "LLM"

    def __init__(self) -> None:
        super().__init__(user_id=self.engine_name, api_key="none")
        self._chain: list = []

    # -- backends ---------------------------------------------------------
    def _backends(self) -> list:
        """Every usable writer backend, best first. Never empty if creds exist."""
        raise NotImplementedError

    def _get_chain(self) -> list:
        if not self._chain:
            self._chain = self._backends()
        return self._chain

    def service_id(self, src: str, tgt: str) -> str:
        # English is only ever the target for the back-translation round trip.
        return _LITERAL_SID if tgt == "en" else _TONE_SID

    def _compute(self, texts: list[str], src: str, tgt: str, sid: str) -> list[str]:
        from common import lang_by_code
        src_name = lang_by_code(src)["name"] if src != "en" else "English"
        tgt_name = lang_by_code(tgt)["name"] if tgt != "en" else "English"
        numbered = "\n".join(f"{i}: {t}" for i, t in enumerate(texts))
        prompt = (_literal_prompt(numbered, src_name, tgt_name)
                  if sid == _LITERAL_SID
                  else _tone_prompt(numbered, src_name, tgt_name, tgt))

        chain = self._get_chain()
        last: Exception | None = None

        # Every backend gets the full retry budget before the next one is tried:
        # a 429 is worth waiting out on a good model before falling back to a
        # worse one, but not worth failing the whole run over.
        for pos, backend in enumerate(chain):
            for attempt in range(_CFG["max_retries"]):
                try:
                    data = backend.generate(prompt, stage="translate", slug="mt",
                                            lang=tgt,
                                            salt=f"b{len(texts)}a{attempt}")
                    mapping = (data.get("translations")
                               if isinstance(data, dict) else None)
                    if not isinstance(mapping, dict):
                        raise BhashiniError(
                            f"{backend.name} returned no usable translations "
                            f"object: {str(data)[:160]}")
                    out, missing = [], []
                    for i in range(len(texts)):
                        val = mapping.get(str(i))
                        if val is None or not str(val).strip():
                            missing.append(i)
                            out.append(texts[i])   # keep the source, never drop it
                        else:
                            out.append(str(val).strip())
                    if missing and attempt < _CFG["max_retries"] - 1:
                        raise BhashiniError(
                            f"{backend.name} dropped {len(missing)}/{len(texts)} "
                            "segment(s)")
                    if missing:
                        warn(f"{backend.name}: {len(missing)} segment(s) came back "
                             "untranslated after retries; kept the source text")
                    self.calls += 1
                    return out
                except Exception as exc:
                    last = exc
                    if attempt < _CFG["max_retries"] - 1:
                        time.sleep(2 ** attempt)
            if pos < len(chain) - 1:
                warn(f"{backend.name} could not translate this batch ({last}); "
                     f"falling through to {chain[pos + 1].name}")

        raise BhashiniError(
            f"translation failed after every backend ({', '.join(b.name for b in chain)}): {last}")

    def translate_article(self, art: Article, tgt: str, *, src: str = "en") -> Article:
        out = super().translate_article(art, tgt, src=src)
        out.meta["mt_engine"] = f"{self.engine_label} (not Bhashini)"
        return out


class GeminiTranslate(LLMTranslate):
    """Gemini AI Studio, falling through to an OpenAI-compatible endpoint.

    Not a new signup: it reuses the same GEMINI_API_KEY already configured for
    the writer backend, so it is available the moment that key exists. Its free
    tier is request-rate limited rather than capped at a daily word count, so it
    survives a far larger run than MyMemory before backing off. Selected with
    --engine gemini, and the default whenever Bhashini has no credentials.
    """

    engine_name = "gemini"
    engine_label = "Gemini"

    def _backends(self) -> list:
        from writer.gemini_free import GeminiFreeWriter
        chain = [GeminiFreeWriter()]
        try:
            from writer.openai_free import OpenAICompatWriter
            chain.append(OpenAICompatWriter())
        except Exception:
            pass          # optional second endpoint; absence is normal
        return chain

    def require_creds(self) -> None:
        try:
            self._get_chain()
        except Exception as exc:
            raise MissingCredential(
                "Gemini API key",
                "translating via the free Gemini engine",
                "Get a free key at https://aistudio.google.com/apikey and save "
                "it to aeo-translator/gemini_api_key.txt, or set GEMINI_API_KEY. "
                f"({exc})") from exc
        extra = [b.name for b in self._chain[1:]]
        warn("engine=gemini: translating for meaning and register, not word for "
             "word. Not Bhashini -- add ULCA credentials when you have them, as "
             "a purpose-built Indic NMT model is a better base."
             + (f" Falling through to {', '.join(extra)} if quota runs out."
                if extra else ""))


class OpenAITranslate(LLMTranslate):
    """Any OpenAI-shaped endpoint as the primary translator. --engine openai.

    OpenAI's own API is paid -- there is no free ChatGPT tier -- but the request
    format is shared by providers that do have one. See writer/openai_free.py.
    """

    engine_name = "openai"
    engine_label = "OpenAI-compatible"

    def _backends(self) -> list:
        from writer.openai_free import OpenAICompatWriter
        chain = [OpenAICompatWriter()]
        try:
            from writer.gemini_free import GeminiFreeWriter
            chain.append(GeminiFreeWriter())
        except Exception:
            pass
        return chain

    def require_creds(self) -> None:
        try:
            self._get_chain()
        except Exception as exc:
            raise MissingCredential(
                "OpenAI-compatible API key",
                "translating via an OpenAI-shaped endpoint",
                "Save the key to aeo-translator/openai_api_key.txt (or set "
                "OPENAI_API_KEY). OpenAI's own API is paid; to use a provider "
                "with a free tier, also write its base URL to "
                f"openai_base_url.txt and its model to openai_model.txt. ({exc})"
            ) from exc


def best_engine() -> str:
    """The best translator this install actually has credentials for.

    The order is a quality judgement, not just availability: Bhashini is
    purpose-built for these languages; an LLM endpoint translates for register
    but is a general model; MyMemory is a way to prove the pipeline runs, and
    its output should not reach a client.
    """
    from common import secret
    if (secret("BHASHINI_USER_ID", "bhashini_user_id.txt")
            and secret("BHASHINI_API_KEY", "bhashini_api_key.txt")):
        return "bhashini"
    if secret("GEMINI_API_KEY", "gemini_api_key.txt"):
        return "gemini"
    if secret("OPENAI_API_KEY", "openai_api_key.txt"):
        return "openai"
    return "mymemory"


_CLIENT: Bhashini | None = None


def client(engine: str = "bhashini") -> Bhashini:
    global _CLIENT
    if _CLIENT is None or getattr(_CLIENT, "_engine", "") != engine:
        if engine == "mymemory":
            _CLIENT = MyMemory()
        elif engine == "gemini":
            _CLIENT = GeminiTranslate()
        elif engine == "openai":
            _CLIENT = OpenAITranslate()
        else:
            _CLIENT = Bhashini()
        _CLIENT._engine = engine
    return _CLIENT
