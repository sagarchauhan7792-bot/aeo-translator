"""Quality scoring: six sub-scores -> Human-Likeness Score -> AI-likeness %.

Read this before trusting any number it produces.

No AI detector is validated on Hindi, Marathi, Gujarati, Bengali, Tamil, Telugu,
Kannada or Punjabi. GPTZero and Originality.ai are English-first; their Indic
output is unaudited. Reporting one of those numbers on Devanagari text would be
reporting noise with a decimal point on it.

So this module does not claim to be an AI detector. It measures six things that
are real, observable and specific to Indic text, and reports the inverse of the
composite as "AI-likeness % (proxy)". Everywhere that number surfaces -- the
sheet, the report, the Doc -- it carries the word proxy.

The thresholds the operator set (rewrite above 30, target 10, hard-fail 20) are
applied to this proxy exactly as specified.

Calibrate before trusting: see calibrate.py.
"""
from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass, field, asdict

import features as FE
import linguistics as L
from common import config, glossary, word_count
from extract import Article, is_question
from patterns import diff_numbers, diff_protected, normalise_digits

CFG = config()
# Config dicts carry "_comment" keys for the humans who edit them; those are
# documentation, not weights.
W = {k: v for k, v in CFG["score_weights"].items() if not k.startswith("_")}
TH = CFG["thresholds"]

_wsum = sum(W.values())
if abs(_wsum - 1.0) > 0.001:
    raise ValueError(f"score_weights must sum to 1.0, got {_wsum:.3f} -- "
                     "fix config.json and re-run calibrate.py")

SENT_SPLIT = re.compile(r"(?<=[.!?।॥])\s+")

EN_STOP = {
    "the", "a", "an", "and", "or", "but", "if", "of", "to", "in", "on", "for",
    "with", "as", "is", "are", "was", "were", "be", "been", "being", "it", "its",
    "this", "that", "these", "those", "at", "by", "from", "can", "may", "will",
    "would", "should", "could", "has", "have", "had", "do", "does", "did", "not",
    "you", "your", "we", "our", "they", "their", "he", "she", "his", "her",
}


# ------------------------------------------------------------------- helpers

def sentences(text: str) -> list[str]:
    return [s.strip() for s in SENT_SPLIT.split(text or "") if s.strip()]


def _norm(value: float, good: float, bad: float) -> float:
    """Map a raw measurement onto 0-100, where `good` scores 100 and `bad` 0."""
    if good == bad:
        return 100.0
    frac = (value - bad) / (good - bad)
    return max(0.0, min(1.0, frac)) * 100.0


def script_ratio(text: str, script: str) -> float:
    """Share of letters that sit in the expected Unicode block."""
    rng = L.SCRIPT_RANGES.get(script)
    if not rng:
        return 1.0
    lo, hi = rng
    letters = [c for c in (text or "") if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if lo <= ord(c) <= hi) / len(letters)


def latin_word_ratio(text: str) -> float:
    words = re.findall(r"[^\s]+", text or "")
    if not words:
        return 0.0
    latin = [w for w in words if re.fullmatch(r"[A-Za-z][A-Za-z'.-]*", w)]
    return len(latin) / len(words)


@dataclass
class Flag:
    kind: str
    severity: str          # error | warn | note
    detail: str
    sample: str = ""

    def dict(self) -> dict:
        return asdict(self)


@dataclass
class SubScore:
    name: str
    score: float
    flags: list[Flag] = field(default_factory=list)
    detail: dict = field(default_factory=dict)

    def dict(self) -> dict:
        return {"name": self.name, "score": round(self.score, 1),
                "flags": [f.dict() for f in self.flags], "detail": self.detail}


# --------------------------------------------------------------- 1. fidelity

