"""Shared writer contract and the prompts every backend uses.

The prompts live here, not in the backends, so that swapping Claude for Gemini
changes the transport and nothing else. If output quality moves, it moved
because of the model, not because two backends drifted apart.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import linguistics as L
from common import lang_by_code, glossary

PACKET_STAGES = ("transcreate", "review", "rewrite")


class WriterUnavailable(RuntimeError):
    """Backend cannot run here -- missing key, missing session, missing package."""


class WriterPending(RuntimeError):
    """Work has been queued but the answer is not back yet (claude_local only)."""

    def __init__(self, packets: list[str]):
        self.packets = packets
        super().__init__(
            f"{len(packets)} work packet(s) queued and waiting for the writer.\n"
            "  Run  /aeo-rewrite  in this Claude Code session to process them, "
            "then re-run the same command -- the pipeline resumes where it stopped.")


# --------------------------------------------------------------- rubric text

def language_rubric(lang: str) -> str:
    """The specific, checkable rules for this language, drawn from calibration.

    Deliberately concrete. "Write naturally" produces nothing; "you are using
    'के रूप में' where a native writer would restructure the sentence" produces
    a fix.
    """
    entry = lang_by_code(lang)
    name, script = entry["name"], entry["script"]
    calques = ", ".join(L.get(L.CALQUES, lang, ())[:8])
    connectives = ", ".join(L.get(L.AI_CONNECTIVES, lang, ())[:8])
    honorific = entry.get("honorific", "formal")
    forms = L.HONORIFICS.get(lang, {}).get("formal", ())

    lines = [
        f"TARGET LANGUAGE: {name} ({script} script).",
        "",
        "These are measured differences between native writing and machine",
        "translation in this language. Every one is checked automatically after",
        "you reply, so fixing them is not a matter of taste:",
        "",
        "1. PREPOSITIONAL CALQUES. Native writing shows ~0.4 per 1000 words;",
        "   machine translation shows ~10. Do not carry English prepositions",
        "   across literally. Restructure the sentence instead.",
        "2. RELATIVE CLAUSES. English 'which/that' clauses render as जो/जिसे/",
        "   जिसमें and pile up. Native prose splits the sentence into two.",
        "   Native ~0.4 per 1000 words; machine translation ~3.7.",
        "3. COMMAS. English punctuation rhythm carries across. Native prose uses",
        "   ~22 commas per 1000 words; machine translation uses ~72.",
        "4. NO SPACE BEFORE PUNCTUATION. Never write 'शब्द ,' or 'शब्द ।'.",
        "   Never write a spaced hyphen ('बार - बार'). These are detokenisation",
        "   artefacts and they are the clearest giveaway in the whole document.",
        "5. SENTENCE RHYTHM. Vary sentence length deliberately. Native prose has",
        "   a length coefficient of variation near 0.6; machine output sits near",
        "   0.3-0.45. Put a four-word sentence next to a twenty-five word one.",
        "6. DO NOT repeat the same sentence opening. Three or more sentences",
        "   starting the same way reads as generated.",
        f"7. HONORIFICS. Address the reader as '{honorific}'"
        + (f" ({', '.join(forms[:3])})" if forms else "")
        + " and never switch level mid-document.",
        "",
        "Sentence-final punctuation is your choice. Both the danda and the full",
        "stop are used by native writers; be consistent within the document.",
    ]
    if calques:
        lines += ["", f"AVOID these stock literal constructions: {calques}"]
    if connectives:
        lines += [f"AVOID leaning on these discourse markers: {connectives}",
                  "They are direct calques of Moreover/Furthermore/However and",
                  "generated text overuses them. Use at most one or two total."]
    return "\n".join(lines)


def glossary_rules(lang: str) -> str:
    g = glossary()
    never = ", ".join(g["never_translate"])
    translit = {k: v.get(lang) for k, v in g["transliterate_only"].items()
                if not k.startswith("_") and isinstance(v, dict) and v.get(lang)}
    preferred = g.get("preferred", {}).get(lang, {})
    guard = g["medical_claim_guard"]

    lines = [
        "TERM LOCK -- these are checked automatically and a mismatch blocks publication:",
        f"  Never translate or alter: {never}",
    ]
    if translit:
        lines.append("  Render in target script exactly as: "
                     + ", ".join(f"{k} -> {v}" for k, v in translit.items()))
    if preferred:
        lines.append("  Prefer these renderings: "
                     + ", ".join(f"{k} -> {v}" for k, v in preferred.items()
                                 if not k.startswith("_")))
    lines += [
        "  Every number, dosage, percentage, price, phone number, email and URL",
        "  must appear in your output byte-identical to the source. Changing",
        "  '500 mg' to 'half a gram' is a defect, not a style choice.",
        "",
        "MEDICAL CLAIMS -- this is health content:",
        f"  Never introduce: {', '.join(guard['forbidden_new_claims'][:8])}",
        "  Never remove a hedge the source had ('may help', 'consult a doctor').",
        "  You may change how a claim is phrased. You may not make it stronger.",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------ prompts

TRANSCREATE_SCHEMA = {
    "title": "string -- native, not translated; <= 60 chars",
    "meta_description": "string -- native; 50-155 chars",
    "tldr_heading": "string -- e.g. 'एक नज़र में'",
    "tldr": ["string x5 -- one fact each, quotable standalone"],
    "blocks": [{"type": "h2|h3|p|li|quote", "text": "string"}],
    "answers": {"<exact heading text>": "40-60 word direct answer"},
    "faqs": [{"q": "string", "a": "string"}],
    "images_alt": {"<image src>": "translated alt text"},
    "slug_roman": "string -- transliterated ASCII slug",
    "notes": "string -- anything you could not do faithfully",
}

REVIEW_SCHEMA = {
    "score": "number 0-100 -- how much this reads as written by a native writer",
    "flags": [{"kind": "string", "severity": "error|warn|note",
               "detail": "what is wrong and how to fix it",
               "sample": "the offending phrase"}],
    "notes": "string",
}


def transcreate_prompt(*, lang: str, source_md: str, mt_md: str,
                       profile: dict, keywords: list[dict], aeo_cfg: dict) -> str:
    entry = lang_by_code(lang)
    kw = ", ".join(k.get("keyword", "") for k in (keywords or [])[:25] if k.get("keyword"))
    voice = profile.get("voice", "clear, plain, conversational")
    brand = profile.get("brand") or "the publisher"

    hinglish_note = ""
    if lang == "hinglish":
        hinglish_note = (
            "\nThis is HINGLISH: Hindi written in Roman script, the way people\n"
            "actually type it in search and WhatsApp. Not English. Not\n"
            "transliterated-formal-Hindi either -- write what someone would type:\n"
            "'diabetes ke lakshan kya hain', not 'madhumeha ke lakshana'.\n")

    return f"""You are localising a blog post into {entry['name']} for {brand}.

