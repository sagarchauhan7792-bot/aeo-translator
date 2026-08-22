"""Full on-page SEO audit for a blog post you paste in.

This is the front door of the tool: give it a post — pasted, uploaded or by URL —
and it reports everything an on-page SEO tool reports, with the specific fix for
each finding rather than a bare score.

Ten groups, every one of which runs with **no credentials at all**:

  title      length, keyword placement, SERP truncation, uniqueness on your site
  meta       description length, keyword, call to action, SERP preview
  headings   H1 count, hierarchy gaps, keyword coverage, question-form ratio
  content    word count, Flesch readability, sentence and paragraph rhythm,
             passive voice, filler phrases
  keywords   density, the placement checklist, over-optimisation, secondary terms
  links      internal, external, anchor-text quality, and live broken-link checks
  images     alt coverage, keyword in alt, filename quality
  technical  slug, canonical, JSON-LD validity, Open Graph, robots
  aeo        answer-engine readiness (shared with the translation pipeline)
  eeat       author, dates, citations, and the YMYL rules for health content

What is deliberately NOT here: search volume, keyword difficulty, backlinks and
rank tracking. None of those can be measured from the page itself. Volume wires
into Google Ads when the credentials exist; the rest need a paid data provider
and inventing them would be worse than their absence.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field, asdict

import quality
from common import config, log, warn, word_count
from extract import Article, is_question

CFG = config()
A = CFG["aeo"]

# Google truncates around these pixel widths; characters are the usable proxy.
TITLE_MIN, TITLE_MAX = 30, 60
DESC_MIN, DESC_MAX = 70, 155
SLUG_MAX = 75

STOP = {
    "the", "a", "an", "and", "or", "but", "if", "of", "to", "in", "on", "for",
    "with", "as", "is", "are", "was", "were", "be", "been", "being", "it", "its",
    "this", "that", "these", "those", "at", "by", "from", "can", "may", "will",
    "would", "should", "could", "has", "have", "had", "do", "does", "did", "not",
    "you", "your", "we", "our", "they", "their", "he", "she", "his", "her", "i",
    "so", "than", "then", "there", "here", "what", "when", "which", "who", "how",
    "why", "all", "any", "more", "most", "some", "such", "no", "nor", "only",
    "own", "same", "too", "very", "just", "also", "about", "into", "over",
}

# Phrases that pad a paragraph without adding information.
FILLER = [
    "it is important to note", "it should be noted", "in today's fast-paced",
    "in this day and age", "when it comes to", "at the end of the day",
    "needless to say", "it goes without saying", "the fact of the matter",
    "in order to", "due to the fact that", "a wide range of", "plays a vital role",
    "plays a crucial role", "in conclusion", "last but not least", "first and foremost",
]
AI_TELLS = ["moreover", "furthermore", "additionally", "in conclusion",
            "it is important to note", "delve into", "navigate the", "tapestry",
            "in the realm of", "unlock the", "harness the power"]

GENERIC_ANCHORS = {"click here", "here", "read more", "learn more", "this",
                   "link", "this link", "more", "see more", "check this out"}

PASSIVE = re.compile(
    r"\b(?:is|are|was|were|be|been|being|get|gets|got)\s+(?:\w+ly\s+)?(\w+(?:ed|en|wn|ne))\b", re.I)

SEVERITY_WEIGHT = {"fail": 1.0, "warn": 0.45, "pass": 0.0}


@dataclass
class Finding:
    group: str
    check: str
    status: str            # pass | warn | fail
    message: str
    fix: str = ""
    impact: str = "medium"  # high | medium | low
    detail: dict = field(default_factory=dict)

    def dict(self) -> dict:
        return asdict(self)


@dataclass
class AuditReport:
    score: int
    grade: str
    groups: dict
    findings: list
    stats: dict
    keyword: str = ""
    blocking: list = field(default_factory=list)

    def dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        fails = sum(1 for f in self.findings if f["status"] == "fail")
        warns = sum(1 for f in self.findings if f["status"] == "warn")
        return (f"SEO {self.score}/100 ({self.grade}) — {fails} failed, {warns} warnings, "
                f"{self.stats.get('words', 0)} words")


# --------------------------------------------------------------- readability

def _syllables(word: str) -> int:
    """Rough English syllable count. Good enough for Flesch, not for poetry."""
    word = re.sub(r"[^a-z]", "", word.lower())
    if not word:
        return 0
    if len(word) <= 3:
        return 1
    word = re.sub(r"(?:[^laeiouy]es|ed|[^laeiouy]e)$", "", word)
    word = re.sub(r"^y", "", word)
    return max(1, len(re.findall(r"[aeiouy]{1,2}", word)))


def flesch(text: str) -> tuple[float, str]:
    """Flesch Reading Ease plus a plain-language band."""
    sents = [s for s in quality.sentences(text) if word_count(s) > 1]
    words = re.findall(r"[A-Za-z']+", text)
    if not sents or not words:
        return 0.0, "not enough text"
    syl = sum(_syllables(w) for w in words)
    score = 206.835 - 1.015 * (len(words) / len(sents)) - 84.6 * (syl / len(words))
    score = max(0.0, min(100.0, score))
    band = ("very easy" if score >= 80 else "easy" if score >= 70 else
            "fairly easy" if score >= 60 else "standard" if score >= 50 else
            "fairly hard" if score >= 30 else "hard")
    return round(score, 1), band


# ------------------------------------------------------------------ keywords

def detect_keyword(art: Article) -> str:
    """Guess the primary keyword from the title, then confirm against the body."""
    title_terms = [w for w in re.findall(r"[a-z]+", (art.title or "").lower())
                   if w not in STOP and len(w) > 2]
    if not title_terms:
        return ""
    body = art.body_text().lower()
    # The longest title bigram that actually recurs in the body is a better
    # primary keyword than any single word.
    for n in (3, 2):
        for i in range(len(title_terms) - n + 1):
            phrase = " ".join(title_terms[i:i + n])
            if body.count(phrase) >= 2:
                return phrase
    scored = sorted(title_terms, key=lambda w: -body.count(w))
    return scored[0] if scored else ""


def keyword_density(text: str, keyword: str) -> tuple[float, int]:
    if not keyword:
        return 0.0, 0
    hits = len(re.findall(re.escape(keyword), text.lower()))
    total = max(1, word_count(text))
    return round(hits / total * 100, 2), hits


# -------------------------------------------------------------------- audit

def audit(art: Article, *, keyword: str = "", site_index: dict | None = None,
          check_links: bool = True, base_url: str = "") -> AuditReport:
    f: list[Finding] = []
    text = art.full_text()
    body = art.body_text()
    words = word_count(text)
    kw = (keyword or detect_keyword(art)).strip().lower()

    f += _title(art, kw, site_index)
    f += _meta(art, kw)
    f += _headings(art, kw)
    f += _content(art, body, words)
    f += _keywords(art, body, kw, words)
    f += _links(art, check_links=check_links)
    f += _images(art, kw)
    f += _technical(art, base_url)
    f += _aeo(art)
    f += _eeat(art)
    if site_index:
        f += _cannibalisation(art, kw, site_index)

    groups: dict[str, dict] = {}
    for item in f:
        g = groups.setdefault(item.group, {"pass": 0, "warn": 0, "fail": 0, "score": 100})
        g[item.status] += 1

    # Score: every check is worth the same, weighted by how badly it failed.
    penalty = sum(SEVERITY_WEIGHT[i.status] * (1.6 if i.impact == "high" else
                                               1.0 if i.impact == "medium" else 0.5)
                  for i in f)
    total = sum(1.6 if i.impact == "high" else 1.0 if i.impact == "medium" else 0.5 for i in f)
    score = int(round(max(0.0, 100.0 * (1 - penalty / max(total, 1)))))

    for name, g in groups.items():
        n = g["pass"] + g["warn"] + g["fail"]
        g["score"] = int(round(100 * (g["pass"] + 0.55 * g["warn"]) / max(n, 1)))

    grade = ("excellent" if score >= 90 else "good" if score >= 75 else
             "needs work" if score >= 55 else "poor")

    fl, band = flesch(body)
    stats = {
        "words": words, "flesch": fl, "readability": band,
        "headings": len(art.headings()),
        "questions": sum(1 for h in art.headings() if is_question(h)),
        "images": len(art.images), "links": len(art.links),
        "faqs": len(art.faqs),
        "keyword": kw,
        "density": keyword_density(body, kw)[0],
        "sentences": len(quality.sentences(body)),
    }

    return AuditReport(
        score=score, grade=grade, groups=groups,
        findings=[i.dict() for i in f], stats=stats, keyword=kw,
        blocking=[i.dict() for i in f if i.status == "fail" and i.impact == "high"])


# ----------------------------------------------------------------- sections

def _title(art: Article, kw: str, site_index: dict | None) -> list[Finding]:
    out, t = [], (art.title or "").strip()
    n = len(t)
    if not t:
        return [Finding("title", "present", "fail", "There is no title.",
                        "Add one. It is the single strongest on-page signal.", "high")]

    if n < TITLE_MIN:
        out.append(Finding("title", "length", "warn",
                           f"Title is {n} characters — short.",
                           f"Aim for {TITLE_MIN}-{TITLE_MAX}. You have room to add the "
                           "keyword or a qualifier people search for.", "medium",
                           {"length": n}))
    elif n > TITLE_MAX:
        out.append(Finding("title", "length", "warn",
                           f"Title is {n} characters and will be cut off in results.",
                           f"Trim to {TITLE_MAX}. Google shows roughly the first "
                           f"{TITLE_MAX} characters; everything after is invisible.",
                           "medium", {"length": n, "visible": t[:TITLE_MAX]}))
    else:
        out.append(Finding("title", "length", "pass",
                           f"Title length is {n} characters.", "", "medium"))

    if kw:
        pos = t.lower().find(kw)
        if pos < 0:
            out.append(Finding("title", "keyword", "fail",
                               f"The title does not contain '{kw}'.",
                               "Put the primary keyword in the title, ideally near "
                               "the start.", "high"))
        elif pos > 35:
            out.append(Finding("title", "keyword_position", "warn",
                               f"'{kw}' appears {pos} characters into the title.",
                               "Move it closer to the front — earlier words carry "
                               "more weight and survive truncation.", "medium"))
        else:
            out.append(Finding("title", "keyword", "pass",
                               f"'{kw}' appears early in the title.", "", "high"))

    if t.isupper():
        out.append(Finding("title", "case", "warn", "The title is in all caps.",
                           "Use sentence case. All caps reads as shouting and can "
                           "be rewritten by Google.", "low"))

    if site_index and site_index.get("urls"):
        from .ideas import _tokens, classify
        status, url, score = classify(t, site_index)
        if status == "covered" and url:
            out.append(Finding("title", "duplicate", "warn",
                               "A post with almost this exact title already exists.",
                               f"Differentiate the angle or update the existing post "
                               f"instead: {url}", "high", {"url": url, "score": score}))
    return out


def _meta(art: Article, kw: str) -> list[Finding]:
    out, d = [], (art.meta_description or "").strip()
    n = len(d)
    if not d:
        return [Finding("meta", "present", "fail", "There is no meta description.",
                        "Write one of 70-155 characters. Without it Google invents "
                        "a snippet from the page, usually badly.", "high")]
    if n < DESC_MIN:
        out.append(Finding("meta", "length", "warn",
                           f"Meta description is {n} characters — short.",
                           f"Aim for {DESC_MIN}-{DESC_MAX}. You are giving away "
                           "free space in the result.", "medium", {"length": n}))
    elif n > DESC_MAX:
        out.append(Finding("meta", "length", "warn",
                           f"Meta description is {n} characters and will be truncated.",
                           f"Trim to {DESC_MAX}.", "medium",
                           {"length": n, "visible": d[:DESC_MAX]}))
    else:
        out.append(Finding("meta", "length", "pass",
                           f"Meta description length is {n} characters.", "", "medium"))

    if kw and kw not in d.lower():
        out.append(Finding("meta", "keyword", "warn",
                           f"The meta description does not mention '{kw}'.",
                           "Include it — matched terms are bolded in results, which "
                           "lifts click-through.", "medium"))
    elif kw:
        out.append(Finding("meta", "keyword", "pass",
                           f"'{kw}' appears in the meta description.", "", "medium"))

    if not re.search(r"\b(learn|find|discover|see|read|get|know|understand|check)\b",
                     d, re.I):
        out.append(Finding("meta", "cta", "warn",
                           "The description has no call to action.",
                           "Add a verb that gives a reason to click — 'Learn how…', "
                           "'See what…'.", "low"))
    return out


def _headings(art: Article, kw: str) -> list[Finding]:
    out = []
    types = [b.type for b in art.blocks if b.type in ("h1", "h2", "h3")]
    heads = art.headings()

    h1s = types.count("h1")
    if h1s > 1:
        out.append(Finding("headings", "h1_count", "fail",
                           f"There are {h1s} H1 tags.",
                           "Use exactly one H1 — the title. Multiple H1s split the "
                           "page's topic signal.", "high"))
    else:
        out.append(Finding("headings", "h1_count", "pass", "One H1, as expected.",
                           "", "medium"))

    if not heads:
        out.append(Finding("headings", "structure", "fail",
                           "The post has no subheadings.",
                           "Break it into sections with H2s. A wall of text ranks "
                           "poorly and cannot be quoted by an answer engine.", "high"))
        return out

    # Hierarchy: an H3 must follow an H2, never jump straight from the title.
    seen_h2 = False
    skipped = 0
    for t in types:
        if t == "h2":
            seen_h2 = True
        elif t == "h3" and not seen_h2:
            skipped += 1
    if skipped:
        out.append(Finding("headings", "hierarchy", "warn",
                           f"{skipped} H3 heading(s) appear before any H2.",
                           "Fix the nesting — H2 for sections, H3 for subsections.",
                           "medium"))
    else:
        out.append(Finding("headings", "hierarchy", "pass",
                           "Heading levels nest correctly.", "", "medium"))

    if kw:
        hits = sum(1 for h in heads if kw in h.lower())
        if hits == 0:
            out.append(Finding("headings", "keyword", "warn",
                               f"No subheading contains '{kw}'.",
                               "Work it into at least one H2, naturally.", "medium"))
        elif hits > max(2, len(heads) // 2):
            out.append(Finding("headings", "keyword_stuffing", "warn",
                               f"'{kw}' appears in {hits} of {len(heads)} headings.",
                               "Vary them. Repeating the exact phrase in most "
                               "headings reads as manipulation.", "medium"))
        else:
            out.append(Finding("headings", "keyword", "pass",
                               f"'{kw}' appears in {hits} of {len(heads)} headings.",
                               "", "medium"))

    qs = sum(1 for h in heads if is_question(h))
    ratio = qs / len(heads)
    if ratio < 0.3:
        out.append(Finding("headings", "questions", "warn",
                           f"Only {qs} of {len(heads)} headings are questions.",
                           "Phrase headings as questions people actually ask. This "
                           "is how AI answers and featured snippets match a page.",
                           "medium", {"ratio": round(ratio, 2)}))
    else:
        out.append(Finding("headings", "questions", "pass",
                           f"{qs} of {len(heads)} headings are questions.", "", "medium"))

    long_h = [h for h in heads if len(h) > 70]
    if long_h:
        out.append(Finding("headings", "length", "warn",
                           f"{len(long_h)} heading(s) are over 70 characters.",
                           "Shorten them — a heading is a signpost, not a sentence.",
                           "low", {"examples": long_h[:3]}))
    return out


def _content(art: Article, body: str, words: int) -> list[Finding]:
    out = []
    if words < 300:
        out.append(Finding("content", "length", "fail",
                           f"The post is {words} words.",
                           "Under about 300 words there is rarely enough to rank. "
                           "Either develop it or merge it into a fuller post.", "high"))
    elif words < 600:
        out.append(Finding("content", "length", "warn",
                           f"The post is {words} words — thin for a competitive topic.",
                           "800-1500 is the usual range for an informational post.",
                           "medium"))
    else:
        out.append(Finding("content", "length", "pass", f"{words} words.", "", "medium"))

    fl, band = flesch(body)
    if fl < 45:
        out.append(Finding("content", "readability", "warn",
                           f"Flesch reading ease is {fl} ({band}).",
                           "Shorten sentences and prefer common words. Aim for 55-70 "
                           "for a general audience.", "medium", {"flesch": fl}))
    else:
        out.append(Finding("content", "readability", "pass",
                           f"Flesch reading ease {fl} ({band}).", "", "medium"))

    sents = quality.sentences(body)
    long_s = [s for s in sents if word_count(s) > 30]
    if len(sents) and len(long_s) / len(sents) > 0.18:
        out.append(Finding("content", "sentence_length", "warn",
                           f"{len(long_s)} of {len(sents)} sentences run over 30 words.",
                           "Split them. Long sentences are the main driver of a low "
                           "readability score.", "medium", {"examples": long_s[:2]}))
    else:
        out.append(Finding("content", "sentence_length", "pass",
                           "Sentence lengths are reasonable.", "", "low"))

    paras = [b.text for b in art.blocks if b.type == "p"]
    fat = [p for p in paras if word_count(p) > 150]
    if fat:
        out.append(Finding("content", "paragraph_length", "warn",
                           f"{len(fat)} paragraph(s) exceed 150 words.",
                           "Break them up. Dense blocks lose mobile readers, who are "
                           "most of an Indian audience.", "medium"))
    else:
        out.append(Finding("content", "paragraph_length", "pass",
                           "Paragraph lengths are readable.", "", "low"))

    low = body.lower()
    fillers = [p for p in FILLER if p in low]
    if fillers:
        out.append(Finding("content", "filler", "warn",
                           f"{len(fillers)} padding phrase(s) found.",
                           "Cut them — they add length without information: "
                           + ", ".join(f'"{p}"' for p in fillers[:4]), "low",
                           {"phrases": fillers}))
    tells = [p for p in AI_TELLS if p in low]
    if len(tells) >= 3:
        out.append(Finding("content", "ai_tells", "warn",
                           f"{len(tells)} phrases common in generated text.",
                           "Rewrite these: " + ", ".join(f'"{p}"' for p in tells[:5])
                           + ". They are not penalised directly, but they read as "
                             "machine-written to a human reviewer.", "low"))

    passive = len(PASSIVE.findall(body))
    if len(sents) and passive / len(sents) > 0.25:
        out.append(Finding("content", "passive_voice", "warn",
                           f"Roughly {passive} passive constructions across "
                           f"{len(sents)} sentences.",
                           "Prefer active voice — it is shorter and clearer.", "low"))

    lists = sum(1 for b in art.blocks if b.type == "li")
    if words > 700 and lists == 0:
        out.append(Finding("content", "formatting", "warn",
                           "No bullet or numbered lists in a long post.",
                           "Add lists where you enumerate things. They are scannable "
                           "and are lifted directly into featured snippets.", "medium"))
    return out


def _keywords(art: Article, body: str, kw: str, words: int) -> list[Finding]:
    out = []
    if not kw:
        return [Finding("keywords", "detected", "warn",
                        "No primary keyword could be detected.",
                        "Set one explicitly so placement can be checked.", "medium")]

    density, hits = keyword_density(body, kw)
    if density == 0:
        out.append(Finding("keywords", "density", "fail",
                           f"'{kw}' never appears in the body.",
                           "Use it naturally in the opening paragraph and once or "
                           "twice more.", "high"))
    elif density > 3.0:
        out.append(Finding("keywords", "density", "fail",
                           f"'{kw}' density is {density}% ({hits} uses) — stuffed.",
                           "Reduce to under 2%. Over-optimisation is actively "
                           "penalised.", "high", {"density": density}))
    elif density > 2.0:
        out.append(Finding("keywords", "density", "warn",
                           f"'{kw}' density is {density}% ({hits} uses) — high.",
                           "Aim for 0.5-2%. Replace some with synonyms.", "medium"))
    elif density < 0.3:
        out.append(Finding("keywords", "density", "warn",
                           f"'{kw}' density is {density}% ({hits} uses) — sparse.",
                           "Use it a little more, where it reads naturally.", "low"))
    else:
        out.append(Finding("keywords", "density", "pass",
                           f"'{kw}' density is {density}% ({hits} uses).", "", "medium"))

    first_100 = " ".join(body.split()[:100]).lower()
    if kw not in first_100:
        out.append(Finding("keywords", "first_paragraph", "warn",
                           f"'{kw}' does not appear in the first 100 words.",
                           "Introduce it early — this is where relevance is judged.",
                           "medium"))
    else:
        out.append(Finding("keywords", "first_paragraph", "pass",
                           f"'{kw}' appears in the opening.", "", "medium"))

    if art.slug and kw.replace(" ", "-") not in art.slug.lower():
        out.append(Finding("keywords", "url", "warn",
                           f"The URL slug does not contain '{kw}'.",
                           f"Use a slug like /{kw.replace(' ', '-')}/.", "medium",
                           {"slug": art.slug}))

    # Secondary terms actually present, for context.
    toks = [w for w in re.findall(r"[a-z]+", body.lower())
            if w not in STOP and len(w) > 3]
    common = [w for w, n in Counter(toks).most_common(12) if w not in kw.split()]
    out.append(Finding("keywords", "secondary", "pass",
                       "Most frequent supporting terms: " + ", ".join(common[:8]),
                       "", "low", {"terms": common[:12]}))
    return out


def _links(art: Article, *, check_links: bool) -> list[Finding]:
    out = []
    links = art.links or []
    internal = [l for l in links if l["href"].startswith("/")
                or (art.source_url and _host(l["href"]) == _host(art.source_url))]
    external = [l for l in links if l not in internal and l["href"].startswith("http")]

    if not internal:
        out.append(Finding("links", "internal", "warn",
                           "There are no internal links.",
                           "Link to 2-4 related posts on your own site. It spreads "
                           "authority and keeps readers on the site.", "medium"))
    else:
        out.append(Finding("links", "internal", "pass",
                           f"{len(internal)} internal link(s).", "", "medium"))

    if not external:
        out.append(Finding("links", "external", "warn",
                           "There are no external links.",
                           "Cite a credible source or two. On health content this is "
                           "a trust signal, not just an SEO one.", "medium"))
    else:
        out.append(Finding("links", "external", "pass",
                           f"{len(external)} external link(s).", "", "low"))

    generic = [l for l in links if l["text"].strip().lower() in GENERIC_ANCHORS]
    if generic:
        out.append(Finding("links", "anchor_text", "warn",
                           f"{len(generic)} link(s) use generic anchor text.",
                           "Describe the destination instead of 'click here'. "
                           "Anchor text tells search engines what the target is about.",
                           "medium", {"examples": [l["text"] for l in generic[:4]]}))

    if check_links and links:
        broken = _check_broken(links)
        if broken:
            out.append(Finding("links", "broken", "fail",
                               f"{len(broken)} link(s) are broken.",
                               "Fix or remove them: "
                               + ", ".join(f"{b['href']} ({b['status']})" for b in broken[:4]),
                               "high", {"broken": broken}))
        else:
            out.append(Finding("links", "broken", "pass",
                               f"All {len(links)} link(s) resolve.", "", "medium"))
    return out


def _host(url: str) -> str:
    import urllib.parse
    h = urllib.parse.urlparse(url).netloc.lower()
    return h[4:] if h.startswith("www.") else h


def _check_broken(links: list[dict], limit: int = 25) -> list[dict]:
    """HEAD each unique external link, falling back to GET where HEAD is refused."""
    import sources
    seen, broken = set(), []
    for l in links[:limit]:
        url = l["href"]
        if not url.startswith("http") or url in seen:
            continue
        seen.add(url)
        try:
            r = sources.SESSION.head(url, timeout=12, allow_redirects=True)
            if r.status_code >= 400:
                r = sources.SESSION.get(url, timeout=15, stream=True)
            if r.status_code >= 400:
                broken.append({"href": url, "text": l["text"], "status": r.status_code})
        except Exception as exc:
            broken.append({"href": url, "text": l["text"],
                           "status": exc.__class__.__name__})
    return broken


def _images(art: Article, kw: str) -> list[Finding]:
    out, imgs = [], art.images or []
    if not imgs:
        out.append(Finding("images", "present", "warn", "The post has no images.",
                           "Add at least one. Images earn image-search traffic and "
                           "break up the text.", "medium"))
        return out

    missing = [i for i in imgs if not (i.get("alt") or "").strip()]
    if missing:
        out.append(Finding("images", "alt", "fail",
                           f"{len(missing)} of {len(imgs)} images have no alt text.",
                           "Describe each image. Alt text is an accessibility "
                           "requirement first and an SEO signal second.", "high",
                           {"srcs": [i["src"] for i in missing[:4]]}))
    else:
        out.append(Finding("images", "alt", "pass",
                           f"All {len(imgs)} images have alt text.", "", "high"))

    if kw and not any(kw in (i.get("alt") or "").lower() for i in imgs):
        out.append(Finding("images", "alt_keyword", "warn",
                           f"No image alt text mentions '{kw}'.",
                           "Include it in one image's alt, where it genuinely "
                           "describes the picture.", "low"))

    bad_names = [i for i in imgs
                 if re.search(r"/(?:img|image|photo|dsc|screenshot)[-_]?\d*\.\w+$",
                              i["src"], re.I)]
    if bad_names:
        out.append(Finding("images", "filename", "warn",
                           f"{len(bad_names)} image(s) have uninformative filenames.",
                           "Rename to describe the content, e.g. "
                           "thyroid-symptoms-chart.jpg.", "low"))
    return out


def _technical(art: Article, base_url: str) -> list[Finding]:
    out = []
    slug = art.slug or ""
    if not slug:
        out.append(Finding("technical", "slug", "warn", "No URL slug is set.",
                           "Set a short, hyphenated, lowercase slug.", "medium"))
    else:
        if len(slug) > SLUG_MAX:
            out.append(Finding("technical", "slug_length", "warn",
                               f"The slug is {len(slug)} characters.",
                               f"Trim to under {SLUG_MAX} and drop stop words.", "low"))
        if "_" in slug or slug != slug.lower():
            out.append(Finding("technical", "slug_format", "warn",
                               "The slug uses underscores or capitals.",
                               "Use lowercase and hyphens.", "low"))
        if re.search(r"%[0-9A-Fa-f]{2}", slug):
            out.append(Finding("technical", "slug_encoding", "warn",
                               "The slug is percent-encoded.",
                               "Transliterate non-Latin slugs to Roman characters — "
                               "an encoded URL is unreadable in results and in chat "
                               "apps.", "medium"))
        if len(slug) <= SLUG_MAX and "_" not in slug and slug == slug.lower():
            out.append(Finding("technical", "slug", "pass",
                               f"Slug is clean: /{slug}/", "", "medium"))

    schema = (art.meta or {}).get("schema")
    if not schema:
        out.append(Finding("technical", "schema", "fail",
                           "There is no JSON-LD structured data.",
                           "Add Article and FAQPage schema. It is the only part of "
                           "the page an answer engine can read without guessing.",
                           "high"))
    else:
        types = {s.get("@type") for s in schema.get("@graph", [])}
        want = {"Article", "MedicalWebPage"} & types
        missing = []
        if not want:
            missing.append("Article")
        if art.faqs and "FAQPage" not in types:
            missing.append("FAQPage")
        if "BreadcrumbList" not in types:
            missing.append("BreadcrumbList")
        if missing:
            out.append(Finding("technical", "schema_types", "warn",
                               "JSON-LD is missing: " + ", ".join(missing),
                               "Generate the full graph — the tool can do this for "
                               "you.", "medium", {"present": sorted(t for t in types if t)}))
        else:
            out.append(Finding("technical", "schema", "pass",
                               "JSON-LD covers " + ", ".join(sorted(t for t in types if t)),
                               "", "high"))

    if not base_url and not art.source_url:
        out.append(Finding("technical", "canonical", "warn",
                           "No canonical URL is set.",
                           "Set one so duplicate or parameterised URLs consolidate.",
                           "medium"))
    else:
        out.append(Finding("technical", "canonical", "pass",
                           "A canonical URL is available.", "", "medium"))
    return out


def _aeo(art: Article) -> list[Finding]:
    """Answer-engine readiness, reusing the pipeline's own AEO scorer."""
    sub = quality.score_aeo(art, art.lang or "en")
    out = [Finding("aeo", "score",
                   "pass" if sub.score >= 70 else "warn" if sub.score >= 45 else "fail",
                   f"Answer-engine readiness is {sub.score:.0f}/100.",
                   "Question headings with a short standalone answer under each, a "
                   "TL;DR block and an FAQ section are what get a page quoted.",
                   "high", sub.detail)]
    for fl in sub.flags:
        out.append(Finding("aeo", fl.kind, "warn" if fl.severity != "error" else "fail",
                           fl.detail, "", "medium"))
    return out


