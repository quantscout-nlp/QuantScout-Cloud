# QuantScout-Cloud

Regime-aware automated NLP/sentiment signal tracker that runs a scan → decide →
trade → log pass against Alpaca's **paper** account on a fixed 60-day trial
rollout.

## Layout

| File | Role |
| --- | --- |
| `engine.py` | Shared fetch/decide/trade/log logic. **No Streamlit dependency** — imported by both entry points. |
| `quantscout_cloud.py` | Streamlit dashboard. **Read-only for trading**: previews signals, shows trial state, submits nothing. |
| `scripts/run_scan.py` | Headless entry point for the scheduled job. Reads config from env vars, not `st.secrets`. |
| `.github/workflows/trading-engine.yml` | The scheduler (`*/15 * * * *`). The **only** process that trades. |
| `run_scan.bat` + `secrets.local.bat.example` | Manual Windows launcher for the same engine, for testing real credentials locally. |
| `requirements.txt` / `requirements-engine.txt` | Dashboard deps / headless-engine deps (a subset, minus Streamlit). |

## Invariants — do not break these

These are load-bearing safety properties, not style preferences. Each one is
here because breaking it either risks real money or corrupts the trial record.

1. **Paper endpoint is hardcoded.** `engine.ALPACA_TRADE_BASE` is pinned to
   `https://paper-api.alpaca.markets` and must never become configurable, env-driven,
   or parameterized. It is what guarantees the engine cannot place a live order
   even if handed live keys. `tests/test_engine.py` asserts this.
2. **Exactly one trading path.** Only `scripts/run_scan.py` (via the scheduled
   workflow) may call `engine.run_scan()`. Never call it from the dashboard, and
   never add order submission or trial-log writes to `quantscout_cloud.py` — two
   processes racing produce double-submitted orders.
3. **`engine.py` stays Streamlit-free.** Importing `streamlit` there would break
   the headless job. Keep `st.*` calls in `quantscout_cloud.py` only.
4. **Trial state lives on the `bot-trial-log` branch**, never `main`. Committing
   state to `main` triggers a Streamlit Cloud redeploy on every log entry.
5. **Phase gating.** `can_open_new` is true only in `PAPER`; `can_close` is true in
   `PAPER` and `REVIEW`. `DRY_RUN` must never touch Alpaca at all. Promotion to
   live trading is a manual, out-of-band decision — nothing here automates it.

## Trial phases

Driven by `get_trial_phase()` off a start date bootstrapped on first run and
persisted to `trial_state/trial_meta.json` on the `bot-trial-log` branch.

- **Days 0–29 — `DRY_RUN`:** signals computed and logged; zero Alpaca orders.
  Holdings are reconstructed by replaying the log (`derive_simulated_positions()`),
  since simulated fills never appear in Alpaca's real position list.
- **Days 30–59 — `PAPER`:** real orders against Alpaca's paper account.
- **Day 60+ — `REVIEW`:** frozen. No new positions; existing ones may still close.

With no GitHub token the engine fails safe into `DRY_RUN`.

## Gotchas

- **An unset GitHub Actions *variable* arrives as `""`, not as an absent key.**
  So `os.environ.get("MAX_POSITIONS", 8)` returns `""`, never `8`. Read all
  env-derived config through the `_env_str` / `_env_int` / `_env_float` /
  `_env_bool` helpers in `scripts/run_scan.py`, which treat `""` as unset.
- **Token naming is deliberate.** The Actions *secret* is `TRIAL_LOG_TOKEN`,
  injected as env `TRIAL_GITHUB_TOKEN` — deliberately *not* `GITHUB_TOKEN`, to
  avoid colliding with the ambient token Actions provides. The dashboard reads its
  own `GITHUB_TOKEN` from `st.secrets`. Don't "unify" these names.
- **RSI uses `ewm(alpha=1/14, adjust=False)`** to reproduce Wilder's recursive
  smoothing. Pandas' default `adjust=True` is a different, biased average — don't
  drop the flag.
- **`decide()` intentionally has only two branches.** BUY needs uptrend + healthy
  momentum + positive sentiment; SELL needs downtrend + momentum breaking down +
  negative sentiment. An oversold mean-reversion branch (`RSI < 35` alone) was
  removed on purpose; don't reintroduce it without being asked.
- **Two requirements files.** Shared pins must be kept identical in both, or the
  scheduled job and the dashboard drift apart.
- **No market-hours gate** anywhere, by design. Off-hours market orders queue for
  the next session rather than filling badly, so extra runs are wasted, not harmful.
- **The scan loop swallows per-ticker exceptions** (`except Exception: pass`) so one
  bad symbol can't abort a pass. This also hides bugs — when debugging a ticker,
  temporarily re-raise rather than adding prints inside the handler.

## Commands

```bash
# Install
pip install -r requirements.txt -r requirements-dev.txt   # dashboard + tooling
pip install -r requirements-engine.txt                    # headless engine only

# Test / lint
pytest -q
ruff check .

# Run one scan pass locally (needs credentials in the environment)
python scripts/run_scan.py

# Run the dashboard
streamlit run quantscout_cloud.py
```

`tests/` covers only pure functions — decision logic, phase math, log replay, env
parsing, and the paper-endpoint invariant. Nothing in the suite makes a network
call or needs credentials, so it is safe to run anywhere.

## Credentials

Never commit real keys. `secrets.local.bat` is gitignored; `secrets.local.bat.example`
is the template. See README.md for the full list of Streamlit secrets, Actions
secrets, and Actions variables.
