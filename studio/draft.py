"""Stage 2: brief -> an English post, written AEO-first.

Structured for answer engines from the outset rather than written and then
retrofitted. Retrofitting produces a post with an FAQ bolted on the end; writing
to the structure produces one where every section is independently quotable,
which is the whole point.

Output is an `extract.Article` so stage 3 consumes it with no conversion --
`run.process_language` cannot tell a drafted post from a scraped one.
"""
from __future__ import annotations

import json
import re

from common import config, log, site_profile, slugify, warn
from extract import Article, Block
import aeo

CFG = config()
A = CFG["aeo"]

DRAFT_SCHEMA = {
    "title": "string -- <= 60 chars, reads like a person wrote it",
    "meta_description": "string -- 50-155 chars",
    "tldr_heading": "string -- e.g. 'The short version'",
    "tldr": ["string x5 -- one fact each, quotable with no context around it"],
    "blocks": [{"type": "h2|h3|p|li", "text": "string"}],
    "answers": {"<exact heading text>": "40-60 word direct answer"},
    "faqs": [{"q": "string", "a": "string"}],
    "slug": "string -- lowercase ascii, hyphenated",
    "notes": "string -- anything you could not support, or had to hedge",
}


def draft_prompt(brief: dict, profile: dict, *, words: int = 900) -> str:
    """Everything the writer needs, with the brief's own research in it."""
    brand = profile.get("brand") or "the publisher"
    voice = profile.get("voice", "clear, plain, conversational")
    ymyl = profile.get("ymyl")

    targets = "\n".join(f"  - {q}" for q in (brief.get("target_queries") or [])[:12])
    questions = "\n".join(f"  - {q}" for q in (brief.get("questions") or [])[:12])
    links = "\n".join(f"  - {l['url']}  (covers: {l['why']})"
                      for l in (brief.get("internal_links") or [])[:6])

    ymyl_block = ""
    if ymyl:
        g = json.loads((__import__("pathlib").Path(__file__).resolve().parent.parent
                        / "glossary.json").read_text(encoding="utf-8"))
        guard = g["medical_claim_guard"]
        ymyl_block = f"""
THIS IS HEALTH CONTENT. It is read by people deciding what to do about a
symptom, so the rules are not stylistic:
  - Never write any of these, in any phrasing: {', '.join(guard['forbidden_new_claims'][:8])}
  - Every treatment claim carries a hedge and a route to a clinician.
    "may help", "talk to a doctor", "under supervision".
  - Never invent a statistic, a dosage, a study or a percentage. If you would
    need a number you do not have, write the sentence without one and say so
    in `notes`. A fabricated figure in health copy is the worst thing this
    tool could produce.
  - Never name a specific drug or dose unless the brief supplied it.
  - Locked terms that must appear exactly as written: {', '.join(g['never_translate'][:8])}
"""

    return f"""Write a blog post in English for {brand}.

BRAND VOICE: {voice}
LENGTH: about {words} words.

Write it structured for answer engines. That is not a formatting step applied
afterwards -- it changes how you write every section:

  - Every H2 is a question a real person would type or say aloud.
  - Under each H2, the FIRST thing is a {A['answer_words_min']}-{A['answer_words_max']} word direct answer that still makes
    complete sense if someone quotes it alone with no surrounding context.
    Put those in `answers`, keyed by the exact heading text used in `blocks`.
    The detail, nuance and caveats come after it, in normal paragraphs.
  - Open with a {A['tldr_bullets']}-bullet TL;DR. This is the passage an answer engine lifts
    first. Each bullet must stand alone -- no "it", no "this", no "as above".
  - Close with {A['faq_min']}-{A['faq_max']} FAQs taken from the real queries below, not invented ones.
  - Name entities explicitly instead of using pronouns. "{brand}'s doctors"
    can be cited in a generated answer; "our doctors" cannot.
  - Title <= {A['meta_title_max']} chars. Meta description 50-{A['meta_desc_max']} chars.

WRITE LIKE A PERSON, NOT A CONTENT MILL:
  - Vary sentence length hard. Put a four-word sentence next to a long one.
    Uniform rhythm is the single most obvious tell of generated text.
  - No "Moreover", "Furthermore", "In conclusion", "It is important to note",
    "In today's fast-paced world". Not one.
  - Do not open three sentences the same way.
  - Do not restate the heading as the first line of the section.
{ymyl_block}
REAL QUERIES this post should answer (from Google autocomplete -- these are
what people actually type). Cover them because they are genuine questions, not
by stuffing the phrases in:
{targets or '  (none supplied)'}

QUESTIONS worth answering directly:
{questions or '  (none supplied)'}

INTERNAL LINKS -- existing posts on this site covering related ground. Refer to
them naturally where it helps the reader:
{links or '  (none)'}

TOPIC: {brief.get('topic', '')}
WORKING ANGLES: {', '.join((brief.get('titles') or [])[:5]) or '(none)'}

Reply with JSON only:
{json.dumps(DRAFT_SCHEMA, indent=2)}"""


