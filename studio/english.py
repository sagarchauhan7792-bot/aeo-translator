"""Scoring for English drafts -- structure AND a real, calibrated AI-likeness gate.

quality.py reports `AI-likeness % (proxy)`, and that number is only meaningful
because calibrate.py measured it against real Hindi: native journalism, human
translation and raw MT, separating at AUC 1.00. Three of its six sub-scores
measure Indic-specific phenomena and are undefined on English (translationese,
register, script purity), so that composite is never run here.

This module carries two SEPARATE gates instead of pretending one number covers
both:

  structure     aeo + burstiness + independent review -- language-independent
                and safety-checked, as before.
  ai_likeness   en_detect.score() -- calibrated the same way as the Hindi gate,
                against real BBC News text vs real raw Gemini output (n=8/8,
                AUC 1.00, Cohen's d 2.79 on the composite; see en_detect.py for
                the full per-signal breakdown, including two folk-wisdom
                assumptions that were tested and found false for this model).

Neither is QuillBot, GPTZero, Originality.ai or any other commercial detector.
Checked directly: QuillBot has no public API at all, and GPTZero's API needs a
paid plan. There is no free way to call either from code. This module reports
what it actually measured, under its own name, and never borrows a vendor's.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

import en_detect
import quality
from extract import Article

# Reweighted over the sub-scores that mean something in English.
WEIGHTS = {"aeo": 0.40, "burstiness": 0.35, "review": 0.25}

MIN_SCORE = 70          # structure gate: below this the draft goes back
AI_REWRITE_TRIGGER = 20  # reused by convention from the Hindi gate's thresholds
AI_TARGET = 10           # (config.json -> thresholds); not independently derived
                         # for English -- there was no operator-specified number
                         # for this gate, so the existing convention is kept
                         # rather than inventing a different one.


@dataclass
class EnglishReport:
    score: float                 # structure score, 0-100
    aeo: float
    burstiness: float
    review: float
    ai_likeness: float            # en_detect composite AI-likeness %, 0-100
    ai_parameters: list           # named pass/fail, one per calibrated signal
    ai_all_passed: bool
    words: int
    flags: list
    blocking: list
    passed: bool
    label: str = "structure"
    ai_label: str = "AI-likeness (calibrated, English-only)"
    caveat: str = ("structure is not the Indic AI-likeness proxy -- three of "
                   "that gate's six sub-scores are undefined on English. "
                   "ai_likeness is a separate, English-specific calibration "
                   "(AUC 1.00 on its own test set), not a QuillBot/GPTZero result.")

    def dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        return (f"english   structure {self.score:5.1f} | AI-likeness "
                f"{self.ai_likeness:5.1f}% | AEO {self.aeo:5.1f} | "
                f"rhythm {self.burstiness:5.1f} | review {self.review:5.1f} | "
                f"{self.words}w | {'PASS' if self.passed else 'FAIL'}")


def score_draft(art: Article, *, review: dict | None = None,
                source: Article | None = None) -> EnglishReport:
    """Score an English draft. `source` enables the safety guards when present."""
    aeo_sub = quality.score_aeo(art, "en")
    burst_sub = quality.score_burstiness(art, "en")
    review_score = float(review.get("score", 75.0)) if review else 75.0
    ai = en_detect.score(art.full_text())

    flags = [f.dict() for f in aeo_sub.flags] + [f.dict() for f in burst_sub.flags]
    if review:
        flags += [{"kind": f.get("kind", "review"), "severity": f.get("severity", "warn"),
                   "detail": f.get("detail", ""), "sample": f.get("sample", "")}
                  for f in review.get("flags", [])]
    else:
        flags.append({"kind": "review", "severity": "note",
                      "detail": "no reviewer pass yet; neutral placeholder used",
                      "sample": ""})

    for p in ai.parameters:
        if not p["passed"]:
            flags.append({"kind": f"ai_{p['key']}", "severity": "warn",
                          "detail": f"{p['label']} -- scored {p['score']:.0f}/100",
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
        ai_likeness=ai.ai_likeness,
        ai_parameters=ai.parameters,
        ai_all_passed=ai.all_passed,
        words=art.words(),
        flags=flags,
        blocking=blocking,
        passed=(score >= MIN_SCORE and ai.ai_likeness <= AI_REWRITE_TRIGGER
                and not blocking),
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


def keyword_brief(art: Article, keywords: list[str], limit: int = 6) -> list[str]:
    """Real, measured density gaps -- not just 'use these keywords' in a prompt.

    seo.py already has the density checker used by the audit; reusing it here
    means the rewrite loop is graded by the exact same yardstick the finished
    post will be audited against, instead of the writer's own unverified claim
    that it "worked the keywords in".
    """
    from . import seo
    body = art.body_text()
    out = []
    for kw in keywords[:limit]:
        density, hits = seo.keyword_density(body, kw.lower())
        if hits == 0:
            out.append(f"[warn] keyword: '{kw}' does not appear anywhere in the body.")
        elif density < 0.15:
            out.append(f"[warn] keyword: '{kw}' appears only {hits}x "
                       f"({density}% density) -- too sparse to help this post rank for it.")
        elif density > 2.5:
            out.append(f"[warn] keyword: '{kw}' appears {hits}x ({density}% density) "
                       "-- over-optimised, reads as stuffed.")
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
