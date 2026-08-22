"""Raw feature extraction, kept separate from scoring so it can be inspected.

quality.py turns these numbers into scores. This module only measures. Run
`python features.py` to dump every feature across the calibration classes --
that table is how the normalisation bands in quality.py were chosen, and it is
how they should be re-chosen if the language set or the MT engine changes.
"""
from __future__ import annotations

import re
import statistics
from pathlib import Path

import linguistics as L
from common import word_count

SENT_SPLIT = re.compile(r"(?<=[.!?।॥])\s+")


def sentences(text: str) -> list[str]:
    return [s.strip() for s in SENT_SPLIT.split(text or "") if s.strip()]


def script_ratio(text: str, script: str) -> float:
    rng = L.SCRIPT_RANGES.get(script)
    if not rng:
        return 1.0
    lo, hi = rng
    letters = [c for c in (text or "") if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if lo <= ord(c) <= hi) / len(letters)


def extract(text: str, lang: str = "hi", script: str = "Devanagari") -> dict:
    """Every measurable signal, normalised per-1000-words where it is a count."""
    text = (text or "").strip()
    words = max(1, word_count(text))
    per_1k = lambda n: round(n / (words / 1000), 2)          # noqa: E731
    sents = sentences(text)
    low = text.lower()

    # --- script and mechanics ---------------------------------------------
    purity = script_ratio(text, script)
    tokens = re.findall(r"[^\s]+", text)
    latin = sum(1 for w in tokens if re.fullmatch(r"[A-Za-z][A-Za-z'.-]*", w)) / max(1, len(tokens))

    space_before_punct = len(re.findall(r"\s+[,.।;:!?]", text))
    spaced_hyphen = len(re.findall(r"\s-\s", text))
    double_space = len(re.findall(r"  +", text))
    no_space_after = len(re.findall(r"[,;:][^\s\d]", text))
    matra_run = len(re.findall(r"[ा-ौ]{3,}", text))
    halant_space = len(re.findall(r"्\s", text))

    # --- punctuation convention (informational, NOT scored) ---------------
    prose_ends = [s.strip()[-1] for s in sents if s.strip()]
    danda_frac = (sum(1 for c in prose_ends if c in "।॥") / len(prose_ends)) if prose_ends else 0.0

    # --- word order --------------------------------------------------------
    verbs = L.get(L.VERB_ENDINGS, lang, ())
    vf, counted = 0, 0
    for s in sents:
        toks = re.findall(r"[^\s]+", s.rstrip(" .।॥!?"))
        if len(toks) < 4:
            continue
        counted += 1
        last = toks[-1].strip(" ,.।॥!?:;\"'()")
        if any(last.endswith(v) for v in verbs):
            vf += 1
    verb_final = vf / counted if counted else 1.0

    # --- lexical tells -----------------------------------------------------
    calques = sum(low.count(c.lower()) for c in L.get(L.CALQUES, lang, ()))
    connectives = sum(low.count(c.lower()) for c in L.get(L.AI_CONNECTIVES, lang, ()))
    formal = sum(low.count(w.lower()) for w in L.get(L.FORMAL_MARKERS, lang, ()))

    # Calqued English relative clauses: "X, जो ... है" mirrors "X, which is ...".
    # Native Hindi prefers a participial construction and uses these far less.
    rel_clause = len(re.findall(r"\bजो\b", text)) + len(re.findall(r"\bजिसे\b", text)) \
        + len(re.findall(r"\bजिसमें\b", text)) + len(re.findall(r"\bजिससे\b", text))
    # "के रूप में" / "के द्वारा" -- literal renderings of "as" and "by".
    prep_calque = low.count("के रूप में") + low.count("के द्वारा") + low.count("के माध्यम से")
    commas = text.count(",") + text.count("،")

    # --- variation ---------------------------------------------------------
    lens = [word_count(s) for s in sents if word_count(s) > 1]
    mean_len = statistics.mean(lens) if lens else 0.0
    sent_cv = (statistics.pstdev(lens) / mean_len) if len(lens) > 2 and mean_len else 0.0

    paras = [word_count(p) for p in text.split("\n") if word_count(p) > 3]
    para_cv = (statistics.pstdev(paras) / statistics.mean(paras)) \
        if len(paras) > 3 and statistics.mean(paras) else 0.0

    openers = [" ".join(re.findall(r"[^\s]+", s)[:2]).lower() for s in sents if len(s.split()) > 3]
    counts: dict[str, int] = {}
    for o in openers:
        counts[o] = counts.get(o, 0) + 1
    repeated_open = (sum(v for v in counts.values() if v >= 3) / len(openers)) if openers else 0.0

    toks_low = re.findall(r"[^\s]+", low)
    ttr = len(set(toks_low)) / len(toks_low) if toks_low else 0.0

    return {
        "words": words,
        "n_sents": len(sents),
        "purity": round(purity, 3),
        "latin": round(latin, 3),
        "danda_frac": round(danda_frac, 3),
        "mech_1k": per_1k(space_before_punct + double_space + no_space_after
                          + matra_run + halant_space),
        "sp_punct_1k": per_1k(space_before_punct),
        "hyphen_1k": per_1k(spaced_hyphen),
        "verb_final": round(verb_final, 3),
        "calque_1k": per_1k(calques),
        "conn_1k": per_1k(connectives),
        "formal_1k": per_1k(formal),
        "rel_1k": per_1k(rel_clause),
        "prep_calque_1k": per_1k(prep_calque),
        "comma_1k": per_1k(commas),
        "mean_len": round(mean_len, 1),
        "sent_cv": round(sent_cv, 3),
        "para_cv": round(para_cv, 3),
        "rep_open": round(repeated_open, 3),
        "ttr": round(ttr, 3),
    }


