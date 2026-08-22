"""Site-wide crawl: the problems that only exist between pages.

A per-page audit is blind to duplicate titles, cannibalisation, orphan pages and
internal-link gaps by construction — none of them are properties of a single
page. This fetches the site and reports on the whole of it.

Design notes that matter:

* **Resumable.** Results append to `cache/crawl/<host>.jsonl` as they arrive. A
  2,434-page crawl at one request a second is roughly forty minutes; it has to
  survive a closed laptop, and re-running must not re-fetch what it already has.
* **Polite by default.** One request per second, two workers, robots.txt obeyed.
  This points at client sites, and hammering a client's server to audit it is a
  bad trade.
* **Fetches, not guesses.** Slugs alone cannot tell you a page is thin or has a
  missing meta description. Those need the body.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import urllib.parse
import urllib.robotparser
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from pathlib import Path

from bs4 import BeautifulSoup

from common import ROOT, log, warn, word_count, write_json, read_json
import sources

CACHE = ROOT / "cache" / "crawl"
DEFAULT_DELAY = 1.0
DEFAULT_WORKERS = 2

THIN_WORDS = 300
TITLE_MIN, TITLE_MAX = 30, 60
DESC_MIN, DESC_MAX = 70, 155

STOP = {
    "the", "a", "an", "and", "or", "of", "for", "in", "on", "to", "with", "at",
    "by", "from", "is", "are", "how", "what", "why", "when", "which", "who",
    "your", "you", "best", "top", "common", "guide", "complete", "know", "about",
    "need", "everyone", "should", "not", "ignore", "signs", "can", "do", "does",
}


def host_of(url: str) -> str:
    h = urllib.parse.urlparse(url).netloc.lower()
    return h[4:] if h.startswith("www.") else h


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(w) > 2 and w not in STOP}


@dataclass
class Page:
    url: str
    status: int = 0
    final_url: str = ""
    redirects: int = 0
    title: str = ""
    description: str = ""
    h1: list = field(default_factory=list)
    h2: list = field(default_factory=list)
    words: int = 0
    canonical: str = ""
    robots: str = ""
    lang: str = ""
    internal: list = field(default_factory=list)
    external: list = field(default_factory=list)
    images: int = 0
    images_no_alt: int = 0
    schema_types: list = field(default_factory=list)
    published: str = ""
    modified: str = ""
    content_hash: str = ""
    error: str = ""

    def dict(self) -> dict:
        return asdict(self)


# ------------------------------------------------------------------ fetching

def _robots(base: str) -> urllib.robotparser.RobotFileParser | None:
    rp = urllib.robotparser.RobotFileParser()
    try:
        rp.set_url(urllib.parse.urljoin(base, "/robots.txt"))
        rp.read()
        return rp
    except Exception as exc:
        warn(f"robots.txt unreadable ({exc.__class__.__name__}); crawling politely anyway")
        return None


def parse_page(url: str, html: str, final_url: str, status: int,
               redirects: int, site_host: str) -> Page:
    p = Page(url=url, status=status, final_url=final_url, redirects=redirects)
    soup = BeautifulSoup(html, "lxml")

    if soup.title and soup.title.string:
        p.title = re.sub(r"\s+", " ", soup.title.get_text()).strip()

    for name, attr in (("description", "name"), ("og:description", "property")):
        tag = soup.find("meta", attrs={attr: name})
        if tag and tag.get("content"):
            p.description = re.sub(r"\s+", " ", tag["content"]).strip()
            break

    rb = soup.find("meta", attrs={"name": "robots"})
    if rb and rb.get("content"):
        p.robots = rb["content"].strip().lower()

    can = soup.find("link", attrs={"rel": lambda v: v and "canonical" in v})
    if can and can.get("href"):
        p.canonical = urllib.parse.urljoin(final_url or url, can["href"])

    if soup.html and soup.html.get("lang"):
        p.lang = soup.html["lang"]

    p.h1 = [re.sub(r"\s+", " ", h.get_text()).strip() for h in soup.find_all("h1")][:5]
    p.h2 = [re.sub(r"\s+", " ", h.get_text()).strip() for h in soup.find_all("h2")][:30]

    # Body text, minus the furniture, so word counts mean something.
    for junk in soup.find_all(["nav", "footer", "header", "aside", "script", "style", "form"]):
        junk.decompose()
    body = soup.find("article") or soup.find("main") or soup.body or soup
    text = re.sub(r"\s+", " ", body.get_text(" ")).strip()
    p.words = word_count(text)
    p.content_hash = hashlib.sha1(text.lower().encode("utf-8")).hexdigest()[:16]

    for a in body.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        full = urllib.parse.urljoin(final_url or url, href)
        full = full.split("#")[0].rstrip("/")
        if not full.startswith("http"):
            continue
        (p.internal if host_of(full) == site_host else p.external).append(full)
    p.internal = list(dict.fromkeys(p.internal))[:200]
    p.external = list(dict.fromkeys(p.external))[:80]

    imgs = body.find_all("img")
    p.images = len(imgs)
    p.images_no_alt = sum(1 for i in imgs if not (i.get("alt") or "").strip())

    for s in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(s.string or "{}")
        except Exception:
            continue
        for node in (data.get("@graph") if isinstance(data, dict) and "@graph" in data
                     else data if isinstance(data, list) else [data]):
            if isinstance(node, dict) and node.get("@type"):
                t = node["@type"]
                p.schema_types.extend(t if isinstance(t, list) else [t])
                p.published = p.published or node.get("datePublished", "")
                p.modified = p.modified or node.get("dateModified", "")
    p.schema_types = sorted(set(p.schema_types))
    return p


def fetch_page(url: str, site_host: str) -> Page:
    try:
        r = sources.SESSION.get(url, timeout=25, allow_redirects=True)
        if not r.encoding or r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding or "utf-8"
        if r.status_code >= 400:
            return Page(url=url, status=r.status_code, final_url=r.url,
                        redirects=len(r.history))
        ctype = r.headers.get("Content-Type", "")
        if "html" not in ctype:
            return Page(url=url, status=r.status_code, final_url=r.url,
                        error=f"not html ({ctype[:40]})")
        return parse_page(url, r.text, r.url, r.status_code, len(r.history), site_host)
    except Exception as exc:
        return Page(url=url, status=0, error=f"{exc.__class__.__name__}: {exc}"[:160])


# --------------------------------------------------------------------- crawl

def crawl(site: str, *, limit: int = 0, url_filter: str = "",
          delay: float = DEFAULT_DELAY, workers: int = DEFAULT_WORKERS,
          refresh: bool = False, progress=None) -> list[Page]:
    """Fetch a site's sitemap URLs. Resumable; skips anything already cached."""
    site_host = host_of(site if "//" in site else f"https://{site}")
    base = f"https://{site_host}"
    store = CACHE / f"{site_host}.jsonl"
    store.parent.mkdir(parents=True, exist_ok=True)

    done: dict[str, dict] = {}
    if store.exists() and not refresh:
        for line in store.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    rec = json.loads(line)
                    done[rec["url"]] = rec
                except json.JSONDecodeError:
                    continue
        log(f"crawl: {len(done)} pages already cached for {site_host}")
    elif refresh and store.exists():
        store.unlink()

    sm = site if site.endswith(".xml") else sources.discover_sitemap(site)
    if not sm:
        raise RuntimeError(f"no sitemap found for {site}")
    urls = sources.crawl_sitemap(sm, url_filter=url_filter, limit=0)
    if limit:
        urls = urls[:limit]

    rp = _robots(base)
    ua = sources.SESSION.headers.get("User-Agent", "*")
    blocked = 0
    todo = []
    for u in urls:
        if u in done:
            continue
        if rp is not None and not rp.can_fetch(ua, u):
            blocked += 1
            continue
        todo.append(u)
    if blocked:
        log(f"crawl: {blocked} URL(s) disallowed by robots.txt, skipped")

    log(f"crawl: {len(urls)} in sitemap, {len(done)} cached, {len(todo)} to fetch "
        f"(~{len(todo) * delay / 60:.0f} min at {delay}s each)")

    lock = threading.Lock()
    counter = {"n": 0}
    fh = store.open("a", encoding="utf-8")

    def one(u: str) -> None:
        page = fetch_page(u, site_host)
        with lock:
            fh.write(json.dumps(page.dict(), ensure_ascii=False) + "\n")
            fh.flush()
            counter["n"] += 1
            n = counter["n"]
        if n % 25 == 0 or n == len(todo):
            log(f"crawl: {n}/{len(todo)} fetched", indent=1)
            if progress:
                progress(n, len(todo))
        time.sleep(delay)          # per worker, so effective rate = workers/delay

    try:
        if todo:
            with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                list(pool.map(one, todo))
    finally:
        fh.close()

    pages = [Page(**{k: v for k, v in rec.items() if k in Page.__dataclass_fields__})
             for rec in done.values()]
    for line in store.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec["url"] not in done:
            done[rec["url"]] = rec
    pages = [Page(**{k: v for k, v in rec.items() if k in Page.__dataclass_fields__})
             for rec in done.values()]
    log(f"crawl: {len(pages)} pages available for {site_host}")
    return pages


