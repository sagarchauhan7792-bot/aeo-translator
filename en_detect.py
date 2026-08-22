"""English AI-likeness: calibrated the same way as quality.py's Hindi gate --
real native fixtures, real AI fixtures, only what measurably separates them.

Measured (calibrate_en.py, BBC News n=8 vs raw Gemini output n=8, two rounds):

    signal                    native    ai      separation
    paragraph-length CV        0.199   0.021       0.90   KEPT
    stock AI phrases /1k       0.000   1.686      -1.00   KEPT
    colon-led list scaffold    0.000   1.700      -1.00   KEPT
    passive-voice ratio        0.273   0.152       0.44   KEPT (reversed sign)
    em-dash density             1.83    1.66       0.10   DROPPED, see below
    sentence-length CV          n/a     n/a        0.08   DROPPED, no signal
    vocabulary diversity        n/a     n/a        0.13   DROPPED, no signal
    repeated openings           n/a     n/a        0.00   DROPPED, no signal
    filler-phrase density       n/a     n/a        0.00   DROPPED, no signal

Two findings worth stating plainly rather than quietly baking in:

  Em-dash overuse is common folk wisdom about LLM writing, and it looked real
  on the first calibration round (sep -0.62) -- but that round used
  single-paragraph AI fixtures against multi-paragraph BBC articles, which is
  a confound, not a finding. Re-run with AI fixtures explicitly written in
  matching multi-paragraph form, the signal collapsed to 0.10 -- no signal.
  Gemini (the model this pipeline actually writes with) does not show the
  em-dash tell that folk wisdom attributes to LLMs generally. Scoring it would
  have penalised normal Gemini output for a habit it does not have.

  Native writers use MORE passive voice than Gemini's output here, not less --
  the opposite of the common "AI overuses passive voice" claim. Confirmed
  across both calibration rounds (0.60, then 0.44). The sign below is set to
  what was actually measured.

n=8/8 is a smaller sample than the Hindi calibration (6/6/4) and the composite
below has not been AUC-tested as rigorously as that one. Treat this as a
reasonable first gate, not a settled instrument -- re-run calibrate_en.py with
more fixtures before leaning on it for anything high-stakes.

This is NOT a claim of parity with QuillBot, GPTZero, Originality.ai or any
other commercial detector. None of those has a usable free API (checked: no
public API exists for QuillBot; GPTZero's API needs a paid plan). This module
measures what is actually measurable for free, states what it measured, and
never reports a number for a check that was not run.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

import en_features as EF

WEIGHTS = {"para_cv": 0.30, "tell_1k": 0.30, "colon_lists_1k": 0.25, "passive_ratio": 0.15}

# Bands taken directly from the measured native/ai means above.
BANDS = {
    "para_cv":        {"good": 0.18, "bad": 0.02},
    "tell_1k":        {"good": 0.00, "bad": 5.00, "invert": True},
    "colon_lists_1k": {"good": 0.00, "bad": 5.00, "invert": True},
    "passive_ratio":  {"good": 0.28, "bad": 0.04},
}

PARAM_LABEL = {
    "para_cv": "Paragraph rhythm varies (not uniform blocks)",
    "tell_1k": "No stock AI phrasing (moreover / furthermore / delve into / ...)",
    "colon_lists_1k": "Not built from colon-led list scaffolding",
    "passive_ratio": "Natural passive/active mix",
}

TARGET = 70.0            # each named parameter must individually clear this


def _norm(value: float, good: float, bad: float, *, invert: bool = False) -> float:
    if invert:
        value, good, bad = -value, -good, -bad
    if good == bad:
        return 100.0
    frac = (value - bad) / (good - bad)
    return max(0.0, min(1.0, frac)) * 100.0


@dataclass
class Parameter:
    key: str
    label: str
    value: float
    score: float
    passed: bool

    def dict(self) -> dict:
        return asdict(self)


@dataclass
class EnDetectReport:
    score: float                     # composite human-likeness, 0-100
    ai_likeness: float                # 100 - score
    parameters: list
    all_passed: bool
    n_words: int
    method_note: str = (
        "Measured signals, calibrated against real BBC News text vs real "
        "Gemini output (n=8/8) -- not a QuillBot or GPTZero result. Neither "
        "has a usable free API; this is the honest free alternative, not a "
        "claim of matching them.")

    def dict(self) -> dict:
        return asdict(self)


def score(text: str) -> EnDetectReport:
    f = EF.extract(text)
    params = []
    for key, band in BANDS.items():
        val = f[key]
        s = _norm(val, band["good"], band["bad"], invert=band.get("invert", False))
        params.append(Parameter(key=key, label=PARAM_LABEL[key], value=val,
                                score=round(s, 1), passed=s >= TARGET))

    composite = sum(p.score * WEIGHTS[p.key] for p in params)
    return EnDetectReport(
        score=round(composite, 1),
        ai_likeness=round(100 - composite, 1),
        parameters=[p.dict() for p in params],
        all_passed=all(p.passed for p in params),
        n_words=f["words"],
    )
