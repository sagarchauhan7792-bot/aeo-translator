"""Fix-it: turn an audit report into a corrected post.

The audit already knows the title is 16 characters and the meta description is
missing. The writer backend can already write. Until now nothing joined those
two facts, so the tool produced homework rather than work.

Fix-it takes the findings, asks the writer for exactly the repairs the audit
asked for, re-audits, and reports the before and after. The improvement is
measured, not claimed.

Two rules the rewrite cannot break, enforced after the model replies and not
merely requested in the prompt:

  * `quality._term_lock_flags` -- no number, dosage, locked brand term or medical
    claim may change. A "fix" that raises the score by promising a cure is not a
    fix, and this is health content.
  * Nothing is applied unless the score actually improves. A model that returns
    something worse is discarded and the original kept.
"""
from __future__ import annotations

import json

import aeo
import en_detect
import quality
from common import config, log, site_profile, warn
from extract import Article, Block, is_question
from . import seo

CFG = config()
A = CFG["aeo"]

FIX_SCHEMA = {
    "title": "string -- <= 60 chars, primary keyword near the front, reads naturally",
    "meta_description": "string -- 70-155 chars, includes the keyword, gives a reason to click",
    "tldr_heading": "string",
    "tldr": ["string x5 -- one fact each, quotable with no context around it"],
    "blocks": [{"type": "h2|h3|p|li", "text": "string"}],
    "answers": {"<exact heading text>": "40-60 word direct answer"},
    "faqs": [{"q": "string", "a": "string"}],
    "images_alt": {"<image src>": "descriptive alt text"},
    "slug": "string -- lowercase, hyphenated",
    "changed": ["string -- one line per change you made"],
    "refused": ["string -- anything you were asked to do but would not, and why"],
}


def fix_prompt(art: Article, report, profile: dict,
               internal_links: list[dict] | None = None) -> str:
    """Ask for exactly the repairs the audit asked for, and nothing else."""
    issues = [f for f in report.findings if f["status"] in ("fail", "warn")]
    issue_lines = "\n".join(
        f"  - [{f['status']}] {f['group']}/{f['check']}: {f['message']}"
        + (f"\n      FIX: {f['fix']}" if f.get("fix") else "")
        for f in issues[:24])

    links = ""
    if internal_links:
        links = "\n".join(f"  - {l['url']}  (relevant because: {l.get('why', l.get('anchor', ''))})"
                          for l in internal_links[:5])

    ymyl = ""
    if profile.get("ymyl"):
        g = json.loads((__import__("pathlib").Path(__file__).resolve().parent.parent
                        / "glossary.json").read_text(encoding="utf-8"))
        guard = g["medical_claim_guard"]
        ymyl = f"""
THIS IS HEALTH CONTENT, and the following are checked automatically after you
reply. Breaking any of them means your version is discarded entirely:
  - Never introduce: {', '.join(guard['forbidden_new_claims'][:8])}
  - Never delete a hedge the text already has ("may help", "consult a doctor").
  - If the current text makes an absolute claim, REMOVE or soften it. Do not
    keep it to preserve the meaning.
  - Never invent a statistic, dosage, percentage or study. If a claim needs a
    figure you do not have, write it without one and list that under `refused`.
"""

    return f"""Repair this blog post. An SEO audit scored it {report.score}/100 ({report.grade}).

Fix the specific findings below. Do not rewrite the post into a different post:
keep the subject, the facts and the author's angle. You are repairing, not
replacing.

FINDINGS:
{issue_lines}

HARD CONSTRAINTS -- checked after you reply:
  - Every number, dosage, price, phone number and URL must survive unchanged.
  - Keep the locked brand and product names exactly as written.
  - Do not shorten the post. If it is thin, develop it with substance, not padding.
  - Do not add "Moreover", "Furthermore", "In conclusion", "It is important to
    note", or "In today's fast-paced world".
  - Vary sentence length deliberately. Uniform rhythm is the clearest sign of
    machine writing.
{ymyl}
ANSWER-ENGINE STRUCTURE -- apply all of it:
  - Every H2 becomes a question a real person would type or ask aloud.
  - Under each question heading, put a {A['answer_words_min']}-{A['answer_words_max']} word direct answer that still makes
    sense quoted with nothing around it. Key them by the exact heading text in `answers`.
  - Open with a {A['tldr_bullets']}-bullet TL;DR. Each bullet stands alone -- no "it", no "this".
  - Close with {A['faq_min']}-{A['faq_max']} FAQs.
  - Start each section by naming the subject, not with "This", "It" or "They".
    Retrieval systems quote a section on its own, and a dangling pronoun makes
    the whole section unusable.

BRAND VOICE: {profile.get('voice', 'clear, plain, conversational')}
{"INTERNAL LINKS to work in naturally where they help the reader:" + chr(10) + links if links else ""}

=== CURRENT POST ===
{aeo.render_markdown(art)}

Reply with JSON only:
{json.dumps(FIX_SCHEMA, indent=2)}"""


