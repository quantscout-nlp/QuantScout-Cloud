# Recovery Desk — project status

As of 2026-11-25. Update this file whenever a setup step or phase completes. No secrets, account identifiers or tickers belong here: the repository is public.

## Built (on this branch, 39 tests green in CI)

- Rules engine (Rule 1/2/3, battery, tiers, RVOL20, AK-D / AK-S / AK Combined), locked to rulebook v1.6.2.
- Data feeds: Alpaca SIP first; Webull, Massive, Tiingo as optional fallbacks.
- Safety: stale-feed block, signed files, tamper rejection, no order code.
- Dashboard with tiers, change history, health line, Refresh button, Robinhood upload with per-name Cap/Mom book, held-name stop block, Backtest evidence.
- Always-on worker plus the GitHub backup schedule (merged to `main` 2026-10-26 via PR #6; runs every 5 minutes, 12:00–21:59 UTC on weekdays).
- Hardened VM setup and the one-command e2-micro script (`deploy/`).
- Tradability test (`scripts/run_backtest.py`); not yet run on real data.
- Weekly paper report (`scripts/weekly_report.py`, Saturday 09:00 ET timer on the VM). Week 1 (Nov 16–20) report: 0 of 5 sessions captured, because the desk has not run yet.

## Setup (docs/RECOVERY_DESK_CLOUD.md §7)

| Step | State |
|---|---|
| 1. Alpaca paper keys for the desk | Done |
| 2. Google connection: Drive API, consent screen published to production, desktop client, token script run | Done |
| 3. GitHub Environment `recovery-desk` with the `DESK_*` secrets and `DESK_RVOL_BASIS=SIP` | Still not visible to the workflow: all 44 backup runs (2026-10-26 to 2026-11-24) saw every secret empty and refused to run. Recheck names and environment |
| 4. Merge PR #6, then Actions → Recovery Desk failover → Run workflow (force = true) | Merged 2026-10-26. First forced run refused safely (no Drive folder ID); rerun after step 3 is fixed |
| 6. Streamlit dashboard from this branch, private | Next, after the first good run |
| 5. Always-on e2-micro worker | After the dashboard |
| Webull / Massive / Tiingo keys | Optional, later |

## Other bot: ORB / VWAP-pullback paper reports

- `tools/orb_vwap_weekly_report.py` (standard library only) builds the same kind of weekly HTML page for the separate intraday ORB / VWAP-pullback paper bot, from its `post_session_summary_YYYY-MM-DD.html` pages. It shares no code with the desk.
- Reports made 2026-11-25: start week (Oct 29–30, 2 sessions) and week 1 (Nov 2–6, 4 of 5 sessions; Nov 3 summary missing though equity shows a session).
- Limits of the source: summaries list orders sent (entry, stop, target), not exits or fills, so there is no per-trade result; P/L is the daily day-change line.
- Open: where that bot runs (to schedule the report there) and whether its trade log or fills can feed per-trade results. Its summaries say "Sized on $100k spec" while the paper account holds about $1M: confirm that is intended.
- Generated reports name tickers and account figures: keep them out of this public repo.

## Decisions

- **RVOL20 basis:** SIP regular hours first, Webull regular hours as fallback, one source per ratio (rulebook v1.6.2, locked 2026-10-22 15:05 ET).
- **Hosting:** the always-on worker is primary; the GitHub schedule is the backup and stands down while the worker is fresh.
- **Isolation:** the desk shares no code or secrets with the QuantScout engine on `main`; its only file there is the backup workflow.
- **Robinhood:** read-only CSV upload (no official API); held names outside the universe are scanned for stops but never join the watchlist.
- **Merge order:** PR #6 is merged only after the step 3 secrets exist, or every scheduled run fails.
- **Google consent screen:** published to production with the homepage and privacy-policy URL on the project website (Google required both to publish).
- **Backtest defaults:** 5 years, today's universe, 0.05% cost per side, entry at the next open, excess over the same-day average, Rule 3 stops.
- **AI models:** stay off the decision path; planned uses are research, engineering, a daily briefing and a weekly audit (Phase 3).

## Open items

- Fix step 3: every backup run fails while the secrets are missing (44 of 44 so far). The desk has never published a snapshot.
- GitHub fires the every-5-minutes schedule only about twice a day, so the backup cannot keep the desk current on its own; the always-on worker (step 5) is required.
- The pinned checkout and setup-python actions target Node.js 20, which GitHub now forces onto Node.js 24 (warning only). Move to releases built for Node.js 24 at the next workflow change.

- If the Google token script was run more than once, Drive holds more than one `Recovery_Desk_Cloud` folder: use the folder ID and refresh token from the last run; delete the older folder once the desk works.
- No provider call has run against live keys yet; expect one round of fixes on the first run.
- Check that the website's Privacy and Risk Disclosure pages load after the latest redeploy (the consent screen links to the privacy page).

## Roadmap

0 Go live → 1 Alerts and history → 2 Validation lab (backtest, then a 30-day forward test) → 3 AI analyst → 4 Streaming and risk → 5 Rule evolution. Each phase closes on a gate; orders stay off throughout.
