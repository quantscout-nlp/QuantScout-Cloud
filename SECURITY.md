# Recovery Desk security

The desk reads market data, applies the locked rules and publishes a signed snapshot. It cannot trade. The design assumes the repository is public, so the code is visible to anyone, and keeps everything secret or personal somewhere else.

## Built in

| Threat | Protection |
|---|---|
| Something places an order | No order code exists in the desk. It calls market-data hosts only, plus Alpaca's market calendar on the **paper** host. No broker SDK is installed |
| The desk drifts into the QuantScout engine | Its own orphan branch, its own GitHub Environment, its own `DESK_*` secrets, Drive folder and Streamlit app. The failover workflow on `main` never references the engine's secrets |
| Stolen secrets from the public repo or its logs | No secrets in code. GitHub Environment secrets are encrypted and masked in logs. Logs print counts and health only, never tickers or errors. No artifacts are uploaded and nothing is committed back |
| Pull requests from forks steal secrets | The failover runs only on `schedule` and `workflow_dispatch`, and dispatch needs write access. Fork PRs never receive secrets. The test workflow has no secrets |
| A malicious update of an Action or a package | Actions pinned to full commit SHAs. Python packages pinned to exact versions. No trading SDK, no scraper. The Webull signer is reimplemented and checked against the official SDK by a test, so Webull's trading code is never installed |
| A leaked GitHub token is misused | The job's token is `contents: read`. `persist-credentials: false`, so the token isn't left on disk |
| A leaked Drive token exposes your Drive | The `drive.file` scope only reaches files the desk created itself. Your trading packs, business and healthcare documents are invisible to it |
| Another bot, AI agent or person edits the desk's files in Drive | Everything the desk reads back (state, daily cache, change history) and the snapshot the dashboard shows is HMAC-signed with `RECOVERY_SNAPSHOT_KEY`. The desk ignores, and the dashboard refuses, anything whose signature fails, and reports it |
| A forged or edited Robinhood book | The dashboard is the only writer of `held.json`. It saves only after you preview the file, needs the snapshot key, and signs it. An unsigned or edited book is ignored and reported. Uploads are size-limited, symbols are validated and formula-like cells are dropped. No account number is kept beyond a masked tail |
| Prompt injection from "stray AI models" (e.g. a headline written to steer an AI) | The desk calls no AI model and never executes text. Headlines are displayed as plain text only |
| Bad or missing data turns into a false PASS | It fails closed: a stale feed blocks all new candidates, missing data is `NOT_SCANNED`, no 5-minute feed is `NO_5MIN_FEED`, and Anti-Knife HOLD/FAIL is never promoted |
| A provider bans the keys | Per-provider rate limits, exponential backoff and honouring `Retry-After` |
| The worker VM is attacked | Runs as an unprivileged user with read-only code, under a systemd sandbox (`ProtectSystem=strict`, no capabilities, no new privileges). No inbound ports. Automatic OS security updates. The secrets file is readable only by root and the desk user |

## Your checklist (settings code can't change)

1. **Accounts.** Turn on 2FA or a passkey for GitHub, Google, Alpaca, Webull and Streamlit. Whoever controls your GitHub account controls the desk.
2. **Branch protection** on `main` and `recovery-desk`: require a pull request with your approval, and block force-pushes and deletion. Any AI agent, including Claude, then has to go through a PR you approve.
3. **Actions settings.** Set the default workflow token to read-only. Don't allow Actions to create or approve PRs. Require approval for workflows from outside contributors.
4. **Environment `recovery-desk`.** Restrict deployment branches to `main`. Optionally add yourself as a required reviewer for manual runs.
5. **Dedicated keys.** Use a separate Alpaca paper account for the desk. Use a Webull app key with market-data permission only, if Webull offers that split. Never reuse the engine's keys.
6. **Streamlit.** Make the app private and invite only your account. Anyone who can open the app can replace the saved Robinhood book. Only add `GITHUB_TOKEN` if you want the Refresh button. That token can start *any* workflow in this repo, so scope it to this repo with Actions: write only.
7. **Google OAuth app.** Publish it (see the docs) and keep `client_secret.json` off shared drives.
8. **If anything leaks:**
   - Rotate that key.
   - Revoke the Google token at myaccount.google.com → Security → Third-party access.
   - Generate a new `RECOVERY_SNAPSHOT_KEY`, then set it everywhere.

## Residual risks

- Losing control of your GitHub or Google account bypasses everything above. That's what checklist item 1 is for.
- Webull app keys belong to your brokerage account. The desk only calls the market-data endpoint, but a stolen key may carry trading permission. Keep it only in the environment and the worker's env file.
- The failover workflow records a GitHub "deployment" on each run. That's noise in the Environments page, not a risk.
