# -*- coding: utf-8 -*-
"""
Headless entry point for the scheduled GitHub Actions job. This is the ONLY place
that should be running engine.run_scan() on a schedule — the Streamlit dashboard
(quantscout_cloud.py) is read-only for trading specifically to avoid two processes
racing to submit the same order.

Reads credentials/config from environment variables (set as GitHub Actions repo
secrets — see .github/workflows/trading-engine.yml), not st.secrets, since this
script has no Streamlit runtime.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import engine


def _tickers_from_env():
    raw = os.environ.get("WATCHLIST", "")
    tickers = [t.strip().upper() for t in raw.split(",") if t.strip()]
    return tickers or engine.DEFAULT_TICKERS


def main():
    creds = {
        "alpaca_id": os.environ.get("ALPACA_ID", ""),
        "alpaca_secret": os.environ.get("ALPACA_SECRET", ""),
        "polygon_key": os.environ.get("POLYGON_KEY", ""),
        "tiingo_key": os.environ.get("TIINGO_KEY", ""),
        "tg_token": os.environ.get("TG_TOKEN", ""),
        "tg_id": os.environ.get("TG_ID", ""),
        "github_token": os.environ.get("TRIAL_GITHUB_TOKEN", ""),
    }
    config = {
        "enable_trading": os.environ.get("ENABLE_TRADING", "true").lower() != "false",
        "notional_per_trade": float(os.environ.get("NOTIONAL_PER_TRADE", "500")),
        "max_positions": int(os.environ.get("MAX_POSITIONS", "8")),
    }

    if not creds["github_token"]:
        print("WARNING: TRIAL_GITHUB_TOKEN not set — trial phase/log can't persist, "
              "engine will fail safe into DRY_RUN and this run will just be discarded state.",
              file=sys.stderr)

    result = engine.run_scan(_tickers_from_env(), creds, config)

    print(f"Phase: {result['phase']} (day {result['trial_day']}) — "
          f"started {result['trial_start']}")
    print(f"Open positions/simulated holds: {sorted(result['open_symbols'])}")
    print(f"{len(result['new_log_rows'])} new trial-log entries this pass")
    for row in result["rows"]:
        if row["SIGNAL"] != "HOLD":
            print(f"  {row['TICKER']:<6} {row['SIGNAL']:<4} price={row['PRICE']} "
                  f"rsi={row['RSI']} conf={row['CONF']} -> {row['TRADE']}")


if __name__ == "__main__":
    main()
