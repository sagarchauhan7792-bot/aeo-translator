"""The master tracker Sheet: one row per (blog, language).

The Google Drive connector can create a Sheet but its update call only changes
title and parent -- it cannot write cells. Hence this client.

Column naming matters here. The AI figure is labelled "AI-likeness % (proxy)"
rather than "AI %" because it is not a commercial-detector reading and this sheet
is the artefact most likely to be forwarded to a client.
"""
from __future__ import annotations

import datetime

from common import ROOT, config, log, warn, write_json, read_json
from gauth import sheets, ensure_folder, drive

CFG = config()
_G = CFG["google"]
TH = CFG["thresholds"]

TAB = "Translations"

HEADERS = [
    "Date", "Source URL", "Title", "Language", "Doc Link", "Words",
    "Fidelity", "Grammar", "Human-likeness", "AI-likeness % (proxy)",
    "AEO score", "Rewrite passes", "Status", "Notes",
]

STATE = ROOT / "sheet_state.json"


def _find_tracker() -> str:
    """Look the tracker up in Drive by name.

    sheet_state.json remembers the id, which is enough on a machine that keeps
    its files. On an ephemeral host it is not: the file is wiped on every
    spin-down, and without this lookup the next publish would create a SECOND
    tracker with the same name, then a third, scattering one project's history
    across a pile of identical spreadsheets in the owner's Drive.

    Drive is already the durable store. Asking it is cheaper than trying to keep
    a local pointer alive, and it makes creating the tracker idempotent.
    """
    try:
        q = ("mimeType = 'application/vnd.google-apps.spreadsheet' and "
             f"name = '{_G['sheet_name']}' and trashed = false")
        found = drive().files().list(q=q, fields="files(id,name)", pageSize=10,
                                     orderBy="createdTime").execute()
        for f in found.get("files") or []:
            # A matching name is not proof it is our tracker. There is already
            # one spreadsheet in this account with this exact name and a single
            # tab called "Log", which is not this file's format -- appending
            # rows into it would corrupt someone else's document. Adopt a
            # candidate only if it carries the tab this module writes.
            try:
                meta = sheets().spreadsheets().get(
                    spreadsheetId=f["id"], fields="sheets.properties.title").execute()
            except Exception:
                continue
            tabs = [sh["properties"]["title"] for sh in meta.get("sheets", [])]
            if TAB in tabs:
                return f["id"]
            log(f"tracker: ignoring '{f['name']}' ({f['id'][:12]}...) -- it has "
                f"tabs {tabs}, not '{TAB}', so it is a different document")
    except Exception as exc:
        warn(f"could not search Drive for the tracker ({exc.__class__.__name__}); "
             "a new one may be created")
    return ""


def _spreadsheet_id() -> str:
    """Find the tracker, or create it once and remember the id."""
    if _G.get("sheet_id"):
        return _G["sheet_id"]
    saved = read_json(STATE, default={}) or {}
    if saved.get("spreadsheet_id"):
        return saved["spreadsheet_id"]

    existing = _find_tracker()
    if existing:
        write_json(STATE, {"spreadsheet_id": existing, "found": "drive"})
        log(f"tracker: reusing the existing sheet {existing}")
        return existing

    svc = sheets()
    ss = svc.spreadsheets().create(body={
        "properties": {"title": _G["sheet_name"]},
        "sheets": [{"properties": {"title": TAB, "gridProperties": {"frozenRowCount": 1}}}],
    }, fields="spreadsheetId").execute()
    sid = ss["spreadsheetId"]

    svc.spreadsheets().values().update(
        spreadsheetId=sid, range=f"{TAB}!A1",
        valueInputOption="RAW", body={"values": [HEADERS]}).execute()

    _format(sid)
    try:
        folder = ensure_folder(_G["drive_folder_name"])
        drive().files().update(fileId=sid, addParents=folder, fields="id").execute()
    except Exception as exc:
        warn(f"could not file the tracker into its folder: {exc.__class__.__name__}")

    write_json(STATE, {"spreadsheet_id": sid, "created": datetime.date.today().isoformat()})
    log(f"created tracker sheet: https://docs.google.com/spreadsheets/d/{sid}")
    return sid


def _sheet_id(sid: str) -> int:
    meta = sheets().spreadsheets().get(spreadsheetId=sid).execute()
    for s in meta.get("sheets", []):
        if s["properties"]["title"] == TAB:
            return s["properties"]["sheetId"]
    return 0


