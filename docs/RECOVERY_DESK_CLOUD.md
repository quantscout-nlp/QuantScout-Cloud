# Recovery Desk: design, data feeds, cadence and setup

Updated Thursday 22 October 2026. This desk screens and labels only. Orders stay OFF, a human submits every order, and none of it is trade advice.

## 1. Where it lives

| Piece | Where | Shares anything with the QuantScout engine? |
|---|---|---|
| Desk code, tests, dashboard, deploy files | Branch `recovery-desk` (an orphan branch with no engine history) | No |
| Failover schedule | `.github/workflows/recovery-desk-failover.yml` on `main` | Only the repo itself. GitHub runs schedules only from the default branch, so this one file must live there. It checks out `recovery-desk` and runs that code |
| Secrets | GitHub Environment `recovery-desk` (`DESK_*` names), the worker's `/etc/recovery-desk.env`, and the dashboard's Streamlit secrets | No. The engine's repo secrets are never referenced |
| Data and history | The desk's own private Drive folder `Recovery_Desk_Cloud` | No |
| Dashboard | Its own Streamlit app: branch `recovery-desk`, file `recovery_app.py` | No. It's a separate app from `quantscout_cloud.py` |

## 2. How it runs: worker first, free schedule as backup

- **Primary: always-on worker (Option B).** `deploy/setup_vm.sh` sets it up on a small Linux VM. A Google Cloud e2-micro in us-east1, us-central1 or us-west1 falls in Google's always-free tier. Otherwise budget about $5–15/month elsewhere. It wakes 20 seconds after every 5-minute bar closes.
- **Failover: GitHub schedule (Option A, $0).** Runs every 5 minutes. While the worker has published in the last 10 minutes it stands down. If the worker goes quiet, it takes over automatically. "Refresh now" on the dashboard forces one run.

Option A alone does refresh automatically, but it isn't real time. GitHub starts scheduled runs on a best-effort basis: often 3–15 minutes late, sometimes skipped. That's why it's the backup.

## 3. Refresh schedule and cadence (weekdays, Eastern Time)

| Time (ET) | What happens |
|---|---|
| 08:00:20, then every 5 min | **Premarket cycles.** Tier 1 / Cap setups come from the last *official* close. AK-D uses today's developing premarket low. AK-S = `PREMARKET_NOT_APPLICABLE` |
| First cycle of the day | Builds the daily qualification if it's missing. Normally it was already built at 16:05 the day before |
| 09:35:20 | First regular-hours cycle: the 09:30–09:35 bar is complete. AK-S starts scoring from this one bar. No full 78-bar session is needed |
| Every 5 min to 16:00:20 | Each cycle uses only completed bars. AK is recomputed from today's bars, so yesterday's PASS can't carry over |
| From 09:40 | **Stale-feed rule:** no new completed bar for 10 minutes means health turns STALE and every new candidate is blocked. It clears automatically on the next good bar |
| 16:05:20 | **Daily rebuild** from the official close: Rule 1/2/3, battery, RVOL20, NEWS token, EPS. This is the next-day preview. The end-of-day record `session_YYYY-MM-DD.json` is saved |
| to 16:30, then idle | Postmarket cycles stop. The worker idles until 08:00 the next weekday |
| Holidays, early closes | Read from the Alpaca market calendar. A 13:00 early close rebuilds at 13:05 |
| Dashboard | Re-reads the snapshot every 60 seconds |

**Latency, worst case:** bar close + 20 s settle + about 5–15 s cycle + up to 60 s dashboard refresh ≈ **1.5 minutes** to your screen.

**API calls per cycle, intraday:** Alpaca about 4–6. Webull 0 normally; it is called only for names Alpaca misses, up to 12 calls if all 232 need it. The once-a-day rebuild uses about 30 Alpaca calls plus Massive EPS lookups for active names.

## 4. Data feeds and fallbacks

| Data | 1st | 2nd | Then |
|---|---|---|---|
| Live 5-min bars (AK-S, AK-D, VWAP) | Alpaca SIP | **Webull OpenAPI** (real time) | `NO_5MIN_FEED`. A missing feed never produces a pass |
| Daily bars (qualification) | Alpaca SIP | **Webull OpenAPI** | Massive, then Tiingo |
| RVOL20 volume | SIP regular hours | **Webull regular hours** | That symbol's daily-bar volume, labelled |
| News token | Alpaca (Benzinga) | none | `NEWS_NOT_SEARCHED` |

- **Webull.** Its 5-minute bars report `delay_minutes: 0`, so it is real time and a better live fallback than Massive Stocks Developer (15-minute delayed). Massive never feeds the live gates. Webull needs an OpenAPI App Key/Secret and an **OpenAPI market-data subscription**; without one it answers 403. If your account enforces Webull's 2FA token, also set `WEBULL_ACCESS_TOKEN`. The request signature reproduces Webull's official SDK exactly (a unit test checks this), so the desk installs none of Webull's trading code.
- **RVOL20.** SIP is first and Webull is the fallback. Each ratio is built from **one** source and never mixed, and the label says which (`SIP_RTH` or `WEBULL_RTH`). IEX volume is partial, so it is never used for RVOL.
- **The basis gap.** The ZS example on 7 October (0.817 Webull vs 0.763 SIP) is a 0.054 gap. That sits inside the `VOL_BORDERLINE` band of 0.72–0.88, so any name the gap could flip is already flagged for a manual look.
- **This is a rule change.** Locked rule v1.6.1 says Webull RTH. Set `DESK_RVOL_BASIS=WEBULL` to go back to the locked basis. If you keep SIP, update the rulebook to v1.6.2 so the desktop and Surface 1 bots label names the same way.
- **Not used:** yfinance (a scraper, throttled from cloud servers) and Seeking Alpha Pro (no API, and scraping is against its terms).