def score_fidelity(source: Article, back: Article | None,
                   translated: Article | None = None) -> SubScore:
    """Did the facts survive the round trip, and did the locked terms survive at all?

    Back-translation is the strongest signal available without a human reader:
    a fact that comes home intact was genuinely carried across.

    The term-lock and medical-claim checks run against the TRANSLATED text
    directly rather than the round trip, because a brand name that got helpfully
    translated would often come back looking correct in English.
    """
    flags: list[Flag] = []
    src_text = source.full_text()

    if translated is not None:
        flags.extend(_term_lock_flags(src_text, translated.full_text()))

    if back is None:
        score = 100.0 - 12.0 * len([f for f in flags if f.severity == "error"])
        return SubScore("fidelity", max(0.0, score),
                        flags + [Flag("fidelity", "note",
                                      "no back-translation available; round-trip "
                                      "fidelity not measured")],
                        {"measured": False})

    back_text = back.full_text()

    # Hard check: protected spans (dosages, prices, phone numbers, percentages).
    problems = diff_protected(src_text, back_text)
    for p in problems:
        flags.append(Flag("protected_span", "error",
                          f"{p['kind']} {p['issue']}: {p['value']}", p["value"]))

    # Soft check: content-word overlap.
    def content_words(t: str) -> set[str]:
        toks = re.findall(r"[a-z]+", normalise_digits(t).lower())
        return {w for w in toks if len(w) > 3 and w not in EN_STOP}

    s_words, b_words = content_words(src_text), content_words(back_text)
    overlap = len(s_words & b_words) / len(s_words) if s_words else 1.0
    lost = sorted(s_words - b_words)
    if overlap < 0.55 and lost:
        flags.append(Flag("meaning_drift", "warn",
                          f"{len(lost)} content words did not survive the round trip",
                          ", ".join(lost[:12])))

    # Length sanity: a large shrink means content was dropped.
    s_len, b_len = word_count(src_text), word_count(back_text)
    ratio = b_len / s_len if s_len else 1.0
    if ratio < 0.7:
        flags.append(Flag("truncation", "error",
                          f"round trip lost {(1 - ratio) * 100:.0f}% of the length "
                          f"({s_len} -> {b_len} words)"))
    elif ratio > 1.45:
        flags.append(Flag("padding", "warn",
                          f"round trip grew {(ratio - 1) * 100:.0f}% -- possible invented content"))

    score = 0.55 * _norm(overlap, 0.80, 0.35) + 0.25 * _norm(min(ratio, 1.0), 1.0, 0.60)
    score += 0.20 * 100.0
    score -= 12.0 * len([f for f in flags if f.severity == "error"])
    score = max(0.0, min(100.0, score))

    return SubScore("fidelity", score, flags,
                    {"overlap": round(overlap, 3), "length_ratio": round(ratio, 3),
                     "protected_problems": len(problems), "measured": True})


def _term_lock_flags(src_text: str, tgt_text: str) -> list[Flag]:
    """Locked terms must survive, and no claim may come out stronger than it went in.

    Both are promised in the writer prompt and in the slash command, so both are
    enforced here rather than trusted.
    """
    g = glossary()
    flags: list[Flag] = []
    src_low, tgt_low = src_text.lower(), tgt_text.lower()

    # Numbers checked source -> translated directly. The back-translation is
    # computed once, before the rewrite loop runs, so it cannot catch a rewrite
    # pass that damages a dosage in the target text.
    for p in diff_numbers(src_text, tgt_text):
        flags.append(Flag("protected_span", "error",
                          f"number {p['issue']} between source and translation: "
                          f"{p['value']}", p["value"]))

    missing = [t for t in g["never_translate"]
               if t.lower() in src_low and t.lower() not in tgt_low]
    if missing:
        flags.append(Flag("locked_term", "error",
                          f"{len(missing)} locked term(s) did not survive translation",
                          ", ".join(missing)))

    guard = g["medical_claim_guard"]
    introduced = [c for c in guard["forbidden_new_claims"]
                  if c.lower() in tgt_low and c.lower() not in src_low]
    if introduced:
        flags.append(Flag("medical_claim", "error",
                          "the output makes a stronger claim than the source did",
                          ", ".join(introduced)))

    # A hedge present in the source must still be present. Losing "consult a
    # doctor" from health copy is a safety regression, not a style improvement.
    src_hedges = sum(1 for h in guard["hedge_markers"] if h.lower() in src_low)
    tgt_hedges = sum(1 for h in guard["hedge_markers"] if h.lower() in tgt_low)
    if src_hedges and tgt_hedges == 0:
        flags.append(Flag("hedge_lost", "error",
                          f"the source carried {src_hedges} hedge/caution marker(s) "
                          "and the output carries none"))

    return flags