def apply_fix(art: Article, result: dict) -> Article:
    """Fold a fix response back into an Article, keeping untouched fields."""
    out = Article.from_dict(art.dict())
    if result.get("title"):
        out.title = result["title"].strip()
    if result.get("meta_description"):
        out.meta_description = result["meta_description"].strip()
    if result.get("slug"):
        from common import slugify
        out.slug = slugify(result["slug"])

    blocks = [Block(type=b.get("type", "p"), text=(b.get("text") or "").strip())
              for b in (result.get("blocks") or []) if (b.get("text") or "").strip()]
    if blocks:
        out.blocks = blocks
    if result.get("faqs"):
        out.faqs = [f for f in result["faqs"]
                    if f.get("q", "").strip() and f.get("a", "").strip()]
    if result.get("images_alt"):
        out.images = [{**im, "alt": result["images_alt"].get(im.get("src", ""), im.get("alt", ""))}
                      for im in out.images]

    return aeo.apply_structure(out, {
        "tldr_heading": result.get("tldr_heading") or "The short version",
        "tldr": result.get("tldr") or [],
        "answers": result.get("answers") or {},
        "faqs": out.faqs,
    })


def _safety(before: Article, after: Article) -> tuple[list[dict], list[dict]]:
    """Split guard flags into blocking and review-worthy.

    Fixing is not translating, and the number rules differ. In translation, a
    dropped number means a lost fact and must block. In a fix, dropping a number
    is often the *point* -- removing "100% guaranteed cure" removes the 100, and
    that is the single most valuable change the tool can make to health copy.

    So: inventing a number always blocks, and dropping one is surfaced for the
    operator to glance at. Locked terms and medical claims block either way.
    """
    flags = quality._term_lock_flags(before.full_text(), after.full_text())
    blocking, review = [], []
    for f in flags:
        d = f.dict()
        if f.severity != "error":
            review.append(d)
        elif f.kind == "protected_span" and "dropped" in f.detail:
            review.append({**d, "severity": "review",
                           "detail": d["detail"].replace("dropped between source and "
                                                         "translation", "removed")})
        else:
            blocking.append(d)
    return blocking, review


