# -*- coding: utf-8 -*-
"""
Tradability test: replays the locked rules over history (recovery/backtest.py).

  python scripts/run_backtest.py                 5 years to the last completed session,
                                                 report to the desk's Drive folder
  python scripts/run_backtest.py --years 3 --out recovery_out

Writes a signed backtest_report.json (aggregates, shown on the dashboard) and
backtest_signals.csv (every signal, names tickers) to the desk's private folder.
Logs print counts only. Run it on the worker VM, not in public CI.
"""
import argparse
import csv
import io
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from recovery import backtest
from recovery.desk import dump_signed, load_universe, market_clock, rule_config
from recovery.providers import ET, DataHub
from recovery.store import LocalStore, open_store


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=5.0)
    ap.add_argument("--cost-bps", type=float, default=5.0, help="per side")
    ap.add_argument("--out", help="write to this local folder instead of Drive")
    args = ap.parse_args()
    if not args.out and not os.environ.get("RECOVERY_DRIVE_FOLDER_ID"):
        sys.exit("RECOVERY_DRIVE_FOLDER_ID is not set: use --out DIR for a local run")

    store = LocalStore(args.out) if args.out else open_store()
    hub = DataHub()
    now = datetime.now(ET)
    end = market_clock(hub, now)["last_completed"] or (now.date() - timedelta(days=1))
    start = end - timedelta(days=int(args.years * 365.25))
    universe = load_universe(store)
    if not universe:
        sys.exit("universe is empty: upload universe.csv to the desk folder or set RECOVERY_UNIVERSE")

    print(f"fetching daily bars for {len(universe)} symbols, {start} to {end}")
    bars, src = hub.daily_bars(universe, start - timedelta(days=backtest.WINDOW_DAYS), end,
                               min_bars=60)
    print(f"bars for {len(bars)} of {len(universe)} symbols; provider calls={hub.stats['calls']}")

    report, signals = backtest.run(bars, rule_config(), start.isoformat(), end.isoformat(), args.cost_bps)
    report.update({"generated_et": now.isoformat(), "universe_requested": len(universe),
                   "daily_sources": {s: list(src.values()).count(s) for s in set(src.values())}})
    store.write_text("backtest_report.json", dump_signed(report))

    rows = backtest.signal_rows(signals)
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    store.write_text("backtest_signals.csv", buf.getvalue())

    print(f"name-days scanned={report['name_days']} signals={len(rows)}")
    for k, b in report["statuses"].items():
        x = b["excess"]["10"]
        if x.get("n"):
            print(f"{k}: signals={b['signals']} excess10_mean={x['mean']:+.4f} n={x['n']}")
    print("written: backtest_report.json, backtest_signals.csv")


if __name__ == "__main__":
    main()
