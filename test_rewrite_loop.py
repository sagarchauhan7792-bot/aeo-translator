"""Control-flow tests for the rewrite loop.

Rewritten after the loop stopped triggering on the AI-likeness score (see
run.py's rewrite-loop comment and quality.py's `passed`): looping to chase a
lower detection score is what caused the model to invent statistics on this
project, since "sound more native" has no ceiling and the model kept adding
authoritative-sounding numbers that were not in the source. The loop now fires
only on a genuine content-integrity defect -- a protected number changed, a
locked term dropped, a hedge lost -- and ai_pct is informational only.

Four behaviours that matter and are easy to get wrong:

  1. A high AI-likeness score alone, with no content defect, never fires the
     loop and never blocks publishing -- this is the behaviour that changed.
  2. A real defect fires the loop, and it stops once the defect is fixed.
  3. A pass that fails to reduce the defect count stops the loop early,
     instead of burning every remaining pass on a fix that isn't working.
  4. A pass that fixes the original defect but introduces a different one is
     rejected outright -- the previous (still-flawed, but not worse) version
     is kept rather than trading one defect for another.

Run: python test_rewrite_loop.py
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import run
from common import ROOT, config
from extract import Article, from_markdown

TH = config()["thresholds"]
SCRATCH = ROOT / "out" / "_looptest"

SRC_MD = ("# Diabetes symptoms\n\nDoctors recommend a 500 mg dose under "
          "supervision. Consult a doctor before starting.")

# Correct dosage, hedge present, but written with every raw-MT tell the style
# scorer keys on (spaced hyphens, spaces before punctuation, comma pile-up).
# No content defect -- this is deliberately "reads very AI-generated but is
# factually and safely correct", the exact case the old loop got wrong.
STYLE_BAD_BUT_SAFE = """# मधुमेह के लक्षण

मधुमेह , जिसे आमतौर पर शुगर के रूप में जाना जाता है , एक ऐसी स्थिति है जो
लंबे समय तक उच्च रक्त शर्करा के स्तर की विशेषता है ।

बार - बार पेशाब आना , प्यास में वृद्धि , और भूख में वृद्धि , ये सभी लक्षण हैं
जो रोगियों में देखे जाते हैं ।

डॉक्टर के द्वारा 500 mg की खुराक की सिफारिश की जाती है , जो कि निगरानी के
माध्यम से दी जाती है , और जिसे डॉक्टर से परामर्श के बाद ही शुरू किया जाना चाहिए ।
"""

# Clean prose, correct dosage, hedge present. What a fix pass should produce.
CLEAN = """# मधुमेह के लक्षण

मधुमेह चुपचाप आती है। शरीर महीनों पहले इशारे देने लगता है।

पेशाब बार-बार आने लगता है। प्यास बढ़ जाती है। भूख भी तेज़ हो जाती है।

डॉक्टर की निगरानी में 500 mg की मानक खुराक दी जाती है। शुरू करने से पहले डॉक्टर
से सलाह ज़रूर लीजिए।
"""

# Same as CLEAN, but the dosage is wrong -- 50 mg instead of 500 mg. A real
# content-integrity defect: diff_numbers(src, translated) catches this
# directly against the CURRENT candidate text, which is why it can be fixed
# (or broken further) by a rewrite pass, unlike the back-translation checks
# that are frozen at the start of the run.
BAD_DOSAGE = CLEAN.replace("500 mg", "50 mg")

# Correct dosage, but every hedge marker removed -- "consult a doctor" gone
# entirely. A different defect from BAD_DOSAGE, used to prove a rewrite that
# fixes one defect while introducing another is rejected, not accepted.
NO_HEDGE = ("# मधुमेह के लक्षण\n\nमधुमेह चुपचाप आती है। शरीर महीनों पहले इशारे "
           "देने लगता है।\n\nपेशाब बार-बार आने लगता है। प्यास बढ़ जाती है।\n\n"
           "500 mg की मानक खुराक दी जाती है।\n")

BACK_OK = SRC_MD  # a clean, faithful back-translation for every case


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


def _run(sequence: list[str], start_md: str, back_md: str = BACK_OK):
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH, ignore_errors=True)
    src = _art(SRC_MD, "en")
    src.slug = "looptest"
    src.source_url = "looptest://case"
    mt = _art(start_md)
    back = _art(back_md, "en")
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

    print("\n1. Bad style, no content defect: loop never fires, publishes anyway")
    rec, w = _run([CLEAN, CLEAN, CLEAN], STYLE_BAD_BUT_SAFE)
    ok &= check("high AI-likeness measured (proves this fixture is genuinely bad style)",
               rec.ai_pct > TH["rewrite_trigger_ai_pct"], f"AI {rec.ai_pct}%")
    ok &= check("loop did not fire despite that", rec.passes == 0, f"{rec.passes} passes")
    ok &= check("no rewrite call was made", w.rewrites == 0, f"{w.rewrites} call(s)")
    ok &= check("status is scored, not held for review", rec.status == "scored", rec.status)

    print("\n2. Real defect (wrong dosage): loop fires and fixes it")
    rec, w = _run([CLEAN, CLEAN, CLEAN], BAD_DOSAGE)
    ok &= check("rewrite loop fired", rec.passes >= 1, f"{rec.passes} passes")
    ok &= check("defect is gone", rec.status == "scored", rec.status)
    ok &= check("did not burn every pass", rec.passes < TH["max_rewrite_passes"],
               f"{rec.passes}/{TH['max_rewrite_passes']}")

    print("\n3. A pass that never fixes the defect stops the loop early")
    rec, w = _run([BAD_DOSAGE, BAD_DOSAGE, BAD_DOSAGE], BAD_DOSAGE)
    ok &= check("stopped well before exhausting passes", w.rewrites <= 2,
               f"{w.rewrites} rewrite call(s)")
    ok &= check("flagged for a human", rec.status == "needs_human_review", rec.status)
    ok &= check("reason recorded", bool(rec.error), (rec.error or "")[:60])

    print("\n4. A pass that fixes the defect but introduces a new one is rejected")
    rec, w = _run([NO_HEDGE, CLEAN, CLEAN], BAD_DOSAGE)
    art = Article.load(SCRATCH.parent / "looptest" / "hi" / "article.json")
    text = art.full_text()
    ok &= check("did not accept the hedge-dropping rewrite",
               "डॉक्टर" in text or "सलाह" in text, "hedge marker present")
    ok &= check("stopped rather than trading one defect for another",
               w.rewrites == 1, f"{w.rewrites} rewrite call(s)")
    ok &= check("flagged for a human", rec.status == "needs_human_review", rec.status)

    print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
