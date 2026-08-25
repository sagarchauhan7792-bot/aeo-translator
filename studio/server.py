"""Blog Studio HTTP server: stdlib only.

No Flask, no FastAPI, no build step. This machine blocks admin installs, and
`http.server.ThreadingHTTPServer` plus one static page is enough for a
single-user local tool. Every dependency the pipeline needs is already present.

Binds to 127.0.0.1 only. The app can start jobs that spend money-adjacent quota
and write to Google Drive, so it must not be reachable from the network.
"""
from __future__ import annotations

import json
import mimetypes
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from common import (ROOT, MissingCredential, config, ledger_load, log,
                    site_profile, read_json, write_json)
from extract import Article
from . import auth
from .jobs import RUNNER

UI_DIR = Path(__file__).resolve().parent / "ui"   # optional extra assets
INDEX = ROOT / "index.html"
DRAFTS = ROOT / "drafts"
BRIEFS = ROOT / "briefs"
CFG = config()


# --------------------------------------------------------------- capability

def capabilities() -> dict:
    """What this install can actually do right now, and what is missing.

    The UI shows this on load so a missing credential is visible before someone
    starts a job that cannot finish, rather than after.
    """
    caps: dict = {"stages": {}, "missing": [], "writer": None}

    try:
        from writer import get_writer
        w = get_writer()
        caps["writer"] = w.name
        caps["writer_inline"] = w.name != "claude_local"
    except Exception as exc:
        caps["writer"] = None
        caps["writer_inline"] = False
        caps["missing"].append({"what": "writer backend", "detail": str(exc)})

    from translate import client
    bh = client()
    has_bhashini = bool(bh.user_id and bh.api_key)
    if not has_bhashini:
        caps["missing"].append({
            "what": "Bhashini credentials",
            "detail": "Translation needs a free ULCA user id and API key from "
                      "bhashini.gov.in. Until then use the mymemory test engine."})

    import keywords as kwmod
    has_ads = kwmod.YAML_PATH.exists() and bool(kwmod._customer_id())
    if not has_ads:
        caps["missing"].append({
            "what": "Google Ads (keyword volumes)",
            "detail": "Volume, competition and CPC columns stay empty. "
                      "Nothing is estimated."})

    from gauth import CRED_PATH
    has_google = CRED_PATH.exists()
    if not has_google:
        caps["missing"].append({
            "what": "Google OAuth (Docs + Sheets)",
            "detail": "Publishing is unavailable. Runs still produce local "
                      "HTML, Markdown and JSON in out/."})

    from . import perf as _perf
    has_psi = bool(_perf.api_key())
    if not has_psi:
        caps["missing"].append({
            "what": "PageSpeed API key (Core Web Vitals)",
            "detail": "The anonymous endpoint's quota is exhausted, so LCP/CLS/INP "
                      "need a free key. Other performance checks still run."})

    caps["stages"] = {
        "site": True,
        "geo": True,
        "compete": True,
        "perf_basic": True,
        "perf_vitals": has_psi,
        "fix": caps["writer"] is not None,
        "report": True,
        "audit": True,                       # needs nothing at all
        "ideas": True,                       # autocomplete needs nothing
        "draft": caps["writer"] is not None,
        "translate": True,                   # mymemory works without credentials
        "publish": has_google,
    }
    caps["engines"] = ["bhashini"] if has_bhashini else []
    if caps["writer_inline"] and caps["writer"] == "gemini_free":
        # Reuses the same GEMINI_API_KEY already configured for the writer, so
        # it is the free fallback for the moment MyMemory's ~1000 word/day
        # anonymous quota runs out -- no separate signup needed.
        caps["engines"].append("gemini")
    caps["engines"].append("mymemory")
    caps["languages"] = [{"code": e["code"], "name": e["name"], "native": e["native"]}
                         for e in CFG["languages"]]
    caps["sites"] = [k for k in CFG["site_profiles"] if not k.startswith("_")]
    caps["thresholds"] = {k: v for k, v in CFG["thresholds"].items()
                          if not k.startswith("_")}
    return caps


