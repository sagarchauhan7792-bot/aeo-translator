"""Client-ready report: a self-contained HTML file that prints clean to PDF.

A findings list in a browser tab is not something you send a client. This
produces one file — no external assets, no JavaScript — with an executive
summary, prioritised actions, and the evidence behind each.

Priority is effort against impact, because a client wants to know what to do on
Monday, not a list of 40 equally-weighted defects.
"""
from __future__ import annotations

import csv
import datetime
import io
import json
from dataclasses import asdict

from common import ROOT, config

CFG = config()

# Roughly how long each fix takes, used to sort quick wins to the top.
EFFORT = {
    "meta": "low", "title": "low", "technical": "low", "images": "low",
    "headings": "medium", "keywords": "medium", "links": "medium",
    "aeo": "medium", "reach": "low", "manifest": "low",
    "content": "high", "eeat": "medium", "retrieve": "medium",
    "quote": "medium", "cannibalisation": "high", "freshness": "low",
    "entities": "low",
}
IMPACT_RANK = {"high": 0, "medium": 1, "low": 2}
EFFORT_RANK = {"low": 0, "medium": 1, "high": 2}


def _esc(t) -> str:
    return (str(t or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def prioritise(findings: list[dict], limit: int = 20) -> list[dict]:
    """Failures before warnings, then high impact, then low effort."""
    out = []
    for f in findings:
        if f.get("status") == "pass":
            continue
        effort = EFFORT.get(f.get("group", ""), "medium")
        out.append({**f, "effort": effort})
    out.sort(key=lambda f: (0 if f["status"] == "fail" else 1,
                            IMPACT_RANK.get(f.get("impact", "medium"), 1),
                            EFFORT_RANK[f["effort"]]))
    return out[:limit]


def build(*, title: str, url: str, brand: str = "", audit: dict | None = None,
          geo: dict | None = None, perf: dict | None = None,
          site: dict | None = None, fix: dict | None = None,
          agency: str = "Revnox Media") -> str:
    today = datetime.date.today().strftime("%d %B %Y")
    findings = list((audit or {}).get("findings", []))
    if geo:
        findings += geo.get("findings", [])
    actions = prioritise(findings)

    score = (audit or {}).get("score")
    geo_score = (geo or {}).get("score")

    def band(v):
        if v is None:
            return "n/a", "#6b645c"
        return (("Good", "#1f7a4d") if v >= 80 else
                ("Needs work", "#8a6212") if v >= 55 else ("Poor", "#a8341f"))

    s_lbl, s_col = band(score)
    g_lbl, g_col = band(geo_score)

    rows = "".join(
        f"""<tr class="{_esc(a['status'])}">
          <td><span class="sev {_esc(a['status'])}">{_esc(a['status'])}</span></td>
          <td class="grp">{_esc(a.get('group'))}</td>
          <td><div class="m">{_esc(a.get('message'))}</div>
              {'<div class="fx">' + _esc(a.get('fix')) + '</div>' if a.get('fix') else ''}</td>
          <td class="eff">{_esc(a.get('effort'))}</td>
          <td class="eff">{_esc(a.get('impact', 'medium'))}</td>
        </tr>""" for a in actions)

    # --- optional sections -------------------------------------------------
    geo_block = ""
    if geo:
        acc = geo.get("crawler_access", {})
        blocked = acc.get("blocked") or []
        geo_block = f"""
    <h2>Answer-engine readiness</h2>
    <div class="grid3">
      <div class="kpi"><b>{geo.get('chunk_score', 0):.0f}</b><span>Retrieval readiness<br><i>can a chunk stand alone</i></span></div>
      <div class="kpi"><b>{geo.get('citation_score', 0):.0f}</b><span>Citation-worthiness<br><i>is it quotable</i></span></div>
      <div class="kpi"><b>{len(blocked)}</b><span>AI crawlers blocked<br><i>of {len(acc.get('agents', {}))} checked</i></span></div>
    </div>
    <p class="note">{'<b>' + ', '.join(map(_esc, blocked)) + ' cannot reach this site.</b> Until that is changed in robots.txt, no amount of content work will make these engines cite it.' if blocked else 'All AI crawlers checked are allowed to reach the site.'}</p>"""

    perf_block = ""
    if perf:
        v = perf.get("vitals")
        if v:
            cells = "".join(
                f"""<div class="kpi"><b class="{m['rating'].replace(' ', '-')}">{_esc(m['display'] or m['value'])}</b>
                    <span>{_esc(k.replace('-', ' '))}<br><i>{_esc(m['rating'])}</i></span></div>"""
                for k, m in v["metrics"].items())
            perf_block = f"""<h2>Performance</h2>
              <div class="grid3">
                <div class="kpi"><b>{v['score']}</b><span>PageSpeed score<br><i>{_esc(v['strategy'])}</i></span></div>
                {cells}</div>"""
        else:
            checks = "".join(
                f"<li><b>{_esc(c['check'])}</b> — {_esc(c['message'])}"
                + (f" <i>{_esc(c['fix'])}</i>" if c.get("fix") else "") + "</li>"
                for c in perf.get("basics", {}).get("checks", [])
                if c["status"] != "pass")
            perf_block = f"""<h2>Performance</h2>
              <p class="note">Core Web Vitals were not measured — no PageSpeed API key
              is configured. The checks below need no credentials.</p>
              <ul class="plain">{checks or '<li>No issues found.</li>'}</ul>"""

    site_block = ""
    if site:
        issues = "".join(
            f"""<tr><td><span class="sev {_esc(i['severity'])}">{_esc(i['severity'])}</span></td>
                <td>{_esc(i['title'])}</td><td class="num">{i['count']}</td>
                <td class="fx">{_esc(i['detail'])}</td></tr>"""
            for i in site.get("issues", [])[:18] if i["severity"] != "note")
        site_block = f"""<h2>Site-wide</h2>
          <p class="note">{site.get('pages_ok', 0)} pages analysed on
          {_esc(site.get('host'))}{'' if site.get('complete') else ' (partial crawl — orphan and inbound-link checks were skipped, as they need full coverage)'}.</p>
          <table><thead><tr><th></th><th>Issue</th><th>Pages</th><th>Why it matters</th></tr></thead>
          <tbody>{issues}</tbody></table>"""

    fix_block = ""
    if fix and fix.get("applied"):
        changed = "".join(f"<li>{_esc(c)}</li>" for c in fix.get("changed", [])[:10])
        fix_block = f"""<h2>Changes applied</h2>
          <div class="ba"><div><b>{fix['before']['score']}</b><span>before</span></div>
            <div class="arrow">&rarr;</div>
            <div><b class="good">{fix['after']['score']}</b><span>after</span></div></div>
          <ul class="plain">{changed}</ul>"""

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SEO &amp; AEO report — {_esc(title)}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  color:#1c1a17;background:#faf7f2;padding:40px 24px}}
.wrap{{max-width:860px;margin:0 auto;background:#fff;border:1px solid #e2dad0;
  border-radius:12px;padding:44px}}
header{{border-bottom:2px solid #1c1a17;padding-bottom:20px;margin-bottom:28px}}
.eyebrow{{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:#938a80}}
h1{{font-size:26px;line-height:1.25;margin:8px 0 6px;font-weight:600}}
.meta{{color:#6b645c;font-size:13px;word-break:break-all}}
h2{{font-size:17px;margin:34px 0 12px;padding-bottom:7px;border-bottom:1px solid #e2dad0}}
.grid3{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:14px}}
.kpi{{background:#f4efe7;border:1px solid #e2dad0;border-radius:9px;padding:14px}}
.kpi b{{display:block;font-size:27px;line-height:1.1;margin-bottom:4px}}
.kpi span{{font-size:11.5px;color:#6b645c}}
.kpi i{{color:#938a80}}
.good,.kpi b.good{{color:#1f7a4d}}
.kpi b.needs-work{{color:#8a6212}} .kpi b.poor{{color:#a8341f}}
.hero{{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:8px}}
.hero .kpi{{flex:1;min-width:150px}}
table{{width:100%;border-collapse:collapse;font-size:13px;margin-bottom:10px}}
th{{text-align:left;font-size:10.5px;letter-spacing:.07em;text-transform:uppercase;
  color:#938a80;background:#f4efe7;padding:8px 10px;border-bottom:1px solid #e2dad0}}
td{{padding:9px 10px;border-bottom:1px solid #eee6dc;vertical-align:top}}
td.num{{text-align:right;font-variant-numeric:tabular-nums}}
.grp,.eff{{color:#6b645c;font-size:11.5px;text-transform:uppercase;letter-spacing:.04em;white-space:nowrap}}
.m{{font-weight:500}} .fx{{color:#6b645c;font-size:12.5px;margin-top:2px}}
.sev{{display:inline-block;padding:2px 8px;border-radius:20px;font-size:10.5px;
  font-weight:600;text-transform:uppercase;letter-spacing:.04em}}
.sev.fail{{background:#fbe8e3;color:#a8341f}} .sev.warn{{background:#f8eed6;color:#8a6212}}
.note{{background:#f4efe7;border-left:3px solid #938a80;padding:11px 14px;
  border-radius:0 8px 8px 0;font-size:13px;margin-bottom:14px}}
ul.plain{{list-style:none}} ul.plain li{{padding:6px 0 6px 18px;position:relative;font-size:13px;
  border-bottom:1px solid #f2ece3}}
ul.plain li::before{{content:"→";position:absolute;left:0;color:#938a80}}
ul.plain i{{color:#6b645c;font-style:normal;display:block;font-size:12.5px}}
.ba{{display:flex;align-items:center;gap:18px;margin-bottom:14px}}
.ba div{{text-align:center}} .ba b{{display:block;font-size:32px;line-height:1}}
.ba span{{font-size:11px;color:#6b645c}} .arrow{{font-size:22px;color:#938a80}}
footer{{margin-top:36px;padding-top:16px;border-top:1px solid #e2dad0;
  font-size:11.5px;color:#938a80}}
@media print{{body{{background:#fff;padding:0}}
  .wrap{{border:none;padding:0;max-width:none}}
  h2{{page-break-after:avoid}} tr{{page-break-inside:avoid}}}}
</style></head><body><div class="wrap">
<header>
  <div class="eyebrow">SEO &amp; Answer-engine report · {today}</div>
  <h1>{_esc(title)}</h1>
  <div class="meta">{_esc(url)}{' · ' + _esc(brand) if brand else ''}</div>
</header>

<div class="hero">
  <div class="kpi"><b style="color:{s_col}">{score if score is not None else '—'}</b>
    <span>On-page SEO<br><i>{s_lbl}</i></span></div>
  <div class="kpi"><b style="color:{g_col}">{f'{geo_score:.0f}' if geo_score is not None else '—'}</b>
    <span>Answer-engine readiness<br><i>{g_lbl}</i></span></div>
  <div class="kpi"><b>{sum(1 for f in findings if f.get('status') == 'fail')}</b>
    <span>Failed checks<br><i>fix these first</i></span></div>
  <div class="kpi"><b>{sum(1 for f in findings if f.get('status') == 'warn')}</b>
    <span>Warnings<br><i>worth doing</i></span></div>
</div>

{fix_block}

<h2>What to do first</h2>
<p class="note">Ordered by severity, then impact, then how long it takes. The top
few are usually an hour's work between them.</p>
<table><thead><tr><th></th><th>Area</th><th>Finding and fix</th><th>Effort</th><th>Impact</th></tr></thead>
<tbody>{rows or '<tr><td colspan="5">No issues found.</td></tr>'}</tbody></table>

{geo_block}
{perf_block}
{site_block}

<footer>
  Prepared by {_esc(agency)}. Scores are measured from the page and the site:
  on-page checks against documented search guidance, answer-engine readiness from
  crawler access, retrieval chunking and citation shape. No search-volume,
  backlink or AI-visibility figures are included, because none were measured.
</footer>
</div></body></html>"""


def to_csv(findings: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["status", "group", "check", "impact", "effort", "message", "fix"])
    for f in prioritise(findings, limit=10_000):
        w.writerow([f.get("status"), f.get("group"), f.get("check"),
                    f.get("impact", "medium"), f.get("effort", "medium"),
                    f.get("message"), f.get("fix", "")])
    return buf.getvalue()
