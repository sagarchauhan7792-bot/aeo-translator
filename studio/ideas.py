"""Stage 1: turn a topic into candidate posts, and say which are already covered.

Two halves.

**Query expansion** uses Google's autocomplete endpoint, seeded with question
prefixes, modifiers and an alphabet sweep. These are queries people actually
typed, not guesses about what they might type, and it needs no credentials.

People Also Ask is deliberately NOT used. It was tested: google.com/search
returns a JavaScript-only shell to a plain HTTP client, with no `data-q`
attributes, no embedded question JSON and no "People also ask" string anywhere
in the 91KB response. Scraping it would need a headless browser and would break
the first time Google changed the markup. Autocomplete is a documented endpoint
that returns the same class of signal.

**Gap analysis** is the part worth opening daily. A site with a couple of
thousand posts has almost certainly covered the obvious topic already, and
writing the same post twice splits its own rankings. Every candidate is checked
against the site's own sitemap and flagged covered / partial / gap.
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
from dataclasses import dataclass, field, asdict
from pathlib import Path

from common import ROOT, log, warn, read_json, write_json
import sources

CACHE = ROOT / "cache" / "siteindex"
SUGGEST = "https://suggestqueries.google.com/complete/search"
INDEX_TTL = 7 * 24 * 3600

# Prefixes that surface informational intent -- the queries an answer engine is
# most likely to be asked and most likely to quote a page for.
QUESTION_PREFIX = {
    "en": ["how to", "how", "why", "what is", "what are", "when", "which",
           "can", "is", "does", "best", "symptoms of", "treatment for"],
    "hi": ["क्या", "कैसे", "क्यों", "कब", "कौन सा", "के लक्षण", "का इलाज",
           "के घरेलू उपाय", "में क्या खाएं"],
}
SUFFIX = {
    "en": ["", " symptoms", " treatment", " causes", " diet", " home remedies",
           " in hindi", " for women", " for men", " vs", " side effects"],
    "hi": ["", " लक्षण", " इलाज", " कारण", " परहेज", " घरेलू उपाय"],
}
ALPHABET = "abcdefghijklmnopqrstuvwxyz"

# Words that carry no topic meaning when matching a query against a URL slug.
STOP = {
    "the", "a", "an", "of", "for", "in", "on", "to", "and", "or", "is", "are",
    "what", "why", "how", "when", "which", "who", "can", "do", "does", "should",
    "you", "your", "my", "with", "at", "by", "from", "it", "its", "that", "this",
    "best", "top", "common", "guide", "complete", "know", "about", "need",
    "everyone", "shouldnt", "should", "not", "ignore", "signs",
}


@dataclass
class Candidate:
    query: str
    source: str = "autocomplete"
    intent: str = "informational"
    status: str = "gap"            # covered | partial | gap
    match_url: str = ""
    match_score: float = 0.0
    volume_in: str = ""            # filled only from Keyword Planner
    competition: str = ""
    cpc_inr: str = ""

    def dict(self) -> dict:
        return asdict(self)


@dataclass
class Brief:
    topic: str
    site: str = ""
    lang: str = "en"
    titles: list[str] = field(default_factory=list)
    target_queries: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    internal_links: list[dict] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)
    notes: str = ""

    def dict(self) -> dict:
        return asdict(self)


# ------------------------------------------------------------------ suggest

def suggest(query: str, lang: str = "en", *, gl: str = "in",
            retries: int = 3) -> list[str]:
    """One autocomplete call. Returns [] rather than raising on any failure."""
    for attempt in range(retries):
        try:
            resp = sources.SESSION.get(
                SUGGEST,
                params={"client": "firefox", "q": query, "hl": lang, "gl": gl},
                timeout=15)
            resp.raise_for_status()
            return [s for s in json.loads(resp.text)[1] if isinstance(s, str)]
        except Exception as exc:
            if attempt == retries - 1:
                warn(f"autocomplete failed for {query!r}: {exc.__class__.__name__}")
            else:
                time.sleep(1.5 * (attempt + 1))
    return []


def expand(topic: str, lang: str = "en", *, deep: bool = False,
           pause: float = 0.25) -> list[str]:
    """Expand a topic into real queries via prefix, suffix and alphabet sweeps.

    Autocomplete echoes the seed back as its first result whether or not anyone
    searches for it, so a seed like "is thyroid symptoms" returns itself and
    looks like demand. Echoes are dropped unless a second, independent seed also
    produced them.
    """
    topic = re.sub(r"\s+", " ", topic).strip()
    if not topic:
        return []

    seeds = [topic]
    seeds += [f"{p} {topic}" for p in QUESTION_PREFIX.get(lang, QUESTION_PREFIX["en"])]
    seeds += [f"{topic}{s}" for s in SUFFIX.get(lang, SUFFIX["en"]) if s]
    if deep:
        seeds += [f"{topic} {c}" for c in ALPHABET]

    seed_set = {s.lower() for s in seeds}
    topic_low = topic.lower()
    hits: dict[str, int] = {}
    for i, seed in enumerate(seeds):
        for s in suggest(seed, lang):
            s = s.strip()
            if len(s) > 4:
                hits[s] = hits.get(s, 0) + 1
        if i < len(seeds) - 1:
            time.sleep(pause)          # be polite to a free endpoint

    # Drop constructed seeds unconditionally. An earlier version kept them when
    # two seeds produced the same string, on the theory that corroboration meant
    # real demand -- it does not. "is thyroid symptoms" and "can thyroid
    # symptoms" both survived that rule and neither is a query anyone types.
    # Losing the rare genuine query that happens to equal a seed costs less than
    # putting fragments in front of someone choosing what to write.
    kept = [q for q in hits
            if q.lower() == topic_low or q.lower() not in seed_set]

    # A useful suggestion says something the topic did not. Keep the bare topic,
    # drop anything that adds no content word of its own.
    topic_tokens = _tokens(topic)
    kept = [q for q in kept
            if q.lower() == topic_low or (_tokens(q) - topic_tokens)]

    ranked = sorted(kept, key=lambda q: (topic_low not in q.lower(),
                                         -hits[q], len(q)))
    log(f"ideas: {len(seeds)} seeds -> {len(hits)} raw, "
        f"{len(ranked)} after dropping seed echoes and empty variants", indent=1)
    return ranked


# ------------------------------------------------------- site index / gaps

def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if len(w) > 2 and w not in STOP}


def _slug_of(url: str) -> str:
    path = urllib.parse.urlparse(url).path.rstrip("/")
    return path.rsplit("/", 1)[-1] if path else ""


def site_index(site: str, *, url_filter: str = "", limit: int = 0,
               refresh: bool = False) -> dict:
    """Crawl a site's sitemap once and cache a slug index.

    Titles would be better than slugs, but fetching a couple of thousand pages
    to get them is not something to do while someone waits. Descriptive slugs
    carry most of the signal -- '10-common-diabetes-symptoms-everyone-should-know'
    tells you what the post covers.
    """
    host = urllib.parse.urlparse(site if "//" in site else f"https://{site}").netloc or site
    host = host[4:] if host.startswith("www.") else host
    path = CACHE / f"{host}.json"

    if not refresh and path.exists():
        cached = read_json(path, default={})
        if time.time() - cached.get("fetched", 0) < INDEX_TTL:
            return cached

    sm = site if site.endswith(".xml") else sources.discover_sitemap(site)
    if not sm:
        warn(f"no sitemap found for {site}; gap analysis unavailable")
        return {"host": host, "urls": [], "fetched": time.time(), "error": "no sitemap"}

    urls = sources.crawl_sitemap(sm, url_filter=url_filter, limit=limit)
    entries = [{"url": u, "slug": _slug_of(u), "tokens": sorted(_tokens(_slug_of(u)))}
               for u in urls]
    data = {"host": host, "sitemap": sm, "urls": entries, "fetched": time.time()}
    write_json(path, data)
    log(f"ideas: indexed {len(entries)} URLs for {host}")
    return data


def classify(query: str, index: dict) -> tuple[str, str, float]:
    """(status, matching_url, score) for one candidate against the site index.

    Scored with F1 over content words, not one-directional coverage. Coverage
    alone asks only "is the query inside the slug", which called "thyroid
    symptoms" fully covered by a post about *thyroid cancer symptoms* -- the
    query fitted inside the slug, so it scored 1.0, while being about a
    different condition.

    F1 penalises a mismatch in either direction:

      query adds a qualifier the post lacks   -> partial (a new angle to write)
      post adds a qualifier the query lacks   -> partial (a different variant)
      both sides agree                        -> covered
    """
    q_tokens = _tokens(query)
    if not q_tokens or not index.get("urls"):
        return "gap", "", 0.0

    best_url, best = "", 0.0
    for entry in index["urls"]:
        s_tokens = set(entry["tokens"])
        if not s_tokens:
            continue
        shared = len(q_tokens & s_tokens)
        if not shared:
            continue
        precision = shared / len(q_tokens)
        recall = shared / len(s_tokens)
        f1 = 2 * precision * recall / (precision + recall)
        if f1 > best:
            best, best_url = f1, entry["url"]

    # Marking a real angle as covered costs you the post. Marking it partial
    # costs nothing, because partial still surfaces the existing article as an
    # internal link.
    if best >= 0.85:
        return "covered", best_url, round(best, 2)
    if best >= 0.45:
        return "partial", best_url, round(best, 2)
    return "gap", best_url if best >= 0.3 else "", round(best, 2)


# ------------------------------------------------------------------ brief

def build_brief(topic: str, *, site: str = "", lang: str = "en",
                deep: bool = False, limit: int = 40,
                with_volumes: bool = True) -> Brief:
    """Topic -> candidates, gap-flagged, plus everything a draft needs."""
    brief = Brief(topic=topic, site=site, lang=lang)

    queries = expand(topic, lang, deep=deep)[:limit]
    index = site_index(site) if site else {"urls": []}

    cands: list[Candidate] = []
    for q in queries:
        status, url, score = classify(q, index)
        cands.append(Candidate(query=q, status=status, match_url=url, match_score=score,
                               intent="question" if _is_question(q, lang) else "informational"))

    if with_volumes:
        _attach_volumes(cands, lang)

    gaps = [c for c in cands if c.status == "gap"]
    partials = [c for c in cands if c.status == "partial"]
    covered = [c for c in cands if c.status == "covered"]

    brief.candidates = [c.dict() for c in cands]
    brief.target_queries = [c.query for c in (gaps + partials)[:12]]
    brief.questions = [c.query for c in cands if c.intent == "question"][:12]
    brief.titles = _title_options(topic, gaps + partials)
    brief.entities = sorted(_tokens(topic))[:8]
    # Deduped by URL. Several candidate queries routinely resolve to the same
    # existing post, and handing the writer the same link four times invites it
    # to link the same article four times.
    links: dict[str, dict] = {}
    for c in covered + partials:
        if c.match_url and c.match_url not in links:
            links[c.match_url] = {"url": c.match_url, "why": c.query,
                                  "score": c.match_score}
    brief.internal_links = list(links.values())[:6]

    note = [f"{len(gaps)} gap, {len(partials)} partial, {len(covered)} already covered"]
    if not site:
        note.append("no site given, so nothing was checked for existing coverage")
    elif index.get("error"):
        note.append(f"gap analysis unavailable: {index['error']}")
    if not any(c.volume_in for c in cands):
        note.append("search volumes empty (needs google-ads.yaml); nothing estimated")
    brief.notes = " | ".join(note)
    return brief


def _is_question(q: str, lang: str) -> bool:
    prefixes = QUESTION_PREFIX.get(lang, QUESTION_PREFIX["en"])
    low = q.lower()
    return q.endswith("?") or any(low.startswith(p) for p in prefixes)


def _title_options(topic: str, cands: list[Candidate]) -> list[str]:
    """Working titles built from the highest-intent uncovered queries."""
    out, seen = [], set()
    for c in cands[:8]:
        t = c.query.strip()
        t = t[0].upper() + t[1:] if t else t
        if t.lower() not in seen and 15 < len(t) < 70:
            seen.add(t.lower())
            out.append(t)
    if not out:
        out = [topic[0].upper() + topic[1:]] if topic else []
    return out[:5]


def _attach_volumes(cands: list[Candidate], lang: str) -> None:
    """Real Keyword Planner numbers when the credentials exist, blanks otherwise."""
    import keywords as kwmod
    from common import MissingCredential

    rows = [{"keyword": c.query, "lang": lang, "script": "Latin", "intent": c.intent,
             "source_heading": "idea", "volume_in": "", "competition": "",
             "cpc_inr": "", "source": "", "checked_on": ""} for c in cands]
    try:
        rows = kwmod.fill_volumes(rows, lang if lang in kwmod.LANG_CONSTANTS else "en")
    except MissingCredential as exc:
        warn(f"idea volumes unavailable: {exc.what}. Columns left empty.")
        return
    except Exception as exc:
        warn(f"idea volumes unavailable: {exc.__class__.__name__}")
        return

    by_kw = {r["keyword"].lower(): r for r in rows}
    for c in cands:
        r = by_kw.get(c.query.lower())
        if r:
            c.volume_in = str(r.get("volume_in", "") or "")
            c.competition = r.get("competition", "") or ""
            c.cpc_inr = str(r.get("cpc_inr", "") or "")