# ------------------------------------------------------------------ actions

def _slugfile(d: Path, slug: str, ext: str = "json") -> Path:
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{slug}.{ext}"


def act_ideas(body: dict, job) -> dict:
    from . import ideas
    brief = ideas.build_brief(
        body.get("topic", "").strip(),
        site=body.get("site", "").strip(),
        lang=body.get("lang", "en"),
        deep=bool(body.get("deep")),
        limit=int(body.get("limit") or 40))
    from common import slugify
    slug = slugify(brief.topic)
    write_json(_slugfile(BRIEFS, slug), brief.dict())
    return {"brief": brief.dict(), "slug": slug}


def act_audit(body: dict, job) -> dict:
    """Front door: a pasted / uploaded / fetched post -> full SEO audit."""
    from . import seo, ideas
    import sources
    import aeo as aeomod
    from extract import from_markdown

    site = (body.get("site") or "").strip()

    if body.get("url"):
        art = sources.load_url(body["url"].strip())
    elif body.get("slug"):
        art = Article.load(_slugfile(DRAFTS, body["slug"]))
    elif (body.get("text") or "").strip():
        art = from_markdown(body["text"], url=body.get("source_url", ""),
                            source_type="file")
    else:
        raise ValueError("paste a post, give a URL, or pick a draft")

    if body.get("title"):
        art.title = body["title"].strip()
    if body.get("meta_description"):
        art.meta_description = body["meta_description"].strip()

    # Generating the schema before auditing means the audit reports what the
    # page WOULD have once you paste the JSON-LD in, and hands you that block.
    profile = site_profile(art.source_url or site)
    base = ("https://" + site.replace("https://", "").replace("http://", "").rstrip("/")
            if site else (CFG["aeo"]["hreflang_base"]))
    generated = None
    if body.get("generate_schema", True):
        url = f"{base.rstrip('/')}/{art.slug}/"
        generated = aeomod.build_schema(art, profile, art.lang or "en", url)

    index = None
    if site and body.get("check_site", True):
        try:
            index = ideas.site_index(site)
        except Exception as exc:
            log(f"site index unavailable: {exc.__class__.__name__}")

    audited = Article.from_dict(art.dict())
    if generated:
        audited.meta = dict(audited.meta or {})
        audited.meta["schema"] = generated

    report = seo.audit(audited, keyword=(body.get("keyword") or "").strip(),
                       site_index=index, base_url=base,
                       check_links=bool(body.get("check_links", True)))
    # Report on what was pasted, not on the schema we just generated for them.
    raw = seo.audit(art, keyword=(body.get("keyword") or "").strip(),
                    site_index=index, base_url=base, check_links=False)

    from common import slugify
    slug = slugify(art.title or "audit")
    art.save(_slugfile(DRAFTS, slug))
    (DRAFTS / f"{slug}.md").write_text(aeomod.render_markdown(art), encoding="utf-8")

    return {"report": report.dict(), "as_pasted_score": raw.score,
            "article": art.dict(), "slug": slug,
            "serp": seo.serp_preview(art, base),
            "schema": generated,
            "html": aeomod.render_html(audited, schema=generated,
                                       canonical=f"{base.rstrip('/')}/{art.slug}/")}


MAX_HUMANIZE_PASSES = 3