## 5. Robinhood positions

The desk can't use a Robinhood MCP connector:

- An MCP connector is a plug-in for an AI assistant, signed in as you with your account's permissions. A scheduled cloud job isn't an AI assistant, and connectors attached to a chat aren't reachable from a VM or GitHub Actions.
- No Robinhood connector is attached to this session.
- Robinhood has no official equities API.
- Giving an unattended server a brokerage login would undo the safety model, which is read-only data with orders OFF.

Safe alternative: a read-only positions file. You (or the desktop HITL plane) upload the Robinhood positions CSV through the dashboard. The desk then prints the held-name stop block (`EXIT_FLAG` / `STOP_NOT_COMPUTED`). This part isn't built yet.

## 6. Statuses

| Status | Meaning |
|---|---|
| `TIER1` | Rule 2 stack + MACD > 0 + Hist0 > 0 + battery +2..+4 + RVOL20 ≥ 0.80. `paper_candidate` unless blocked |
| `CAP_PAPER` | Rule 1 passes **and** AK Combined PASS in the live session |
| `CAP_WAIT_AK` | Rule 1 passes; AK is HOLD, NOT_SCORED, PREMARKET_NOT_APPLICABLE or NO_5MIN_FEED |
| `CAP_SETUP` | Rule 1 passes; the next session hasn't started |
| `TIER2` | Stack met, another gate fails (`BAT +1`, `MACD <= 0`, `failed-reclaim`, `RVOL20 … < 0.80`) |
| `CAP_AK_BLOCK` | Rule 1 passes but Anti-Knife FAIL |
| `WATCH_HOLD` | Everything else; `STACK N` and `no SMA200` live here |
| `NOT_SCANNED` | No qualifying data. Never a silent pass |

These block a paper candidate: a non-session day, a stale feed, and the wash-sale list (`DESK_WASH_SALE`, e.g. `STX,WDC:2026-10-17`). Interpretations to review are marked `INTERPRETATION` in `recovery/rules.py`.

## 7. Setup, one step at a time

1. **Google OAuth client.** In Google Cloud Console, enable the Drive API. Set up the OAuth consent screen (External), then **Publish app**: tokens from apps left in "Testing" expire after 7 days. Create an OAuth client ID of type *Desktop app* and download `client_secret.json`.
2. **Drive folder and keys.** On your PC run `pip install google-auth-oauthlib requests`, then `python scripts/google_drive_token.py client_secret.json universe_232_tickers.csv`. It creates the private `Recovery_Desk_Cloud` folder, uploads the universe, and prints 5 values.
3. **GitHub environment.** In GitHub → Settings → Environments, create **`recovery-desk`** and restrict deployment branches to `main`. Add these secrets:
   - `DESK_ALPACA_ID`, `DESK_ALPACA_SECRET` (a separate Alpaca paper account just for the desk)
   - `DESK_WEBULL_APP_KEY`, `DESK_WEBULL_APP_SECRET`, `DESK_MASSIVE_KEY`, `DESK_TIINGO_KEY`
   - `DESK_GOOGLE_OAUTH_CLIENT_ID`, `DESK_GOOGLE_OAUTH_CLIENT_SECRET`, `DESK_GOOGLE_OAUTH_REFRESH_TOKEN`
   - `DESK_DRIVE_FOLDER_ID`, `DESK_SNAPSHOT_KEY`

   Optional variables: `DESK_RVOL_BASIS` (`SIP` or `WEBULL`) and `DESK_WASH_SALE`.
4. **Test the failover.** Merge the failover workflow PR into `main`, then go to Actions → *Recovery Desk failover* → *Run workflow* (force = true).
5. **Worker.** Create the VM (e2-micro, Debian 12, no external ingress except SSH). Then run `curl -fsSLO https://raw.githubusercontent.com/quantscout-nlp/QuantScout-Cloud/recovery-desk/deploy/setup_vm.sh && sudo bash setup_vm.sh`. Fill `/etc/recovery-desk.env` with the same values but without the `DESK_` prefix (the template lists them). Finally run `sudo systemctl enable --now recovery-desk`.
6. **Dashboard.** In Streamlit Community Cloud, create a new app: repo `quantscout-nlp/QuantScout-Cloud`, branch `recovery-desk`, file `recovery_app.py`.
   - Secrets: the Google values, `RECOVERY_DRIVE_FOLDER_ID`, and `RECOVERY_SNAPSHOT_KEY`.
   - Optional: `GITHUB_TOKEN` for the Refresh button (a fine-grained token, this repo only, Actions: write).
   - Then set sharing to private and invite only yourself.
7. **Security settings.** Work through the checklist in `SECURITY.md`.

Run the tests with `python -m unittest discover -s tests`.
