"""AEO / GEO: whether an AI engine can reach, retrieve and quote this page.

Classic SEO asks whether a page ranks. Answer engines do something different:
they crawl, chunk, retrieve, and quote a fragment. Each of those steps can fail
for reasons an on-page SEO checker never looks at.

  reach     Is GPTBot allowed in? If robots.txt blocks the AI crawlers, every
            other AEO effort on the site is wasted, and sites block them by
            accident constantly.
  retrieve  Retrieval works on chunks of roughly 200-500 tokens, not pages. A
            section beginning "This means the dosage should be halved" is
            useless once retrieved -- no entity, no antecedent, no context.
  quote     Engines quote specific shapes: definitions, statistics with a named
            source, step lists, comparison tables, direct answers.

Everything here runs with no credentials. `visibility_hook` is the one deliberate
stub: actually measuring whether ChatGPT names you requires a paid API, and a
guessed number would be worse than an absent one.
"""
from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass, field, asdict

from common import log, warn, word_count
from extract import Article, is_question
import sources

# The crawlers that matter, and what blocking each one costs you.
AI_CRAWLERS = {
    "GPTBot":             "OpenAI training and ChatGPT browsing index",
    "OAI-SearchBot":      "ChatGPT Search results",
    "ChatGPT-User":       "live fetches when a ChatGPT user follows a link",
    "ClaudeBot":          "Anthropic training and Claude citations",
    "anthropic-ai":       "Anthropic (legacy agent name)",
    "Claude-Web":         "Claude live browsing",
    "PerplexityBot":      "Perplexity's index",
    "Perplexity-User":    "live fetches for a Perplexity answer",
    "Google-Extended":    "Gemini and Google AI Overviews grounding",
    "Applebot-Extended":  "Apple Intelligence",
    "CCBot":              "Common Crawl, which seeds many open models",
    "Bytespider":         "ByteDance / Doubao",
    "meta-externalagent": "Meta AI",
}

# Pronouns and connectives that leave a retrieved chunk dangling.
ORPHAN_OPENERS = re.compile(
    r"^\s*(this|that|these|those|it|they|he|she|such|the former|the latter|"
    r"here|there|then|also|however|therefore|moreover|furthermore|additionally|"
    r"as a result|in addition|consequently|meanwhile|otherwise|instead)\b",
    re.I)

STAT = re.compile(r"\b\d+(?:\.\d+)?\s*(?:%|percent|per cent|million|billion|lakh|crore)\b", re.I)
ATTRIB = re.compile(
    r"\b(according to|per the|reported by|study|research|survey|data from|"
    r"published in|found that|WHO|ICMR|NIH|Lancet|journal)\b", re.I)
DEFINITION = re.compile(
    r"\b\w[\w\s-]{2,40}\s+(?:is|are|refers to|means|is defined as)\s+(?:a|an|the|when)\b", re.I)
STEPS = re.compile(r"^\s*(?:step\s*\d|first|second|third|next|then|finally)\b", re.I)
YEAR = re.compile(r"\b(20[0-2]\d)\b")

CHUNK_WORDS = 320          # ~450 tokens, the middle of the usual retrieval band


@dataclass
class GeoFinding:
    group: str
    check: str
    status: str            # pass | warn | fail
    message: str
    fix: str = ""
    impact: str = "medium"
    detail: dict = field(default_factory=dict)

    def dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------- 1. can the AI reach you