def _format(sid: str) -> None:
    """Bold header, sensible widths, and red rows for anything that failed."""
    gid = _sheet_id(sid)
    ai_col = HEADERS.index("AI-likeness % (proxy)")
    status_col = HEADERS.index("Status")
    grid = {"sheetId": gid, "startRowIndex": 1, "startColumnIndex": 0,
            "endColumnIndex": len(HEADERS)}

    reqs = [
        {"repeatCell": {
            "range": {"sheetId": gid, "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": {
                "textFormat": {"bold": True},
                "backgroundColor": {"red": 0.92, "green": 0.92, "blue": 0.94}}},
            "fields": "userEnteredFormat(textFormat,backgroundColor)"}},
        {"updateDimensionProperties": {
            "range": {"sheetId": gid, "dimension": "COLUMNS",
                      "startIndex": 1, "endIndex": 3},
            "properties": {"pixelSize": 260}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {
            "range": {"sheetId": gid, "dimension": "COLUMNS",
                      "startIndex": 4, "endIndex": 5},
            "properties": {"pixelSize": 220}, "fields": "pixelSize"}},
        {"addConditionalFormatRule": {"rule": {
            "ranges": [grid],
            "booleanRule": {
                "condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue":
                    f'=$%s2>{TH["hard_fail_ai_pct"]}' % _col_letter(ai_col)}]},
                "format": {"backgroundColor": {"red": 1.0, "green": 0.90, "blue": 0.90}}}},
            "index": 0}},
        {"addConditionalFormatRule": {"rule": {
            "ranges": [grid],
            "booleanRule": {
                "condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue":
                    f'=$%s2="needs_human_review"' % _col_letter(status_col)}]},
                "format": {"backgroundColor": {"red": 1.0, "green": 0.85, "blue": 0.75}}}},
            "index": 1}},
    ]
    sheets().spreadsheets().batchUpdate(spreadsheetId=sid,
                                        body={"requests": reqs}).execute()


def _col_letter(idx: int) -> str:
    out = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        out = chr(65 + rem) + out
    return out


def _row(rec, title: str) -> list:
    return [
        datetime.date.today().isoformat(),
        rec.source, title, rec.lang, rec.doc_url or "",
        rec.words or "", rec.fidelity or "", rec.grammar or "",
        rec.human_likeness or "", rec.ai_pct if rec.ai_pct is not None else "",
        rec.aeo or "", rec.passes, rec.status, rec.error or "",
    ]


def append(records: list, titles: dict[str, str]) -> str:
    """Append one row per record. Returns the spreadsheet URL."""
    if not records:
        return ""
    sid = _spreadsheet_id()
    values = [_row(r, titles.get(r.key, "")) for r in records]
    sheets().spreadsheets().values().append(
        spreadsheetId=sid, range=f"{TAB}!A1",
        valueInputOption="RAW", insertDataOption="INSERT_ROWS",
        body={"values": values}).execute()
    url = f"https://docs.google.com/spreadsheets/d/{sid}"
    log(f"tracker: appended {len(values)} row(s) -> {url}")
    return url


def recent(limit: int = 200) -> list[dict]:
    """Read the tracker back, newest first.

    The Library tab is built from state.jsonl, which does not survive a host
    without a disk. The same rows are already in the tracker, which does, so
    this is where the history comes from once the local ledger is gone.
    """
    # Deliberately NOT _spreadsheet_id(): that creates the tracker when none
    # exists, and opening a read-only tab must never conjure a spreadsheet into
    # the owner's Drive as a side effect.
    saved = read_json(STATE, default={}) or {}
    sid = _G.get("sheet_id") or saved.get("spreadsheet_id") or _find_tracker()
    if not sid:
        return []
    try:
        rows = (sheets().spreadsheets().values()
                .get(spreadsheetId=sid, range=f"{TAB}!A2:Z{limit + 1}")
                .execute().get("values") or [])
    except Exception as exc:
        warn(f"tracker: could not read '{TAB}' ({exc.__class__.__name__})")
        return []
    idx = {h: i for i, h in enumerate(HEADERS)}

    def cell(row, name):
        i = idx.get(name)
        return row[i] if i is not None and i < len(row) else ""

    def num(row, name):
        try:
            return float(cell(row, name))
        except (TypeError, ValueError):
            return None

    out = []
    for row in rows:
        if not any(c.strip() for c in row):
            continue
        out.append({
            "slug": cell(row, "Title"),
            "lang": cell(row, "Language"),
            "status": (cell(row, "Status") or "").lower().replace(" ", "_"),
            "ai_pct": num(row, "AI-likeness % (proxy)"),
            "human_likeness": num(row, "Human-likeness"),
            "aeo": num(row, "AEO score"),
            "fidelity": num(row, "Fidelity"),
            "words": num(row, "Words"),
            "doc_url": cell(row, "Doc Link"),
            "source": cell(row, "Source URL"),
            "ts": 0,
            "from_sheet": True,
        })
    out.reverse()
    return out
