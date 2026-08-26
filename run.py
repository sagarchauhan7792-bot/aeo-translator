"""Orchestrator: ingest -> translate -> transcreate -> score -> rewrite -> publish.

Resumable throughout. Every expensive step writes its result to out/<slug>/<lang>/
and the ledger records what finished, so re-running after an interruption, a
credential fix, or a /aeo-rewrite pass picks up exactly where it stopped rather
than starting over.

  python run.py --url https://hiims.in/blog/... --langs hi
  python run.py --sitemap hiims.in --filter /blog --limit 5 --langs hi,mr,ta
  python run.py --file mypost.md --langs all --no-publish
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

import aeo
import quality
import sources
from common import (ROOT, JobRecord, MissingCredential, config, job_key, ledger_append,
                    ledger_load, lang_by_code, log, site_profile, warn, write_json,
                    write_text)
from extract import Article, Block
from writer import get_writer
from writer.base import WriterPending, WriterUnavailable

CFG = config()
TH = CFG["thresholds"]
OUT = ROOT / "out"


# --------------------------------------------------------------------- paths

def workdir(slug: str, lang: str) -> Path:
    d = OUT / slug / lang
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load(path: Path) -> Article | None:
    return Article.load(path) if path.exists() else None


# ------------------------------------------------------------------ per lang

def process_language(src: Article, lang: str, *, profile: dict, writer,
                     bh, publish: bool, force: bool = False) -> JobRecord:
    """Run one (article, language) pair as far as it can get."""
    slug, key = src.slug, job_key(src.source_url or src.slug, lang)
    d = workdir(slug, lang)
    rec = JobRecord(key=key, source=src.source_url or src.slug, slug=slug, lang=lang)
    entry = lang_by_code(lang)
    tgt = entry["bhashini"]

    log(f"--- {lang} ({entry['name']}) ---")

    # 1. translate ---------------------------------------------------------
    mt = None if force else _load(d / "mt.json")
    if mt is None:
        mt = bh.translate_article(src, tgt, src="en")
        mt.save(d / "mt.json")
    log(f"translated: {mt.words()} words", indent=1)

    # Keywords are seeded from the TRANSLATED headings. Seeding from the English
    # ones and appending Hindi question stems yields "diabetes symptoms के लक्षण",
    # which is not a query anyone types.
    import keywords as kwmod
    keywords = kwmod.build(mt, lang)
    kwmod.save_csv(keywords, d / "keywords.csv")

    # 2. back-translate for the fidelity check ------------------------------
    back = None if force else _load(d / "back.json")
    if back is None:
        back = bh.back_translate(mt, src_lang=tgt, to="en")
        back.save(d / "back.json")

    # 3. transcreate + AEO structure ---------------------------------------
    plan_path = d / "plan.json"
    if force or not plan_path.exists():
        plan = writer.transcreate(
            lang=lang, slug=slug,
            source_md=aeo.render_markdown(src),
            mt_md=aeo.render_markdown(mt),
            profile=profile, keywords=keywords, aeo_cfg=CFG["aeo"])
        write_json(plan_path, plan)
    else:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))

    art = aeo.apply_structure(mt, plan)
    art.lang = lang
    art.slug = plan.get("slug_roman") or aeo.roman_slug(art.title, lang)
    art.author = src.author
    art.author_credentials = src.author_credentials
    art.published = src.published
    art.source_url = src.source_url
    art.keywords = keywords

    # 4. score --------------------------------------------------------------
    honorific = entry.get("honorific", "formal")
    report = quality.score_article(art, src, back, lang, expected_honorific=honorific)
    log(f"first pass: {report.summary()}", indent=1)

    # 5. rewrite loop -------------------------------------------------------
    # Fires ONLY on a real content-integrity defect (a dropped/invented number,
    # a wrong locked term, a lost hedge, mixed script, an empty block) -- never
    # on the AI-likeness score alone. ai_pct is measured and shown throughout
    # this pipeline as information, but publishing does not wait on it; see
    # quality.score_article's `passed` for the one thing that still gates.
    #
    # This used to also loop while ai_pct was above a target, asking the model
    # to "sound more native". That framing was the problem: chasing a lower
    # detection score, the model invented statistics not present in the
    # source to sound more authoritative -- which then tripped the blocking
    # check anyway. Looping on blocking alone removes the incentive that
    # caused it. The regression guard below compares WHICH defects are
    # present between passes, not just the count -- a raw count can drop
    # while a rewrite trades one real defect (a wrong number) for a
    # different one (a lost hedge), which test_rewrite_loop.py case 4 caught.
    passes = 0
    while report.blocking and passes < TH["max_rewrite_passes"]:
        passes += 1
        log(f"rewrite pass {passes} -- fixing {len(report.blocking)} "
            f"blocking defect(s)", indent=1)
        result = writer.rewrite(
            lang=lang, slug=slug, text_md=aeo.render_markdown(art),
            brief=quality.rewrite_brief(report), profile=profile,
            ai_pct=report.ai_pct, target=TH["target_ai_pct"], attempt=passes)
        write_json(d / f"rewrite_{passes}.json", result)

        candidate = _apply_rewrite(art, result)
        new_report = quality.score_article(candidate, src, back, lang,
                                           expected_honorific=honorific)

        # Compare WHICH defects are present, not just how many. A raw count
        # comparison lets a rewrite trade two number defects for one lost
        # hedge and call it progress (2 -> 1 looks like an improvement) --
        # caught by test_rewrite_loop.py case 4. A rewrite that fixes some
        # defects while introducing a defect that was not there before is
        # rejected outright, regardless of the net count.
        old_sigs = {(f["kind"], f.get("sample", "")) for f in report.blocking}
        new_sigs = {(f["kind"], f.get("sample", "")) for f in new_report.blocking}
        introduced = new_sigs - old_sigs
        if introduced:
            new_kind = next(f for f in new_report.blocking
                            if (f["kind"], f.get("sample", "")) in introduced)
            warn(f"pass {passes} introduced a new blocking defect "
                 f"({new_kind['detail'][:70]}); keeping previous version")
            break
        if len(new_sigs) >= len(old_sigs):
            log(f"pass {passes} did not reduce the blocking defects; stopping", indent=2)
            break
        art, report = candidate, new_report
        log(f"after pass {passes}: {report.summary()} "
            f"({len(report.blocking)} blocking defect(s) left)", indent=2)

    # 6. independent native review, then final score ------------------------
    try:
        review = writer.review(lang=lang, slug=slug,
                               text=aeo.render_markdown(art), salt=f"p{passes}")
        write_json(d / "review.json", review)
        report = quality.score_article(art, src, back, lang, review=review,
                                       expected_honorific=honorific)
        log(f"after review: {report.summary()}", indent=1)
    except WriterPending:
        raise
    except Exception as exc:
        warn(f"review pass failed ({exc.__class__.__name__}); scoring without it")

    # 7. render -------------------------------------------------------------
    url = _public_url(profile, art, lang)
    schema = aeo.build_schema(art, profile, lang, url)
    art.meta["schema"] = schema
    report = quality.score_article(art, src, back, lang,
                                   review=_maybe(d / "review.json"),
                                   expected_honorific=honorific)

    art.save(d / "article.json")
    write_text(d / "article.html", aeo.render_html(art, schema=schema, canonical=url))
    write_text(d / "article.md", aeo.render_markdown(art))
    write_json(d / "report.json", report.dict())

    rec.passes = passes
    rec.ai_pct = report.ai_pct
    rec.human_likeness = report.human_likeness
    rec.fidelity = report.subs["fidelity"]["score"]
    rec.grammar = report.subs["grammar"]["score"]
    rec.aeo = report.aeo
    rec.words = report.words

    # report.passed == (not report.blocking): a real content-integrity defect,
    # not the AI-likeness score. A high ai_pct alone never lands here.
    if report.passed:
        rec.status = "scored"
    else:
        rec.status = "needs_human_review"
        rec.error = "; ".join(b["detail"] for b in report.blocking[:2])
        warn(f"{lang}: NEEDS HUMAN REVIEW -- {rec.error}")

    # 8. publish ------------------------------------------------------------
    if publish:
        from publish_gdocs import publish_article
        rec.doc_url = publish_article(art, report, profile)
        rec.status = "published" if report.passed else "needs_human_review"
        log(f"doc: {rec.doc_url}", indent=1)

    return rec


def _maybe(path: Path) -> dict | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _apply_rewrite(art: Article, result: dict) -> Article:
    """Fold a rewrite response back in, keeping everything it did not touch."""
    out = Article.from_dict(art.dict())
    if result.get("title"):
        out.title = result["title"].strip()
    if result.get("meta_description"):
        out.meta_description = result["meta_description"].strip()
    if result.get("blocks"):
        out.blocks = [Block(type=b.get("type", "p"), text=(b.get("text") or "").strip())
                      for b in result["blocks"] if (b.get("text") or "").strip()]
    if result.get("faqs"):
        out.faqs = [f for f in result["faqs"]
                    if f.get("q", "").strip() and f.get("a", "").strip()]
    return out


def _public_url(profile: dict, art: Article, lang: str) -> str:
    base = (profile.get("hreflang_base") or CFG["aeo"]["hreflang_base"]).rstrip("/")
    return f"{base}/{lang}/{art.slug}/"


# ---------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", help="a manually supplied post (.md/.html/.txt)")
    src.add_argument("--url", help="one live blog URL")
    src.add_argument("--sitemap", help="a sitemap URL or a bare domain")
    ap.add_argument("--filter", default="", help="substring a sitemap URL must contain")
    ap.add_argument("--limit", type=int, default=0, help="max articles from a sitemap")
    ap.add_argument("--langs", default="hi",
                    help="comma-separated codes, or 'all'")
    ap.add_argument("--no-publish", action="store_true",
                    help="write local files only; skip Google Docs and the Sheet")
    ap.add_argument("--force", action="store_true",
                    help="ignore cached intermediates and redo every step")
    ap.add_argument("--writer", default=None, help="force a writer backend")
    ap.add_argument("--engine", default="auto",
                    choices=["auto", "bhashini", "gemini", "openai", "mymemory"],
                    help="translation engine. auto picks the best one this "
                         "install has credentials for: bhashini, then gemini or "
                         "openai (which translate for register rather than word "
                         "for word), then mymemory -- a rate-limited test "
                         "stand-in for verifying the pipeline, not for shipping")
    args = ap.parse_args()

    langs = ([e["code"] for e in CFG["languages"]] if args.langs == "all"
             else [s.strip() for s in args.langs.split(",") if s.strip()])
    for code in langs:
        lang_by_code(code)                    # fail fast on a typo

    articles = sources.resolve(args)
    if not articles:
        warn("nothing to do")
        return 1

    try:
        writer = get_writer(args.writer)
        log(f"writer backend: {writer.name}")
    except WriterUnavailable as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 2

    from translate import client as bh_client, best_engine
    engine = best_engine() if args.engine == "auto" else args.engine
    if args.engine == "auto":
        log(f"translation engine: {engine}")
    bh = bh_client(engine)
    try:
        bh.require_creds()
    except MissingCredential as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 2

    done = ledger_load()
    pending_packets: list[str] = []
    results: list[JobRecord] = []
    titles: dict[str, str] = {}

    for art in articles:
        log(f"\n===== {art.title[:70]} ({art.words()} words) =====")
        art.save(workdir(art.slug, "en") / "source.json")

        for lang in langs:
            key = job_key(art.source_url or art.slug, lang)
            if not args.force and done.get(key, {}).get("status") == "published":
                log(f"--- {lang}: already published, skipping ---")
                continue

            try:
                rec = process_language(
                    art, lang, profile=site_profile(art.source_url), writer=writer,
                    bh=bh, publish=not args.no_publish, force=args.force)
            except WriterPending as exc:
                pending_packets.extend(exc.packets)
                log(f"--- {lang}: waiting on the writer ---")
                continue
            except MissingCredential as exc:
                print(f"\n{exc}\n", file=sys.stderr)
                return 2
            except Exception as exc:
                warn(f"{lang} failed: {exc.__class__.__name__}: {exc}")
                traceback.print_exc(limit=3)
                rec = JobRecord(key=key, source=art.source_url or art.slug,
                                slug=art.slug, lang=lang, status="failed",
                                error=f"{exc.__class__.__name__}: {exc}")

            ledger_append(rec)
            results.append(rec)
            titles[rec.key] = art.title

    # The app writes a tracker row on every published run; the CLI did not,
    # so a `python run.py` publish left a Doc in Drive and no record of it
    # anywhere except the local ledger -- which is the one place that does not
    # survive a host with no disk. Same call, same place in the flow.
    if not args.no_publish and results:
        try:
            import sheet
            sheet.append(results, titles)
        except MissingCredential as exc:
            warn(f"tracker sheet not updated: {exc.what}")
        except Exception as exc:
            warn(f"tracker sheet not updated ({exc.__class__.__name__}): {exc}")

    # ------------------------------------------------------------- summary
    print("\n" + "=" * 78)
    if results:
        print(f"{'lang':10}{'status':22}{'AI%':>7}{'HLS':>7}{'AEO':>7}{'fid':>7}"
              f"{'passes':>8}{'words':>7}")
        for r in results:
            print(f"{r.lang:10}{r.status:22}"
                  f"{(r.ai_pct if r.ai_pct is not None else 0):>7.1f}"
                  f"{(r.human_likeness or 0):>7.1f}{(r.aeo or 0):>7.1f}"
                  f"{(r.fidelity or 0):>7.1f}{r.passes:>8}{(r.words or 0):>7}")
        bad = [r for r in results if r.status in ("needs_human_review", "failed")]
        if bad:
            print(f"\n{len(bad)} of {len(results)} need attention:")
            for r in bad:
                print(f"  {r.lang:10} {r.error}")

    if pending_packets:
        print(f"\n{len(pending_packets)} work packet(s) are waiting for the writer.")
        print("  Run  /aeo-rewrite  in this Claude Code session, then re-run this")
        print("  same command. Translation results are cached, so nothing is redone.")
        return 3

    return 0 if all(r.status == "published" or r.status == "scored" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
