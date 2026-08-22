"""Competitor comparison: what they cover that you don't.

Deliberately outputs a gap list, not a score. "You: 62, them: 71" tells you
nothing you can act on. "They answer 'how long does it take to work' and you
don't" is a sentence you can write a section from.

No credentials. Everything is read from the pages themselves.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict

import quality
from common import log, warn, word_count
from extract import Article, is_question
from . import seo, geo
import sources

STOP = seo.STOP


def _topic_tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(w) > 3 and w not in STOP}


def _similar(a: str, b: str, threshold: float = 0.55) -> bool:
    ta, tb = _topic_tokens(a), _topic_tokens(b)
    if not ta or not tb:
        return False
    shared = len(ta & tb)
    return shared and (2 * shared / (len(ta) + len(tb))) >= threshold


def profile_page(art: Article) -> dict:
    """The comparable facts about one page."""
    text = art.full_text()
    fl, band = seo.flesch(art.body_text())
    heads = art.headings()
    return {
        "title": art.title,
        "url": art.source_url,
        "words": art.words(),
        "headings": heads,
        "questions": [h for h in heads if is_question(h)],
        "faqs": len(art.faqs),
        "images": len(art.images),
        "links_internal": sum(1 for l in art.links if l["href"].startswith("/")),
        "links_external": sum(1 for l in art.links if l["href"].startswith("http")),
        "flesch": fl, "readability": band,
        "entities": geo.extract_entities(art),
        "schema_types": sorted({s.get("@type") for s in
                                (art.meta.get("schema", {}).get("@graph") or [])
                                if isinstance(s, dict) and s.get("@type")}),
        "author": art.author, "published": art.published,
        "stats": len(geo.STAT.findall(text)),
        "has_tldr": any(b.type == "tldr" for b in art.blocks),
        "answer_blocks": sum(1 for b in art.blocks if b.type == "answer"),
    }


def compare(mine: Article, others: list[Article]) -> dict:
    """Diff one page against up to a few competitors."""
    me = profile_page(mine)
    them = [profile_page(o) for o in others]
    if not them:
        return {"mine": me, "competitors": [], "gaps": [], "wins": [], "summary": {}}

    # --- topic gaps: their headings with no counterpart of mine -----------
    gaps: list[dict] = []
    for t in them:
        for h in t["headings"]:
            if len(h) < 8 or any(_similar(h, mh) for mh in me["headings"]):
                continue
            if any(_similar(h, g["heading"]) for g in gaps):
                for g in gaps:
                    if _similar(h, g["heading"]) and t["url"] not in g["sources"]:
                        g["sources"].append(t["url"])
                continue
            gaps.append({"heading": h, "sources": [t["url"]],
                         "is_question": is_question(h)})
    gaps.sort(key=lambda g: (-len(g["sources"]), not g["is_question"]))

    # --- entity gaps -------------------------------------------------------
    my_ents = {e.lower() for e in me["entities"]}
    ent_gaps: dict[str, list[str]] = {}
    for t in them:
        for e in t["entities"]:
            if e.lower() not in my_ents:
                ent_gaps.setdefault(e, []).append(t["url"])

    # --- numeric comparisons ----------------------------------------------
    def spread(key):
        vals = [t[key] for t in them]
        return {"mine": me[key], "best": max(vals), "median": sorted(vals)[len(vals) // 2],
                "behind": me[key] < max(vals)}

    summary = {k: spread(k) for k in
               ("words", "faqs", "images", "links_external", "answer_blocks", "stats")}
    summary["questions"] = {"mine": len(me["questions"]),
                            "best": max(len(t["questions"]) for t in them),
                            "median": sorted(len(t["questions"]) for t in them)[len(them) // 2],
                            "behind": len(me["questions"]) < max(len(t["questions"]) for t in them)}

    wins = []
    if not any(t["has_tldr"] for t in them) and me["has_tldr"]:
        wins.append("You have a TL;DR block and none of them do.")
    if me["answer_blocks"] and not any(t["answer_blocks"] for t in them):
        wins.append("You have standalone answer blocks and none of them do.")
    if me["schema_types"] and not any(t["schema_types"] for t in them):
        wins.append("You have structured data and none of them do.")
    if me["words"] >= max(t["words"] for t in them):
        wins.append(f"Your page is the longest ({me['words']} words).")

    return {
        "mine": me,
        "competitors": them,
        "gaps": gaps[:25],
        "entity_gaps": [{"entity": e, "sources": u} for e, u in
                        sorted(ent_gaps.items(), key=lambda kv: -len(kv[1]))][:15],
        "wins": wins,
        "summary": summary,
    }


def fetch_all(urls: list[str], pause: float = 1.0) -> list[Article]:
    import time
    out = []
    for i, u in enumerate(urls):
        try:
            out.append(sources.load_url(u))
        except Exception as exc:
            warn(f"could not fetch competitor {u}: {exc.__class__.__name__}")
        if i < len(urls) - 1:
            time.sleep(pause)
    return out
