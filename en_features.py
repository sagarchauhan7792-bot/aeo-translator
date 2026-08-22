"""Raw, measured signals for English AI-likeness. Same discipline as features.py:
nothing here is trusted until calibrate_en.py shows it actually separates real
native writing from real AI output. Every constant below is a hypothesis.
"""
from __future__ import annotations

import re
import statistics

from common import word_count

SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")

# Reused, not redefined: these are the exact lists already used elsewhere in
# the tool (seo.py's on-page checks, geo.py's citation scoring), so a post that
# passes calibration here is consistent with what the rest of the app flags.
FILLER = [
    "it is important to note", "it should be noted", "in today's fast-paced",
    "in this day and age", "when it comes to", "at the end of the day",
    "needless to say", "it goes without saying", "the fact of the matter",
    "in order to", "due to the fact that", "a wide range of", "plays a vital role",
    "plays a crucial role", "in conclusion", "last but not least", "first and foremost",
]
AI_TELLS = [
    "moreover", "furthermore", "additionally", "in conclusion",
    "it is important to note", "delve into", "navigate the", "tapestry",
    "in the realm of", "unlock the", "harness the power", "in summary",
    "on the other hand", "as a result", "underscore", "testament to",
    "boasts a", "elevate your", "seamless", "robust", "cutting-edge",
]

PASSIVE = re.compile(
    r"\b(?:is|are|was|were|be|been|being|get|gets|got)\s+(?:\w+ly\s+)?(\w+(?:ed|en|wn|ne))\b", re.I)


def sentences(text: str) -> list[str]:
    return [s.strip() for s in SENT_SPLIT.split(text or "") if s.strip()]


def extract(text: str) -> dict:
    text = (text or "").strip()
    words = max(1, word_count(text))
    per_1k = lambda n: round(n / (words / 1000), 2)          # noqa: E731
    sents = sentences(text)
    low = text.lower()

    # --- rhythm --------------------------------------------------------
    lens = [word_count(s) for s in sents if word_count(s) > 1]
    mean_len = statistics.mean(lens) if lens else 0.0
    sent_cv = (statistics.pstdev(lens) / mean_len) if len(lens) > 2 and mean_len else 0.0

    paras = [word_count(p) for p in text.split("\n") if word_count(p) > 3]
    para_cv = (statistics.pstdev(paras) / statistics.mean(paras)) \
        if len(paras) > 3 and statistics.mean(paras) else 0.0

    # --- repeated openings and templated sentences ----------------------
    openers = [" ".join(re.findall(r"[^\s]+", s)[:2]).lower() for s in sents if len(s.split()) > 3]
    counts: dict[str, int] = {}
    for o in openers:
        counts[o] = counts.get(o, 0) + 1
    rep_open = (sum(v for v in counts.values() if v >= 3) / len(openers)) if openers else 0.0

    # --- lexical tells ---------------------------------------------------
    filler_hits = sum(low.count(p) for p in FILLER)
    tell_hits = sum(low.count(t) for t in AI_TELLS)

    # Em dashes: a well-documented overuse tell in recent LLM output --
    # tested here rather than assumed, same as every other signal.
    em_dash = text.count("—") + text.count(" - ")
    em_dash_1k = per_1k(em_dash)

    # --- mechanics ---------------------------------------------------------
    passive = len(PASSIVE.findall(text))
    passive_ratio = passive / len(sents) if sents else 0.0

    toks = re.findall(r"[^\s]+", low)
    ttr = len(set(toks)) / len(toks) if toks else 0.0

    # Colon-led list-style sentences ("Here's why: X, Y, Z") -- common
    # generated-content scaffolding.
    colon_lists = len(re.findall(r":\s*\w+.*?,\s*\w+.*?,\s*(?:and\s+)?\w+", text))

    return {
        "words": words,
        "n_sents": len(sents),
        "mean_len": round(mean_len, 1),
        "sent_cv": round(sent_cv, 3),
        "para_cv": round(para_cv, 3),
        "rep_open": round(rep_open, 3),
        "filler_1k": per_1k(filler_hits),
        "tell_1k": per_1k(tell_hits),
        "em_dash_1k": em_dash_1k,
        "passive_ratio": round(passive_ratio, 3),
        "ttr": round(ttr, 3),
        "colon_lists_1k": per_1k(colon_lists),
    }