def run(art: Article, report, *, writer, site: str = "", keyword: str = "",
        site_index: dict | None = None,
        internal_links: list[dict] | None = None,
        base_url: str = "") -> dict:
    """Fix, verify, and report before/after. Returns the original if it got worse."""
    profile = site_profile(art.source_url or site)
    base = base_url or CFG["aeo"]["hreflang_base"]

    log(f"fix: repairing '{art.title[:50]}' from {report.score}/100")
    prompt = fix_prompt(art, report, profile, internal_links)
    fixed = None
    blocking: list[dict] = []
    review: list[dict] = []
    result: dict = {}

    # One retry, with the exact violation fed back. The first real run of this
    # invented five statistics (a 279388 among them) despite the prompt
    # forbidding it -- which is precisely why the check runs after the reply
    # rather than trusting the instruction.
    for attempt in range(2):
        result = writer.generate(prompt, stage="fix", slug=art.slug or "post",
                                 lang="en", salt=f"a{attempt}")
        candidate = apply_fix(art, result)
        blocking, review = _safety(art, candidate)
        if not blocking:
            fixed = candidate
            break
        warn(f"fix attempt {attempt + 1} rejected: {blocking[0]['detail'][:70]}")
        if attempt == 0:
            prompt += ("\n\n=== YOUR PREVIOUS ATTEMPT WAS REJECTED ===\n"
                       + "\n".join(f"  - {b['detail']}" for b in blocking[:8])
                       + "\nYou introduced figures that are not in the source. Write the "
                         "post WITHOUT any statistic, percentage, year or count that does "
                         "not already appear in the original text. Removing a number is "
                         "fine. Inventing one is not.")

    if fixed is None:
        return {"applied": False, "reason": "safety", "blocking": blocking,
                "before": report.dict(), "after": None, "article": art.dict(),
                "changed": result.get("changed", []),
                "refused": result.get("refused", [])}

    # --- schema, then re-audit -------------------------------------------
    url = f"{base.rstrip('/')}/{fixed.slug}/"
    fixed.meta = dict(fixed.meta or {})
    fixed.meta["schema"] = aeo.build_schema(fixed, profile, "en", url)

    after = seo.audit(fixed, keyword=keyword, site_index=site_index,
                      base_url=base, check_links=False)

    if after.score <= report.score:
        warn(f"fix did not improve the score ({report.score} -> {after.score}); "
             "keeping the original")
        return {"applied": False, "reason": "no_improvement", "review": review,
                "before": report.dict(), "after": after.dict(),
                "article": art.dict(),
                "changed": result.get("changed", []),
                "refused": result.get("refused", [])}

    # AI-likeness gate: the SEO fix succeeded on its own terms, but the
    # calibrated English detector (en_detect.py; AUC 1.00 on its own test set,
    # not a QuillBot/GPTZero result -- neither has a usable free API) may still
    # flag it as reading generated. One extra humanising pass, kept only if it
    # does not cost any SEO score.
    ai = en_detect.score(fixed.full_text())
    if not ai.all_passed:
        findings = [f"[warn] {p['label']} -- scored {p['score']:.0f}/100"
                    for p in ai.parameters if not p["passed"]]
        log(f"fix: AI-likeness {ai.ai_likeness:.0f}% after the SEO fix "
            f"({len(findings)} parameter(s) failing); one humanising pass", indent=1)
        try:
            from . import draft as draftmod
            h_result = writer.generate(
                draftmod.humanize_prompt(fixed, findings, profile),
                stage="fix_humanize", slug=fixed.slug or "post", lang="en")
            h_candidate = apply_fix(fixed, h_result)
            h_blocking, _ = _safety(fixed, h_candidate)
            if h_blocking:
                warn(f"humanising pass rejected: {h_blocking[0]['detail'][:70]}")
            else:
                h_ai = en_detect.score(h_candidate.full_text())
                h_after = seo.audit(h_candidate, keyword=keyword, site_index=site_index,
                                    base_url=base, check_links=False)
                # Only keep it if BOTH scores hold or improve -- a rewrite that
                # buys a lower AI-likeness number by breaking the SEO fix is a
                # net loss, not a win.
                if h_ai.ai_likeness <= ai.ai_likeness and h_after.score >= after.score:
                    fixed, after, ai = h_candidate, h_after, h_ai
                    fixed.meta = dict(fixed.meta or {})
                    fixed.meta["schema"] = aeo.build_schema(fixed, profile, "en", url)
                    log(f"fix: humanising pass -> AI-likeness {ai.ai_likeness:.0f}%, "
                        f"SEO {after.score}", indent=2)
                else:
                    log("fix: humanising pass did not help without cost; kept "
                        "the SEO-fixed version", indent=2)
        except Exception as exc:
            if exc.__class__.__name__ == "WriterPending":
                raise
            warn(f"fix: humanising pass skipped ({exc.__class__.__name__})")

    log(f"fix: {report.score} -> {after.score} (+{after.score - report.score}) | "
        f"AI-likeness {ai.ai_likeness:.0f}%", indent=1)
    return {
        "applied": True,
        "review": review,
        "before": report.dict(),
        "after": after.dict(),
        "delta": after.score - report.score,
        "ai_detect": ai.dict(),
        "article": fixed.dict(),
        "schema": fixed.meta["schema"],
        "html": aeo.render_html(fixed, schema=fixed.meta["schema"], canonical=url),
        "markdown": aeo.render_markdown(fixed),
        "changed": result.get("changed", []),
        "refused": result.get("refused", []),
        "slug": fixed.slug,
    }
