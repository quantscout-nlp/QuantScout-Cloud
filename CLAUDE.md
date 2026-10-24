# CLAUDE.md — Cloud Recovery Watchlist Dashboard (Recovery Desk)

**Version 1.1 · 2026-10-24** — adapted from the Trading-Bot Project Template v1.0 (2026-10-15). Project facts only; doctrine is cited, not restated. **Read `docs/STATUS.md` first** in a new session (setup progress, decisions, open items) and update it when a step or phase completes.

## What this project is
An automated Capitulation-Recovery / Momentum watchlist: it screens a 232-name universe on the locked Rule 1/2/3 + Anti-Knife rulebook, refreshes after every 5-minute bar, and publishes signed snapshots to a Streamlit dashboard. **Screening only: orders are OFF and there is no order code.** A human submits every order. Data comes from Alpaca (a separate paper account used for market data only), with Webull, Massive and Tiingo as optional fallbacks; Robinhood is a read-only CSV upload. It lives on the orphan branch `recovery-desk` of the public `quantscout-nlp/QuantScout-Cloud` repo and shares no code, history or secrets with the QuantScout engine on `main`; its only file on `main` is `.github/workflows/recovery-desk-failover.yml`. Owner agent: CLAUDE_CODE / Track B. Its output feeds real trading decisions. Bugs cost money. Act like it.

