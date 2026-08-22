"""AEO / GEO layer: the structure answer engines actually quote from.

Search engines rank pages. Answer engines lift passages. The difference decides
the whole shape of this module:

  * A question-shaped H2 with a 40-60 word direct answer underneath is a unit an
    engine can quote whole. A 300-word section that eventually answers the
    question is not.
  * A TL;DR block at the top is the passage that gets lifted first.
  * FAQPage JSON-LD is the only part of a page an engine can consume without
    guessing at structure.
  * Entities get cited; pronouns do not. "HIIMS ke Ayurvedic doctors" survives
    into an answer, "our doctors" does not.
  * hreflang is what stops nine language variants from cannibalising each other.
"""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import date

from common import config, lang_by_code, slugify, word_count
from extract import Article, Block, is_question

CFG = config()
A = CFG["aeo"]

_SCRIPT_TO_SANSCRIPT = {
    "Devanagari": "devanagari", "Bengali": "bengali", "Gujarati": "gujarati",
    "Gurmukhi": "gurmukhi", "Tamil": "tamil", "Telugu": "telugu",
    "Kannada": "kannada", "Oriya": "oriya", "Malayalam": "malayalam",
}


# ------------------------------------------------------------------- slugs

def roman_slug(text: str, lang: str) -> str:
    """Transliterate an Indic title to a readable ASCII slug.

    A percent-encoded Devanagari URL is unreadable in a SERP, unshareable in
    WhatsApp, and truncated in most analytics tools. Transliteration keeps the
    keyword legible: /madhumeh-ke-lakshan, not /%E0%A4%AE%E0%A4%A7...
    """
    entry = lang_by_code(lang)
    script = entry.get("script", "Latin")
    if script == "Latin":
        return slugify(text)

    scheme = _SCRIPT_TO_SANSCRIPT.get(script)
    if scheme:
        try:
            from indic_transliteration import sanscript
            text = sanscript.transliterate(text, scheme, sanscript.IAST)
        except Exception:
            pass                      # fall through to the ASCII-fold below
    folded = unicodedata.normalize("NFKD", text)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return slugify(folded)


# ------------------------------------------------------------- restructuring

def apply_structure(art: Article, plan: dict) -> Article:
    """Fold the writer backend's AEO plan back into the Article.

    `plan` carries the pieces a rewriter produced: a TL;DR, question-form
    headings with their direct answers, and an FAQ set. Everything is inserted
    in place so block order still reflects reading order.
    """
    out = Article.from_dict(art.dict())

    if plan.get("title"):
        out.title = plan["title"].strip()
    if plan.get("meta_description"):
        out.meta_description = plan["meta_description"].strip()

    blocks: list[Block] = []
    tldr = [b.strip() for b in (plan.get("tldr") or []) if b.strip()]
    if tldr:
        blocks.append(Block(type="h2", text=plan.get("tldr_heading") or "TL;DR"))
        for bullet in tldr[: A["tldr_bullets"]]:
            blocks.append(Block(type="tldr", text=bullet))

    # answers: {heading text -> direct answer}. Inserted right after the heading
    # so the quotable unit is heading + answer, adjacent.
    answers = {k.strip(): v.strip() for k, v in (plan.get("answers") or {}).items() if v}
    rewritten = plan.get("blocks")

    source_blocks = ([Block(type=b["type"], text=b["text"]) for b in rewritten]
                     if rewritten else out.blocks)

    for b in source_blocks:
        blocks.append(b)
        if b.type in ("h2", "h3") and b.text.strip() in answers:
            blocks.append(Block(type="answer", text=answers[b.text.strip()]))

    out.blocks = blocks

    faqs = [f for f in (plan.get("faqs") or [])
            if f.get("q", "").strip() and f.get("a", "").strip()]
    if faqs:
        out.faqs = faqs[: A["faq_max"]]

    if plan.get("images_alt"):
        out.images = [{**im, "alt": plan["images_alt"].get(im.get("src", ""), im.get("alt", ""))}
                      for im in out.images]

    out.meta = dict(out.meta or {})
    out.meta["aeo_applied"] = True
    return out


def answer_length_ok(text: str) -> bool:
    n = word_count(text)
    return A["answer_words_min"] - 10 <= n <= A["answer_words_max"] + 15