def _eeat(art: Article) -> list[Finding]:
    out = []
    if not (art.author or "").strip():
        out.append(Finding("eeat", "author", "warn", "No author is credited.",
                           "Name a real author with a bio. Anonymous content ranks "
                           "worse, and on health topics it is a serious weakness.",
                           "medium"))
    else:
        out.append(Finding("eeat", "author", "pass", f"Author: {art.author}", "", "medium"))

    if not (art.published or "").strip():
        out.append(Finding("eeat", "date", "warn", "No publish date is set.",
                           "Show published and last-updated dates. Freshness is a "
                           "ranking factor for health and news topics.", "medium"))
    else:
        out.append(Finding("eeat", "date", "pass", f"Published {art.published}",
                           "", "low"))

    text = art.full_text().lower()
    profile_ymyl = any(w in text for w in
                       ("symptom", "treatment", "dosage", "diagnos", "disease",
                        "medicine", "patient", "therapy", "mg ", "doctor"))
    if profile_ymyl:
        if not re.search(r"\b(consult|doctor|physician|medical advice|supervision)\b", text):
            out.append(Finding("eeat", "medical_disclaimer", "fail",
                               "Health content with no advice to consult a clinician.",
                               "Add a line directing readers to a doctor, and a "
                               "disclaimer. This is a YMYL page — Google holds it to "
                               "a higher standard, and so should you.", "high"))
        else:
            out.append(Finding("eeat", "medical_disclaimer", "pass",
                               "The post routes readers to a clinician.", "", "high"))

        if not (art.author_credentials or "").strip():
            out.append(Finding("eeat", "credentials", "warn",
                               "Health content with no reviewer credentials.",
                               "Add 'Medically reviewed by …' with a qualification.",
                               "high"))

        strong = [c for c in ("cure", "guaranteed", "100%", "permanent cure",
                              "no side effects", "miracle") if c in text]
        if strong:
            out.append(Finding("eeat", "overclaim", "fail",
                               "Absolute medical claims found: " + ", ".join(strong),
                               "Remove or hedge them. These breach advertising rules "
                               "for health products in India and destroy trust.",
                               "high"))
    return out