# ---------------------------------------------------------------- 2. grammar

def score_grammar(art: Article, lang: str) -> SubScore:
    """Script integrity and mechanical correctness.

    Bands come from the measured feature table in features.py, not from
    intuition. The dominant signal is space-before-punctuation: native writing
    shows ~0.5 instances per 1000 words, raw MT shows ~30. It is a tokenisation
    artefact of the engine and it is close to a giveaway.

    The danda rate is measured and reported but deliberately NOT scored. The
    first version of this function penalised documents for using a full stop
    instead of a danda; calibration showed BBC Hindi -- professionally written by
    Hindi journalists -- uses full stops in 100% of paragraphs, while both the
    translated and machine-translated classes used dandas throughout. Scoring it
    would have marked native prose as machine output. It is a house-style choice,
    not a correctness or humanness signal.
    """
    entry = next((e for e in CFG["languages"] if e["code"] == lang), {})
    script = entry.get("script", "Devanagari")
    text = art.full_text()
    f = FE.extract(text, lang, script)
    flags: list[Flag] = []

    if script != "Latin" and f["purity"] < 0.80:
        flags.append(Flag("mixed_script", "error" if f["purity"] < 0.6 else "warn",
                          f"only {f['purity'] * 100:.0f}% of letters are {script}; "
                          "untranslated English is left in the body"))

    if script != "Latin" and f["latin"] > 0.18:
        flags.append(Flag("loanword_density", "warn",
                          f"{f['latin'] * 100:.0f}% of tokens are Latin-script words"))

    if f["sp_punct_1k"] > 4:
        flags.append(Flag("spaced_punctuation", "warn",
                          f"{f['sp_punct_1k']:.0f} spaces before punctuation per 1000 words "
                          "(native writing sits near 0.5) -- a machine-translation artefact"))

    if f["hyphen_1k"] > 2:
        flags.append(Flag("spaced_hyphen", "warn",
                          f"{f['hyphen_1k']:.0f} spaced hyphens per 1000 words, "
                          "e.g. 'बार - बार' -- detokenisation artefact"))

    other_mech = max(0.0, f["mech_1k"] - f["sp_punct_1k"])
    if other_mech > 6:
        flags.append(Flag("punctuation", "warn",
                          f"{other_mech:.0f} other spacing/matra defects per 1000 words"))

    empties = [i for i, b in enumerate(art.blocks) if not b.text.strip()]
    if empties:
        flags.append(Flag("empty_block", "error",
                          f"{len(empties)} blocks came back empty from translation"))

    score = (0.28 * _norm(f["purity"], 0.99, 0.70)
             + 0.40 * _norm(f["sp_punct_1k"], 0.5, 25.0)
             + 0.17 * _norm(f["hyphen_1k"], 0.0, 6.0)
             + 0.15 * _norm(other_mech, 0.5, 18.0))
    score -= 10.0 * len([fl for fl in flags if fl.severity == "error"])
    score = max(0.0, min(100.0, score))

    return SubScore("grammar", score, flags,
                    {"script_purity": f["purity"], "latin_ratio": f["latin"],
                     "spaced_punct_per_1k": f["sp_punct_1k"],
                     "spaced_hyphen_per_1k": f["hyphen_1k"],
                     "other_mech_per_1k": round(other_mech, 2),
                     "danda_frac_unscored": f["danda_frac"]})