def crawler_access(site: str) -> dict:
    """Parse robots.txt and report, per AI crawler, whether it is allowed in.

    Uses its own parser rather than urllib.robotparser: the stdlib parser answers
    "can this agent fetch this path", which collapses the distinction between
    "explicitly allowed", "allowed only by the wildcard rule" and "explicitly
    blocked". For an AEO report that distinction is the whole point.
    """
    host = site if "//" in site else f"https://{site}"
    base = f"{urllib.parse.urlparse(host).scheme}://{urllib.parse.urlparse(host).netloc}"
    try:
        txt = sources.fetch(urllib.parse.urljoin(base, "/robots.txt"), retries=2)
    except Exception as exc:
        return {"ok": False, "error": f"robots.txt unreachable ({exc.__class__.__name__})",
                "agents": {}}

    # Group directives by user-agent block.
    blocks: dict[str, list[tuple[str, str]]] = {}
    current: list[str] = []
    for raw in txt.splitlines():
        line = raw.split("#")[0].strip()
        if not line or ":" not in line:
            continue
        field_, _, value = line.partition(":")
        field_, value = field_.strip().lower(), value.strip()
        if field_ == "user-agent":
            current = [value.lower()] if current and blocks.get(current[-1]) is not None \
                else current + [value.lower()]
            # A run of consecutive user-agent lines shares one rule block.
            blocks.setdefault(value.lower(), [])
            current = [value.lower()]
        elif field_ in ("allow", "disallow") and current:
            for agent in current:
                blocks.setdefault(agent, []).append((field_, value))

    def verdict(agent: str) -> dict:
        low = agent.lower()
        rules = blocks.get(low)
        if rules is None:
            wild = blocks.get("*", [])
            blocked = any(f == "disallow" and v == "/" for f, v in wild)
            return {"state": "blocked_by_wildcard" if blocked else "allowed",
                    "explicit": False,
                    "rules": [f"{f}: {v}" for f, v in wild][:4]}
        blocked = any(f == "disallow" and v == "/" for f, v in rules)
        return {"state": "blocked" if blocked else "allowed", "explicit": True,
                "rules": [f"{f}: {v}" for f, v in rules][:4]}

    agents = {name: {**verdict(name), "why": why} for name, why in AI_CRAWLERS.items()}
    blocked = [n for n, a in agents.items() if a["state"].startswith("blocked")]
    return {"ok": True, "agents": agents, "blocked": blocked,
            "allowed": [n for n in agents if n not in blocked],
            "robots_url": urllib.parse.urljoin(base, "/robots.txt")}


def crawler_findings(access: dict) -> list[GeoFinding]:
    if not access.get("ok"):
        return [GeoFinding("reach", "robots", "warn",
                           f"Could not read robots.txt: {access.get('error')}",
                           "Confirm it is reachable — crawlers that cannot read it "
                           "may treat the site as disallowed.", "high")]
    blocked = access["blocked"]
    if not blocked:
        return [GeoFinding("reach", "ai_crawlers", "pass",
                           f"All {len(access['agents'])} AI crawlers are allowed.",
                           "", "high", {"allowed": access["allowed"]})]
    return [GeoFinding(
        "reach", "ai_crawlers", "fail",
        f"{len(blocked)} AI crawler(s) are blocked: {', '.join(blocked)}",
        "Unblock these in robots.txt. While they are blocked the site cannot be "
        "cited by the engines behind them, whatever else is done to the content: "
        + "; ".join(f"{b} = {access['agents'][b]['why']}" for b in blocked[:4]),
        "high", {"blocked": blocked})]


# ------------------------------------------------------ 2. llms.txt

def llms_txt(site: str, *, sitemap_urls: list[str] | None = None) -> dict:
    """Fetch and audit llms.txt — the emerging manifest for AI crawlers."""
    host = site if "//" in site else f"https://{site}"
    parsed = urllib.parse.urlparse(host)
    base = f"{parsed.scheme}://{parsed.netloc}"
    out = {"present": False, "url": urllib.parse.urljoin(base, "/llms.txt")}
    try:
        txt = sources.fetch(out["url"], retries=1)
    except Exception:
        return out

    out["present"] = True
    out["bytes"] = len(txt)
    lines = txt.splitlines()
    out["lines"] = len(lines)
    out["has_h1"] = bool(lines and lines[0].startswith("# "))
    out["has_summary"] = any(l.startswith("> ") for l in lines[:6])
    out["sections"] = [l[3:].strip() for l in lines if l.startswith("## ")]
    links = re.findall(r"\]\((https?://[^)]+)\)", txt)
    out["links"] = len(links)
    out["link_urls"] = links

    if sitemap_urls:
        listed = {l.rstrip("/") for l in links}
        out["coverage"] = round(
            len([u for u in sitemap_urls if u.rstrip("/") in listed]) / max(1, len(sitemap_urls)), 3)
    return out


