# Cloud Recovery Desk: assessment, design and setup

Status as of Thursday 22 October 2026. This is a screening and status desk only. Orders stay OFF, a human submits every order, and none of it is trade advice.

## 1. What the screenshots got right, and what was missing

Review of the "Dave's Recovery Desk" conversation (ChatGPT sites page, evidence ending 16 October):

| Point in the screenshots | Verdict | What this build does |
|---|---|---|
| The desk is a saved snapshot, not a live scanner | Correct | Replaces the snapshot with a cycle that refetches data every 5 minutes |
| "Automatic" (who starts the work) differs from "real time" (how fresh the data is) | Correct | Both are covered: the scheduler starts the work, and the 5-minute SIP bars keep the data fresh |
| A private background service with credentials held on the server | Correct | Keys live in GitHub Actions / host secrets. Snapshots live in your private Drive folder |
| Reset AK to `NOT_SCORED` at each new session, with no carry-forward | Correct | AK is recomputed from today's bars on every cycle, so it can't carry over. A test checks this |
| AK needs a full 78-bar session, so it can't drive live decisions | Correct diagnosis | AK-S now scores from the first completed bar, using the locked text (close vs VWAP, low held ≥ 3 bars) |
| Persistent storage, a Refresh Now button, a last-update time, a feed-delay badge, a health indicator | Correct | All built: Drive store, `workflow_dispatch` button, and health / delay / coverage badges |
| Keep order execution off | Correct, and required by the hard bans | No broker code exists in `recovery/` |
| Which subscription feeds the screen without throttling | Answer was cut off | See section 3 |
| Where to host it | Not specified | See section 4 |

Three issues the screenshots did not raise:

1. **This GitHub repo is public.** Anything committed here, including the existing `bot-trial-log` branch, and every Actions log is world-readable. The new desk therefore never writes to the repo and never uploads artifacts. Its logs print counts only, and all output goes to a private Drive folder. Consider making the repo private, or moving the trial log to Drive as well.
2. **RVOL20 basis.** The locked rule (v1.6.1) uses **Webull RTH** volume. In the cloud, the desk reads **Alpaca SIP RTH** volume (summed from 30-minute bars between 09:30 and 16:00) and labels it `vol_source=SIP_RTH`. The two bases can disagree near 0.80. The ZS example on 7 October was 0.817 on Webull and 0.763 on SIP. The `VOL_BORDERLINE` flag (0.72–0.88) is kept, so check those names by hand. **Your decision:** accept the SIP basis in the cloud, or add a Webull OpenAPI adapter later.
3. **Held-name stop block.** It needs your Robinhood positions. Robinhood has no official API, so this part is not automated. The desk still computes each name's Rule 3 stop (SMA20 for Mom, SMA50 for Cap) and adds a "LIVE below stop" flag intraday.

## 2. What runs

```
            ┌──────────── every 5 min, 12:00–21:59 UTC weekdays (GitHub Actions)
            │             or right after each 5-min bar close (always-on worker)
            ▼
 scripts/run_recovery_desk.py ──► recovery/desk.py run_cycle()
            │
            ├─ market clock ........ Alpaca calendar (holidays, early closes)
            ├─ universe ............ universe_232_tickers.csv read from Drive by file ID
            ├─ daily qualification . once per completed session, cached
            │     Alpaca SIP 1Day bars → Massive → Tiingo → yfinance (labelled per name)
            │     RTH volume from SIP 30-min bars → RVOL20
            │     Benzinga news via Alpaca → NEWS_CONFIRMED / STALE / ABSENT / NOT_SEARCHED
            │     Massive TTM EPS (informational, never a gate)
            │     Rule 1 / Rule 2 / battery / Rule 3 / tiers  (recovery/rules.py)
            ├─ session overlay ..... today's completed 5-min bars only (incl. premarket for AK-D)
            │     AK-D, AK-S, AK Combined for Cap names; VWAP; live-below-stop flags
            │     feed-stale guard: no fresh bar for 10 min in session → every new eligibility blocked
            ├─ diff ................ status changes → changes_YYYY-MM-DD.jsonl (when and why)
            └─ publish ............. latest.json + latest.csv to the private Drive folder;
                                     session_YYYY-MM-DD.json saved after the close
                                                │
                                                ▼
                     Streamlit page "Recovery Desk" (pages/1_Recovery_Desk.py), read-only
```