## Precedence
1. The owner's doctrine, on the owner's PC: `F:\Claude Agent System\ClaudeCodeAgent_v1.2_2026-10-15\01_Identity\Claude Code Agent.md` (boundaries, tiers) and `...\02_TrackB_SystemBuilder\TrackB_SystemBuilder_v1.3.md`. Cloud sessions cannot read `F:\`; ask the user when a doctrine point decides the work.
2. The rulebook, the spec for every rule: `SKILL.md` / `AGENT.md` in the owner's Google Drive rule folder, **v1.6.2** (locked 2026-10-22 15:05 ET).
3. This file. The stricter rule wins on conflict; gate-skipping is non-overridable.

## Absolute rules — NEVER violate
1. **No look-ahead.** A status for session *t* uses bars dated ≤ *t* only; live uses completed bars only. The backtest replays the live functions over the same 420-day window. `tests/test_recovery.py::Backtest::test_statuses_are_point_in_time` must pass before any commit touching `rules.py`, `indicators.py`, `desk.py` or `backtest.py`.
2. **No order path.** Never add broker order code, an order endpoint, or an "auto-trade" switch. The only Alpaca trading-host call is `/v2/calendar`. EXIT_FLAG and the held-name stop block are audit output; never an automatic flatten.
3. **No hardcoded secrets, and nothing private in a public repo.** Secrets come only from the GitHub Environment `recovery-desk` (`DESK_*`), the worker's `/etc/recovery-desk.env`, or Streamlit secrets. Never commit or upload as a CI artifact anything a run produces (tickers, snapshots, signals CSV); scripts log counts only. A key in code is a bug: remove it, flag rotation.
4. **No live switch.** There is no live mode to default to. Do not add one.
5. **Never skip the suite.** Run `python -m unittest discover -s tests` before finishing work on rules, desk, held, providers or backtest.
6. **One account per project (doctrine boundary #6).** The desk uses its own Alpaca paper account. Never point it at the engine's keys or secrets.
7. **Never change thresholds silently.** Every threshold lives in `rules.RuleConfig` and mirrors the rulebook. A change needs the user's explicit instruction, a rulebook version bump in Drive, and a backtest.
8. **Fail closed.** A stale feed (no new bar for `RECOVERY_MAX_BAR_AGE_S`) blocks every new candidate; a file that fails its signature is ignored and reported. A check that never ran is UNKNOWN, not IDLE.

## Commands
```bash
pip install -r requirements.txt                                # app + worker deps (CI installs requirements-worker.txt)
python -m unittest discover -s tests -v                        # full suite; no network, no secrets
python -m unittest tests.test_recovery.Backtest.test_statuses_are_point_in_time   # look-ahead test
python -m unittest tests.test_recovery.AntiKnife               # one class
python scripts/run_recovery_desk.py --out recovery_out         # one cycle into a local folder
python scripts/run_recovery_desk.py --loop                     # always-on worker (RECOVERY_ROLE=worker)
python scripts/run_backtest.py --out recovery_out              # tradability test (needs Alpaca keys; VM only)
RECOVERY_LOCAL_DIR=recovery_out streamlit run recovery_app.py  # dashboard
```
Python 3.11 (CI, `.github/workflows/desk-tests.yml`). No linter config. Dependencies are exact-pinned and actions SHA-pinned; keep it that way.

## Workflow
- Read the module and its tests first; match existing patterns. Small changes over big refactors.
- New behaviour in `rules.py`, `desk.py`, `held.py` or `backtest.py` → tests in the same change; bug fix → a regression test. Dashboard changes: render-check with `streamlit.testing.v1.AppTest`.
- Tiers (doctrine §6D): `git commit` = Tier 2; `git push`, merging to `main`, key rotation = Tier 3, unless the user asked for that push in the session. Work only on `recovery-desk` (desk code) or the session's designated branch (the `main` workflow PR).
- Where the rule text leaves a gap, choose and mark it `INTERPRETATION` in code so the user can review it.

## Architecture
- **Rules** (`recovery/rules.py`, `indicators.py`): pure functions of completed bars. `daily_metrics` → `classify_daily` (TIER1 > CAP_SETUP > TIER2 > WATCH_HOLD); `ak_d`, `ak_s`, `ak_combined` for Anti-Knife.
- **One cycle** (`desk.run_cycle(store, hub, now, env)`), called by the worker loop and the GitHub backup:
  1. Load signed `state.json`; a backup run stands down while the worker published within `RECOVERY_FAILOVER_AFTER_S`.
  2. Scan list = universe (`RECOVERY_UNIVERSE`, `universe.csv` in the Drive folder, or `RECOVERY_UNIVERSE_PATH`) + held names from signed `held.json`. Held names outside the universe get stops but never join the watchlist or the "N of 232 scanned" count.
  3. `build_daily` once per completed session (cached in `daily_cache.json`).
  4. `overlay` on completed 5-minute bars; STALE blocks candidates.
  5. Publish signed `latest.json`, `state.json`, `session_*.json`, signed-per-line changes jsonl, `latest.csv`.
- **Data** (`recovery/providers.py`, `DataHub`); every row carries its source label.
  - Daily: Alpaca SIP → Webull → Massive → Tiingo → yfinance (optional).
  - Intraday: Alpaca SIP (IEX on a subscription error), Webull for missing names.
  - RVOL20: one source per ratio, SIP RTH first (`RECOVERY_RVOL_BASIS`), never IEX or full-day volume.
  - Webull signing (`webull_signed_headers`) must keep the fixed test vector in `tests/test_recovery.py::Webull` passing.
- **Tradability test** (`recovery/backtest.py`, `scripts/run_backtest.py`): calls the same `daily_metrics` / `classify_daily`; never give it its own copy of a rule.
  - First day of a status run only; entry at the next open; excess over the same-day average; month-clustered bootstrap.
  - `NOTES` lists the known gaps from live (daily-bar RVOL, no AK-S); update them when the gap changes.
- **Storage** (`recovery/store.py`): `DriveStore` (OAuth refresh token, `drive.file` scope, `Recovery_Desk_Cloud` folder) or `LocalStore`. Every persisted JSON goes through `desk.dump_signed` / `load_signed` (HMAC, `RECOVERY_SNAPSHOT_KEY`); new files must too.
- **Dashboard** (`recovery_app.py`): read-only over signed files. Its only write is the Robinhood upload (signed `held.json` with per-name Cap/Mom book). Refresh dispatches the backup workflow. Read secrets via `_secret()`.
- **Deployment**: `deploy/create_vm_gcloud.sh` (Cloud Shell) → `deploy/setup_vm.sh` → hardened `deploy/recovery-desk.service`; `Dockerfile` is the alternative.

## Project conventions
- Configurable numbers live in `RuleConfig` or env vars; no magic numbers in code.
- Logs print counts and health only (public CI); details that name tickers go to `health.errors` in the private snapshot.
- Market data only through `DataHub`; no provider SDKs (Webull signing is re-implemented, not imported).
- Tests use `FakeHub` and the deterministic fixtures `uptrend_bars`, `cap_bars`, `wave_bars`; several reproduce worked examples from the owner's files (AMGN, IQV, MDB, CRDO; the ZS RVOL gap).

## When stuck
- First run against live providers: read `health.errors` in `latest.json` and the worker log (`journalctl -u recovery-desk`).
- `docs/STATUS.md` (open items), `docs/RECOVERY_DESK_CLOUD.md` (cadence, feeds, setup, secrets), `SECURITY.md` (threat model).
- The doctrine's SKILL.md Debug Playbook, when working on the owner's PC.
- Read `tests/` for expected usage. If a design decision is unclear, ask — do not guess.

## Changelog
**v1.1 · 2026-10-24** — Restructured on the Trading-Bot Project Template v1.0. Rule 2 (stops on orders) became "no order path", and rule 4 (never default to live) became "no live switch", since the desk places no orders. Added rule 8 (fail closed) and the Drive rulebook to Precedence. Dropped `/review_for_prod` → `/go_live` (no go-live path) and the JSON equity/PnL log convention (no positions).
**v1.0 · 2026-10-22** — First CLAUDE.md for the `recovery-desk` branch.