def act_draft(body: dict, job) -> dict:
    from . import draft, english, fix as fixmod
    from writer import get_writer

    brief = body.get("brief")
    if not brief and body.get("slug"):
        brief = read_json(_slugfile(BRIEFS, body["slug"]), default=None)
    if not brief:
        raise ValueError("no brief supplied")

    writer = get_writer()
    art = draft.write(brief, writer=writer, site=body.get("site", ""),
                      words=int(body.get("words") or 900))
    profile = site_profile(body.get("site") or brief.get("site"))
    keywords = [q for q in (brief.get("target_queries") or []) if q][:6]

    def _review(a):
        try:
            return writer.generate(_review_prompt(a), stage="draft_review",
                                   slug=a.slug, lang="en")
        except Exception as exc:
            if exc.__class__.__name__ == "WriterPending":
                raise
            log(f"draft review skipped: {exc.__class__.__name__}")
            return None

    review = _review(art)
    report = english.score_draft(art, review=review)
    log(f"draft: first pass structure {report.score:.0f}, "
        f"AI-likeness {report.ai_likeness:.0f}%", indent=1)

    # Humanising rewrite loop: fires when the calibrated AI-likeness gate or a
    # keyword-density gap says so, not on a vague "make it more human" request.
    passes = 0
    while (not report.ai_all_passed or not report.passed) and passes < MAX_HUMANIZE_PASSES:
        passes += 1
        findings = english.rewrite_brief(report) + english.keyword_brief(art, keywords)
        if not findings:
            break
        log(f"draft: humanise pass {passes} "
            f"(AI-likeness {report.ai_likeness:.0f}%)", indent=1)
        result = writer.generate(
            draft.humanize_prompt(art, findings, profile, attempt=passes),
            stage="humanize", slug=art.slug, lang="en", salt=f"h{passes}")
        candidate = fixmod.apply_fix(art, result)

        blocking, _review_notes = fixmod._safety(art, candidate)
        if blocking:
            log(f"draft: pass {passes} rejected -- {blocking[0]['detail'][:70]}",
                indent=2)
            break

        cand_review = _review(candidate)
        cand_report = english.score_draft(candidate, review=cand_review)

        # Regression guard, on BOTH axes independently: a rewrite that trades
        # structure for a lower AI-likeness number (or the reverse) is a
        # regression, not an improvement, even if one number moved the right
        # way. Caught by testing: a first version of this only rejected when
        # NEITHER axis improved, which let a pass through that dropped
        # structure 52 -> 43 because AI-likeness happened to tick down 1 point.
        TOL = 1.0
        structure_worse = cand_report.score < report.score - TOL
        ai_worse = cand_report.ai_likeness > report.ai_likeness + TOL
        if structure_worse or ai_worse:
            log(f"draft: pass {passes} regressed "
                f"(structure {report.score:.0f}->{cand_report.score:.0f}, "
                f"AI-likeness {report.ai_likeness:.0f}->{cand_report.ai_likeness:.0f}%); "
                "keeping the previous version", indent=2)
            break
        if cand_report.ai_likeness >= report.ai_likeness and cand_report.score <= report.score:
            log(f"draft: pass {passes} made no real difference; stopping", indent=2)
            break
        art, review, report = candidate, cand_review, cand_report
        log(f"draft: after pass {passes} structure {report.score:.0f}, "
            f"AI-likeness {report.ai_likeness:.0f}%", indent=2)

    art.save(_slugfile(DRAFTS, art.slug))
    (DRAFTS / f"{art.slug}.md").write_text(
        __import__("aeo").render_markdown(art), encoding="utf-8")

    return {"article": art.dict(), "slug": art.slug, "report": report.dict(),
            "humanize_passes": passes,
            "claims": english.claim_audit(art)}


def _review_prompt(art: Article) -> str:
    import aeo
    return f"""You are an editor reading a blog post for the first time. You did not
write it and you have not seen a brief. Judge only what is in front of you.

Score 0-100 on whether this reads as written by a person who knows the subject,
rather than assembled to fill a word count. Be strict: 75 means "fine but you
can tell", 90+ means "genuinely good".

Flag specific sentences, never general impressions. Look for: uniform sentence
rhythm, sections that restate their own heading, padding phrases, claims made
without support, and answers that do not actually answer the question above them.

If a factual claim, number or dosage appears that a reader would need to trust,
flag it as severity "error" with kind "unsupported_claim" -- this post has no
source document behind it, so nothing can be checked automatically.

=== POST ===
{aeo.render_markdown(art)}

Reply with JSON only:
{{"score": 0-100,
  "flags": [{{"kind": "string", "severity": "error|warn|note",
              "detail": "what is wrong and what to write instead",
              "sample": "the offending text"}}],
  "notes": "string"}}"""


