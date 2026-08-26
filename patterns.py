"""Protected spans: the things a translation or a humanising rewrite must never alter.

These live in code rather than glossary.json because they are regexes, and JSON
escaping of regexes through shell heredocs is a reliable source of silent bugs.

The contract quality.py enforces: every protected span present in the source must
still be present, byte-identical, in the output. A rewrite that improves the prose
but changes "500 mg" to "50 mg" is not a style variation -- it is a defect, and on
YMYL health content it is a dangerous one.
"""
from __future__ import annotations

import re
from typing import Iterable

# Indic digits map back to ASCII so "५००" and "500" compare equal. Translation
# engines legitimately localise numerals; that must not read as a dropped fact.
_DIGIT_MAP = {}
for _base in (0x0966, 0x09E6, 0x0A66, 0x0AE6, 0x0BE6, 0x0C66, 0x0CE6, 0x0B66):
    for _i in range(10):
        _DIGIT_MAP[chr(_base + _i)] = str(_i)

PROTECTED = [
    ("url",       re.compile(r"https?://[^\s<>\"')]+")),
    ("email",     re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")),
    ("phone",     re.compile(r"(?:\+91[\s-]?)?[6-9]\d{9}\b")),
    ("dosage",    re.compile(r"\d+(?:[.,]\d+)?\s*(?:mg|ml|mcg|gm|kg|g|IU)\b", re.I)),
    ("price_inr", re.compile(r"(?:₹|Rs\.?|INR)\s*\d[\d,]*(?:\.\d+)?")),
    ("percent",   re.compile(r"\d+(?:\.\d+)?\s*%")),
    ("number",    re.compile(r"\b\d+(?:[.,]\d+)*\b")),
]

# Ordered longest-match-first so "500 mg" is captured as a dosage rather than
# leaving a bare "500" behind to be matched again by the number pattern.
_PRIORITY = ["url", "email", "phone", "dosage", "price_inr", "percent", "number"]


# Enumerators: the "1." in "Q1.", "Question 1.", "प्रश्न 1." or a bare "1." at
# the head of a list item. These number the document; they do not state a fact
# about it, and the AEO layer replaces them by design -- source FAQs written as
# "Q1. What is ...?" become structured {q, a} pairs rendered as headings.
#
# The rule is deliberately blind to whatever precedes the digit. An earlier
# version keyed on a "Q"/"A" prefix and made things worse: it stripped the
# source's "Q1." while leaving the translation's "प्रश्न 1." and the
# back-translation's "Question 1." intact, so a document that had merely
# renumbered its FAQ was reported as having INVENTED the numbers 1, 2 and 3.
# Symmetry across scripts matters more here than precision about the prefix.
#
# Found by the first real end-to-end run: a HIIMS post with a Q1-Q5 FAQ block
# was held back with nine defects, eight of which were this. Their house style
# is a numbered FAQ, so it would have blocked most of the site.
#
# The cost is narrow and worth stating: a sentence ending on a bare one- or
# two-digit number ("reduce the dose to 2.") loses that number from the check.
# Anything carrying a unit, a currency symbol or a percent sign is matched by a
# higher-priority pattern before the bare-number rule ever sees it.
_ENUM_RX = re.compile(r"(?<!\d)\d{1,2}[.)](?=\s|$)")


def normalise_digits(text: str) -> str:
    return "".join(_DIGIT_MAP.get(ch, ch) for ch in text or "")


def find_protected(text: str) -> dict[str, list[str]]:
    """Return every protected span in `text`, keyed by kind, with overlaps removed."""
    text = normalise_digits(text or "")
    # Blank out enumerators the same way diff_numbers does, and with spaces of
    # equal length so every offset below still lines up. Without this the
    # multiset comparison in diff_protected counts "Q1." against "1." and
    # reports an invented number on a document that only renumbered its FAQ.
    text = _ENUM_RX.sub(lambda m: " " * len(m.group(0)), text)
    claimed: list[tuple[int, int]] = []
    found: dict[str, list[str]] = {}

    def overlaps(a: int, b: int) -> bool:
        return any(not (b <= s or a >= e) for s, e in claimed)

    by_name = dict(PROTECTED)
    for name in _PRIORITY:
        rx = by_name[name]
        hits: list[str] = []
        for m in rx.finditer(text):
            if overlaps(m.start(), m.end()):
                continue
            claimed.append((m.start(), m.end()))
            hits.append(m.group(0).strip())
        if hits:
            found[name] = hits
    return found


def _canon(kind: str, value: str) -> str:
    """Compare on meaning, not formatting: 1,000 == 1000, Rs.500 == Rs 500."""
    v = normalise_digits(value).lower().strip()
    v = re.sub(r"[\s,]", "", v)
    if kind == "price_inr":
        v = re.sub(r"^(?:rs\.?|inr|₹)", "", v)
    if kind == "number":
        v = v.rstrip(".")
    return v


def diff_protected(source: str, target: str) -> list[dict]:
    """Report protected spans that were dropped from or invented in the target.

    Returned entries are defects, not warnings. Callers should refuse to publish.
    """
    src = find_protected(source)
    tgt = find_protected(target)
    problems: list[dict] = []

    for kind in set(src) | set(tgt):
        s_vals = [_canon(kind, v) for v in src.get(kind, [])]
        t_vals = [_canon(kind, v) for v in tgt.get(kind, [])]
        s_pool = list(s_vals)
        for tv in t_vals:
            if tv in s_pool:
                s_pool.remove(tv)
            else:
                problems.append({"kind": kind, "issue": "invented", "value": tv})
        for sv in s_pool:
            problems.append({"kind": kind, "issue": "dropped", "value": sv})

    return problems


_NUM_RX = re.compile(r"\d+(?:\.\d+)?")

def diff_numbers(source: str, target: str) -> list[dict]:
    """Compare the bare numeric values of two texts, across scripts and units.

    The back-translation check in quality.py cannot catch a rewrite that damages
    a number, because the rewrite edits the translated text while the
    back-translation stays as it was. This runs source -> translated directly.

    It compares numbers only, not units, because units do not survive
    translation as ASCII: "500 mg" becomes "500 मिलीग्राम", so a unit-aware diff
    reports a dropped dosage and an invented number on every correct document.
    The number itself is what matters -- 500 becoming 50 is the failure worth
    catching, and it is caught in any script.

    List and FAQ enumerators are removed from both sides first: "Q1." numbers
    the document rather than stating a fact about it, and the AEO layer replaces
    that numbering by design.

    Compared as SETS, not multisets. The AEO layer deliberately restates key
    facts: "30 minutes" appears in the TL;DR, again in the direct answer, and
    again in an FAQ. Counting occurrences flags every correctly-structured
    document as having invented numbers. What must hold is that the set of
    values is unchanged -- nothing new appears, nothing goes missing. A 500
    silently becoming 50 still fails, because 50 is not in the source set.
    """
    def values(text: str) -> set[str]:
        cleaned = re.sub(r"(?<=\d)[,](?=\d{3}\b)", "", normalise_digits(text or ""))
        cleaned = _ENUM_RX.sub(" ", cleaned)
        return {v.rstrip(".").lstrip("0") or "0" for v in _NUM_RX.findall(cleaned)}

    src, tgt = values(source), values(target)
    problems = [{"kind": "number", "issue": "invented", "value": v}
                for v in sorted(tgt - src)]
    problems += [{"kind": "number", "issue": "dropped", "value": v}
                 for v in sorted(src - tgt)]
    return problems


def locked_terms_present(text: str, terms: Iterable[str]) -> list[str]:
    """Which never-translate terms went missing. Case-insensitive, whole-word."""
    missing = []
    low = (text or "").lower()
    for term in terms:
        if term.lower() not in low:
            missing.append(term)
    return missing


def mask_protected(text: str) -> tuple[str, dict[str, str]]:
    """Replace protected spans with opaque tokens before sending to a rewriter.

    A model that never sees "500 mg" cannot helpfully round it to "half a gram".
    Tokens use a shape no natural language produces so they survive translation.
    """
    text = normalise_digits(text or "")
    mapping: dict[str, str] = {}
    claimed: list[tuple[int, int]] = []
    spans: list[tuple[int, int, str]] = []
    by_name = dict(PROTECTED)

    for name in _PRIORITY:
        for m in by_name[name].finditer(text):
            if any(not (m.end() <= s or m.start() >= e) for s, e in claimed):
                continue
            claimed.append((m.start(), m.end()))
            spans.append((m.start(), m.end(), m.group(0)))

    out, cursor = [], 0
    for idx, (start, end, val) in enumerate(sorted(spans)):
        token = f"⦉{idx}⦊"          # ⦉0⦊ -- not produced by any MT engine
        mapping[token] = val
        out.append(text[cursor:start])
        out.append(token)
        cursor = end
    out.append(text[cursor:])
    return "".join(out), mapping


def unmask_protected(text: str, mapping: dict[str, str]) -> str:
    for token, val in mapping.items():
        text = text.replace(token, val)
    return text
