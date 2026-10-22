# -*- coding: utf-8 -*-
"""
One-time Google Drive setup for the Recovery Desk. Run it on your own PC; it opens
a browser so you can sign in:

  pip install google-auth-oauthlib requests
  python scripts/google_drive_token.py client_secret.json universe_232_tickers.csv

What it does:
1. Signs in with the narrow `drive.file` scope (the desk sees only what it creates).
2. Creates a private folder "Recovery_Desk_Cloud" owned by you.
3. Uploads your universe CSV into it as universe.csv.
4. Prints the five values to store as secrets (GitHub environment `recovery-desk`,
   the worker's env file, and Streamlit app secrets). Never commit them.

client_secret.json: Google Cloud Console -> enable "Google Drive API" -> OAuth
consent screen (External, add yourself, then PUBLISH the app: tokens from apps in
"Testing" expire after 7 days) -> Credentials -> OAuth client ID -> Desktop app.
"""
import json
import secrets
import sys

from google.auth.transport.requests import AuthorizedSession
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPE = "https://www.googleapis.com/auth/drive.file"


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    client_file, universe_file = sys.argv[1], sys.argv[2]
    flow = InstalledAppFlow.from_client_secrets_file(client_file, [SCOPE])
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    s = AuthorizedSession(creds)

    r = s.post("https://www.googleapis.com/drive/v3/files",
               json={"name": "Recovery_Desk_Cloud", "mimeType": "application/vnd.google-apps.folder"})
    r.raise_for_status()
    folder = r.json()["id"]

    with open(universe_file, "rb") as f:
        csv_bytes = f.read()
    boundary = "recoverydeskboundary"
    meta = json.dumps({"name": "universe.csv", "parents": [folder]})
    body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
            f"--{boundary}\r\nContent-Type: text/csv\r\n\r\n").encode() + csv_bytes + f"\r\n--{boundary}--".encode()
    r = s.post("https://www.googleapis.com/upload/drive/v3/files", params={"uploadType": "multipart"},
               data=body, headers={"Content-Type": f"multipart/related; boundary={boundary}"})
    r.raise_for_status()

    with open(client_file) as f:
        client = json.load(f)
    client = client.get("installed") or client.get("web")
    print("\nStore these as secrets (never in git):\n")
    print("GOOGLE_OAUTH_CLIENT_ID=" + client["client_id"])
    print("GOOGLE_OAUTH_CLIENT_SECRET=" + client["client_secret"])
    print("GOOGLE_OAUTH_REFRESH_TOKEN=" + creds.refresh_token)
    print("RECOVERY_DRIVE_FOLDER_ID=" + folder)
    print("RECOVERY_SNAPSHOT_KEY=" + secrets.token_hex(32))
    print("\nUse the SAME RECOVERY_SNAPSHOT_KEY everywhere: it signs and verifies every snapshot.")


if __name__ == "__main__":
    main()
