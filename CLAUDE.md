# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this branch is

`recovery-desk` is an **orphan branch**: the Recovery Desk's own home inside the QuantScout-Cloud repo. It shares no history or code with the QuantScout trading engine on `main`. Don't import from, merge with, or copy code to or from `main`. The desk's only file on `main` is `.github/workflows/recovery-desk-failover.yml`, because GitHub runs scheduled workflows only from the default branch. That workflow checks out `recovery-desk` and runs one cycle, with secrets from the GitHub Environment `recovery-desk` (`DESK_*` names).

The desk screens and publishes status only. **There is no order code, and none should be added.** The only Alpaca trading-host call is `/v2/calendar`. The repo is public, so nothing a run produces (tickers, snapshots, logs with symbols) may be committed or uploaded as a CI artifact. The scripts log counts only.

## Commands

```
pip install -r requirements.txt                       # app + worker deps (CI installs requirements-worker.txt only)
python -m unittest discover -s tests -v               # full suite; no network, no secrets needed
python -m unittest tests.test_recovery.AntiKnife      # one class
python -m unittest tests.test_recovery.HeldPositions.test_per_name_book_is_carried_and_editable   # one test
python scripts/run_recovery_desk.py --out recovery_out   # one cycle into a local folder instead of Drive
python scripts/run_recovery_desk.py --loop               # always-on worker (sets RECOVERY_ROLE=worker)
python scripts/run_backtest.py --out recovery_out        # tradability test (needs Alpaca keys; run on the VM, not public CI)
RECOVERY_LOCAL_DIR=recovery_out streamlit run recovery_app.py
```

There is no linter or formatter config. CI (`.github/workflows/desk-tests.yml`) runs the unittest suite on Python 3.11. Dependencies are pinned to exact versions and the actions are pinned by SHA; keep it that way.

## Architecture

**The rulebook is the spec.** `recovery/rules.py` encodes the locked "capitulation-momentum-rules" skill. Its source of truth is `SKILL.md` / `AGENT.md` in the user's Google Drive rule folder, currently v1.6.2. The rules cover Rule 1 Cap, Rule 2 Momentum, the MACD-histogram battery, Rule 3 exits, tiers, RVOL20 and Anti-Knife (AK-D / AK-S / AK Combined).
- All thresholds live in `RuleConfig`.
- Wherever the rule text leaves a gap, the code makes a choice and marks it with an `INTERPRETATION` comment. Keep that convention so the user can review those choices.
- A threshold change is a rule change. It needs the user's explicit instruction and a matching rulebook version bump; never tune it silently.
- `rules.py` and `indicators.py` are pure functions of completed bars: no I/O.

**One cycle** is `desk.run_cycle(store, hub, now, env)`. Both the worker loop and the GitHub failover call it.
1. **Load and verify state.** Load the signed `state.json`. A failover run stands down (returns `None`) while the worker has published within `RECOVERY_FAILOVER_AFTER_S`.
2. **Build the scan list.** It is the universe (from `RECOVERY_UNIVERSE`, `universe.csv` in the Drive folder, or `RECOVERY_UNIVERSE_PATH`) plus held names from the signed `held.json`. Held names outside the universe are scanned for their stops but never join the watchlist or change the "N of 232 scanned" count.
3. **Daily qualification** (`build_daily`). Runs once per completed session, cached in `daily_cache.json`, keyed by session date and a hash of the scan list.
4. **Live-session overlay** (`overlay`). Uses completed 5-minute bars. If no new bar has arrived for `RECOVERY_MAX_BAR_AGE_S`, the feed is marked STALE and every new candidate is blocked (fail-closed).
5. **Publish.** Writes signed `latest.json`, `state.json` and `session_*.json`, a changes jsonl where each line is signed, and `latest.csv`.

**Data feeds** are all in `recovery/providers.py` (`DataHub`). Every row carries its source label.
- **Daily bars:** Alpaca SIP → Webull → Massive → Tiingo → yfinance (optional import, not installed in the cloud).
- **Intraday:** Alpaca SIP (falls back to IEX on a subscription error), with Webull filling missing names.
- **RVOL20:** `rth_volumes()` uses one source per ratio, never mixed. `RECOVERY_RVOL_BASIS` picks which is tried first: SIP by default, per rule v1.6.2.
- **Webull:** requests are signed by `webull_signed_headers`, which re-implements Webull's official SDK signature. `tests/test_recovery.py::Webull` holds a fixed test vector; any change to the signing code must keep that vector passing.

**Tradability test** (`recovery/backtest.py`, run by `scripts/run_backtest.py`). It calls the same `rules.daily_metrics` / `classify_daily` per historical session over the live 420-day window, so it scores the exact live logic. Keep it that way: never give the backtest its own copy of a rule.
- It counts only the first day of a status run and enters at the next open.
- It reports excess returns over the same-day average of all scanned names, with a month-clustered bootstrap range.
- Its `NOTES` list the known differences from live (daily-bar RVOL, no AK-S); update them whenever the gap changes.
- Its signals CSV names tickers, so it is written only to the private store.

**Storage** (`recovery/store.py`). `DriveStore` uses an OAuth refresh token with the `drive.file` scope, so it can only see files the app created (the `Recovery_Desk_Cloud` folder). `LocalStore` is the stand-in for `--out` and for tests.
- Every JSON the desk reads back goes through `desk.dump_signed` / `desk.load_signed`, an HMAC with `RECOVERY_SNAPSHOT_KEY`. A file that fails the check comes back as `TAMPERED` and is ignored, and the problem is reported in `health.errors`.
- New persisted files must use the same signing.

**The dashboard** (`recovery_app.py`) is read-only over signed snapshots. Its only write is the Robinhood positions upload: `recovery/held.py` parses the CSV and the app saves a signed `held.json` with a per-name Cap/Mom `book` choice.
- Its Refresh button only dispatches the failover workflow.
- Read secrets through `_secret()`. Newer Streamlit raises when no secrets file exists, and `_secret()` catches that.

**Deployment.** `deploy/create_vm_gcloud.sh` (run in Cloud Shell) creates the free-tier e2-micro and runs `deploy/setup_vm.sh`. That script installs the hardened `deploy/recovery-desk.service`, which reads its secrets from `/etc/recovery-desk.env` (names without the `DESK_` prefix). `Dockerfile` is the container alternative.

## Tests

`tests/test_recovery.py` is a single file:
- `FakeHub` stands in for `DataHub`.
- `uptrend_bars()` and `cap_bars()` are deterministic fixtures.
- Several cases reproduce worked examples from the user's own files (AMGN, IQV, MDB, CRDO Anti-Knife cases; the ZS RVOL gap).

New behaviour in `rules.py`, `desk.py` or `held.py` needs a test here. Dashboard changes have no unit tests; check them with `streamlit.testing.v1.AppTest`.

## Further reading

- `docs/STATUS.md`: setup progress, decisions and open items. Read it first in a new session, and update it when a step or phase completes.
- `docs/RECOVERY_DESK_CLOUD.md`: cadence, feed table, Robinhood upload, setup steps, where each secret comes from.
- `SECURITY.md`: threat model and checklist.
