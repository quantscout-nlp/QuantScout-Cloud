# QuantScout-Cloud
Regime Aware Automated QuantScout-Cloud NLP Google Sentiment Analytic Tracking Tool

## Architecture

- **`engine.py`** — shared fetch/decide/trade/log logic. No Streamlit dependency.
- **`quantscout_cloud.py`** — the Streamlit dashboard. Read-only: it previews live
  signals and shows the trial's authoritative state, but never places orders or
  writes to the trial log itself.
- **`scripts/run_scan.py`** + **`.github/workflows/trading-engine.yml`** — the
  actual trading engine. Runs on a schedule via GitHub Actions, independent of
  whether the dashboard is open in a browser. This is the only process that
  submits Alpaca orders or advances the trial log — keeping it singular avoids
  the dashboard and the scheduled job racing each other into double orders.

## Research scan (Track A watchlist construction)

A separate, offline-first layer that turns overlapping AI-chat research lists into a
single ranked, deduped watchlist — and then decides which of those names are actually
tradeable.

- **`research/track_a_<session>.json`** — the source of record. Every ticker carries
  its provenance: which chat sources mentioned it, which screen it came from, its
  verified move, the dated catalyst, and a source URL. Hand-maintained on purpose;
  it is data, not output.
- **`research_scan.py`** — dedup + cross-source consensus scoring. Pure functions,
  no clock, no network, no RNG: the same JSON always produces the same ranking, and
  reordering the file by hand cannot change it.
- **`mechanical_filters.py`** — the SMA20/50/200 stack, liquidity, ATR-band,
  extension, gap and spread gates, plus risk-based position sizing. **Fails closed**:
  any unknown is a rejection, never a pass.
- **`scripts/build_watchlist.py`** — CLI that ties them together.
- **`docs/STRATEGY_<date>.md`** — the deterministic playbook derived from a session.

```bash
python scripts/build_watchlist.py                       # rank only, offline
python scripts/build_watchlist.py --live --top 20       # + mechanical gate on real bars
python scripts/build_watchlist.py --watchlist-var       # emit a WATCHLIST repo-var line
python scripts/build_watchlist.py --size 25000 1.5 84.30 82.10   # size one trade
python tests/test_research_scan.py                      # 24 tests, no pytest needed
```

The division of responsibility is the point: **consensus scoring only ranks; the
mechanical gate decides.** A name every chat source agrees on is still rejected if it
lacks 200 sessions of history, because an SMA200 gate cannot evaluate what does not
exist. Consensus across AI chats measures salience, not independent confirmation —
those sessions read the same wire feeds — so its weight is concave and hard-capped.

This layer is advisory only. It produces a watchlist; it places no orders. Feeding
its output into live trading still goes through the `WATCHLIST` repo variable and the
60-day trial gate below.

## 60-day trial

The engine runs a fixed rollout before any live-trading decision:
- **Days 1–30 (DRY RUN):** signals computed and logged, no Alpaca orders at all.
- **Days 31–60 (PAPER):** real orders against Alpaca's *paper* (simulated) account.
- **Day 61+ (REVIEW):** frozen — no new positions open; existing paper positions
  can still be closed. Promotion to live trading is a separate, manual decision;
  nothing in this repo does that automatically.

The trial's start date and its decision log live on a dedicated `bot-trial-log`
git branch (not `main`, so log commits never trigger a redeploy) and are
readable from the dashboard sidebar / "Recent Trial Log" section.

## Required secrets / variables

**Streamlit Cloud app secrets** (`.streamlit/secrets.toml` or the Cloud UI):
`ALPACA_ID`, `ALPACA_SECRET` (Alpaca **paper** account keys — live keys will
just get a 401), `POLYGON_KEY`, `TIINGO_KEY`, `GITHUB_TOKEN` (read access is
enough for the dashboard).

**GitHub Actions repo secrets** (Settings → Secrets and variables → Actions):
`ALPACA_ID`, `ALPACA_SECRET`, `POLYGON_KEY`, `TIINGO_KEY`, `TG_TOKEN`, `TG_ID`,
and `TRIAL_LOG_TOKEN` — a fine-grained PAT scoped to this repo with **Contents:
Read and write** permission, so the scheduled job can commit to `bot-trial-log`.

**GitHub Actions repo variables** (same settings page, "Variables" tab):
`WATCHLIST` (comma-separated tickers; defaults to the built-in list if unset),
`ENABLE_TRADING` (`true`/`false`), `NOTIONAL_PER_TRADE` (dollars per position),
`MAX_POSITIONS` (concurrent position cap).