def llms_findings(info: dict) -> list[GeoFinding]:
    if not info.get("present"):
        return [GeoFinding("manifest", "llms_txt", "warn",
                           "No /llms.txt on the site.",
                           "Add one. It is the emerging convention for telling AI "
                           "crawlers what the site is and which pages matter — "
                           "cheap to publish and increasingly read.", "medium")]
    out = []
    if not info.get("has_h1") or not info.get("has_summary"):
        out.append(GeoFinding("manifest", "llms_format", "warn",
                              "llms.txt exists but is missing the expected header.",
                              "It should open with '# Site Name' then a '> one-line "
                              "summary' before any sections.", "low"))
    else:
        out.append(GeoFinding("manifest", "llms_txt", "pass",
                              f"llms.txt present and well formed — {info['lines']} lines, "
                              f"{len(info.get('sections', []))} sections, "
                              f"{info.get('links', 0)} links.", "", "medium"))
    if info.get("links", 0) < 5:
        out.append(GeoFinding("manifest", "llms_links", "warn",
                              f"llms.txt lists only {info.get('links', 0)} pages.",
                              "Link the pages you most want cited.", "low"))
    return out


def generate_llms_txt(site: str, brand: str, summary: str,
                      pages: list[dict]) -> str:
    """Build an llms.txt from crawl data. Grouped, newest and richest first."""
    host = site if "//" in site else f"https://{site}"
    lines = [f"# {brand}", "", f"> {summary}", ""]
    by_section: dict[str, list[dict]] = {}
    for p in pages:
        path = urllib.parse.urlparse(p.get("url", "")).path.strip("/").split("/")
        section = path[0].title() if path and path[0] else "Pages"
        by_section.setdefault(section, []).append(p)
    for section, items in sorted(by_section.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"## {section}")
        lines.append("")
        for p in sorted(items, key=lambda x: -(x.get("words") or 0))[:40]:
            title = (p.get("title") or p.get("url", "")).strip()
            desc = (p.get("description") or "").strip()
            lines.append(f"- [{title}]({p['url']})" + (f": {desc[:110]}" if desc else ""))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------- 3. is the page worth quoting

def citation_worthiness(art: Article) -> tuple[float, list[GeoFinding]]:
    """The shapes answer engines actually lift into an answer."""
    text = art.full_text()
    sents = re.split(r"(?<=[.!?])\s+", text)
    findings: list[GeoFinding] = []

    stats = STAT.findall(text)
    attributed = bool(ATTRIB.search(text))
    definitions = DEFINITION.findall(text)
    has_steps = any(STEPS.match(b.text) for b in art.blocks)
    lists = sum(1 for b in art.blocks if b.type == "li")
    answers = [b for b in art.blocks if b.type == "answer"]
    q_heads = [h for h in art.headings() if is_question(h)]

    score = 0.0
    if stats:
        score += 20 if attributed else 10
        if not attributed:
            findings.append(GeoFinding(
                "quote", "unsourced_stats", "warn",
                f"{len(stats)} statistic(s) with no named source.",
                "Attribute each figure. Engines strongly prefer citing a page "
                "that itself cites something — and an unsourced number in health "
                "content is a liability regardless.", "high",
                {"examples": stats[:4]}))
        else:
            findings.append(GeoFinding("quote", "sourced_stats", "pass",
                                       f"{len(stats)} statistic(s), with attribution.",
                                       "", "medium"))
    else:
        findings.append(GeoFinding(
            "quote", "no_stats", "warn", "No statistics or concrete figures.",
            "Add one or two attributed figures. Quantified claims get quoted; "
            "general advice does not.", "medium"))

    if definitions:
        score += 15
    else:
        findings.append(GeoFinding(
            "quote", "no_definition", "warn",
            "No clean definition sentence ('X is a ...').",
            "Define the main term in one plain sentence. It is the single most "
            "quoted sentence shape there is.", "medium"))

    if has_steps or lists >= 3:
        score += 15
    else:
        findings.append(GeoFinding("quote", "no_list", "warn",
                                   "No step list or bulleted list.",
                                   "Engines lift lists verbatim. Enumerate what can "
                                   "be enumerated.", "medium"))

    if answers:
        score += 25
        findings.append(GeoFinding("quote", "answer_blocks", "pass",
                                   f"{len(answers)} standalone answer block(s).",
                                   "", "high"))
    elif q_heads:
        score += 8
        findings.append(GeoFinding(
            "quote", "no_answer_blocks", "warn",
            f"{len(q_heads)} question heading(s) with no short direct answer under them.",
            "Put a 40-60 word answer immediately under each question heading, "
            "before the detail. That block is what gets quoted.", "high"))

    if YEAR.search(text):
        score += 10
    if art.faqs:
        score += 15

    return min(100.0, score), findings


# ------------------------------------------- 4. chunk-readiness (the GEO bit)

