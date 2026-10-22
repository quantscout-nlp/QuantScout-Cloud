# -*- coding: utf-8 -*-
"""
One-time helper: get a Google Drive refresh token for the Recovery Desk.

Run it on your own PC (it opens a browser to sign in):
  pip install google-auth-oauthlib
  python scripts/google_drive_token.py path\\to\\client_secret.json

client_secret.json comes from Google Cloud Console -> APIs & Services ->
Credentials -> Create credentials -> OAuth client ID -> Desktop app (enable the
Google Drive API first). Paste the three printed values into GitHub Actions
secrets and Streamlit secrets. Never commit them.
"""
import json
import sys

from google_auth_oauthlib.flow import InstalledAppFlow


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    flow = InstalledAppFlow.from_client_secrets_file(sys.argv[1], ["https://www.googleapis.com/auth/drive"])
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    client = json.load(open(sys.argv[1]))
    client = client.get("installed") or client.get("web")
    print("GOOGLE_OAUTH_CLIENT_ID=" + client["client_id"])
    print("GOOGLE_OAUTH_CLIENT_SECRET=" + client["client_secret"])
    print("GOOGLE_OAUTH_REFRESH_TOKEN=" + creds.refresh_token)


if __name__ == "__main__":
    main()
