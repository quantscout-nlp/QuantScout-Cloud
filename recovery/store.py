# -*- coding: utf-8 -*-
"""
Where the desk keeps its state and publishes snapshots.

This repository is public, so nothing the desk produces is committed here.
- LocalStore: a folder. Used by the always-on worker and for local runs.
- DriveStore: one private Google Drive folder, the same "mailbox" the desktop
  HITL plane already uses. Scheduled GitHub Actions runs keep state here between
  runs, and the dashboard reads from here.

Drive auth, either:
- OAuth user token (works with a personal gmail.com Drive): GOOGLE_OAUTH_CLIENT_ID,
  GOOGLE_OAUTH_CLIENT_SECRET, GOOGLE_OAUTH_REFRESH_TOKEN. Get the refresh token
  once with scripts/google_drive_token.py.
- Service account JSON in GOOGLE_SERVICE_ACCOUNT_JSON. Only for a Workspace
  Shared Drive: service accounts have no My Drive storage quota, so they can't
  create files in a personal folder.

Scope is `drive.file`: the desk can only see files and folders it created itself.
A leaked desk token can't read or change anything else in your Drive (trading
packs, business documents), and other apps' files are invisible to the desk.
"""
import json
import os
from pathlib import Path
from typing import Dict, Optional

DRIVE = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"
ALL_DRIVES = {"supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}


class LocalStore:
    kind = "local"

    def __init__(self, root: str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def read_text(self, name: str) -> Optional[str]:
        p = self.root / name
        return p.read_text(encoding="utf-8") if p.exists() else None

    def write_text(self, name: str, text: str, mime: str = "application/json"):
        tmp = self.root / (name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self.root / name)      # atomic: the dashboard never reads a half file

    def read_file_id(self, file_id: str) -> Optional[str]:
        return None


class DriveStore:
    kind = "drive"

    def __init__(self, folder_id: str, session):
        self.folder = folder_id
        self.s = session
        self._ids: Dict[str, str] = {}

    @classmethod
    def from_env(cls, env=os.environ) -> Optional["DriveStore"]:
        folder = env.get("RECOVERY_DRIVE_FOLDER_ID", "")
        if not folder:
            return None
        from google.auth.transport.requests import AuthorizedSession
        scopes = [DRIVE_SCOPE]
        if env.get("GOOGLE_OAUTH_REFRESH_TOKEN"):
            from google.oauth2.credentials import Credentials
            creds = Credentials(None, refresh_token=env["GOOGLE_OAUTH_REFRESH_TOKEN"],
                                client_id=env.get("GOOGLE_OAUTH_CLIENT_ID"),
                                client_secret=env.get("GOOGLE_OAUTH_CLIENT_SECRET"),
                                token_uri="https://oauth2.googleapis.com/token", scopes=scopes)
        elif env.get("GOOGLE_SERVICE_ACCOUNT_JSON"):
            from google.oauth2 import service_account
            creds = service_account.Credentials.from_service_account_info(
                json.loads(env["GOOGLE_SERVICE_ACCOUNT_JSON"]), scopes=scopes)
        else:
            raise RuntimeError("RECOVERY_DRIVE_FOLDER_ID is set but no Google credentials were given")
        return cls(folder, AuthorizedSession(creds))

    def _find(self, name: str) -> Optional[str]:
        if name in self._ids:
            return self._ids[name]
        q = f"name = '{name}' and '{self.folder}' in parents and trashed = false"
        r = self.s.get(f"{DRIVE}/files", params={"q": q, "fields": "files(id,name)", **ALL_DRIVES}, timeout=30)
        r.raise_for_status()
        files = r.json().get("files", [])
        if files:
            self._ids[name] = files[0]["id"]
            return files[0]["id"]
        return None

    def read_text(self, name: str) -> Optional[str]:
        fid = self._find(name)
        return self.read_file_id(fid) if fid else None

    def read_file_id(self, file_id: str) -> Optional[str]:
        r = self.s.get(f"{DRIVE}/files/{file_id}", params={"alt": "media", "supportsAllDrives": "true"}, timeout=60)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.content.decode("utf-8-sig")

    def write_text(self, name: str, text: str, mime: str = "application/json"):
        body = text.encode("utf-8")
        fid = self._find(name)
        if fid:
            r = self.s.patch(f"{DRIVE_UPLOAD}/files/{fid}", params={"uploadType": "media", "supportsAllDrives": "true"},
                             data=body, headers={"Content-Type": mime}, timeout=60)
        else:
            boundary = "recoverydeskboundary"
            meta = json.dumps({"name": name, "parents": [self.folder]})
            payload = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
                       f"--{boundary}\r\nContent-Type: {mime}\r\n\r\n").encode("utf-8") + body + \
                      f"\r\n--{boundary}--".encode("utf-8")
            r = self.s.post(f"{DRIVE_UPLOAD}/files", params={"uploadType": "multipart", "supportsAllDrives": "true"},
                            data=payload, headers={"Content-Type": f"multipart/related; boundary={boundary}"}, timeout=60)
        r.raise_for_status()
        self._ids[name] = r.json()["id"]


def open_store(env=os.environ, local_dir: Optional[str] = None):
    """Drive when configured, else a local folder."""
    return DriveStore.from_env(env) or LocalStore(local_dir or env.get("RECOVERY_LOCAL_DIR", "recovery_out"))