def act_translate(body: dict, job) -> dict:
    import run as pipeline
    import sources
    from common import job_key, ledger_append
    from translate import client as bh_client
    from writer import get_writer

    langs = body.get("langs") or ["hi"]
    engine = body.get("engine") or "mymemory"
    publish = bool(body.get("publish"))

    if body.get("slug"):
        art = Article.load(_slugfile(DRAFTS, body["slug"]))
    elif body.get("url"):
        art = sources.load_url(body["url"])
    elif body.get("text"):
        from extract import from_markdown
        art = from_markdown(body["text"], url=body.get("source_url", ""),
                            source_type="file")
    else:
        raise ValueError("give one of slug, url or text")

    art.save(pipeline.workdir(art.slug, "en") / "source.json")
    writer = get_writer()
    bh = bh_client(engine)
    bh.require_creds()

    profile = site_profile(art.source_url or body.get("site", ""))
    results, pending = [], []
    for lang in langs:
        try:
            rec = pipeline.process_language(art, lang, profile=profile, writer=writer,
                                            bh=bh, publish=publish,
                                            force=bool(body.get("force")))
            ledger_append(rec)
            results.append(rec.dict())
        except Exception as exc:
            if exc.__class__.__name__ == "WriterPending":
                pending.extend(getattr(exc, "packets", []))
                continue
            raise

    if publish and results:
        try:
            import sheet
            from common import JobRecord
            sheet.append([JobRecord(**{k: v for k, v in r.items()
                                       if k in JobRecord.__dataclass_fields__})
                          for r in results], {r["key"]: art.title for r in results})
        except MissingCredential as exc:
            log(f"tracker sheet not updated: {exc.what}")

    if pending:
        from writer.base import WriterPending
        raise WriterPending(pending)
    return {"results": results, "slug": art.slug}



def act_site(body: dict, job) -> dict:
    """Crawl a site and report what only a crawl can see."""
    from . import crawl as crawlmod
    site = (body.get("site") or "").strip()
    if not site:
        raise ValueError("give a site")
    limit = int(body.get("limit") or 0)
    url_filter = (body.get("filter") or "").strip()

    pages = crawlmod.crawl(
        site, limit=limit, url_filter=url_filter,
        delay=float(body.get("delay") or crawlmod.DEFAULT_DELAY),
        workers=int(body.get("workers") or crawlmod.DEFAULT_WORKERS),
        refresh=bool(body.get("refresh")))

    complete = not limit and not url_filter
    report = crawlmod.analyse(pages, crawlmod.host_of(
        site if "//" in site else f"https://{site}"), complete=complete,
        verify_links=int(body.get("verify_links") or 40))
    return {"site": report}


def act_geo(body: dict, job) -> dict:
    """AEO/GEO report for one page plus the site-level reach checks."""
    from . import geo as geomod
    import sources
    from extract import from_markdown

    site = (body.get("site") or "").strip()
    if body.get("url"):
        art = sources.load_url(body["url"].strip())
    elif body.get("slug"):
        art = Article.load(_slugfile(DRAFTS, body["slug"]))
    elif (body.get("text") or "").strip():
        art = from_markdown(body["text"])
    else:
        raise ValueError("paste a post, give a URL, or pick a draft")

    profile = site_profile(art.source_url or site)
    return {"geo": geomod.audit(art, site=site, brand=profile.get("brand") or "",
                                resolve_entities=bool(body.get("entities", True))),
            "title": art.title}


def act_perf(body: dict, job) -> dict:
    from . import perf as perfmod
    url = (body.get("url") or "").strip()
    if not url:
        raise ValueError("give a URL")
    return {"perf": perfmod.report(url, strategy=body.get("strategy") or "mobile")}