# --------------------------------------------------------- 3. translationese

def score_translationese(art: Article, lang: str) -> SubScore:
    """Does it read like it was written in this language, or converted into it?

    What the calibration data actually supports, per 1000 words, native vs
    raw MT:

        prepositional calques  ("के रूप में", "के द्वारा")   0.4  vs  10.7
        relative clauses       ("जो", "जिसे", "जिसमें")      0.4  vs   3.7
        comma density                                        22   vs   72

    All three are English constructions carried across intact: English says
    "known as X" and "a group of disorders which are characterised by", and the
    engine renders them literally. Native writers use participles and split the
    sentence instead. Comma density is the same story in punctuation.

    Verb-finality was the original headline hypothesis of this module and it is
    NOT used, because it does not survive contact with data: native 0.92,
    human-translated 0.96, raw MT 0.91. Indic MT gets word order right. It is
    still measured and reported, because it may separate a badly-rewritten
    document, but it carries no weight in the score.
    """
    entry = next((e for e in CFG["languages"] if e["code"] == lang), {})
    script = entry.get("script", "Devanagari")
    text = art.full_text()
    f = FE.extract(text, lang, script)
    flags: list[Flag] = []
    low = text.lower()

    if f["prep_calque_1k"] > 2.5:
        flags.append(Flag("prep_calque", "warn",
                          f"{f['prep_calque_1k']:.1f} literal prepositional constructions per "
                          "1000 words (के रूप में / के द्वारा / के माध्यम से); native writing "
                          "restructures these rather than carrying them across"))

    if f["rel_1k"] > 2.0:
        flags.append(Flag("relative_clause", "warn",
                          f"{f['rel_1k']:.1f} English-style relative clauses per 1000 words "
                          "(जो / जिसे / जिसमें); prefer splitting the sentence"))

    if f["comma_1k"] > 45:
        flags.append(Flag("comma_density", "warn",
                          f"{f['comma_1k']:.0f} commas per 1000 words -- English punctuation "
                          "rhythm; native prose sits near 22"))

    hits = [c for c in L.get(L.CALQUES, lang, ()) if c.lower() in low]
    if hits:
        flags.append(Flag("calque", "warn",
                          f"{len(hits)} stock literal English constructions",
                          ", ".join(hits[:6])))

    over = [c for c in L.get(L.AI_CONNECTIVES, lang, ()) if low.count(c.lower()) >= 3]
    if over:
        flags.append(Flag("connective_overuse", "warn",
                          f"discourse markers repeated 3+ times: {', '.join(over[:5])}"))

    if f["verb_final"] < 0.55:
        flags.append(Flag("word_order", "note",
                          f"only {f['verb_final'] * 100:.0f}% of sentences end on a verb "
                          "(informational -- not scored)"))

    score = (0.34 * _norm(f["prep_calque_1k"], 0.4, 9.0)
             + 0.22 * _norm(f["rel_1k"], 0.4, 3.5)
             + 0.20 * _norm(f["comma_1k"], 22.0, 68.0)
             + 0.14 * _norm(f["calque_1k"], 0.0, 4.0)
             + 0.10 * _norm(f["conn_1k"], 1.2, 9.0))
    score = max(0.0, min(100.0, score))

    return SubScore("translationese", score, flags,
                    {"prep_calque_per_1k": f["prep_calque_1k"],
                     "relative_clause_per_1k": f["rel_1k"],
                     "comma_per_1k": f["comma_1k"],
                     "calque_per_1k": f["calque_1k"],
                     "connectives_per_1k": f["conn_1k"],
                     "verb_final_frac_unscored": f["verb_final"],
                     "mean_sentence_words": f["mean_len"]})


# --------------------------------------------------------------- 4. register