# ---------------------------------------------------------------- schema

def build_schema(art: Article, profile: dict, lang: str, url: str,
                 *, hreflang_urls: dict[str, str] | None = None) -> dict:
    """JSON-LD @graph: Article + FAQPage + BreadcrumbList + publisher."""
    entry = lang_by_code(lang)
    today = date.today().isoformat()
    org_type = profile.get("org_type", "Organization")
    brand = profile.get("brand") or art.meta.get("sitename") or "Publisher"
    base = url.rsplit("/", 1)[0] if "/" in url else url

    publisher = {
        "@type": org_type,
        "@id": f"{base}#organization",
        "name": brand,
    }
    if profile.get("location"):
        publisher["address"] = {"@type": "PostalAddress",
                                "addressLocality": profile["location"]}

    author = {
        "@type": "Person" if art.author and " " in art.author else "Organization",
        "name": art.author or brand,
    }
    # YMYL: health content that does not name a reviewer with credentials is
    # both a trust problem and an E-E-A-T ranking problem.
    if profile.get("ymyl"):
        author["jobTitle"] = art.author_credentials or "Reviewed by the medical team"

    article = {
        "@type": "MedicalWebPage" if profile.get("ymyl") else "Article",
        "@id": f"{url}#article",
        "headline": art.title[:110],
        "description": art.meta_description,
        "inLanguage": entry["bhashini"] if lang != "hinglish" else "hi-Latn",
        "datePublished": art.published or today,
        "dateModified": today,
        "author": author,
        "publisher": publisher,
        "mainEntityOfPage": {"@type": "WebPage", "@id": url},
        "wordCount": art.words(),
        # `speakable` is what voice assistants read aloud. Pointing it at the
        # TL;DR means the answer, not the intro, is what gets spoken.
        "speakable": {"@type": "SpeakableSpecification",
                      "cssSelector": [".tldr", "h1"]},
    }
    if art.images:
        article["image"] = [im["src"] for im in art.images[:3]]
    if profile.get("ymyl"):
        article["reviewedBy"] = author
        article["lastReviewed"] = today

    entities = sorted({e for e in _entities(art, profile) if e})
    if entities:
        article["about"] = [{"@type": "Thing", "name": e} for e in entities[:8]]

    graph = [article, publisher]

    if art.faqs:
        graph.append({
            "@type": "FAQPage",
            "@id": f"{url}#faq",
            "mainEntity": [
                {"@type": "Question", "name": f["q"],
                 "acceptedAnswer": {"@type": "Answer", "text": f["a"]}}
                for f in art.faqs
            ],
        })

    graph.append({
        "@type": "BreadcrumbList",
        "@id": f"{url}#breadcrumb",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": brand, "item": base},
            {"@type": "ListItem", "position": 2, "name": art.title[:90], "item": url},
        ],
    })

    return {"@context": "https://schema.org", "@graph": graph}


def _entities(art: Article, profile: dict) -> list[str]:
    """Named things worth anchoring. Engines cite entities, not pronouns."""
    out = [profile.get("brand"), profile.get("location")]
    for term in (CFG.get("_entities") or []):
        out.append(term)
    # Headings usually name the condition, product or process the page is about.
    for h in art.headings()[:10]:
        cleaned = re.sub(r"^\s*\d+[.)]\s*", "", h).strip(" ?:।")
        if 2 < len(cleaned) < 60:
            out.append(cleaned)
    return out


# --------------------------------------------------------------- hreflang

def build_hreflang(slug_map: dict[str, str], base: str) -> list[dict]:
    """One alternate per language variant, plus x-default.

    Without this, nine translations of the same post compete with each other
    for the same intent and split their own authority.
    """
    codes = {"hinglish": "hi-Latn"}
    out = []
    for lang, url in slug_map.items():
        entry = lang_by_code(lang) if lang != "en" else {"bhashini": "en"}
        hl = codes.get(lang, entry.get("bhashini", lang))
        out.append({"hreflang": f"{hl}-IN" if hl not in ("en",) else "en",
                    "href": url})
    if A["x_default"] in slug_map:
        out.append({"hreflang": "x-default", "href": slug_map[A["x_default"]]})
    return out