def to_article(plan: dict, brief: dict, profile: dict) -> Article:
    """Fold a draft response into an Article, AEO blocks in reading order."""
    art = Article(lang="en", source_type="draft")
    art.title = (plan.get("title") or brief.get("topic") or "Untitled").strip()
    art.meta_description = (plan.get("meta_description") or "").strip()
    art.author = profile.get("brand") or ""
    art.author_credentials = ("Reviewed by the medical team" if profile.get("ymyl") else "")
    art.slug = slugify(plan.get("slug") or art.title)
    art.meta = {"brief_topic": brief.get("topic", ""),
                "drafted": True,
                "notes": plan.get("notes", "")}

    # apply_structure already knows how to interleave TL;DR bullets, blocks and
    # answer blocks in reading order -- reuse it rather than repeating the logic.
    art.blocks = [Block(type=b.get("type", "p"), text=(b.get("text") or "").strip())
                  for b in (plan.get("blocks") or [])
                  if (b.get("text") or "").strip()]
    art.faqs = [f for f in (plan.get("faqs") or [])
                if f.get("q", "").strip() and f.get("a", "").strip()]

    structured = aeo.apply_structure(art, {
        "tldr_heading": plan.get("tldr_heading") or "The short version",
        "tldr": plan.get("tldr") or [],
        "answers": plan.get("answers") or {},
        "faqs": art.faqs,
    })
    structured.slug = art.slug
    structured.lang = "en"
    structured.source_type = "draft"
    structured.meta = art.meta
    return structured


def write(brief: dict, *, writer, site: str = "", words: int = 900) -> Article:
    """Brief -> Article. Raises WriterPending under the claude_local backend."""
    profile = site_profile(site or brief.get("site"))
    slug = slugify(brief.get("topic", "draft"))
    log(f"draft: writing '{brief.get('topic', '')}' ({words} words target)")
    plan = writer.generate(draft_prompt(brief, profile, words=words),
                           stage="draft", slug=slug, lang="en")
    art = to_article(plan, brief, profile)
    log(f"draft: {art.words()} words, {len(art.headings())} headings, "
        f"{len(art.faqs)} FAQs", indent=1)
    return art


HUMANIZE_SCHEMA = {
    "title": "string -- <= 60 chars",
    "meta_description": "string -- 50-155 chars",
    "tldr_heading": "string",
    "tldr": ["string x5"],
    "blocks": [{"type": "h2|h3|p|li", "text": "string"}],
    "answers": {"<exact heading text>": "40-60 word direct answer"},
    "faqs": [{"q": "string", "a": "string"}],
    "notes": "string -- what you changed",
}


def humanize_prompt(art, findings: list[str], profile: dict, *, attempt: int = 1) -> str:
    """Rewrite to fix specific, measured findings -- not 'sound more human'.

    `findings` mixes structural/review flags (english.rewrite_brief) with real
    keyword-density gaps (english.keyword_brief) so the model gets the same
    concrete, checkable feedback the translation rewrite loop uses, rather than
    a vague instruction that produces nothing testable.
    """
    import aeo
    lines = "\n".join(f"  - {f}" for f in findings)
    return f"""Revise this post. Automated checks found specific issues -- fix
these exactly, do not do a generic pass:

{lines}

WHY THESE MATTER:
  - The paragraph-rhythm and stock-phrase checks are calibrated against real
    human writing vs real AI output (measured AUC 1.00 on the test set). They
    catch uniform paragraph lengths, "moreover/furthermore/delve into"-style
    filler, and colon-led list scaffolding -- rewrite AWAY from those shapes,
    do not just swap synonyms.
  - Vary paragraph length on purpose: a two-sentence paragraph next to a
    five-sentence one reads as written, not generated.
  - Cut every stock transition. Replace with nothing, or with how someone
    would actually continue the thought.
  - Work each named keyword in naturally, in a sentence where it belongs --
    not by inserting a sentence whose only purpose is containing the keyword.

HARD CONSTRAINTS -- checked automatically after you reply:
  - Every number, dosage, price and URL must survive unchanged.
  - Keep the subject, the facts and the structure. This is a repair, not a
    rewrite into a different post.
  - Do not shorten it.

BRAND VOICE: {profile.get('voice', 'clear, plain, conversational')}

=== CURRENT POST ===
{aeo.render_markdown(art)}

Reply with JSON only:
{json.dumps(HUMANIZE_SCHEMA, indent=2)}"""