def score_register(art: Article, lang: str, expected: str = "formal") -> SubScore:
    """Honorific consistency and formality level."""
    flags: list[Flag] = []
    text = art.full_text()
    table = L.HONORIFICS.get(lang, {})

    counts = {}
    for level, forms in table.items():
        counts[level] = sum(len(re.findall(rf"(?<!\w){re.escape(f)}(?!\w)", text)) for f in forms)
    total = sum(counts.values())

    consistency = 1.0
    if total >= 4:
        dominant = max(counts, key=counts.get)
        consistency = counts[dominant] / total
        if consistency < 0.85:
            mix = ", ".join(f"{k}={v}" for k, v in counts.items() if v)
            flags.append(Flag("honorific_mix", "error" if consistency < 0.7 else "warn",
                              f"politeness level is inconsistent ({mix}); pick one and hold it"))
        if dominant != expected and counts[dominant] > 0:
            flags.append(Flag("honorific_level", "warn",
                              f"document addresses the reader as '{dominant}', "
                              f"brand voice expects '{expected}'"))

    formal_words = L.get(L.FORMAL_MARKERS, lang, ())
    low = text.lower()
    formal_hits = sum(low.count(w.lower()) for w in formal_words)
    formal_per_1k = formal_hits / max(1, word_count(text) / 1000)
    if formal_per_1k > 9:
        present = [w for w in formal_words if low.count(w.lower())][:6]
        flags.append(Flag("over_formal", "warn",
                          f"heavily Sanskritised register ({formal_per_1k:.0f} markers per 1000 words): "
                          f"{', '.join(present)}"))

    score = (0.65 * _norm(consistency, 1.0, 0.55)
             + 0.35 * _norm(formal_per_1k, 2.0, 18.0))
    score = max(0.0, min(100.0, score))

    return SubScore("register", score, flags,
                    {"honorific_counts": counts, "consistency": round(consistency, 3),
                     "formal_per_1k": round(formal_per_1k, 1)})


# ------------------------------------------------------------- 5. burstiness

def score_burstiness(art: Article, lang: str) -> SubScore:
    """Statistical variation. Machine prose is metronomic; people are not.

    Measured sentence-length CV: native 0.58, human-translated 0.38, raw MT 0.47.
    Type-token ratio is measured but not scored -- it ran *higher* in the machine
    class (0.62 vs 0.53), the opposite of the usual claim, because MT reaches for
    a different synonym each time rather than repeating a word naturally.
    """
    entry = next((e for e in CFG["languages"] if e["code"] == lang), {})
    script = entry.get("script", "Devanagari")
    text = art.full_text()
    sents = sentences(text)

    if len([s for s in sents if word_count(s) > 1]) < 5:
        return SubScore("burstiness", 75.0,
                        [Flag("too_short", "note", "not enough sentences to measure variation")],
                        {"n_sentences": len(sents)})

    f = FE.extract(text, lang, script)
    flags: list[Flag] = []

    if f["sent_cv"] < 0.42:
        flags.append(Flag("uniform_sentences", "warn",
                          f"sentence lengths barely vary (CV {f['sent_cv']:.2f}); "
                          "native prose sits nearer 0.55-0.75 -- vary the rhythm, "
                          "mix short sentences with long ones"))

    paras = [word_count(b.text) for b in art.blocks if b.type == "p" and word_count(b.text) > 3]
    p_cv = (statistics.pstdev(paras) / statistics.mean(paras)) \
        if len(paras) > 3 and statistics.mean(paras) else 0.40
    if len(paras) > 3 and p_cv < 0.28:
        flags.append(Flag("uniform_paragraphs", "warn",
                          f"every paragraph is nearly the same length (CV {p_cv:.2f})"))

    if f["rep_open"] > 0.08:
        flags.append(Flag("repeated_openings", "warn",
                          f"{f['rep_open'] * 100:.0f}% of sentences share an opening with "
                          "two or more others"))

    score = (0.50 * _norm(f["sent_cv"], 0.60, 0.25)
             + 0.28 * _norm(p_cv, 0.45, 0.10)
             + 0.22 * _norm(f["rep_open"], 0.0, 0.20))
    score = max(0.0, min(100.0, score))

    return SubScore("burstiness", score, flags,
                    {"sentence_cv": f["sent_cv"], "paragraph_cv": round(p_cv, 3),
                     "repeated_opening_frac": f["rep_open"],
                     "ttr_unscored": f["ttr"], "n_sentences": f["n_sents"]})