def act_compete(body: dict, job) -> dict:
    from . import compete as cmod
    import sources
    from extract import from_markdown

    if body.get("url"):
        mine = sources.load_url(body["url"].strip())
    elif body.get("slug"):
        mine = Article.load(_slugfile(DRAFTS, body["slug"]))
    elif (body.get("text") or "").strip():
        mine = from_markdown(body["text"])
    else:
        raise ValueError("give your page first")

    rivals = [u.strip() for u in (body.get("competitors") or []) if u.strip()][:3]
    if not rivals:
        raise ValueError("give at least one competitor URL")
    others = cmod.fetch_all(rivals)
    if not others:
        raise ValueError("none of the competitor URLs could be fetched")
    return {"compare": cmod.compare(mine, others)}


def act_fix(body: dict, job) -> dict:
    from . import fix as fixmod, seo, crawl as crawlmod
    import sources
    from extract import from_markdown
    from writer import get_writer

    site = (body.get("site") or "").strip()
    if body.get("slug"):
        art = Article.load(_slugfile(DRAFTS, body["slug"]))
    elif body.get("url"):
        art = sources.load_url(body["url"].strip())
    elif (body.get("text") or "").strip():
        art = from_markdown(body["text"])
    else:
        raise ValueError("nothing to fix")

    base = ("https://" + site.replace("https://", "").replace("http://", "").rstrip("/")
            if site else CFG["aeo"]["hreflang_base"])
    keyword = (body.get("keyword") or "").strip()
    before = seo.audit(art, keyword=keyword, base_url=base, check_links=False)

    links = None
    if site:
        cached = crawlmod.load_cached(site)
        if cached:
            from . import ideas
            idx = {"urls": [{"url": p.url, "slug": p.url.rstrip("/").rsplit("/", 1)[-1],
                             "tokens": sorted(crawlmod._tokens(p.title or ""))}
                            for p in cached if p.title]}
            status, url, score = ideas.classify(art.title, idx)
            if url:
                links = [{"url": url, "why": "closely related existing post"}]

    result = fixmod.run(art, before, writer=get_writer(), site=site,
                        keyword=keyword, internal_links=links, base_url=base)

    if result.get("applied"):
        from common import slugify
        slug = slugify(result["article"]["title"])
        fixed = Article.from_dict(result["article"])
        fixed.save(_slugfile(DRAFTS, slug))
        (DRAFTS / f"{slug}.md").write_text(result["markdown"], encoding="utf-8")
        result["slug"] = slug
    return result


def act_report(body: dict, job) -> dict:
    """Build the client-facing HTML report from whatever has been run."""
    from . import report as repmod
    import aeo as aeomod

    html = repmod.build(
        title=body.get("title") or "SEO report",
        url=body.get("url") or "",
        brand=body.get("brand") or "",
        audit=body.get("audit"), geo=body.get("geo"),
        perf=body.get("perf"), site=body.get("site"), fix=body.get("fix"))

    findings = list((body.get("audit") or {}).get("findings", []))
    findings += (body.get("geo") or {}).get("findings", [])

    from common import slugify
    name = slugify(body.get("title") or "report") or "report"
    out = ROOT / "reports"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{name}.html").write_text(html, encoding="utf-8")
    (out / f"{name}.csv").write_text(repmod.to_csv(findings), encoding="utf-8-sig")
    return {"html": html, "csv": repmod.to_csv(findings),
            "path": str(out / f"{name}.html"), "name": name}


ACTIONS = {"audit": act_audit, "ideas": act_ideas, "draft": act_draft,
           "translate": act_translate, "site": act_site, "geo": act_geo,
           "perf": act_perf, "compete": act_compete, "fix": act_fix,
           "report": act_report}


# ------------------------------------------------------------------ handler