Statuses, in order of precedence:

| Status | Meaning |
|---|---|
| `TIER1` | Rule 2 stack + MACD > 0 + Hist0 > 0 + battery +2..+4 + RVOL20 ≥ 0.80. `paper_candidate` is true unless something blocks it |
| `CAP_PAPER` | Rule 1 passes and AK Combined is PASS in the live session |
| `CAP_WAIT_AK` | Rule 1 passes; AK is HOLD, NOT_SCORED, PREMARKET_NOT_APPLICABLE or NO_5MIN_FEED |
| `CAP_SETUP` | Rule 1 passes; the next session hasn't started, so AK isn't scored yet |
| `TIER2` | Stack met, another gate fails (`BAT +1`, `MACD <= 0`, `failed-reclaim`, `RVOL20 … < 0.80`) |
| `CAP_AK_BLOCK` | Rule 1 passes but Anti-Knife FAIL (falling knife) |
| `WATCH_HOLD` | Everything else; `STACK N` and `no SMA200` live here |
| `NOT_SCANNED` | No qualifying data. Never a silent pass |

These things block `paper_candidate`: a non-session day, a stale feed, and the wash-sale list (`RECOVERY_WASH_SALE`, e.g. `STX,WDC,AVGO:2026-10-17`).

Interpretations to review (marked `INTERPRETATION` in `rules.py`):

- Tier 1 needs the close to be above SMA20. A new Paper can't open below its own EXIT level.
- When a name passes Rule 1 but misses Tier 1, Rule 1 takes precedence over Tier 2.

## 3. Your data subscriptions: which one feeds the screen

Nothing is truly "unlimited". What matters is having headroom over what a cycle needs. With bulk multi-symbol endpoints, one cycle over 232 names needs about 4–6 Alpaca calls intraday. The once-a-day rebuild needs about 20–30.

