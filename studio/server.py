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
from .jobs import RUNNER

UI_DIR = Path(__file__).resolve().parent / "ui"
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

    caps["stages"] = {
        "audit": True,                       # needs nothing at all
        "ideas": True,                       # autocomplete needs nothing
        "draft": caps["writer"] is not None,
        "translate": True,                   # mymemory works without credentials
        "publish": has_google,
    }
    caps["engines"] = ["bhashini"] if has_bhashini else []
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


def act_draft(body: dict, job) -> dict:
    from . import draft, english
    from writer import get_writer

    brief = body.get("brief")
    if not brief and body.get("slug"):
        brief = read_json(_slugfile(BRIEFS, body["slug"]), default=None)
    if not brief:
        raise ValueError("no brief supplied")

    writer = get_writer()
    art = draft.write(brief, writer=writer, site=body.get("site", ""),
                      words=int(body.get("words") or 900))

    review = None
    try:
        review = writer.generate(
            _review_prompt(art), stage="draft_review", slug=art.slug, lang="en")
    except Exception as exc:
        if exc.__class__.__name__ == "WriterPending":
            raise
        log(f"draft review skipped: {exc.__class__.__name__}")

    report = english.score_draft(art, review=review)
    art.save(_slugfile(DRAFTS, art.slug))
    (DRAFTS / f"{art.slug}.md").write_text(
        __import__("aeo").render_markdown(art), encoding="utf-8")

    return {"article": art.dict(), "slug": art.slug, "report": report.dict(),
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


ACTIONS = {"audit": act_audit, "ideas": act_ideas, "draft": act_draft,
           "translate": act_translate}


# ------------------------------------------------------------------ handler

class Handler(BaseHTTPRequestHandler):
    server_version = "BlogStudio"

    def log_message(self, fmt, *args):        # quiet; jobs do the logging
        pass

    # -- helpers ----------------------------------------------------------
    def _send(self, code: int, payload: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
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

        if path in ("/", "/index.html"):
            return self._file(UI_DIR / "index.html")
        if path.startswith("/ui/"):
            return self._file(UI_DIR / path[4:])

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

        if path == "/api/packets":
            try:
                from writer.claude_local import pending
                return self._json([p.name for p in pending()])
            except Exception:
                return self._json([])

        return self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if not path.startswith("/api/run/"):
            return self._json({"error": "not found"}, 404)

        kind = path.rsplit("/", 1)[-1]
        fn = ACTIONS.get(kind)
        if not fn:
            return self._json({"error": f"unknown action {kind}"}, 400)

        body = self._body()
        label = (body.get("topic") or body.get("slug") or body.get("url")
                 or kind).strip()[:60]
        job = RUNNER.submit(kind, label, lambda j, b=body, f=fn: f(b, j))
        return self._json({"job": job.id})

    def _file(self, path: Path) -> None:
        try:
            path = path.resolve()
            path.relative_to(UI_DIR.resolve())       # no traversal out of ui/
        except (ValueError, OSError):
            return self._json({"error": "forbidden"}, 403)
        if not path.is_file():
            return self._json({"error": "not found"}, 404)
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        self._send(200, path.read_bytes(), ctype)


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    httpd = ThreadingHTTPServer((host, port), Handler)
    log(f"Blog Studio on http://{host}:{port}")
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