def _cannibalisation(art: Article, kw: str, site_index: dict) -> list[Finding]:
    """Are you already competing with yourself for this keyword?"""
    from .ideas import classify
    if not kw:
        return []
    status, url, score = classify(kw, site_index)
    if status == "covered" and url:
        return [Finding("cannibalisation", "duplicate_target", "warn",
                        f"An existing post already targets '{kw}'.",
                        f"Two pages chasing one keyword split their own rankings. "
                        f"Either merge them or retarget this one: {url}", "high",
                        {"url": url, "score": score})]
    if status == "partial" and url:
        return [Finding("cannibalisation", "related_post", "pass",
                        f"A related post exists — link to it: {url}",
                        "", "low", {"url": url, "score": score})]
    return [Finding("cannibalisation", "unique", "pass",
                    f"No existing post on your site targets '{kw}'.", "", "medium")]


# ------------------------------------------------------------ SERP preview

def serp_preview(art: Article, base_url: str = "https://example.com") -> dict:
    """What the result actually looks like, truncated the way Google truncates."""
    title = (art.title or "").strip()
    desc = (art.meta_description or "").strip()
    url = f"{base_url.rstrip('/')}/{art.slug}/" if art.slug else base_url
    return {
        "url": url,
        "title": title if len(title) <= TITLE_MAX else title[:TITLE_MAX - 1].rstrip() + "…",
        "description": desc if len(desc) <= DESC_MAX else desc[:DESC_MAX - 1].rstrip() + "…",
        "title_truncated": len(title) > TITLE_MAX,
        "desc_truncated": len(desc) > DESC_MAX,
        "title_len": len(title), "desc_len": len(desc),
    }
