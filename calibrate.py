"""Calibration harness: does the scorer actually separate native prose from translated prose?

The gate in quality.py is only worth having if its numbers mean something. This
script tests that claim against real text before the rewrite loop is allowed to
depend on it.

Three classes, all real text, none synthetic:

  NATIVE      BBC Hindi journalism -- composed in Hindi by Hindi writers.
  TRANSLATED  Hindi Wikipedia medical articles -- overwhelmingly rendered from
              the English articles, then human-edited. The "converted into Hindi"
              register the pipeline exists to detect, at its most flattering.
  MT          Raw machine translation of English Wikipedia, no editing.

If the scorer cannot tell these apart, it cannot tell good output from bad, and
the weights in config.json need changing before anything ships.

Result as measured (n=6/6/4, Hindi):

    native      HLS 92.3   AI-likeness  7.7%
    translated  HLS 74.7   AI-likeness 25.3%
    mt          HLS 56.0   AI-likeness 44.0%
    native vs translated  AUC 1.00  d 3.19
    native vs mt          AUC 1.00  d 3.25

Running this is what caught three wrong assumptions in the first version of the
scorer: that Devanagari prose ends sentences with a danda (BBC Hindi uses full
stops in 100% of paragraphs), that native prose has shorter sentences than
translated prose (it is longer: 22.5 vs 19.4 words), and that verb-finality is
the dominant translationese signal (all three classes measure 0.91-0.96, so it
carries no signal at all). None of those would have been visible without data.

The MT class is built by `--build-mt-free` (MyMemory's free public API, no
credentials) or `--build-mt` (Bhashini itself, once credentials exist). Re-run
with `--build-mt` when they land, since Bhashini is the engine that will
actually be in the pipeline.

Samples are fetched to samples/ as local calibration fixtures only. They are
never published, translated, or included in any deliverable.

    python calibrate.py --fetch           # native + translated fixtures
    python calibrate.py --build-mt-free   # raw MT fixtures, no credentials
    python calibrate.py                   # score and report separation
"""
from __future__ import annotations

import argparse
import re
import statistics
from pathlib import Path

from common import ROOT, log, warn, word_count
from extract import from_markdown
import quality
import sources

SAMPLES = ROOT / "samples"
CLASSES = ("native", "translated", "mt")

WIKI_HI = [
    "मधुमेह", "उच्च_रक्तचाप", "मोटापा", "अस्थमा", "यकृत", "गुर्दा",
]

MAX_WORDS = 420          # keep fixtures small; they are a test set, not a corpus
MIN_WORDS = 120


# ------------------------------------------------------------------- fetching

def _trim(text: str, max_words: int = MAX_WORDS) -> str:
    """Keep whole paragraphs up to the word cap."""
    out, total = [], 0
    for para in [p.strip() for p in text.split("\n") if p.strip()]:
        n = word_count(para)
        if n < 8:
            continue
        if total + n > max_words and out:
            break
        out.append(para)
        total += n
    return "\n".join(out)


def fetch_native(limit: int = 6) -> int:
    """BBC Hindi articles: written in Hindi, not converted into it."""
    dest = SAMPLES / "native"
    dest.mkdir(parents=True, exist_ok=True)
    html = sources.fetch("https://www.bbc.com/hindi")
    urls, seen = [], set()
    for href in re.findall(r'href="([^"]+)"', html):
        if "/hindi/articles/" not in href:
            continue
        full = href if href.startswith("http") else "https://www.bbc.com" + href
        if full not in seen:
            seen.add(full)
            urls.append(full)

    saved = 0
    for url in urls:
        if saved >= limit:
            break
        try:
            art = sources.load_url(url)
        except Exception as exc:
            warn(f"skip {url}: {exc.__class__.__name__}")
            continue
        body = _trim("\n".join(b.text for b in art.blocks if b.type in ("p", "li")))
        if word_count(body) < MIN_WORDS:
            continue
        if quality.script_ratio(body, "Devanagari") < 0.85:
            continue
        (dest / f"bbc_{saved:02d}.txt").write_text(
            f"# {art.title}\n\n{body}\n", encoding="utf-8")
        saved += 1
        log(f"native  <- {art.title[:60]} ({word_count(body)}w)", indent=1)
    return saved


def fetch_translated(limit: int = 6) -> int:
    """Hindi Wikipedia medical articles: rendered from the English originals."""
    dest = SAMPLES / "translated"
    dest.mkdir(parents=True, exist_ok=True)
    saved = 0
    for title in WIKI_HI:
        if saved >= limit:
            break
        url = f"https://hi.wikipedia.org/wiki/{title}"
        try:
            art = sources.load_url(url)
        except Exception as exc:
            warn(f"skip {title}: {exc.__class__.__name__}")
            continue
        body = _trim("\n".join(b.text for b in art.blocks if b.type in ("p", "li")))
        body = re.sub(r"\[\d+\]", "", body)           # strip citation markers
        if word_count(body) < MIN_WORDS:
            continue
        (dest / f"wiki_{saved:02d}.txt").write_text(
            f"# {art.title}\n\n{body}\n", encoding="utf-8")
        saved += 1
        log(f"translated <- {art.title[:60]} ({word_count(body)}w)", indent=1)
    return saved