class Handler(BaseHTTPRequestHandler):
    server_version = "BlogStudio"

    # Set by serve(); when False the app is localhost-only and needs no login.
    require_auth = False
    behind_tls = False
    # Origins allowed to call the API from another site. Empty by default:
    # same-origin only, exactly as before. Populated by --allow-origin, which
    # exists so the same index.html served as a static page elsewhere (GitHub
    # Pages) can drive a backend running here.
    allow_origins: frozenset = frozenset()

    def log_message(self, fmt, *args):        # quiet; jobs do the logging
        pass

    # ------------------------------------------------------------------ auth
    def _client(self) -> str:
        # Behind a tunnel every request arrives from 127.0.0.1, so the real
        # client is whatever the proxy put in the forwarding header. Only
        # trusted when we know we are behind one.
        if self.behind_tls:
            fwd = self.headers.get("CF-Connecting-IP") or self.headers.get("X-Forwarded-For")
            if fwd:
                return fwd.split(",")[0].strip()
        return self.client_address[0]

    def _session(self):
        if not self.require_auth:
            return {"name": "local"}
        return auth.session_from_headers(self.headers)

    def _guard(self) -> bool:
        """True if the request may proceed. Sends the rejection itself if not."""
        if not self.require_auth:
            return True
        if not auth.is_configured():
            # From this machine, offer to set the password rather than telling
            # the owner to go and use a terminal.
            if auth.is_local_request(self.client_address[0]) and not self.path.startswith("/api/"):
                return self._redirect("/setup") or False
            self._send(503, auth.SETUP_PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return False
        if self._session():
            return True
        if self.path.startswith("/api/"):
            self._json({"error": "not signed in", "login": "/login"}, 401)
        else:
            self.send_response(302)
            self.send_header("Location", "/login")
            self.send_header("Content-Length", "0")
            self.end_headers()
        return False

    # -- helpers ----------------------------------------------------------
    def _allowed_origin(self) -> str:
        """The request's Origin, if it is on the allowlist. Empty otherwise."""
        origin = (self.headers.get("Origin") or "").strip()
        return origin if origin and origin in self.allow_origins else ""

    def _cors(self) -> None:
        origin = self._allowed_origin()
        if not origin:
            return
        self.send_header("Access-Control-Allow-Origin", origin)
        # Without this, a shared cache could hand an allowed origin's response
        # to a request from a different one.
        self.send_header("Vary", "Origin")

    def _send(self, code: int, payload: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def _json(self, data, code: int = 200) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    # -- routes -----------------------------------------------------------
    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == "/setup":
            if auth.is_configured():
                return self._redirect("/login")
            if not auth.is_local_request(self.client_address[0]):
                return self._send(503, auth.SETUP_PAGE.encode("utf-8"),
                                  "text/html; charset=utf-8")
            return self._send(200, auth.first_run_page(), "text/html; charset=utf-8")

        if path == "/login":
            if not self.require_auth:
                return self._redirect("/")
            if not auth.is_configured():
                if auth.is_local_request(self.client_address[0]):
                    return self._redirect("/setup")
                return self._send(503, auth.SETUP_PAGE.encode("utf-8"),
                                  "text/html; charset=utf-8")
            return self._send(200, auth.login_page(), "text/html; charset=utf-8")

        if path == "/logout":
            self.send_response(302)
            self.send_header("Location", "/login")
            self.send_header("Set-Cookie", auth.clear_cookie())
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if path == "/api/whoami":
            sess = self._session()
            return self._json({"signed_in": bool(sess),
                               "name": (sess or {}).get("name", ""),
                               "auth_required": self.require_auth})

        if not self._guard():
            return

        if path in ("/", "/index.html"):
            return self._file(INDEX, INDEX.parent)
        if path.startswith("/ui/"):
            return self._file(UI_DIR / path[4:], UI_DIR)

        if path == "/api/capabilities":
            return self._json(capabilities())

        if path == "/api/jobs":
            return self._json([j.dict(with_log=False) for j in RUNNER.recent()])

        if path.startswith("/api/job/"):
            job = RUNNER.get(path.rsplit("/", 1)[-1])
            return self._json(job.dict() if job else {"error": "unknown job"},
                              200 if job else 404)

        if path == "/api/library":
            rows = sorted(ledger_load().values(), key=lambda r: r.get("ts", 0),
                          reverse=True)
            return self._json(rows[:200])

        if path == "/api/drafts":
            DRAFTS.mkdir(parents=True, exist_ok=True)
            out = []
            for p in sorted(DRAFTS.glob("*.json"), key=lambda p: -p.stat().st_mtime):
                try:
                    d = json.loads(p.read_text(encoding="utf-8"))
                    out.append({"slug": p.stem, "title": d.get("title", ""),
                                "words": len(d.get("blocks", [])),
                                "mtime": p.stat().st_mtime})
                except Exception:
                    continue
            return self._json(out[:60])

        if path == "/api/draft":
            slug = (qs.get("slug") or [""])[0]
            f = _slugfile(DRAFTS, slug)
            if not f.exists():
                return self._json({"error": "no such draft"}, 404)
            return self._json(json.loads(f.read_text(encoding="utf-8")))

        if path.startswith("/report/"):
            f = (ROOT / "reports" / path.rsplit("/", 1)[-1])
            if f.is_file() and f.suffix in (".html", ".csv"):
                return self._send(200, f.read_bytes(),
                                  ("text/html" if f.suffix == ".html" else "text/csv")
                                  + "; charset=utf-8")
            return self._json({"error": "no such report"}, 404)

        if path == "/api/packets":
            try:
                from writer.claude_local import pending
                return self._json([p.name for p in pending()])
            except Exception:
                return self._json([])

        return self._json({"error": "not found"}, 404)

    def do_OPTIONS(self) -> None:
        """CORS preflight. Answers only for allowlisted origins."""
        if not self._allowed_origin():
            return self._send(403, b"", "text/plain; charset=utf-8")
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        # Chrome's Private Network Access check: a page on a public origin
        # calling 127.0.0.1 sends this on the preflight and refuses the real
        # request without the matching answer.
        if self.headers.get("Access-Control-Request-Private-Network"):
            self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path

        if path == "/setup":
            return self._do_setup()

        if path == "/login":
            return self._do_login()

        if not self._guard():
            return

        # A hostile page must not be able to make the browser fire an
        # authenticated request on the user's behalf.
        if not (self._allowed_origin()
                or auth.origin_ok(self.headers, self.headers.get("Host", ""))):
            return self._json({"error": "cross-origin request refused"}, 403)

        if not path.startswith("/api/run/"):
            return self._json({"error": "not found"}, 404)

        kind = path.rsplit("/", 1)[-1]
        fn = ACTIONS.get(kind)
        if not fn:
            return self._json({"error": f"unknown action {kind}"}, 400)

        body = self._body()
        label = (body.get("topic") or body.get("slug") or body.get("url")
                 or kind).strip()[:60]
        who = (self._session() or {}).get("name", "local")
        job = RUNNER.submit(kind, f"{label}" + (f"  ·  {who}" if who != "local" else ""),
                            lambda j, b=body, f=fn: f(b, j))
        return self._json({"job": job.id})

    def _redirect(self, to: str) -> None:
        self.send_response(302)
        self.send_header("Location", to)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _do_setup(self) -> None:
        """First-run password, from this machine only."""
        if auth.is_configured():
            return self._redirect("/login")
        if not auth.is_local_request(self.client_address[0]):
            return self._send(403, auth.SETUP_PAGE.encode("utf-8"),
                              "text/html; charset=utf-8")

        n = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace") if n else "")
        pw = (form.get("password") or [""])[0]
        again = (form.get("confirm") or [""])[0]

        if pw != again:
            return self._send(400, auth.first_run_page("Those do not match."),
                              "text/html; charset=utf-8")
        try:
            auth.set_password(pw)
        except ValueError as exc:
            return self._send(400, auth.first_run_page(str(exc)),
                              "text/html; charset=utf-8")

        Handler.require_auth = True
        log("auth: password set from the browser")
        self.send_response(302)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie",
                         auth.cookie_header(auth.issue("owner"), secure=self.behind_tls))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _do_login(self) -> None:
        addr = self._client()
        wait = auth.throttled(addr)
        if wait:
            return self._send(429, auth.login_page(
                f"Too many attempts. Try again in {wait} seconds."),
                "text/html; charset=utf-8")

        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        form = urllib.parse.parse_qs(raw)
        password = (form.get("password") or [""])[0]
        name = (form.get("name") or [""])[0].strip() or "someone"

        if not auth.check_password(password):
            auth.record_failure(addr)
            log(f"auth: failed sign-in from {addr}")
            return self._send(401, auth.login_page("That password is not right."),
                              "text/html; charset=utf-8")

        auth.clear_failures(addr)
        log(f"auth: {name} signed in from {addr}")
        self.send_response(302)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie",
                         auth.cookie_header(auth.issue(name), secure=self.behind_tls))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _file(self, path: Path, base: Path) -> None:
        try:
            path = path.resolve()
            path.relative_to(base.resolve())         # no traversal out of base
        except (ValueError, OSError):
            return self._json({"error": "forbidden"}, 403)
        # The repo root holds credentials.json and token.json, so serving from
        # it is allowed for exactly one file and nothing else.
        if base.resolve() == ROOT.resolve() and path != INDEX.resolve():
            return self._json({"error": "forbidden"}, 403)
        if not path.is_file():
            return self._json({"error": "not found"}, 404)
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        self._send(200, path.read_bytes(), ctype)