def chunks_of(art: Article, size: int = CHUNK_WORDS) -> list[dict]:
    """Split the way a retrieval system would: at headings, then by length."""
    chunks: list[dict] = []
    current = {"heading": art.title, "blocks": [], "words": 0}
    for b in art.blocks:
        if b.type in ("h2", "h3") or current["words"] >= size:
            if current["blocks"]:
                chunks.append(current)
            current = {"heading": b.text if b.type in ("h2", "h3") else current["heading"],
                       "blocks": [], "words": 0}
            if b.type in ("h2", "h3"):
                continue
        current["blocks"].append(b)
        current["words"] += word_count(b.text)
    if current["blocks"]:
        chunks.append(current)
    for c in chunks:
        c["text"] = " ".join(b.text for b in c["blocks"])
    return chunks


def chunk_readiness(art: Article, entities: list[str]) -> tuple[float, list[GeoFinding]]:
    """Would each chunk still make sense retrieved on its own?

    This is the measurement that separates GEO from SEO. A page reads fine top to
    bottom and still retrieves badly, because the reader of a chunk has none of
    the preceding text.
    """
    chunks = chunks_of(art)
    if not chunks:
        return 0.0, [GeoFinding("retrieve", "chunks", "fail", "No content to chunk.",
                                "", "high")]

    ent_low = [e.lower() for e in entities if len(e) > 3]
    orphaned, entity_less, oversized = [], [], []

    for c in chunks:
        first = (c["text"] or "").strip()
        if ORPHAN_OPENERS.match(first):
            orphaned.append({"heading": c["heading"][:60], "opens": first[:90]})
        low = (c["heading"] + " " + c["text"]).lower()
        if ent_low and not any(e in low for e in ent_low):
            entity_less.append({"heading": c["heading"][:60], "words": c["words"]})
        if c["words"] > CHUNK_WORDS * 1.6:
            oversized.append({"heading": c["heading"][:60], "words": c["words"]})

    findings: list[GeoFinding] = []
    n = len(chunks)

    if orphaned:
        findings.append(GeoFinding(
            "retrieve", "orphan_opening", "fail" if len(orphaned) > n * 0.3 else "warn",
            f"{len(orphaned)} of {n} chunks open with a dangling reference.",
            "Rewrite these openings to name the subject. Retrieved alone, "
            "'This means...' has no antecedent and the chunk is unusable.",
            "high", {"examples": orphaned[:4]}))
    else:
        findings.append(GeoFinding("retrieve", "orphan_opening", "pass",
                                   f"All {n} chunks open self-containedly.", "", "high"))

    if entity_less:
        findings.append(GeoFinding(
            "retrieve", "entity_anchor", "warn",
            f"{len(entity_less)} of {n} chunks never name the subject.",
            "Repeat the main entity once per section. A chunk that says 'the "
            "condition' throughout cannot be matched to a query naming it.",
            "high", {"examples": entity_less[:4]}))
    else:
        findings.append(GeoFinding("retrieve", "entity_anchor", "pass",
                                   "Every chunk names its subject.", "", "high"))

    if oversized:
        findings.append(GeoFinding(
            "retrieve", "chunk_size", "warn",
            f"{len(oversized)} section(s) exceed ~{int(CHUNK_WORDS * 1.6)} words.",
            "Split them with a subheading. An oversized section gets cut "
            "mid-thought by the chunker, not at a sensible boundary.",
            "medium", {"examples": oversized[:3]}))

    score = 100.0
    score -= 55 * (len(orphaned) / n)
    score -= 30 * (len(entity_less) / n)
    score -= 15 * (len(oversized) / n)
    return max(0.0, score), findings


# ---------------------------------------------------- 5. entities / Wikidata

def extract_entities(art: Article, extra: list[str] | None = None) -> list[str]:
    """Capitalised multi-word phrases and repeated proper nouns."""
    text = art.full_text()
    counts: dict[str, int] = {}
    for m in re.finditer(r"\b([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,}){0,3})\b", text):
        phrase = m.group(1).strip()
        if phrase.lower() in ("the", "this", "these", "however", "these are"):
            continue
        counts[phrase] = counts.get(phrase, 0) + 1
    ranked = [p for p, n in sorted(counts.items(), key=lambda kv: -kv[1]) if n >= 2]
    for e in (extra or []):
        if e and e not in ranked:
            ranked.insert(0, e)
    return ranked[:12]


