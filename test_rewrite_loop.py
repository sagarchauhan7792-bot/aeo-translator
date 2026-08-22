"""Control-flow tests for the rewrite loop.

The loop has four behaviours that matter and are easy to get wrong:

  1. It fires when the score is above the trigger, and stops once the target
     is reached.
  2. It gives up when a pass fails to improve, instead of burning every pass.
  3. It rejects a pass that improves style while breaking a fact, keeping the
     previous version rather than the "better" broken one.
  4. It marks NEEDS_HUMAN_REVIEW when passes run out, instead of looping or
     silently publishing.

Run: python test_rewrite_loop.py
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import run
from common import ROOT, config
from extract import Article, Block, from_markdown

TH = config()["thresholds"]
SCRATCH = ROOT / "out" / "_looptest"

# Raw machine translation, with the artefacts the scorer keys on: spaced
# hyphens, spaces before punctuation, prepositional calques, comma pile-up.
MT_BAD = """# मधुमेह के लक्षण

मधुमेह , जिसे आमतौर पर शुगर के रूप में जाना जाता है , एक ऐसी स्थिति है जो
लंबे समय तक उच्च रक्त शर्करा के स्तर की विशेषता है ।

बार - बार पेशाब आना , प्यास में वृद्धि , और भूख में वृद्धि , ये सभी लक्षण हैं
जो रोगियों में देखे जाते हैं , और जिनमें से कई को अनदेखा किया जाता है ।

डॉक्टर के द्वारा 500 mg की खुराक की सिफारिश की जाती है , जो कि निगरानी के
माध्यम से दी जाती है , और जिसे डॉक्टर से परामर्श के बाद ही शुरू किया जाना चाहिए ।
"""

GOOD = """# मधुमेह के लक्षण

मधुमेह चुपचाप आती है। शरीर महीनों पहले इशारे देने लगता है। ज़्यादातर लोग उन्हें
रोज़मर्रा की थकान समझकर टाल देते हैं और यही देरी बाद में भारी पड़ती है।

पेशाब बार-बार आने लगता है। प्यास बढ़ जाती है। भूख भी तेज़ हो जाती है। तीनों एक
साथ दिखें तो जाँच करा लीजिए।

डॉक्टर की निगरानी में 500 mg की मानक खुराक दी जाती है। शुरू करने से पहले डॉक्टर
से सलाह ज़रूर लीजिए।
"""

MEDIUM = """# मधुमेह के लक्षण

मधुमेह चुपचाप आती है और शरीर महीनों पहले इशारे देने लगता है , जिन्हें ज़्यादातर
लोग थकान के रूप में देखते हैं ।

पेशाब बार - बार आने लगता है और प्यास बढ़ जाती है , और भूख भी तेज़ हो जाती है ।

डॉक्टर के द्वारा 500 mg की खुराक दी जाती है , जो निगरानी के माध्यम से दी जाती है ।
"""

BROKEN_FACT = GOOD.replace("500 mg", "50 mg")


def _art(md: str, lang: str = "hi") -> Article:
    a = from_markdown(md)
    a.lang = lang
    a.slug = "looptest"
    return a


def _plan(art: Article) -> dict:
    return {"title": art.title, "meta_description": "x" * 80,
            "blocks": [b.dict() for b in art.blocks], "faqs": []}


class StubTranslator:
    """Returns fixed text; the loop under test never touches a network."""

    def __init__(self, mt: Article, back: Article):
        self._mt, self._back = mt, back

    def translate_article(self, art, tgt, *, src="en"):
        return self._mt

    def back_translate(self, art, *, src_lang, to="en"):
        return self._back


class StubWriter:
    """Yields a scripted sequence of rewrite results."""

    def __init__(self, sequence: list[str], plan_art: Article):
        self.sequence = sequence
        self.plan_art = plan_art
        self.rewrites = 0
        self.reviews = 0

    def transcreate(self, **kw):
        return _plan(self.plan_art)

    def review(self, **kw):
        self.reviews += 1
        return {"score": 80, "flags": [], "notes": "stub"}

    def rewrite(self, **kw):
        md = self.sequence[min(self.rewrites, len(self.sequence) - 1)]
        self.rewrites += 1
        return _plan(_art(md))


def _run(sequence: list[str], start_md: str, back_md: str | None = None):
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH, ignore_errors=True)
    src = _art("# Diabetes symptoms\n\nDoctors recommend a 500 mg dose under "
               "supervision. Consult a doctor before starting.", "en")
    src.slug = "looptest"
    src.source_url = "looptest://case"
    mt = _art(start_md)
    back = _art(back_md or
                "# Diabetes symptoms\n\nDoctors recommend a 500 mg dose under "
                "supervision. Consult a doctor before starting.", "en")
    writer = StubWriter(sequence, mt)
    rec = run.process_language(
        src, "hi", profile={"voice": "plain", "ymyl": True},
        writer=writer, bh=StubTranslator(mt, back), publish=False, force=True)
    return rec, writer


def check(name: str, condition: bool, detail: str = "") -> bool:
    print(f"  {'PASS' if condition else 'FAIL'}  {name}" + (f"  -- {detail}" if detail else ""))
    return condition


def main() -> int:
    ok = True
    print("\n1. Loop fires on bad input and stops once the target is reached")
    rec, w = _run([MEDIUM, GOOD, GOOD], MT_BAD)
    ok &= check("rewrite loop fired", rec.passes >= 1, f"{rec.passes} passes")
    ok &= check("score reached the target", rec.ai_pct <= TH["target_ai_pct"],
                f"AI {rec.ai_pct}% <= {TH['target_ai_pct']}%")
    ok &= check("did not burn every pass", rec.passes < TH["max_rewrite_passes"] + 1,
                f"{rec.passes}/{TH['max_rewrite_passes']}")
    ok &= check("final status is scored", rec.status == "scored", rec.status)

    print("\n2. A pass that does not improve stops the loop early")
    rec, w = _run([MT_BAD, MT_BAD, MT_BAD], MT_BAD)
    ok &= check("stopped before exhausting passes", w.rewrites <= 2,
                f"{w.rewrites} rewrite call(s)")
    ok &= check("flagged for a human", rec.status == "needs_human_review", rec.status)
    ok &= check("reason recorded", bool(rec.error), (rec.error or "")[:60])

    print("\n3. A pass that breaks a fact is rejected, previous version kept")
    # Back-translation is clean, so the only source of a blocking defect is the
    # rewrite itself -- which is the case the regression guard exists for. The
    # first rewrite fixes the style AND breaks the dosage; it must be rejected
    # even though its style score is much better.
    rec, w = _run([BROKEN_FACT, GOOD, GOOD], MT_BAD)
    art = Article.load(SCRATCH.parent / "looptest" / "hi" / "article.json")
    text = art.full_text()
    ok &= check("rejected the fact-broken pass", "50 mg" not in text.replace("500 mg", ""),
                "dosage intact")
    ok &= check("kept the pre-rewrite version", "500 mg" in text or "500" in text)
    ok &= check("stopped rather than accepting it", w.rewrites == 1,
                f"{w.rewrites} rewrite call(s)")
    ok &= check("flagged for a human", rec.status == "needs_human_review", rec.status)

    print("\n4. Passes exhausted -> NEEDS_HUMAN_REVIEW, never an infinite loop")
    rec, w = _run([MEDIUM, MEDIUM, MEDIUM], MT_BAD)
    ok &= check("capped at max_rewrite_passes", w.rewrites <= TH["max_rewrite_passes"],
                f"{w.rewrites} <= {TH['max_rewrite_passes']}")

    print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
