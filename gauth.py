"""One Google OAuth flow shared by the Docs, Sheets and Drive clients."""
from __future__ import annotations

from pathlib import Path

from common import ROOT, MissingCredential, config, log

SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

_G = config()["google"]
CRED_PATH = ROOT / _G["credentials"]
TOKEN_PATH = ROOT / _G["token"]

_SERVICES: dict[str, object] = {}


def credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if TOKEN_PATH.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        except Exception:
            creds = None

    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
        return creds

    if not CRED_PATH.exists():
        raise MissingCredential(
            "Google OAuth client secrets (credentials.json)",
            "creating the translation Docs and writing rows to the tracker Sheet",
            "In Google Cloud Console: create a project, enable the Google Docs, "
            "Google Sheets and Google Drive APIs, then Credentials -> Create "
            "credentials -> OAuth client ID -> Desktop app -> Download JSON. "
            f"Save it as {CRED_PATH}. The first run opens a browser once to "
            "authorise; the token is cached afterwards.")

    log("google: opening a browser for one-time authorisation")
    flow = InstalledAppFlow.from_client_secrets_file(str(CRED_PATH), SCOPES)
    creds = flow.run_local_server(port=0)
    TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    log("google: token saved")
    return creds


def service(api: str, version: str):
    key = f"{api}{version}"
    if key not in _SERVICES:
        from googleapiclient.discovery import build
        _SERVICES[key] = build(api, version, credentials=credentials(),
                               cache_discovery=False)
    return _SERVICES[key]


def docs():
    return service("docs", "v1")


def sheets():
    return service("sheets", "v4")


def drive():
    return service("drive", "v3")


def ensure_folder(name: str, parent: str | None = None) -> str:
    """Find or create a Drive folder, returning its id."""
    drv = drive()
    q = (f"name = '{name}' and mimeType = 'application/vnd.google-apps.folder' "
         "and trashed = false")
    if parent:
        q += f" and '{parent}' in parents"
    found = drv.files().list(q=q, fields="files(id,name)", pageSize=1).execute()
    if found.get("files"):
        return found["files"][0]["id"]

    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent:
        meta["parents"] = [parent]
    created = drv.files().create(body=meta, fields="id").execute()
    log(f"google: created Drive folder '{name}'", indent=1)
    return created["id"]