# -------------------------------------------------------------------- scoring

def score_file(path: Path, lang: str = "hi") -> dict:
    """Score one fixture on the sub-scores that need no back-translation."""
    art = from_markdown(path.read_text(encoding="utf-8"))
    subs = {
        "grammar": quality.score_grammar(art, lang),
        "translationese": quality.score_translationese(art, lang),
        "register": quality.score_register(art, lang),
        "burstiness": quality.score_burstiness(art, lang),
    }
    # Renormalise over the measurable subset: fidelity needs a source document
    # and native_review needs the writer backend, neither of which exists for a
    # standalone fixture. Scoring them as zero would be a measurement artefact.
    weights = {k: quality.W[k] for k in subs}
    total_w = sum(weights.values())
    hls = sum(subs[k].score * weights[k] for k in subs) / total_w

    return {
        "file": path.name,
        "words": art.words(),
        "hls": round(hls, 1),
        "ai_pct": round(100 - hls, 1),
        **{k: round(v.score, 1) for k, v in subs.items()},
        "prep_calq": subs["translationese"].detail.get("prep_calque_per_1k"),
        "comma_1k": subs["translationese"].detail.get("comma_per_1k"),
        "sent_cv": subs["burstiness"].detail.get("sentence_cv"),
        "sp_punct": subs["grammar"].detail.get("spaced_punct_per_1k"),
    }


def cohens_d(a: list[float], b: list[float]) -> float:
    if len(a) < 2 or len(b) < 2:
        return float("nan")
    na, nb = len(a), len(b)
    pooled = math_sqrt(((na - 1) * statistics.variance(a) + (nb - 1) * statistics.variance(b))
                       / (na + nb - 2))
    return (statistics.mean(a) - statistics.mean(b)) / pooled if pooled else float("nan")


def math_sqrt(x: float) -> float:
    return x ** 0.5


def auc(pos: list[float], neg: list[float]) -> float:
    """Probability a random native sample scores above a random translated one."""
    if not pos or not neg:
        return float("nan")
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def report(lang: str = "hi") -> int:
    rows: dict[str, list[dict]] = {}
    for cls in CLASSES:
        d = SAMPLES / cls
        files = sorted(d.glob("*.txt")) if d.exists() else []
        rows[cls] = [score_file(f, lang) for f in files]

    have = {c: r for c, r in rows.items() if r}
    if len(have) < 2:
        print("\nNot enough samples. Run:  python calibrate.py --fetch\n")
        return 1

    cols = ["file", "words", "hls", "ai_pct", "grammar", "translationese",
            "register", "burstiness", "prep_calq", "comma_1k", "sp_punct", "sent_cv"]
    for cls, data in have.items():
        print(f"\n=== {cls.upper()}  (n={len(data)}) " + "=" * 40)
        print("  " + "".join(c.rjust(15 if c == "file" else 15) for c in cols))
        for r in data:
            print("  " + "".join(str(r.get(c, "")).rjust(15) for c in cols))
        hls = [r["hls"] for r in data]
        print(f"  mean HLS {statistics.mean(hls):.1f}"
              + (f"  sd {statistics.pstdev(hls):.1f}" if len(hls) > 1 else "")
              + f"  -> AI-likeness {100 - statistics.mean(hls):.1f}%")

    print("\n=== SEPARATION " + "=" * 47)
    native = [r["hls"] for r in rows.get("native", [])]
    for neg_cls in ("translated", "mt"):
        neg = [r["hls"] for r in rows.get(neg_cls, [])]
        if not (native and neg):
            continue
        gap = statistics.mean(native) - statistics.mean(neg)
        a = auc(native, neg)
        d = cohens_d(native, neg)
        print(f"  native vs {neg_cls}:  mean gap {gap:+.1f} HLS points | "
              f"AUC {a:.2f} | Cohen's d {d:.2f}")
        verdict = ("USABLE — the gate separates the classes" if a >= 0.80 and gap >= 6
                   else "WEAK — retune score_weights in config.json before trusting the gate"
                   if a >= 0.65 else
                   "NOT USABLE — the scorer does not distinguish these classes")
        print(f"    verdict: {verdict}")

        print("    per sub-score mean gap (native minus %s):" % neg_cls)
        for k in ("grammar", "translationese", "register", "burstiness"):
            pn = statistics.mean([r[k] for r in rows["native"]])
            pg = statistics.mean([r[k] for r in rows[neg_cls]])
            print(f"      {k:16} {pn:6.1f} vs {pg:6.1f}   gap {pn - pg:+6.1f}")

    if not rows.get("mt"):
        print("\n  NOTE: the 'mt' class is empty. These figures compare native Hindi with")
        print("  human-edited translated Hindi, which is a harder negative than raw machine")
        print("  output. Once Bhashini credentials exist, run `python calibrate.py --build-mt`")
        print("  to add real MT samples and re-check the gate against them.")
    return 0