def _dump() -> None:
    """Print every feature by class, with per-class means, for band setting."""
    root = Path(__file__).resolve().parent / "samples"
    classes = ["native", "translated", "mt"]
    keys = ["words", "purity", "latin", "danda_frac", "mech_1k", "sp_punct_1k",
            "hyphen_1k", "verb_final", "calque_1k", "conn_1k", "formal_1k",
            "rel_1k", "prep_calque_1k", "comma_1k", "mean_len", "sent_cv",
            "para_cv", "rep_open", "ttr"]
    means: dict[str, dict[str, float]] = {}

    for cls in classes:
        d = root / cls
        files = sorted(d.glob("*.txt")) if d.exists() else []
        if not files:
            continue
        rows = []
        for f in files:
            body = f.read_text(encoding="utf-8")
            body = "\n".join(l for l in body.split("\n") if not l.startswith("#"))
            rows.append(extract(body))
        print(f"\n=== {cls.upper()} (n={len(rows)}) ===")
        print("  " + "".join(k.rjust(13) for k in keys))
        for f, r in zip(files, rows):
            print("  " + "".join(str(r[k]).rjust(13) for k in keys))
        means[cls] = {k: statistics.mean([r[k] for r in rows]) for k in keys}
        print("  " + "".join(f"{means[cls][k]:13.3f}" for k in keys) + "   <- MEAN")

    if len(means) >= 2 and "native" in means:
        print("\n=== DISCRIMINATION (native mean vs each negative) ===")
        print(f"  {'feature':16}{'native':>10}{'translated':>12}{'mt':>10}"
              f"{'sep_tr':>10}{'sep_mt':>10}")
        for k in keys:
            n = means["native"][k]
            t = means.get("translated", {}).get(k, float("nan"))
            m = means.get("mt", {}).get(k, float("nan"))
            scale = max(abs(n), 1e-6)
            print(f"  {k:16}{n:10.3f}{t:12.3f}{m:10.3f}"
                  f"{(n - t) / scale:10.2f}{(n - m) / scale:10.2f}")
        print("\n  sep_* is the relative gap. |value| > 0.25 means the feature")
        print("  carries real signal for that class; near 0 means it does not.")


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    _dump()