| Subscription | Real-time? | Limit | Role in the desk |
|---|---|---|---|
| **Alpaca Algo Trader Plus** (SIP + IEX + Benzinga news) | Yes, full SIP | 10,000 REST calls/min, unlimited stream symbols ([Alpaca docs](https://docs.alpaca.markets/docs/about-market-data-api)) | **Primary for everything live**: 5-min bars, daily bars, RTH volume, calendar, news. About 0.1% of the limit is used |
| **Massive (formerly Polygon) Stocks Developer** | Mostly listed as 15-min delayed on this tier; one source says real-time IEX ([pricing summary](https://apicostcalc.com/blog/polygon-massive-rebrand-api-pricing.html)). Check on your plan page | Unlimited calls | Fallback daily bars, TTM EPS, and authoritative end-of-day / flat files (as LeanData already does). Never used for the intraday AK gate |
| **Tiingo** (news, price, fundamentals) | End-of-day for this use | Hourly / daily request caps; check your dashboard ([older figures](https://www.quantstart.com/articles/evaluating-data-coverage-with-tiingo/)) | Daily-bar fallback |
| **yfinance + Yahoo Finance Gold** | No | Unofficial scraper. Cloud IPs get throttled, and Gold does not grant API access | Last resort, labelled `yfinance` |
| **Seeking Alpha Pro** | No | No public API, and the terms forbid scraping | Manual research only |
| **Google Drive** | n/a | Generous | Universe in, snapshots and history out, read by the dashboard |

If your Alpaca keys turn out not to carry the SIP entitlement, the desk switches to IEX automatically. When that happens, the dashboard shows the feed as IEX with a warning: IEX has partial volume, so VWAP is approximate. Note that the keys in this repo are paper keys.

## 4. Hosting options

| | A. GitHub Actions schedule (built, $0) | B. Always-on worker (recommended for intraday) |
|---|---|---|
| How it runs | `.github/workflows/recovery-desk.yml`, every 5 min | `Dockerfile.recovery` running `--loop` on a small VM, Fly.io, Railway or Cloud Run with min-instances 1 |
| Timing | GitHub cron is best effort: often 3–15 min late, sometimes skipped, plus about 40 s of job startup | Wakes 20 s after each 5-min bar closes. The dashboard shows it within about 1–1.5 min |
| Cost | Free (Actions minutes are free on public repos) | About $5–15/month |
| State | Read from and written to Drive on every run | Same code |
| Good for | Daily qualification, 08:30 premarket, postmarket rebuild, slow intraday | AK-S timing that matches the bar close |

For a 5-minute-bar rule, polling right after the bar closes is as fresh as a websocket. A websocket only adds value for tick-level alerts, and Algo Trader Plus supports it if you want that later.

## 5. Advantages and limitations of cloud, real-time hosting

**Advantages**

- Runs when your PC, browser or phone is off. It doesn't depend on `F:\`, File Stream sync or the 17:35 desktop import.
- Identical rule code every cycle, with an audit trail of what changed, when and why.
- Credentials stay server-side, never in a page or chat.
- Health is visible: last update, feed, data delay, coverage (`N of 232 scanned`), 429 count, errors.
- Fails closed. A stale feed, missing data or a non-session day blocks eligibility instead of passing silently.
- Phone-friendly dashboard. Drive output can still feed the desktop HITL plane.

**Limitations**

- **Not tick real time.** Statuses move on completed 5-min bars, and Tier 1 / Rule 1 move on the official daily close (by design: "intraday volume never makes Paper").
- **Scheduler jitter** on option A (see section 4). Option B fixes it.
- **Provisional bars.** Today's daily bar can still be revised. Volume shifts most until Massive's file is ingested, which LeanData marks as `provisional`.
- **RVOL basis** is SIP, not Webull (section 1).
- **No Robinhood positions**, so no automated held-name stop block.
- **Public repo**: logs and anything committed are public. Keep output in Drive.
- **Service accounts can't write to a personal gmail Drive**, so use the OAuth refresh-token path (section 6).
- **Streamlit Community Cloud** apps from public repos are public by default. Set the app to private and invite only your account.
- **Third-party outages** (Alpaca, Drive, GitHub) pause the desk. The health badge turns STALE or DEGRADED and nothing is promoted.
- **Not verified against your live keys yet.** This build was tested offline with the rule fixtures from your Drive files (AMGN, IQV, MDB and CRDO Anti-Knife examples). The build sandbox could not reach Alpaca, Massive or Tiingo directly. The first real run will show any feed issues on the health badge.

## 6. Setup (one step at a time)

1. **Create a private Drive folder** (for example `Recovery_Desk_Cloud`) and copy its ID from the URL.
2. **Copy the universe ID.** The latest `universe_232_tickers.csv` already lives in Drive. Copy its file ID, or upload a copy named `universe.csv` into the folder.
3. **Make a Google OAuth client.** In Google Cloud Console, enable the Drive API, then create an OAuth client ID of type *Desktop app* and download `client_secret.json`.
4. **Get a refresh token.** On your PC run `pip install google-auth-oauthlib` and then `python scripts/google_drive_token.py client_secret.json`. Keep the three printed values.
5. **Add GitHub secrets.** In GitHub → Settings → Secrets and variables → Actions, add:
   - `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REFRESH_TOKEN`
   - `RECOVERY_DRIVE_FOLDER_ID`, `RECOVERY_UNIVERSE_FILE_ID`

   `ALPACA_ID`, `ALPACA_SECRET`, `POLYGON_KEY` and `TIINGO_KEY` already exist.
6. **Test once.** Actions → *Recovery Desk (screening only)* → *Run workflow*. Check `latest.json` in the Drive folder and the health line in the log.
7. **Connect the dashboard.** Add the same Google values plus `RECOVERY_DRIVE_FOLDER_ID` to the Streamlit app secrets. For the Refresh button, add `GITHUB_TOKEN` (fine-grained, this repo only, *Actions: write*). Then set the app to private.
8. **Optional, for option B.** Run `Dockerfile.recovery` on an always-on host with the same variables. Once it's live, disable the Actions schedule so the two don't race.

Run the tests with `python -m unittest discover -s tests`.