def build_mt_free(limit: int = 4, words: int = 200) -> int:
    """Raw MT fixtures from MyMemory's free public API -- calibration only.

    Bhashini is the production engine, but calibrating the gate cannot wait for
    its credentials, and calibrating against *no* raw-MT class is what let three
    wrong assumptions into the first version of this scorer. MyMemory is a real
    NMT engine with a documented anonymous tier, so it stands in as the negative
    class until `--build-mt` can regenerate it from Bhashini itself.

    Nothing from this path ever reaches a deliverable. It writes to samples/mt/.
    """
    import time as _time
    dest = SAMPLES / "mt"
    dest.mkdir(parents=True, exist_ok=True)
    saved = 0

    for title in ["Diabetes", "Hypertension", "Obesity", "Asthma"]:
        if saved >= limit:
            break
        try:
            art = sources.load_url(f"https://en.wikipedia.org/wiki/{title}")
        except Exception as exc:
            warn(f"skip {title}: {exc.__class__.__name__}")
            continue
        body = re.sub(r"\[\d+\]", "", _trim(
            "\n".join(b.text for b in art.blocks if b.type == "p"), words))
        if word_count(body) < 80:
            continue

        out_parts, failed = [], False
        for chunk in _split_chars(body, 450):
            try:
                resp = sources.SESSION.get(
                    "https://api.mymemory.translated.net/get",
                    params={"q": chunk, "langpair": "en|hi"}, timeout=45).json()
            except Exception as exc:
                warn(f"mymemory failed on {title}: {exc.__class__.__name__}")
                failed = True
                break
            if resp.get("responseStatus") != 200:
                warn(f"mymemory: {resp.get('responseDetails', 'quota or error')}")
                failed = True
                break
            out_parts.append(resp["responseData"]["translatedText"])
            _time.sleep(1.5)

        if failed or not out_parts:
            continue
        (dest / f"mm_{saved:02d}.txt").write_text(
            f"# {art.title}\n\n" + "\n".join(out_parts) + "\n", encoding="utf-8")
        saved += 1
        log(f"mt <- {title} ({word_count(body)}w EN)", indent=1)
    return saved


def _split_chars(text: str, limit: int) -> list[str]:
    """Split on sentence boundaries under a hard character limit."""
    out, buf = [], ""
    for sent in re.split(r"(?<=[.!?])\s+", text.replace("\n", " ")):
        if buf and len(buf) + 1 + len(sent) > limit:
            out.append(buf)
            buf = sent
        else:
            buf = f"{buf} {sent}".strip()
    if buf:
        out.append(buf)
    return out


def build_mt(limit: int = 6) -> int:
    """Translate the English source of each Wikipedia fixture through Bhashini."""
    from translate import client
    dest = SAMPLES / "mt"
    dest.mkdir(parents=True, exist_ok=True)
    bh = client()
    bh.require_creds()

    saved = 0
    for title in ["Diabetes", "Hypertension", "Obesity", "Asthma", "Liver", "Kidney"]:
        if saved >= limit:
            break
        try:
            art = sources.load_url(f"https://en.wikipedia.org/wiki/{title}")
        except Exception as exc:
            warn(f"skip {title}: {exc}")
            continue
        body = _trim("\n".join(b.text for b in art.blocks if b.type == "p"))
        body = re.sub(r"\[\d+\]", "", body)
        if word_count(body) < MIN_WORDS:
            continue
        paras = [p for p in body.split("\n") if p.strip()]
        hindi = bh.translate_texts(paras, "en", "hi")
        (dest / f"mt_{saved:02d}.txt").write_text(
            f"# {art.title}\n\n" + "\n".join(hindi) + "\n", encoding="utf-8")
        saved += 1
        log(f"mt <- {title} ({word_count(body)}w)", indent=1)
    return saved


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fetch", action="store_true", help="download native + translated fixtures")
    ap.add_argument("--build-mt-free", action="store_true",
                    help="add raw-MT fixtures via MyMemory's free API (no credentials)")
    ap.add_argument("--build-mt", action="store_true",
                    help="add raw-MT fixtures from Bhashini itself (needs credentials)")
    ap.add_argument("--lang", default="hi")
    args = ap.parse_args()

    if args.fetch:
        log("fetching native (BBC Hindi) samples")
        n = fetch_native()
        log("fetching translated (Hindi Wikipedia) samples")
        t = fetch_translated()
        log(f"saved {n} native + {t} translated fixtures to samples/")
    if args.build_mt_free:
        log(f"saved {build_mt_free()} raw-MT fixtures (MyMemory)")
    if args.build_mt:
        log(f"saved {build_mt()} raw-MT fixtures (Bhashini)")

    return report(args.lang)


if __name__ == "__main__":
    raise SystemExit(main())