def wikidata_sameas(entities: list[str], *, limit: int = 6) -> dict[str, str]:
    """Resolve entities against Wikidata's free API for schema `sameAs`.

    A page that merely mentions "Ayurveda" is a string match. One that links it
    to Q170519 joins a knowledge graph, which is how an engine knows two pages
    are about the same thing.
    """
    out: dict[str, str] = {}
    for e in entities[:limit]:
        try:
            r = sources.SESSION.get(
                "https://www.wikidata.org/w/api.php",
                params={"action": "wbsearchentities", "search": e, "language": "en",
                        "format": "json", "limit": 1, "type": "item"},
                timeout=15)
            hits = r.json().get("search", [])
            if hits:
                out[e] = hits[0]["concepturi"]
        except Exception:
            continue
    return out


# ------------------------------------------------------------ 6. freshness

def freshness(art: Article) -> list[GeoFinding]:
    import datetime
    out: list[GeoFinding] = []
    this_year = datetime.date.today().year
    text = art.full_text()

    if not (art.published or "").strip():
        out.append(GeoFinding("freshness", "date", "warn", "No publish date.",
                              "Show published and last-updated dates. Answer engines "
                              "weight recency heavily and skip undated pages.", "medium"))
    years = [int(y) for y in YEAR.findall(text)]
    stale = [y for y in years if y < this_year - 1]
    if stale and not any(y >= this_year - 1 for y in years):
        out.append(GeoFinding("freshness", "stale_year", "warn",
                              f"The most recent year mentioned is {max(stale)}.",
                              "Update the figures and the year references, or the "
                              "page reads as abandoned.", "medium"))
    return out


# ------------------------------------------------- 7. the measurement stub

def visibility_hook(site: str, queries: list[str]) -> dict:
    """Deliberately not implemented. See the module docstring.

    Measuring whether ChatGPT or Perplexity actually names a business requires
    putting real queries to those engines, which needs a paid API. The wiring
    lives here so it can be switched on without touching anything else; what it
    must never do is return a plausible-looking number that was not measured.
    """
    return {
        "measured": False,
        "reason": "AI visibility measurement is not enabled. It needs a paid "
                  "provider (the Website Auditor connector, or direct LLM APIs). "
                  "Everything else in this report is measured from the page and "
                  "the site; this number is not estimated.",
        "queries": queries[:10],
        "site": site,
    }


# ----------------------------------------------------------------- combined

def audit(art: Article, *, site: str = "", brand: str = "",
          sitemap_urls: list[str] | None = None,
          resolve_entities: bool = True) -> dict:
    """Full AEO/GEO report for one page, plus the site-level reach checks."""
    findings: list[GeoFinding] = []

    access = crawler_access(site) if site else {"ok": False, "error": "no site given",
                                                "agents": {}, "blocked": []}
    if site:
        findings += crawler_findings(access)

    llms = llms_txt(site, sitemap_urls=sitemap_urls) if site else {"present": False}
    if site:
        findings += llms_findings(llms)

    entities = extract_entities(art, extra=[brand] if brand else None)
    cite_score, cite_findings = citation_worthiness(art)
    chunk_score, chunk_findings = chunk_readiness(art, entities)
    findings += cite_findings + chunk_findings + freshness(art)

    same_as = wikidata_sameas(entities) if resolve_entities else {}
    if same_as:
        findings.append(GeoFinding(
            "entities", "sameas", "pass",
            f"{len(same_as)} entity(ies) resolved to Wikidata.",
            "Add these as `sameAs` in the schema so the page joins the knowledge "
            "graph rather than just matching strings.", "medium",
            {"sameAs": same_as}))

    reach_ok = access.get("ok") and not access.get("blocked")
    # Reach is a gate, not a component: if the crawlers are blocked, the rest of
    # the score describes a page no engine can read.
    combined = round(0.45 * chunk_score + 0.40 * cite_score
                     + 0.15 * (100 if llms.get("present") else 0), 1)

    return {
        "score": combined if reach_ok or not site else round(combined * 0.4, 1),
        "gated": bool(site) and not reach_ok,
        "citation_score": round(cite_score, 1),
        "chunk_score": round(chunk_score, 1),
        "crawler_access": access,
        "llms_txt": llms,
        "entities": entities,
        "same_as": same_as,
        "chunks": len(chunks_of(art)),
        "findings": [f.dict() for f in findings],
        "visibility": visibility_hook(site, [h for h in art.headings() if is_question(h)]),
    }
