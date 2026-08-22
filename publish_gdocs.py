"""Render an Article into a properly formatted Google Doc.

Not a text dump: real heading styles, real bullets, a highlighted TL;DR and
answer blocks, the quality report, and the JSON-LD in an appendix ready to paste
into the CMS.

Docs API offsets are UTF-16 code units, not Python characters. For Devanagari
that happens to be the same, but it is not the same for emoji or for any
supplementary-plane character, and getting it wrong shifts every style in the
document. _u16 does the conversion everywhere an index is computed.
"""
from __future__ import annotations

import json

import aeo
from common import config, log, warn
from extract import Article
from gauth import docs, drive, ensure_folder

CFG = config()
_G = CFG["google"]

HEADING = {"h2": "HEADING_2", "h3": "HEADING_3"}


def _u16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _segments(art: Article, report, profile: dict) -> list[tuple[str, str]]:
    """(text, kind) in document order. Every text ends with a newline."""
    segs: list[tuple[str, str]] = [(art.title + "\n", "title")]

    meta = [
        f"Language: {art.lang}",
        f"Source: {art.source_url or 'manual'}",
        f"Words: {report.words}",
        f"AI-likeness (proxy): {report.ai_pct:.1f}%",
        f"Human-likeness: {report.human_likeness:.1f}",
        f"AEO score: {report.aeo:.1f}",
        f"Status: {'PASS' if report.passed else 'NEEDS HUMAN REVIEW'}",
    ]
    segs.append(("  |  ".join(meta) + "\n", "meta"))
    if art.meta_description:
        segs.append((f"Meta description: {art.meta_description}\n", "meta"))
    segs.append(("\n", "normal"))

    for b in art.blocks:
        if b.type in ("h2", "h3"):
            segs.append((b.text + "\n", b.type))
        elif b.type == "tldr":
            segs.append((b.text + "\n", "bullet"))
        elif b.type == "li":
            segs.append((b.text + "\n", "bullet"))
        elif b.type == "answer":
            segs.append((b.text + "\n", "answer"))
        elif b.type == "quote":
            segs.append((b.text + "\n", "quote"))
        else:
            segs.append((b.text + "\n", "normal"))

    if art.faqs:
        segs.append(("FAQ\n", "h2"))
        for f in art.faqs:
            segs.append((f["q"] + "\n", "h3"))
            segs.append((f["a"] + "\n", "normal"))

    # --- appendix ---------------------------------------------------------
    segs.append(("\n", "normal"))
    segs.append(("Quality report\n", "h2"))
    for name, sub in report.subs.items():
        segs.append((f"{name}: {sub['score']}\n", "normal"))
    flags = [f for f in report.flags if f["severity"] in ("error", "warn")]
    if flags:
        segs.append(("Open issues\n", "h3"))
        for f in flags[:20]:
            segs.append((f"[{f['severity']}] {f['kind']}: {f['detail']}\n", "bullet"))
    segs.append(("The AI-likeness figure is a proxy computed from Indic-specific "
                 "signals (translationese, mechanical artefacts, register, "
                 "sentence variation), not a reading from a commercial AI "
                 "detector. No detector is validated on these languages.\n",
                 "note"))

    if art.meta.get("schema"):
        segs.append(("JSON-LD (paste into the page head)\n", "h2"))
        segs.append((json.dumps(art.meta["schema"], ensure_ascii=False, indent=2) + "\n",
                     "code"))

    return segs


def _requests(segs: list[tuple[str, str]]) -> tuple[str, list[dict]]:
    """Build the full text plus the styling requests that apply to it."""
    body = "".join(t for t, _ in segs)
    reqs: list[dict] = [{"insertText": {"location": {"index": 1}, "text": body}}]

    cursor = 1
    for text, kind in segs:
        start, end = cursor, cursor + _u16(text)
        cursor = end
        rng = {"startIndex": start, "endIndex": end}

        if kind == "title":
            reqs.append({"updateParagraphStyle": {
                "range": rng, "paragraphStyle": {"namedStyleType": "TITLE"},
                "fields": "namedStyleType"}})
        elif kind in HEADING:
            reqs.append({"updateParagraphStyle": {
                "range": rng, "paragraphStyle": {"namedStyleType": HEADING[kind]},
                "fields": "namedStyleType"}})
        elif kind == "bullet":
            reqs.append({"createParagraphBullets": {
                "range": rng, "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE"}})
        elif kind == "answer":
            reqs.append({"updateTextStyle": {
                "range": rng, "textStyle": {"bold": True},
                "fields": "bold"}})
            reqs.append({"updateParagraphStyle": {
                "range": rng,
                "paragraphStyle": {"shading": {"backgroundColor": {"color": {"rgbColor": {
                    "red": 0.94, "green": 0.96, "blue": 1.0}}}}},
                "fields": "shading"}})
        elif kind in ("meta", "note"):
            reqs.append({"updateTextStyle": {
                "range": rng,
                "textStyle": {"italic": True, "fontSize": {"magnitude": 9, "unit": "PT"},
                              "foregroundColor": {"color": {"rgbColor": {
                                  "red": 0.35, "green": 0.35, "blue": 0.35}}}},
                "fields": "italic,fontSize,foregroundColor"}})
        elif kind == "quote":
            reqs.append({"updateParagraphStyle": {
                "range": rng, "paragraphStyle": {"indentStart": {"magnitude": 36, "unit": "PT"}},
                "fields": "indentStart"}})
            reqs.append({"updateTextStyle": {
                "range": rng, "textStyle": {"italic": True}, "fields": "italic"}})
        elif kind == "code":
            reqs.append({"updateTextStyle": {
                "range": rng,
                "textStyle": {"weightedFontFamily": {"fontFamily": "Consolas"},
                              "fontSize": {"magnitude": 8, "unit": "PT"}},
                "fields": "weightedFontFamily,fontSize"}})

    return body, reqs


def publish_article(art: Article, report, profile: dict) -> str:
    """Create the Doc and return its URL."""
    svc = docs()
    root = ensure_folder(_G["drive_folder_name"])
    folder = ensure_folder(art.lang, parent=root)

    title = f"[{art.lang}] {art.title[:90]}"
    doc = svc.documents().create(body={"title": title}).execute()
    doc_id = doc["documentId"]

    _body, reqs = _requests(_segments(art, report, profile))
    # Style requests are applied back-to-front so that no request's indices are
    # invalidated by an earlier one. The insertText must still run first.
    ordered = [reqs[0]] + list(reversed(reqs[1:]))
    for i in range(0, len(ordered), 400):        # API caps requests per call
        svc.documents().batchUpdate(
            documentId=doc_id, body={"requests": ordered[i:i + 400]}).execute()

    try:
        drive().files().update(fileId=doc_id, addParents=folder,
                               fields="id,parents").execute()
    except Exception as exc:
        warn(f"could not move the doc into its folder: {exc.__class__.__name__}")

    url = f"https://docs.google.com/document/d/{doc_id}/edit"
    log(f"published {art.lang}: {url}", indent=1)
    return url