# -------------------------------------------------------------------- report

def _verify_links(candidates: list[dict], *, limit: int = 40) -> list[dict]:
    """HEAD each candidate; only genuine failures are reported."""
    if not candidates:
        return []
    broken: list[dict] = []
    for c in candidates[:limit]:
        try:
            r = sources.SESSION.head(c["value"], timeout=12, allow_redirects=True)
            if r.status_code >= 400:
                r = sources.SESSION.get(c["value"], timeout=15, stream=True)
            if r.status_code >= 400:
                broken.append({**c, "status": r.status_code})
        except Exception as exc:
            broken.append({**c, "status": exc.__class__.__name__})
        time.sleep(0.2)
    if broken:
        log(f"crawl: {len(broken)}/{min(len(candidates), limit)} off-crawl links are "
            f"genuinely broken", indent=1)
    return broken


def analyse(pages: list[Page], site_host: str, *, complete: bool = False,
            verify_links: int = 40) -> dict:
    """Everything that needs more than one page to see.

    `complete` must be True only when the crawl covered the whole site with no
    limit and no URL filter. Several findings are meaningless otherwise.
    """
    ok = [p for p in pages if p.status == 200 and not p.error]
    issues: list[dict] = []

    def add(kind: str, severity: str, title: str, detail: str, items: list) -> None:
        if items:
            issues.append({"kind": kind, "severity": severity, "title": title,
                           "detail": detail, "count": len(items), "items": items[:60]})

    # --- duplicates ---------------------------------------------------------
    by_title = defaultdict(list)
    for p in ok:
        if p.title:
            by_title[p.title.strip().lower()].append(p.url)
    add("duplicate_title", "fail", "Duplicate page titles",
        "Identical titles make pages compete for the same result and look "
        "templated. Differentiate them or consolidate the pages.",
        [{"value": t, "urls": u} for t, u in by_title.items() if len(u) > 1])

    by_desc = defaultdict(list)
    for p in ok:
        if p.description:
            by_desc[p.description.strip().lower()].append(p.url)
    add("duplicate_meta", "warn", "Duplicate meta descriptions",
        "A shared description means the same snippet in results for different pages.",
        [{"value": d[:90], "urls": u} for d, u in by_desc.items() if len(u) > 1])

    by_hash = defaultdict(list)
    for p in ok:
        if p.content_hash and p.words > 100:
            by_hash[p.content_hash].append(p.url)
    add("duplicate_content", "fail", "Pages with identical body content",
        "Byte-identical content. Consolidate and redirect, or canonicalise one to the other.",
        [{"value": h, "urls": u} for h, u in by_hash.items() if len(u) > 1])

    # --- per-page defects ---------------------------------------------------
    add("missing_title", "fail", "Missing page title",
        "No title tag at all.", [{"url": p.url} for p in ok if not p.title])
    add("title_length", "warn", "Title too long or too short",
        f"Outside {TITLE_MIN}-{TITLE_MAX} characters; long titles are truncated in results.",
        [{"url": p.url, "len": len(p.title), "value": p.title}
         for p in ok if p.title and not (TITLE_MIN <= len(p.title) <= TITLE_MAX)])
    add("missing_meta", "fail", "Missing meta description",
        "Google invents a snippet when there is none, usually badly.",
        [{"url": p.url} for p in ok if not p.description])
    add("meta_length", "warn", "Meta description too long or too short",
        f"Outside {DESC_MIN}-{DESC_MAX} characters.",
        [{"url": p.url, "len": len(p.description)}
         for p in ok if p.description and not (DESC_MIN <= len(p.description) <= DESC_MAX)])
    add("thin", "warn", "Thin content",
        f"Under {THIN_WORDS} words. Develop it or merge it into a fuller page.",
        [{"url": p.url, "words": p.words} for p in ok if 0 < p.words < THIN_WORDS])
    add("no_h1", "warn", "No H1 heading",
        "Every page should have exactly one H1.",
        [{"url": p.url} for p in ok if not p.h1])
    add("multi_h1", "warn", "More than one H1",
        "Multiple H1s split the page's topic signal.",
        [{"url": p.url, "count": len(p.h1)} for p in ok if len(p.h1) > 1])
    add("no_schema", "warn", "No structured data",
        "Without JSON-LD an answer engine has to guess at the page's meaning.",
        [{"url": p.url} for p in ok if not p.schema_types])
    add("images_no_alt", "warn", "Images without alt text",
        "An accessibility failure before it is an SEO one.",
        [{"url": p.url, "count": p.images_no_alt} for p in ok if p.images_no_alt])

    # --- indexability -------------------------------------------------------
    add("noindex", "fail", "Page is set to noindex",
        "In the sitemap but telling search engines to ignore it. One of the two is wrong.",
        [{"url": p.url, "value": p.robots} for p in ok if "noindex" in p.robots])
    add("canonical_conflict", "fail", "Canonical points at another page",
        "This page is in the sitemap but hands its ranking to a different URL.",
        [{"url": p.url, "value": p.canonical} for p in ok
         if p.canonical and p.canonical.rstrip("/") != (p.final_url or p.url).rstrip("/")])
    add("redirect", "warn", "URL redirects",
        "The sitemap lists a URL that redirects. List the destination instead.",
        [{"url": p.url, "value": p.final_url, "hops": p.redirects}
         for p in ok if p.redirects])
    add("error_status", "fail", "Page returned an error",
        "In the sitemap but not reachable.",
        [{"url": p.url, "status": p.status, "value": p.error}
         for p in pages if p.status >= 400 or p.error])

    # --- link graph ---------------------------------------------------------
    # These three findings are only meaningful on a COMPLETE crawl. On a capped
    # or filtered one every link to an uncrawled page looks broken and every
    # page looks orphaned -- verified: a 60-page sample of hiims.in reported 80
    # "broken" internal links, and the first four all returned HTTP 200. They
    # were tag and contact pages outside the /blog filter. Reporting that to a
    # client would be worse than reporting nothing.
    inbound: dict[str, int] = Counter()
    known = {(p.final_url or p.url).rstrip("/") for p in ok}
    for p in ok:
        for target in p.internal:
            t = target.rstrip("/")
            if t in known:
                inbound[t] += 1

    if complete:
        orphans = [{"url": p.url, "words": p.words} for p in ok
                   if inbound.get((p.final_url or p.url).rstrip("/"), 0) == 0]
        add("orphan", "fail", "Orphan pages",
            "In the sitemap but linked from no other page on the site. Crawlers "
            "and readers reach them only by accident.", orphans)
        add("few_inbound", "warn", "Fewer than three inbound internal links",
            "Under-linked pages look unimportant to a crawler.",
            [{"url": p.url, "inbound": inbound.get((p.final_url or p.url).rstrip("/"), 0)}
             for p in ok
             if 0 < inbound.get((p.final_url or p.url).rstrip("/"), 0) < 3])
    else:
        orphans = []
        issues.append({
            "kind": "link_graph_skipped", "severity": "note",
            "title": "Orphan and inbound-link checks skipped",
            "detail": "These need a complete crawl. On a capped or filtered crawl "
                      "every link to an uncrawled page looks broken and every page "
                      "looks orphaned. Re-run without a limit or a URL filter.",
            "count": 0, "items": []})

    # Candidate broken links are VERIFIED with a real request, never inferred
    # from crawl membership.
    candidates: list[dict] = []
    seen_targets: set[str] = set()
    for p in ok:
        for target in p.internal:
            t = target.rstrip("/")
            if t in known or host_of(t) != site_host or t in seen_targets:
                continue
            seen_targets.add(t)
            candidates.append({"url": p.url, "value": target})

    broken_internal = _verify_links(candidates, limit=verify_links)
    add("broken_internal", "fail", "Broken internal links",
        "Verified with a live request, not inferred from the crawl.",
        broken_internal)
    if len(candidates) > verify_links:
        issues.append({
            "kind": "links_unverified", "severity": "note",
            "title": f"{len(candidates) - verify_links} internal links not checked",
            "detail": f"Only the first {verify_links} off-crawl internal links were "
                      "verified, to keep the run short. Raise the limit to check all.",
            "count": len(candidates) - verify_links, "items": []})

    # --- cannibalisation ----------------------------------------------------
    cannibals = _cannibalisation(ok)
    add("cannibalisation", "fail", "Pages competing for the same topic",
        "Two or more pages targeting one topic split their own rankings. "
        "Merge them, or retarget one.", cannibals)

    order = {"fail": 0, "warn": 1}
    issues.sort(key=lambda i: (order.get(i["severity"], 2), -i["count"]))

    return {
        "host": site_host,
        "complete": complete,
        "pages_crawled": len(pages),
        "pages_ok": len(ok),
        "issues": issues,
        "totals": {
            "fail": sum(i["count"] for i in issues if i["severity"] == "fail"),
            "warn": sum(i["count"] for i in issues if i["severity"] == "warn"),
            "words_median": sorted(p.words for p in ok)[len(ok) // 2] if ok else 0,
            "with_schema": sum(1 for p in ok if p.schema_types),
            "orphans": len(orphans) if complete else None,
        },
        "link_suggestions": suggest_links(ok, inbound),
    }


def _cannibalisation(pages: list[Page], threshold: float = 0.72) -> list[dict]:
    """Pages whose titles overlap enough to be chasing the same query.

    F1 over content words, the same measure the gap analysis uses -- coverage in
    one direction only would flag every long title as competing with every short
    one that fits inside it.
    """
    groups: list[dict] = []
    toks = [(p, _tokens(p.title)) for p in pages if p.title and len(_tokens(p.title)) > 1]
    used: set[str] = set()

    for i, (pa, ta) in enumerate(toks):
        if pa.url in used:
            continue
        cluster = []
        for pb, tb in toks[i + 1:]:
            if pb.url in used or not tb:
                continue
            shared = len(ta & tb)
            if not shared:
                continue
            f1 = 2 * shared / (len(ta) + len(tb))
            if f1 >= threshold:
                cluster.append({"url": pb.url, "title": pb.title, "score": round(f1, 2)})
        if cluster:
            used.add(pa.url)
            used.update(c["url"] for c in cluster)
            groups.append({"value": pa.title, "url": pa.url,
                           "urls": [pa.url] + [c["url"] for c in cluster],
                           "competing": cluster[:6]})
    return groups


def suggest_links(pages: list[Page], inbound: dict) -> list[dict]:
    """Which existing pages should link to which, and with what anchor text.

    The highest-ROI action in SEO, and it is pure computation over data the
    crawl already holds. Targets under-linked pages first, since those are where
    a new link changes anything.
    """
    toks = [(p, _tokens(p.title)) for p in pages if p.title]
    targets = sorted(
        pages,
        key=lambda p: inbound.get((p.final_url or p.url).rstrip("/"), 0))[:40]

    out: list[dict] = []
    for target in targets:
        t_tokens = _tokens(target.title)
        if len(t_tokens) < 2:
            continue
        t_url = (target.final_url or target.url).rstrip("/")
        froms = []
        for src, s_tokens in toks:
            s_url = (src.final_url or src.url).rstrip("/")
            if s_url == t_url:
                continue
            if any(l.rstrip("/") == t_url for l in src.internal):
                continue                     # already links there
            shared = t_tokens & s_tokens
            if len(shared) < 2:
                continue
            score = len(shared) / len(t_tokens)
            if score >= 0.4:
                froms.append({"url": src.url, "title": src.title,
                              "anchor": " ".join(sorted(shared))[:60],
                              "score": round(score, 2)})
        if froms:
            froms.sort(key=lambda f: -f["score"])
            out.append({
                "target": target.url, "title": target.title,
                "inbound_now": inbound.get(t_url, 0),
                "link_from": froms[:5],
            })
    out.sort(key=lambda o: (o["inbound_now"], -len(o["link_from"])))
    return out[:30]


def load_cached(site: str) -> list[Page]:
    store = CACHE / f"{host_of(site if '//' in site else 'https://' + site)}.jsonl"
    if not store.exists():
        return []
    seen: dict[str, dict] = {}
    for line in store.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rec = json.loads(line)
                seen[rec["url"]] = rec
            except json.JSONDecodeError:
                continue
    return [Page(**{k: v for k, v in r.items() if k in Page.__dataclass_fields__})
            for r in seen.values()]
