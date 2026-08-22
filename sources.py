"""Ingest: manual file, a single URL, or a whole sitemap.

All three modes produce the same Article, so nothing downstream needs to know
where a post came from.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

import requests

from common import log, warn
from extract import Article, from_html, from_markdown

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 RevnoxAEO/1.0")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "en-IN,en;q=0.9"})

SKIP_EXT = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg",
            ".mp4", ".mp3", ".zip", ".xml", ".css", ".js")


def fetch(url: str, *, timeout: int = 30, retries: int = 3) -> str:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            resp = SESSION.get(url, timeout=timeout)
            resp.raise_for_status()
            # requests guesses latin-1 for text/html without a charset, which
            # mangles any Devanagari already on the page.
            if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
                resp.encoding = resp.apparent_encoding or "utf-8"
            return resp.text
        except Exception as exc:
            last = exc
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"could not fetch {url}: {last}")


# ----------------------------------------------------------------- sitemap

def _sitemap_urls(xml_text: str, base: str) -> tuple[list[str], list[str]]:
    """Return (page_urls, nested_sitemap_urls)."""
    pages, nested = [], []
    try:
        root = ElementTree.fromstring(xml_text.encode("utf-8"))
    except ElementTree.ParseError as exc:
        warn(f"sitemap parse failed: {exc}")
        return pages, nested

    tag = root.tag.split("}")[-1]
    for child in root:
        loc_el = None
        for sub in child:
            if sub.tag.split("}")[-1] == "loc":
                loc_el = sub
                break
        if loc_el is None or not (loc_el.text or "").strip():
            continue
        loc = urljoin(base, loc_el.text.strip())
        (nested if tag == "sitemapindex" else pages).append(loc)
    return pages, nested


def crawl_sitemap(url: str, *, url_filter: str = "", limit: int = 0,
                  max_sitemaps: int = 25) -> list[str]:
    """Walk a sitemap (following sitemap-index files) and return article URLs."""
    seen_maps: set[str] = set()
    queue = [url]
    found: list[str] = []

    while queue and len(seen_maps) < max_sitemaps:
        current = queue.pop(0)
        if current in seen_maps:
            continue
        seen_maps.add(current)
        log(f"sitemap: {current}", indent=1)
        try:
            xml_text = fetch(current)
        except RuntimeError as exc:
            warn(str(exc))
            continue

        pages, nested = _sitemap_urls(xml_text, current)
        queue.extend(n for n in nested if n not in seen_maps)

        for page in pages:
            if page.lower().endswith(SKIP_EXT):
                continue
            if url_filter and url_filter not in page:
                continue
            if page not in found:
                found.append(page)

        if limit and len(found) >= limit:
            break

    return found[:limit] if limit else found


def discover_sitemap(site: str) -> str | None:
    """Find a sitemap from robots.txt, then the usual locations."""
    parsed = urlparse(site if "//" in site else f"https://{site}")
    base = f"{parsed.scheme}://{parsed.netloc}"
    try:
        robots = fetch(urljoin(base, "/robots.txt"), retries=1)
        hits = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", robots)
        if hits:
            return hits[0].strip()
    except RuntimeError:
        pass
    for path in ("/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml", "/sitemap-index.xml"):
        candidate = urljoin(base, path)
        try:
            text = fetch(candidate, retries=1)
            if "<urlset" in text or "<sitemapindex" in text:
                return candidate
        except RuntimeError:
            continue
    return None


# ------------------------------------------------------------------ loaders

def load_url(url: str) -> Article:
    log(f"fetch {url}")
    art = from_html(fetch(url), url=url, source_type="url")
    log(f"  -> '{art.title[:70]}' | {len(art.blocks)} blocks | {art.words()} words", indent=1)
    return art


def load_file(path: str | Path) -> Article:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"no such file: {p}")
    text = p.read_text(encoding="utf-8", errors="replace")
    log(f"read {p.name} ({len(text)} chars)")

    if p.suffix.lower() in (".html", ".htm") or text.lstrip()[:200].lower().startswith(("<!doctype", "<html")):
        art = from_html(text, url=str(p), source_type="file")
    else:
        art = from_markdown(text, url=str(p), source_type="file")

    log(f"  -> '{art.title[:70]}' | {len(art.blocks)} blocks | {art.words()} words", indent=1)
    return art


def load_many(urls: list[str], *, pause: float = 1.0) -> list[Article]:
    """Fetch a list of posts politely. One bad page must not abort the batch."""
    out: list[Article] = []
    for i, url in enumerate(urls, 1):
        log(f"[{i}/{len(urls)}] {url}")
        try:
            out.append(load_url(url))
        except Exception as exc:
            warn(f"skipped {url}: {exc.__class__.__name__}: {exc}")
        if i < len(urls):
            time.sleep(pause)
    return out


def resolve(args) -> list[Article]:
    """Turn CLI args into Articles. Exactly one of file/url/sitemap is expected."""
    if getattr(args, "file", None):
        return [load_file(args.file)]
    if getattr(args, "url", None):
        return [load_url(args.url)]
    if getattr(args, "sitemap", None):
        sm = args.sitemap
        if not sm.endswith(".xml"):
            found = discover_sitemap(sm)
            if not found:
                raise RuntimeError(f"no sitemap found for {sm} -- pass the .xml URL directly")
            log(f"discovered sitemap: {found}")
            sm = found
        urls = crawl_sitemap(sm, url_filter=getattr(args, "filter", "") or "",
                             limit=getattr(args, "limit", 0) or 0)
        log(f"{len(urls)} article URLs matched")
        return load_many(urls)
    raise SystemExit("give one of --file, --url or --sitemap")