def serve(host: str = "127.0.0.1", port: int = 8765, *,
          behind_tls: bool = False, no_auth: bool = False,
          allow_origins: tuple = ()) -> None:
    local_only = host in ("127.0.0.1", "localhost", "::1")

    # The interlock. Everything else in this file is convenience; this is the
    # part that stops the app being handed to the internet with live API keys
    # and no login -- unless `no_auth` is explicitly passed, which only
    # happens from `--no-auth` on the command line. That flag exists because
    # it was asked for, deliberately, after being told exactly what it opens
    # up: anyone who finds the URL gets the Gemini quota, the crawler, and
    # future Drive write access, no login at all. It is not the default and
    # never triggers by omission -- a bare public bind with no password still
    # refuses to start, same as before.
    if no_auth:
        Handler.require_auth = False
    elif not local_only and not auth.is_configured():
        raise SystemExit(
            f"\nRefusing to listen on {host} without a password.\n\n"
            "Blog Studio holds live API keys, can spend your quota, crawls any\n"
            "site you point it at, and can write to your Google Drive. Bound to\n"
            "a public address with no login, all of that belongs to whoever\n"
            "finds the URL.\n\n"
            "  python -m studio --set-password\n\n"
            "Then start it again, or pass --no-auth to run with no login at all\n"
            "(only if you mean it -- this is reachable from the internet).\n")
    else:
        Handler.require_auth = not local_only or auth.is_configured()
    Handler.behind_tls = behind_tls
    Handler.allow_origins = frozenset(o.rstrip("/") for o in allow_origins if o)

    httpd = ThreadingHTTPServer((host, port), Handler)
    log(f"Blog Studio on http://{host}:{port}")
    if no_auth and not local_only:
        log("  *** NO LOGIN -- open to anyone who reaches this address ***")
    elif Handler.require_auth:
        log("  sign-in required" + ("" if auth.is_configured()
                                    else "  (NO PASSWORD SET -- run --set-password)"))
    else:
        log("  localhost only, no sign-in required")
    for o in sorted(Handler.allow_origins):
        log(f"  API open to {o}")

    caps = capabilities()
    log(f"  writer: {caps['writer'] or 'NONE'}"
        + ("" if caps.get("writer_inline") else "  (queues packets for /aeo-rewrite)"))
    for m in caps["missing"]:
        log(f"  missing: {m['what']}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")
        httpd.shutdown()