# ---------------------------------------------------------- 6. native review

def score_native_review(review: dict | None) -> SubScore:
    """Scored by the writer backend against the rubric, in a separate prompt.

    Separate is the point. A model asked to check its own output inside the same
    prompt approves it; a fresh call with only the rubric and the text does not.
    """
    if not review:
        return SubScore("native_review", 75.0,
                        [Flag("native_review", "note",
                              "no reviewer pass yet; neutral placeholder used")],
                        {"measured": False})
    flags = [Flag(f.get("kind", "native"), f.get("severity", "warn"),
                  f.get("detail", ""), f.get("sample", ""))
             for f in review.get("flags", [])]
    return SubScore("native_review", float(review.get("score", 75.0)), flags,
                    {"measured": True, "notes": review.get("notes", "")})


# -------------------------------------------------------------------- AEO

def score_aeo(art: Article, lang: str) -> SubScore:
    """Is this structured the way answer engines actually quote from?"""
    a = CFG["aeo"]
    flags: list[Flag] = []
    heads = art.headings()
    q_heads = [h for h in heads if is_question(h)]
    q_ratio = len(q_heads) / len(heads) if heads else 0.0

    if q_ratio < 0.4:
        flags.append(Flag("question_headings", "warn",
                          f"only {len(q_heads)}/{len(heads)} headings are questions; "
                          "answer engines match on question-shaped headings"))

    n_faq = len(art.faqs)
    if n_faq < a["faq_min"]:
        flags.append(Flag("faq_count", "warn",
                          f"{n_faq} FAQs, want at least {a['faq_min']}"))

    has_tldr = any(b.type == "tldr" for b in art.blocks)
    if not has_tldr:
        flags.append(Flag("tldr", "warn", "no TL;DR block -- this is the passage engines lift first"))

    answers = [b for b in art.blocks if b.type == "answer"]
    good_answers = [b for b in answers
                    if a["answer_words_min"] - 10 <= word_count(b.text) <= a["answer_words_max"] + 15]
    ans_ratio = len(good_answers) / len(q_heads) if q_heads else 0.0
    if q_heads and ans_ratio < 0.7:
        flags.append(Flag("answer_blocks", "warn",
                          f"{len(good_answers)}/{len(q_heads)} question headings have a "
                          f"{a['answer_words_min']}-{a['answer_words_max']} word direct answer"))

    t_len, d_len = len(art.title), len(art.meta_description)
    if not (10 < t_len <= a["meta_title_max"]):
        flags.append(Flag("meta_title", "warn", f"title is {t_len} chars (max {a['meta_title_max']})"))
    if not (50 < d_len <= a["meta_desc_max"]):
        flags.append(Flag("meta_description", "warn",
                          f"meta description is {d_len} chars (want 50-{a['meta_desc_max']})"))

    schema = art.meta.get("schema") or {}
    types = {s.get("@type") for s in schema.get("@graph", [])} if schema else set()
    want = {"Article", "FAQPage", "BreadcrumbList"}
    missing = want - types
    if missing:
        flags.append(Flag("schema", "warn", f"JSON-LD missing: {', '.join(sorted(missing))}"))

    score = (0.22 * _norm(q_ratio, 0.6, 0.0)
             + 0.18 * _norm(n_faq, a["faq_min"], 0)
             + 0.15 * (100.0 if has_tldr else 0.0)
             + 0.20 * _norm(ans_ratio, 0.85, 0.0)
             + 0.10 * (100.0 if 10 < t_len <= a["meta_title_max"] else 40.0)
             + 0.05 * (100.0 if 50 < d_len <= a["meta_desc_max"] else 40.0)
             + 0.10 * _norm(len(want & types), len(want), 0))
    score = max(0.0, min(100.0, score))

    return SubScore("aeo", score, flags,
                    {"question_heading_ratio": round(q_ratio, 2), "faqs": n_faq,
                     "tldr": has_tldr, "answer_ratio": round(ans_ratio, 2),
                     "schema_types": sorted(types)})


