# -*- coding: utf-8 -*-
"""
Weekly paper report (recovery/weekly.py): one self-contained HTML page per week.

  python scripts/weekly_report.py                    last completed Mon-Fri week, to the desk folder
  python scripts/weekly_report.py --week 2026-11-16 --out recovery_out
  python scripts/weekly_report.py --no-prices --note "..."   skip the bar fetch; add an operator note

Writes weekly_report_YYYY-MM-DD.html (the week's Monday) to the desk's private
folder, or to --out. Run it on the worker VM (deploy/recovery-desk-weekly.timer runs
it every Saturday); it names tickers, so never in public CI. Logs print counts only.
"""
import argparse
import os
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from recovery import weekly
from recovery.providers import ET, DataHub
from recovery.store import LocalStore, open_store


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", help="any date in the week to report (default: last completed week)")
    ap.add_argument("--out", help="write to this local folder instead of Drive")
    ap.add_argument("--notional", type=float, default=1000.0, help="paper dollars per entry")
    ap.add_argument("--cost-bps", type=float, default=5.0, help="per side")
    ap.add_argument("--no-prices", action="store_true", help="skip the daily-bar fetch")
    ap.add_argument("--note", action="append", default=[], help="operator note shown under Data health")
    args = ap.parse_args()
    if not args.out and not os.environ.get("RECOVERY_DRIVE_FOLDER_ID"):
        sys.exit("RECOVERY_DRIVE_FOLDER_ID is not set: use --out DIR for a local run")

    now = datetime.now(ET)
    monday = (date.fromisoformat(args.week) - timedelta(days=date.fromisoformat(args.week).weekday())
              if args.week else weekly.last_completed_week(now.date()))
    friday = monday + timedelta(days=4)
    store = LocalStore(args.out) if args.out else open_store()
    days = weekly.collect(store, monday, os.environ)

    candidates = sorted({r["ticker"] for d in days if d["snap"] for r in d["snap"].get("rows", [])
                         if r.get("paper_candidate")})
    bars, prices_note = {}, None
    if candidates and not args.no_prices:
        hub = DataHub()
        bars, _src = hub.daily_bars(candidates, monday - timedelta(days=7), friday, min_bars=1)
        if len(bars) < len(candidates):
            prices_note = f"Daily bars missing for {len(candidates) - len(bars)} of {len(candidates)} paper names."
    elif candidates:
        prices_note = "Prices were not fetched (--no-prices): entries and P/L are not computed."

    trades = weekly.ledger(days, bars, args.notional, args.cost_bps)
    dates = [d["date"] for d in days]
    pnl = weekly.pnl_by_day(trades, bars, dates, args.notional, args.cost_bps)
    page = weekly.render(monday, days, trades, pnl, now.strftime("%Y-%m-%d %H:%M"), args.notional,
                         args.cost_bps, args.note, prices_note)
    name = f"weekly_report_{monday.isoformat()}.html"
    store.write_text(name, page, "text/html")
    print(f"week {monday} sessions_captured={sum(1 for d in days if d['snap'])}/5 "
          f"paper_entries={sum(1 for t in trades if 'entry' in t)} written: {name}")


if __name__ == "__main__":
    main()
