"""Scoring for English drafts -- deliberately NOT the Indic gate.

quality.py reports `AI-likeness % (proxy)`, and that number is only meaningful
because calibrate.py measured it against real Hindi: native journalism, human
translation and raw MT, separating at AUC 1.00. Three of its six sub-scores
measure Indic-specific phenomena and are undefined on English:

  translationese  prepositional calques, जो/जिसे relative clauses, Devanagari
                  comma habits -- none of which exist in an English draft
  register        aap/tum honorific consistency -- English has no such axis
  grammar         script purity against a Unicode block -- trivially 100 for
                  Latin text, so it measures nothing

Running the composite on English would produce a number that looks like the
Hindi one, sits in the same column, and means something entirely different.
This module therefore scores what genuinely transfers and gives the result a
different name -- **AEO + rhythm** -- so the two are never read as comparable.

What does transfer:
  aeo         structure is language-independent
  burstiness  sentence-length variation is a real signal in any language
  review      an independent reader pass
  safety      the term-lock and medical-claim guards

A commercial detector WOULD be valid on English, unlike on Indic. None is wired
in because none is paid for. If one is added later it belongs here, reported as
its own number next to this one -- not folded into it.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

import quality
from extract import Article

# Reweighted over the sub-scores that mean something in English.
WEIGHTS = {"aeo": 0.40, "burstiness": 0.35, "review": 0.25}

MIN_SCORE = 70          # below this the draft goes back for another pass


@dataclass
class EnglishReport:
    score: float                 # "AEO + rhythm", 0-100
    aeo: float
    burstiness: float
    review: float
    words: int
    flags: list
    blocking: list
    passed: bool
    label: str = "AEO + rhythm"
    caveat: str = ("Not the Indic AI-likeness proxy. Three of that gate's six "
                   "sub-scores are undefined on English, and its calibration was "
                   "measured on Hindi. The two numbers are not comparable.")

    def dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        return (f"english   {self.label} {self.score:5.1f} | AEO {self.aeo:5.1f} | "
                f"rhythm {self.burstiness:5.1f} | review {self.review:5.1f} | "
                f"{self.words}w | {'PASS' if self.passed else 'FAIL'}")


def score_draft(art: Article, *, review: dict | None = None,
                source: Article | None = None) -> EnglishReport:
    """Score an English draft. `source` enables the safety guards when present."""
    aeo_sub = quality.score_aeo(art, "en")
    burst_sub = quality.score_burstiness(art, "en")
    review_score = float(review.get("score", 75.0)) if review else 75.0

    flags = [f.dict() for f in aeo_sub.flags] + [f.dict() for f in burst_sub.flags]
    if review:
        flags += [{"kind": f.get("kind", "review"), "severity": f.get("severity", "warn"),
                   "detail": f.get("detail", ""), "sample": f.get("sample", "")}
                  for f in review.get("flags", [])]
    else:
        flags.append({"kind": "review", "severity": "note",
                      "detail": "no reviewer pass yet; neutral placeholder used",
                      "sample": ""})

    # Safety rails apply to a draft exactly as they do to a translation: an
    # invented dosage is no safer for having been written rather than translated.
    if source is not None:
        guard = quality._term_lock_flags(source.full_text(), art.full_text())
        flags += [f.dict() for f in guard]

    score = (WEIGHTS["aeo"] * aeo_sub.score
             + WEIGHTS["burstiness"] * burst_sub.score
             + WEIGHTS["review"] * review_score)

    blocking = [f for f in flags
                if f["severity"] == "error"
                and f["kind"] in ("locked_term", "medical_claim", "hedge_lost",
                                  "protected_span", "empty_block")]

    return EnglishReport(
        score=round(score, 1),
        aeo=round(aeo_sub.score, 1),
        burstiness=round(burst_sub.score, 1),
        review=round(review_score, 1),
        words=art.words(),
        flags=flags,
        blocking=blocking,
        passed=score >= MIN_SCORE and not blocking,
    )


def rewrite_brief(report: EnglishReport, limit: int = 12) -> list[str]:
    """Flags as specific instructions, same shape as quality.rewrite_brief."""
    order = {"error": 0, "warn": 1, "note": 2}
    ranked = sorted(report.flags, key=lambda f: order.get(f["severity"], 3))
    out = []
    for f in ranked[:limit]:
        line = f"[{f['severity']}] {f['kind']}: {f['detail']}"
        if f.get("sample"):
            line += f"  (e.g. {f['sample'][:120]})"
        out.append(line)
    return out


def claim_audit(art: Article) -> list[dict]:
    """Every number in a drafted post, for a human to check against a source.

    A draft has no source document, so there is nothing to diff numbers against
    -- the fidelity check that protects a translation cannot protect a draft.
    Every figure is therefore surfaced for review rather than silently trusted.
    """
    import re
    from patterns import find_protected

    found = find_protected(art.full_text())
    out = []
    for kind, values in found.items():
        for v in values:
            sentence = ""
            for s in quality.sentences(art.full_text()):
                if v in s:
                    sentence = s.strip()[:180]
                    break
            out.append({"kind": kind, "value": v, "context": sentence,
                        "verified": False})
    return out