# ------------------------------------------------------------------ composite

@dataclass
class Report:
    lang: str
    human_likeness: float
    ai_pct: float
    aeo: float
    subs: dict
    flags: list[dict]
    blocking: list[dict]
    passed: bool
    words: int

    def dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        s = self.subs
        return (f"{self.lang:9} HLS {self.human_likeness:5.1f} | AI {self.ai_pct:5.1f}% | "
                f"AEO {self.aeo:5.1f} | fid {s['fidelity']['score']:.0f} "
                f"gram {s['grammar']['score']:.0f} trans {s['translationese']['score']:.0f} "
                f"reg {s['register']['score']:.0f} burst {s['burstiness']['score']:.0f} "
                f"| {'PASS' if self.passed else 'FAIL'}")


def score_article(translated: Article, source: Article, back: Article | None,
                  lang: str, *, review: dict | None = None,
                  expected_honorific: str = "formal") -> Report:
    subs = {
        "fidelity": score_fidelity(source, back, translated),
        "grammar": score_grammar(translated, lang),
        "translationese": score_translationese(translated, lang),
        "register": score_register(translated, lang, expected_honorific),
        "burstiness": score_burstiness(translated, lang),
        "native_review": score_native_review(review),
    }
    aeo = score_aeo(translated, lang)

    hls = sum(subs[k].score * W[k] for k in W)
    ai_pct = round(100.0 - hls, 1)

    all_flags = [f.dict() for s in subs.values() for f in s.flags] + [f.dict() for f in aeo.flags]

    # Blocking defects are never traded away for a better style score.
    blocking = [f for f in all_flags
                if f["severity"] == "error" and f["kind"] in
                ("protected_span", "truncation", "empty_block", "mixed_script",
                 "locked_term", "medical_claim", "hedge_lost")]

    # `passed` gates whether the translation is safe to ship, not whether it
    # reads as human. ai_pct, fidelity and grammar scores are still computed
    # and shown -- useful information -- but only `blocking` (protected
    # numbers/dosages/URLs, locked brand terms, medical-claim inflation, a
    # lost hedge, mixed script, an empty block) withholds publishing. Content
    # that scores 40% AI-likeness still gets a Doc; content with an invented
    # statistic does not, regardless of how human the rest of it reads.
    passed = not blocking

    return Report(
        lang=lang,
        human_likeness=round(hls, 1),
        ai_pct=ai_pct,
        aeo=round(aeo.score, 1),
        subs={k: v.dict() for k, v in {**subs, "aeo": aeo}.items()},
        flags=all_flags,
        blocking=blocking,
        passed=passed,
        words=translated.words(),
    )


def rewrite_brief(report: Report, limit: int = 14) -> list[str]:
    """Turn flags into specific instructions for the rewriter.

    "Make it sound more human" produces nothing. "62% of your sentences end on a
    noun -- Hindi puts the verb last" produces a fix.
    """
    order = {"error": 0, "warn": 1, "note": 2}
    ranked = sorted(report.flags, key=lambda f: order.get(f["severity"], 3))
    out = []
    for f in ranked[:limit]:
        line = f"[{f['severity']}] {f['kind']}: {f['detail']}"
        if f.get("sample"):
            line += f"  (e.g. {f['sample'][:120]})"
        out.append(line)
    return out