# ------------------------------------------------------------------ render

def _esc(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def render_html(art: Article, *, schema: dict | None = None,
                hreflang: list[dict] | None = None, canonical: str = "") -> str:
    """Publish-ready HTML: head tags, body, and the JSON-LD, paste-able as-is."""
    entry = lang_by_code(art.lang) if art.lang != "en" else {"bhashini": "en"}
    lang_attr = "hi-Latn" if art.lang == "hinglish" else entry.get("bhashini", art.lang)

    head = [
        f'<title>{_esc(art.title)}</title>',
        f'<meta name="description" content="{_esc(art.meta_description)}">',
        f'<meta property="og:title" content="{_esc(art.title)}">',
        f'<meta property="og:description" content="{_esc(art.meta_description)}">',
        '<meta property="og:type" content="article">',
    ]
    if canonical:
        head.append(f'<link rel="canonical" href="{_esc(canonical)}">')
    for alt in (hreflang or []):
        head.append(f'<link rel="alternate" hreflang="{alt["hreflang"]}" '
                    f'href="{_esc(alt["href"])}">')
    if schema:
        head.append('<script type="application/ld+json">\n'
                    + json.dumps(schema, ensure_ascii=False, indent=2)
                    + '\n</script>')

    body = [f"<article lang=\"{lang_attr}\">", f"  <h1>{_esc(art.title)}</h1>"]
    in_tldr = False
    in_list = False

    def close_blocks() -> None:
        nonlocal in_tldr, in_list
        if in_tldr:
            body.append("  </ul></aside>")
            in_tldr = False
        if in_list:
            body.append("  </ul>")
            in_list = False

    for b in art.blocks:
        if b.type == "tldr":
            if not in_tldr:
                close_blocks()
                body.append('  <aside class="tldr"><ul>')
                in_tldr = True
            body.append(f"    <li>{_esc(b.text)}</li>")
            continue
        if b.type == "li":
            if not in_list:
                close_blocks()
                body.append("  <ul>")
                in_list = True
            body.append(f"    <li>{_esc(b.text)}</li>")
            continue
        close_blocks()
        if b.type in ("h2", "h3"):
            body.append(f"  <{b.type}>{_esc(b.text)}</{b.type}>")
        elif b.type == "answer":
            body.append(f'  <p class="answer"><strong>{_esc(b.text)}</strong></p>')
        elif b.type == "quote":
            body.append(f"  <blockquote>{_esc(b.text)}</blockquote>")
        elif b.type == "code":
            body.append(f"  <pre>{_esc(b.text)}</pre>")
        else:
            body.append(f"  <p>{_esc(b.text)}</p>")
    close_blocks()

    if art.faqs:
        body.append('  <section class="faq">')
        body.append(f"    <h2>{_esc(art.meta.get('faq_heading') or 'FAQ')}</h2>")
        for f in art.faqs:
            body.append(f"    <h3>{_esc(f['q'])}</h3>")
            body.append(f"    <p>{_esc(f['a'])}</p>")
        body.append("  </section>")
    body.append("</article>")

    return ("<!-- head -->\n" + "\n".join(head)
            + "\n\n<!-- body -->\n" + "\n".join(body) + "\n")


def render_markdown(art: Article) -> str:
    out = [f"# {art.title}", ""]
    if art.meta_description:
        out += [f"> {art.meta_description}", ""]

    in_list = False
    for b in art.blocks:
        # A list that runs straight into the next paragraph renders as one
        # blob, both in the .md deliverable and in the text sent to the reviewer.
        if b.type in ("li", "tldr"):
            out.append(f"- {b.text}")
            in_list = True
            continue
        if in_list:
            out.append("")
            in_list = False

        if b.type == "h2":
            out += [f"## {b.text}", ""]
        elif b.type == "h3":
            out += [f"### {b.text}", ""]
        elif b.type == "answer":
            out += [f"**{b.text}**", ""]
        elif b.type == "quote":
            out += [f"> {b.text}", ""]
        else:
            out += [b.text, ""]
    if in_list:
        out.append("")
    if art.faqs:
        out += ["", "## FAQ", ""]
        for f in art.faqs:
            out += [f"### {f['q']}", "", f["a"], ""]
    return "\n".join(out) + "\n"
