"""Calibration for the English AI-likeness gate. Same method as calibrate.py:
real native text vs real AI text, measured separation, nothing trusted without
evidence.

  native  BBC News English -- composed by a person for publication.
  ai      Gemini, asked to write a generic blog paragraph on a comparable
          health/informational topic, UNEDITED raw output -- the same class of
          content this tool's Draft/Fix stages themselves produce before any
          humanising pass, which is exactly what needs to be told apart from
          the finished, edited version.

    python calibrate_en.py --fetch      # build fixtures (network + Gemini calls)
    python calibrate_en.py              # score and report separation
"""
from __future__ import annotations

import argparse
import re
import statistics
from pathlib import Path

from common import ROOT, log, warn, word_count
import en_features as EF
import sources

SAMPLES = ROOT / "samples_en"
CLASSES = ("native", "ai")
MAX_WORDS = 420
MIN_WORDS = 120

AI_TOPICS = [
    "the early symptoms of thyroid problems",
    "how blood pressure medication works",
    "the benefits of a Mediterranean diet",
    "how to recognise signs of anxiety",
    "why sleep quality matters for heart health",
    "the difference between type 1 and type 2 diabetes",
    "how to start a daily walking habit",
    "what causes seasonal allergies",
    "the basics of intermittent fasting",
    "how vaccines train the immune system",
]


def _trim(text: str, max_words: int = MAX_WORDS) -> str:
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


def fetch_native(limit: int = 8) -> int:
    dest = SAMPLES / "native"
    dest.mkdir(parents=True, exist_ok=True)
    html = sources.fetch("https://www.bbc.com/news")
    urls, seen = [], set()
    for href in re.findall(r'href="([^"]+)"', html):
        if not re.search(r"/news/(articles|world|health)", href):
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
        (dest / f"bbc_{saved:02d}.txt").write_text(
            f"# {art.title}\n\n{body}\n", encoding="utf-8")
        saved += 1
        log(f"native <- {art.title[:60]} ({word_count(body)}w)", indent=1)
    return saved


def fetch_ai(limit: int = 8) -> int:
    """Raw, unedited Gemini output -- exactly what Draft produces before any
    humanising pass, which is the class this gate exists to catch."""
    from writer.gemini_free import GeminiFreeWriter
    dest = SAMPLES / "ai"
    dest.mkdir(parents=True, exist_ok=True)
    w = GeminiFreeWriter()
    saved = 0
    for topic in AI_TOPICS:
        if saved >= limit:
            break
        # Multiple paragraphs, plainly asked for -- matching the shape real
        # Draft/Fix output actually has, so a signal found here is a property
        # of the writing rather than an artifact of "one paragraph vs several"
        # that a single-paragraph fixture would have produced.
        prompt = (f"Write a 300-350 word blog post about {topic}, in 3-4 "
                  "paragraphs the way a helpful blog post is normally "
                  "structured. Write it the way you naturally would; do not "
                  "try to sound more or less like AI than you normally do. "
                  "Reply with JSON only: {\"title\": \"...\", \"body\": \"...\"} "
                  "-- body separated into paragraphs with \\n\\n between them.")
        try:
            data = w.generate(prompt, stage="calibration_fixture", slug="fixture", lang="en")
        except Exception as exc:
            warn(f"skip {topic}: {exc.__class__.__name__}")
            continue
        body = _trim(data.get("body", ""))
        if word_count(body) < MIN_WORDS:
            continue
        (dest / f"gem_{saved:02d}.txt").write_text(
            f"# {data.get('title', topic)}\n\n{body}\n", encoding="utf-8")
        saved += 1
        log(f"ai <- {topic} ({word_count(body)}w)", indent=1)
    return saved


def score_file(path: Path) -> dict:
    body = "\n".join(l for l in path.read_text(encoding="utf-8").split("\n")
                     if not l.startswith("#"))
    f = EF.extract(body)
    return {"file": path.name, **f}


def auc(pos: list[float], neg: list[float]) -> float:
    if not pos or not neg:
        return float("nan")
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def cohens_d(a: list[float], b: list[float]) -> float:
    if len(a) < 2 or len(b) < 2:
        return float("nan")
    na, nb = len(a), len(b)
    pooled = (((na - 1) * statistics.variance(a) + (nb - 1) * statistics.variance(b))
             / (na + nb - 2)) ** 0.5
    return (statistics.mean(a) - statistics.mean(b)) / pooled if pooled else float("nan")


def report() -> int:
    rows: dict[str, list[dict]] = {}
    for cls in CLASSES:
        d = SAMPLES / cls
        files = sorted(d.glob("*.txt")) if d.exists() else []
        rows[cls] = [score_file(f) for f in files]

    if not all(rows.values()):
        print("\nNot enough samples. Run:  python calibrate_en.py --fetch\n")
        return 1

    keys = ["file", "words", "sent_cv", "para_cv", "rep_open", "filler_1k",
            "tell_1k", "em_dash_1k", "passive_ratio", "ttr", "colon_lists_1k"]
    for cls, data in rows.items():
        print(f"\n=== {cls.upper()} (n={len(data)}) " + "=" * 40)
        print("  " + "".join(str(k).rjust(13) for k in keys))
        for r in data:
            print("  " + "".join(str(r.get(k, ""))[:12].rjust(13) for k in keys))

    print("\n=== DISCRIMINATION (native mean vs ai mean) " + "=" * 20)
    print(f"  {'feature':16}{'native':>10}{'ai':>10}{'sep':>10}")
    for k in keys[1:]:
        n = statistics.mean([r[k] for r in rows["native"]])
        a = statistics.mean([r[k] for r in rows["ai"]])
        scale = max(abs(n), abs(a), 1e-6)
        sep = (n - a) / scale
        print(f"  {k:16}{n:10.3f}{a:10.3f}{sep:10.2f}")
    print("\n  |sep| > 0.25 means real signal for that feature; near 0 means none.")

    # Prove the actual composite scorer separates the classes, not just the
    # individual features -- this is the number that matters.
    import en_detect
    native_scores = [en_detect.score(
        "\n".join(l for l in (SAMPLES / "native" / r["file"]).read_text(encoding="utf-8").split("\n")
                 if not l.startswith("#"))).score for r in rows["native"]]
    ai_scores = [en_detect.score(
        "\n".join(l for l in (SAMPLES / "ai" / r["file"]).read_text(encoding="utf-8").split("\n")
                 if not l.startswith("#"))).score for r in rows["ai"]]

    print("\n=== COMPOSITE SCORER (en_detect.score) " + "=" * 25)
    print(f"  native: {[round(s,1) for s in native_scores]}  mean {statistics.mean(native_scores):.1f}")
    print(f"  ai:     {[round(s,1) for s in ai_scores]}  mean {statistics.mean(ai_scores):.1f}")
    a = auc(native_scores, ai_scores)
    d = cohens_d(native_scores, ai_scores)
    print(f"  AUC {a:.2f} | Cohen's d {d:.2f}")
    verdict = ("USABLE" if a >= 0.80 else "WEAK -- retune WEIGHTS/BANDS" if a >= 0.65
              else "NOT USABLE")
    print(f"  verdict: {verdict}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true")
    args = ap.parse_args()
    if args.fetch:
        log("fetching native (BBC News) samples")
        n = fetch_native()
        log("fetching AI (raw Gemini) samples")
        a = fetch_ai()
        log(f"saved {n} native + {a} AI fixtures")
    return report()


if __name__ == "__main__":
    raise SystemExit(main())
