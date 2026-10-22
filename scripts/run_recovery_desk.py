# -*- coding: utf-8 -*-
"""
Runs the Cloud Recovery Desk.

  python scripts/run_recovery_desk.py            one cycle (GitHub Actions / Cloud Scheduler)
  python scripts/run_recovery_desk.py --loop     always-on worker: a cycle right after each
                                                 5-minute bar closes, 08:00-16:30 ET on weekdays

Screening only. Orders are OFF; there is no broker code in this path.

Logs print counts and health only, never tickers: GitHub Actions logs on a
public repository are public.
"""
import argparse
import os
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from recovery.desk import run_cycle
from recovery.providers import ET, DataHub
from recovery.store import open_store


def one_cycle(local_dir):
    snap = run_cycle(open_store(local_dir=local_dir), DataHub())
    h = snap["health"]
    print(f"{snap['generated_et']} phase={snap['phase']} health={h['status']} feed={h['feed']} "
          f"{h['scanned']} qualified_from={snap['qualified_from_session']} delay_s={h['data_delay_s']} "
          f"calls={h['provider_calls']} 429s={h['rate_limited']} changes_today={len(snap['changes_today'])}")
    print("counts:", ", ".join(f"{k}={v}" for k, v in sorted(snap["counts"].items())))
    if h["errors"]:
        # Messages name tickers, so they stay in the private snapshot (health.errors).
        print(f"errors: {len(h['errors'])} (see health.errors in latest.json)", file=sys.stderr)
    return snap


def next_wake(now: datetime, settle_s: int) -> datetime:
    """Next 5-minute boundary + settle (the bar is complete and published by then)."""
    base = now.replace(second=0, microsecond=0)
    base += timedelta(minutes=5 - base.minute % 5)
    return base + timedelta(seconds=settle_s)


def in_window(now: datetime) -> bool:
    return now.weekday() < 5 and (8, 0) <= (now.hour, now.minute) <= (16, 30)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--out", default=None, help="local output folder when Drive is not configured")
    ap.add_argument("--settle", type=int, default=int(os.environ.get("RECOVERY_SETTLE_S", "20")))
    args = ap.parse_args()
    if not args.loop:
        one_cycle(args.out)
        return
    while True:
        now = datetime.now(ET)
        if in_window(now):
            try:
                one_cycle(args.out)
            except Exception as e:  # noqa: BLE001 - a worker must survive one bad cycle and retry
                print(f"cycle failed: {e}", file=sys.stderr)
        wake = next_wake(datetime.now(ET), args.settle)
        time.sleep(max(1.0, (wake - datetime.now(ET)).total_seconds()))


if __name__ == "__main__":
    main()