This is TRANSCREATION, not translation. A reader in {entry.get('region', 'India')}
must not be able to tell this began in English. You have a raw machine
translation to work from; treat it as a first draft that is factually right and
stylistically wrong.

BRAND VOICE: {voice}
{hinglish_note}
{language_rubric(lang)}

{glossary_rules(lang)}

ANSWER-ENGINE STRUCTURE -- this is what the page is for:
  - Rewrite headings as questions a real person would type or ask aloud.
  - For every question heading, write a {aeo_cfg['answer_words_min']}-{aeo_cfg['answer_words_max']} word DIRECT answer that stands
    alone if quoted with no surrounding context. Put it in `answers`, keyed by
    the exact heading text you used in `blocks`.
  - Write a {aeo_cfg['tldr_bullets']}-bullet TL;DR. This is the passage an answer engine lifts
    first. Each bullet must make sense on its own.
  - Write {aeo_cfg['faq_min']}-{aeo_cfg['faq_max']} FAQs from real questions, not invented ones.
  - Name entities explicitly instead of using pronouns. "{brand} ke doctors"
    survives into a cited answer; "our doctors" does not.
  - Title <= {aeo_cfg['meta_title_max']} chars, meta description 50-{aeo_cfg['meta_desc_max']} chars, both written
    natively rather than translated.

KEYWORDS to place naturally where they already fit. Do not force them, and do
not repeat any of them more than twice:
  {kw or '(none supplied -- use your own judgement about how a reader would search)'}

=== ENGLISH SOURCE ===
{source_md}

=== RAW MACHINE TRANSLATION (first draft -- fix the style, keep the facts) ===
{mt_md}

Reply with JSON only, matching this shape:
{json.dumps(TRANSCREATE_SCHEMA, ensure_ascii=False, indent=2)}"""


def review_prompt(*, lang: str, text: str) -> str:
    """Deliberately given only the target text -- no source, no translator notes.

    A model shown its own reasoning approves its own output. A model shown only
    the result and the rubric does not.
    """
    entry = lang_by_code(lang)
    return f"""You are a native {entry['name']} editor. You have not seen where this
text came from and you should not speculate about it. Judge only what is here.

Question: does this read as though a {entry['name']} writer wrote it, or as
though it was converted into {entry['name']} from another language?

{language_rubric(lang)}

Score 0-100, where 100 means a native reader would never suspect translation and
0 means it is obviously machine output. Be strict: 75 is "acceptable but you can
tell", 90+ is "genuinely native".

Flag specific phrases, not general impressions. Every flag must name the actual
offending text and say what to write instead.

=== TEXT ===
{text}

Reply with JSON only, matching this shape:
{json.dumps(REVIEW_SCHEMA, ensure_ascii=False, indent=2)}"""


def rewrite_prompt(*, lang: str, text_md: str, brief: list[str],
                   profile: dict, ai_pct: float, target: float) -> str:
    entry = lang_by_code(lang)
    issues = "\n".join(f"  - {b}" for b in brief)
    return f"""Revise this {entry['name']} article so it reads as native writing.

An automated check scored it at {ai_pct:.0f}% machine-likeness. The target is
{target:.0f}% or below. These are the specific defects it found -- fix these,
not your general impression of the text:

{issues}

{language_rubric(lang)}

{glossary_rules(lang)}

HARD CONSTRAINTS:
  - Keep every fact, number, dosage, name and URL exactly as it appears now.
  - Keep the heading structure and the block order. Rewrite the prose inside
    them; do not reorganise the document.
  - Do not make any medical claim stronger than it currently is.
  - Do not shorten the article. Fixing translationese means restructuring
    sentences, not deleting them.

BRAND VOICE: {profile.get('voice', 'clear, plain, conversational')}

=== CURRENT TEXT ===
{text_md}

Reply with JSON only:
{{
  "title": "string",
  "meta_description": "string",
  "blocks": [{{"type": "h2|h3|p|li|quote|tldr|answer", "text": "string"}}],
  "faqs": [{{"q": "string", "a": "string"}}],
  "notes": "string -- what you changed and anything you could not fix"
}}"""


@dataclass
class Writer:
    """Interface every backend implements."""
    name: str = "base"

    def transcreate(self, **kwargs) -> dict:
        raise NotImplementedError

    def review(self, **kwargs) -> dict:
        raise NotImplementedError

    def rewrite(self, **kwargs) -> dict:
        raise NotImplementedError
