"""Page performance: Core Web Vitals, plus the checks that need nothing.

Two halves, so the section is never simply blank:

  basics()  page weight, image sizes, HTTPS, viewport, compression and cache
            headers. Fetched directly, no credentials, always available.
  vitals()  PageSpeed Insights v5 -- LCP, CLS, INP, TBT, plus CrUX field data.
            Needs a free Google API key.

The anonymous PSI endpoint was tested and returns HTTP 429: its shared quota is
permanently exhausted. A key is free (25k requests/day) and takes two minutes, so
the module asks for one clearly rather than silently reporting nothing.
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field, asdict

from common import ROOT, MissingCredential, log, secret, warn
import sources

PSI = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"

# Google's own "good" thresholds.
THRESHOLDS = {
    "largest-contentful-paint": (2500, 4000, "ms"),
    "cumulative-layout-shift": (0.1, 0.25, ""),
    "total-blocking-time": (200, 600, "ms"),
    "first-contentful-paint": (1800, 3000, "ms"),
    "interaction-to-next-paint": (200, 500, "ms"),
    "speed-index": (3400, 5800, "ms"),
}


def api_key() -> str | None:
    return secret("PAGESPEED_API_KEY", "pagespeed_api_key.txt") or \
        secret("GOOGLE_API_KEY", "google_api_key.txt")


def require_key() -> None:
    if not api_key():
        raise MissingCredential(
            "PageSpeed Insights API key",
            "Core Web Vitals (LCP, CLS, INP) and performance opportunities",
            "The anonymous endpoint's shared quota is exhausted (verified: it "
            "returns HTTP 429), so a key is required. Get a free one at "
            "https://console.cloud.google.com/apis/credentials — enable the "
            "PageSpeed Insights API, create an API key, and save it to "
            "aeo-translator/pagespeed_api_key.txt or set PAGESPEED_API_KEY. "
            "Free tier is 25,000 requests a day.")


# ------------------------------------------------------------------ basics

def basics(url: str, *, max_images: int = 12) -> dict:
    """Everything measurable by just fetching the page."""
    out: dict = {"url": url, "checks": []}

    def add(check, status, message, fix="", detail=None):
        out["checks"].append({"check": check, "status": status, "message": message,
                              "fix": fix, "detail": detail or {}})

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        add("https", "fail", "The page is not served over HTTPS.",
            "HTTPS is a ranking signal and a browser trust requirement.")
    else:
        add("https", "pass", "Served over HTTPS.")

    try:
        r = sources.SESSION.get(url, timeout=30)
    except Exception as exc:
        add("reachable", "fail", f"Could not fetch the page ({exc.__class__.__name__}).")
        return out

    html = r.text
    html_bytes = len(r.content)
    out["html_bytes"] = html_bytes
    out["status"] = r.status_code

    if html_bytes > 250_000:
        add("html_size", "warn", f"HTML is {html_bytes // 1024} KB.",
            "Over ~150 KB of HTML usually means inlined data or a bloated "
            "template. It delays first paint on a slow connection.")
    else:
        add("html_size", "pass", f"HTML is {html_bytes // 1024} KB.")

    enc = (r.headers.get("Content-Encoding") or "").lower()
    if enc in ("gzip", "br", "zstd", "deflate"):
        add("compression", "pass", f"Compressed with {enc}.")
    else:
        add("compression", "fail", "The response is not compressed.",
            "Enable gzip or brotli. It is usually a one-line server change and "
            "typically cuts transfer size by 70%.")

    cache = r.headers.get("Cache-Control", "")
    if not cache or "no-store" in cache:
        add("cache", "warn", f"Cache-Control is {cache or 'absent'}.",
            "Set a sensible max-age so repeat visits do not refetch everything.")
    else:
        add("cache", "pass", f"Cache-Control: {cache[:60]}")

    if re.search(r'<meta[^>]+name=["\']viewport["\']', html, re.I):
        add("viewport", "pass", "Mobile viewport meta tag present.")
    else:
        add("viewport", "fail", "No mobile viewport meta tag.",
            "Add <meta name=\"viewport\" content=\"width=device-width, "
            "initial-scale=1\">. Without it the page renders desktop-width on "
            "phones, which is most of an Indian audience.")

    # Image weight, by HEAD so nothing large is downloaded.
    srcs = re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', html, re.I)[:max_images]
    heavy, total = [], 0
    for s in srcs:
        full = urllib.parse.urljoin(url, s)
        try:
            h = sources.SESSION.head(full, timeout=12, allow_redirects=True)
            size = int(h.headers.get("Content-Length") or 0)
        except Exception:
            continue
        total += size
        if size > 200_000:
            heavy.append({"src": full[-70:], "kb": size // 1024})
    out["image_bytes_sampled"] = total
    if heavy:
        add("image_weight", "warn",
            f"{len(heavy)} image(s) over 200 KB (sampled {len(srcs)}).",
            "Compress and serve WebP or AVIF. Images are almost always the "
            "largest thing on the page.", {"heavy": heavy})
    elif srcs:
        add("image_weight", "pass",
            f"Sampled {len(srcs)} images, {total // 1024} KB total.")

    blocking = len(re.findall(r'<script(?![^>]*(?:async|defer|type=["\']module))[^>]*src=',
                              html, re.I))
    if blocking > 2:
        add("render_blocking", "warn", f"{blocking} render-blocking script(s) in the HTML.",
            "Add defer or async, or move them below the fold.")
    else:
        add("render_blocking", "pass", f"{blocking} render-blocking script(s).")

    return out


# ------------------------------------------------------------------ vitals

def vitals(url: str, *, strategy: str = "mobile") -> dict:
    """Core Web Vitals from PageSpeed Insights. Raises MissingCredential if unset."""
    require_key()
    log(f"perf: PageSpeed ({strategy}) for {url}")
    r = sources.SESSION.get(PSI, params={
        "url": url, "strategy": strategy, "key": api_key(),
        "category": "performance"}, timeout=180)
    if r.status_code == 429:
        raise RuntimeError("PageSpeed quota exceeded for this key; try again later.")
    r.raise_for_status()
    data = r.json()
    lh = data.get("lighthouseResult", {})
    audits = lh.get("audits", {})

    metrics = {}
    for key, (good, poor, unit) in THRESHOLDS.items():
        a = audits.get(key)
        if not a or a.get("numericValue") is None:
            continue
        v = a["numericValue"]
        metrics[key] = {
            "value": round(v, 3) if unit == "" else round(v),
            "display": a.get("displayValue", ""),
            "unit": unit,
            "rating": "good" if v <= good else "needs work" if v <= poor else "poor",
            "good_below": good,
        }

    opportunities = []
    for k, a in audits.items():
        if a.get("details", {}).get("type") == "opportunity":
            saving = a.get("details", {}).get("overallSavingsMs", 0)
            if saving and saving > 100:
                opportunities.append({"id": k, "title": a.get("title", ""),
                                      "saving_ms": round(saving),
                                      "description": (a.get("description") or "")[:180]})
    opportunities.sort(key=lambda o: -o["saving_ms"])

    field = {}
    for k, v in (data.get("loadingExperience", {}).get("metrics", {}) or {}).items():
        field[k] = {"percentile": v.get("percentile"), "category": v.get("category")}

    return {
        "url": url, "strategy": strategy,
        "score": round((lh.get("categories", {}).get("performance", {}).get("score") or 0) * 100),
        "metrics": metrics,
        "field_data": field,
        "has_field_data": bool(field),
        "opportunities": opportunities[:8],
    }


def report(url: str, *, strategy: str = "mobile") -> dict:
    """Basics always; vitals when a key exists, with a clear note when not."""
    out = {"basics": basics(url), "vitals": None, "vitals_note": None}
    try:
        out["vitals"] = vitals(url, strategy=strategy)
    except MissingCredential as exc:
        out["vitals_note"] = str(exc)
    except Exception as exc:
        out["vitals_note"] = f"PageSpeed unavailable: {exc.__class__.__name__}: {exc}"
    return out
