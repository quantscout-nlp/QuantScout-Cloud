# -*- coding: utf-8 -*-
"""
Tradability test: replay the locked rules day by day over history and measure what
each status was worth afterwards. Pure functions of completed daily bars, no I/O,
so the backtest scores exactly the code the live desk runs.

Point in time: the status for session t comes from bars dated <= t only, over the
same 420-calendar-day window `desk.build_daily` fetches live. Entry is the next
session's open (the desk's 16:05 rebuild is a next-day preview). Outcomes:
- forward returns 1/5/10/20 sessions after entry, net of costs, raw and in excess
  of the same-day average of every scanned name (removes market moves and most of
  the universe's survivorship bias, which the baseline shares);
- a Rule 3 trade: hold until the first close under the stop (SMA20 Mom, SMA50 Cap,
  recomputed each day), exit at the next open, at most 60 sessions.
Only the first day of a run counts (a name that stays TIER1 all week is one entry).

Known differences from live, printed in every report (NOTES).
"""
import math
import random
from collections import defaultdict
from dataclasses import replace
from datetime import date, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from . import rules

HORIZONS = (1, 5, 10, 20)
FLOORS = (0.70, 0.75, 0.80, 0.85, 0.90)
STATUSES = ("TIER1", "CAP_SETUP", "TIER2", "WATCH_HOLD")
WINDOW_DAYS = 420          # same lookback as desk.build_daily
MAX_HOLD = 60
NOTES = [
    "RVOL20 uses daily-bar volume: five years of 30-minute SIP history is not fetched. Live uses SIP RTH.",
    "AK-S needs 5-minute bars, so CAP_SETUP is scored raw and with AK-D held through the entry day "
    "(entered at that close only if the day's low stayed above the prior session's low).",
    "NEWS, EPS and the wash-sale list are not applied.",
    "Universe = today's names, so names that collapsed and left it are missing. The same-day "
    "baseline shares that bias, so excess returns are the fairer read.",
    "Daily bars are split-adjusted, as live; dividends are not added back.",
]


def scan_symbol(bars: Sequence[dict], cfg: rules.RuleConfig, start: str,
                floors: Sequence[float] = FLOORS, window_days: int = WINDOW_DAYS) -> List[dict]:
    """One row per session on or after `start`, computed from that session's window only."""
    dates = [b["date"] for b in bars]
    out, lo = [], 0
    for i, b in enumerate(bars):
        if b["date"] < start:
            continue
        cutoff = (date.fromisoformat(b["date"]) - timedelta(days=window_days)).isoformat()
        while dates[lo] < cutoff:
            lo += 1
        win = bars[lo:i + 1]
        if len(win) < 60:
            continue
        m = rules.daily_metrics(win, None, cfg, daily_source="DAILY")
        c = rules.classify_daily(m, cfg)
        out.append({"i": i, "date": b["date"], "status": c["status"], "book": c["book"],
                    "sma20": m["sma20"], "sma50": m["sma50"], "rvol": m["rvol20"],
                    "by_floor": {f: rules.classify_daily(m, replace(cfg, rvol_floor=f))["status"]
                                 for f in floors}})
    return out


def first_days(rows: List[dict], key=lambda r: r["status"]) -> Dict[str, List[dict]]:
    """status -> rows where that status starts a run (absent the previous session).
    The first scanned session is never a start: its run may have begun earlier."""
    out: Dict[str, List[dict]] = defaultdict(list)
    prev = None
    for r in rows:
        s = key(r)
        if prev is not None and not (prev["i"] == r["i"] - 1 and key(prev) == s):
            out[s].append(r)
        prev = r
    return out


def forward(bars: Sequence[dict], e: int, px: float, at_close: bool, cost: float,
            horizons: Sequence[int] = HORIZONS) -> Dict[int, Optional[float]]:
    """Net return from entry (session e, at its open or close) to the close h sessions on."""
    out = {}
    for h in horizons:
        j = e + h if at_close else e + h - 1
        out[h] = (bars[j]["c"] / px - 1 - 2 * cost) if j < len(bars) else None
    return out


def excursion(bars: Sequence[dict], e: int, px: float, n: int = 20) -> Tuple[Optional[float], Optional[float]]:
    span = bars[e:e + n]
    if len(span) < n:
        return None, None
    return max(b["h"] for b in span) / px - 1, min(b["l"] for b in span) / px - 1


def rule3_trade(bars: Sequence[dict], by_i: Dict[int, dict], e: int, px: float, at_close: bool,
                book: str, cost: float, max_hold: int = MAX_HOLD) -> dict:
    """Hold until the first close under the book's stop, exit at the next open."""
    first = e + 1 if at_close else e
    last = e + max_hold
    for j in range(first, min(len(bars), last)):
        r = by_i.get(j)
        stop = (r["sma50"] if book == "Cap" else r["sma20"]) if r else None
        if stop is not None and bars[j]["c"] < stop:
            if j + 1 >= len(bars):
                return {"exit": "open"}
            return {"exit": "stop", "ret": bars[j + 1]["o"] / px - 1 - 2 * cost, "days": j + 1 - e}
    if last - 1 >= len(bars):
        return {"exit": "open"}
    return {"exit": "time", "ret": bars[last - 1]["c"] / px - 1 - 2 * cost, "days": max_hold}


def summarize(values: Sequence[float], months: Sequence[str], n_boot: int = 1000, seed: int = 7) -> dict:
    """Mean, median, hit rate and a 95% interval for the mean from a bootstrap that
    resamples whole calendar months: signals on the same day move together, so
    resampling single signals would overstate the confidence."""
    n = len(values)
    if n == 0:
        return {"n": 0}
    mean = sum(values) / n
    srt = sorted(values)
    med = srt[n // 2] if n % 2 else (srt[n // 2 - 1] + srt[n // 2]) / 2
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1)) if n > 1 else 0.0
    out = {"n": n, "mean": mean, "median": med, "hit": sum(v > 0 for v in values) / n, "sd": sd}
    groups: Dict[str, List[float]] = defaultdict(list)
    for v, mo in zip(values, months):
        groups[mo].append(v)
    keys = sorted(groups)
    if len(keys) >= 5:
        rng = random.Random(seed)
        means = []
        for _ in range(n_boot):
            tot = cnt = 0
            for _k in keys:
                g = groups[keys[rng.randrange(len(keys))]]
                tot += sum(g)
                cnt += len(g)
            means.append(tot / cnt)
        means.sort()
        out["ci95"] = [means[int(0.025 * n_boot)], means[int(0.975 * n_boot) - 1]]
        out["months"] = len(keys)
    return out


def _signal(sym, kind, bars, by_i, r, cost, at_close=False, book=None):
    e = r["i"] + 1
    if e >= len(bars):
        return None
    px = bars[e]["c"] if at_close else bars[e]["o"]
    mfe, mae = excursion(bars, e, px)
    return {"ticker": sym, "kind": kind, "date": r["date"], "entry_date": bars[e]["date"], "entry": px,
            "fwd": forward(bars, e, px, at_close, cost), "mfe20": mfe, "mae20": mae,
            "rule3": rule3_trade(bars, by_i, e, px, at_close, book or r["book"], cost)}


def run(bars_by_sym: Dict[str, List[dict]], cfg: rules.RuleConfig, start: str, end: str,
        cost_bps: float = 5.0, n_boot: int = 1000) -> Tuple[dict, List[dict]]:
    """Returns (report, signal rows). The report holds aggregates only; the rows name tickers."""
    cost = cost_bps / 10_000.0
    base_sum = defaultdict(lambda: defaultdict(float))     # date -> h -> sum of open-entry returns
    base_cnt = defaultdict(lambda: defaultdict(int))
    signals: List[dict] = []
    name_days = 0
    for sym in sorted(bars_by_sym):
        bars = [b for b in bars_by_sym[sym] if b["date"] <= end]
        rows = scan_symbol(bars, cfg, start)
        name_days += len(rows)
        by_i = {r["i"]: r for r in rows}
        for r in rows:                                     # baseline: every scanned name-day
            e = r["i"] + 1
            if e < len(bars):
                for h, v in forward(bars, e, bars[e]["o"], False, cost).items():
                    if v is not None:
                        base_sum[r["date"]][h] += v
                        base_cnt[r["date"]][h] += 1
        for status, firsts in first_days(rows).items():
            for r in firsts:
                s = _signal(sym, status, bars, by_i, r, cost)
                if s:
                    signals.append(s)
                if status == "CAP_SETUP":                  # AK-D variant: the entry day held the prior low
                    e = r["i"] + 1
                    if e < len(bars) and bars[e]["l"] > bars[r["i"]]["l"]:
                        s = _signal(sym, "CAP_SETUP_AKD", bars, by_i, r, cost, at_close=True, book="Cap")
                        if s:
                            signals.append(s)
        for f in FLOORS:                                   # RVOL floor sensitivity, TIER1 only
            for r in first_days(rows, key=lambda x, f=f: x["by_floor"][f]).get("TIER1", []):
                s = _signal(sym, f"TIER1@{f:.2f}", bars, by_i, r, cost)
                if s:
                    signals.append(s)

    base_mean = {d: {h: base_sum[d][h] / base_cnt[d][h] for h in base_sum[d]} for d in base_sum}
    for s in signals:
        bm = base_mean.get(s["date"], {})
        s["excess"] = {h: (v - bm[h]) if v is not None and h in bm else None for h, v in s["fwd"].items()}

    def block(kind):
        sel = [s for s in signals if s["kind"] == kind]
        out = {"signals": len(sel), "fwd": {}, "excess": {}}
        for h in HORIZONS:
            for name in ("fwd", "excess"):
                vals = [(s[name][h], s["date"][:7]) for s in sel if s[name][h] is not None]
                out[name][str(h)] = summarize([v for v, _ in vals], [m for _, m in vals], n_boot)
        done = [s["rule3"] for s in sel if s["rule3"]["exit"] != "open"]
        r3 = summarize([t["ret"] for t in done], [s["date"][:7] for s in sel if s["rule3"]["exit"] != "open"], n_boot)
        r3["avg_days"] = (sum(t["days"] for t in done) / len(done)) if done else None
        r3["stopped"] = sum(t["exit"] == "stop" for t in done)
        r3["open"] = len(sel) - len(done)
        out["rule3"] = r3
        ex = [s for s in sel if s["mfe20"] is not None]
        out["mfe20_mean"] = (sum(s["mfe20"] for s in ex) / len(ex)) if ex else None
        out["mae20_mean"] = (sum(s["mae20"] for s in ex) / len(ex)) if ex else None
        years: Dict[str, List[float]] = defaultdict(list)
        for s in sel:
            if s["excess"][10] is not None:
                years[s["date"][:4]].append(s["excess"][10])
        out["by_year_excess10"] = {y: {"n": len(v), "mean": sum(v) / len(v)} for y, v in sorted(years.items())}
        return out

    all_base = defaultdict(list)
    for d in base_mean:
        for h, v in base_mean[d].items():
            all_base[h].append(v)
    report = {
        "period": {"start": start, "end": end}, "symbols": len(bars_by_sym), "name_days": name_days,
        "cost_bps_per_side": cost_bps, "entry": "next session open", "horizons": list(HORIZONS),
        "baseline_daily_mean_fwd": {str(h): (sum(v) / len(v)) for h, v in sorted(all_base.items()) if v},
        "statuses": {k: block(k) for k in STATUSES + ("CAP_SETUP_AKD",)},
        "rvol_floor_sensitivity": {f"{f:.2f}": block(f"TIER1@{f:.2f}") for f in FLOORS},
        "notes": NOTES,
    }
    return report, signals


def signal_rows(signals: List[dict]) -> List[dict]:
    """Flat rows for backtest_signals.csv (private: names tickers)."""
    out = []
    for s in signals:
        if "@" in s["kind"]:
            continue
        row = {"date": s["date"], "ticker": s["ticker"], "kind": s["kind"], "entry_date": s["entry_date"],
               "entry": round(s["entry"], 4)}
        for h in HORIZONS:
            row[f"fwd{h}"] = None if s["fwd"][h] is None else round(s["fwd"][h], 5)
            row[f"excess{h}"] = None if s["excess"][h] is None else round(s["excess"][h], 5)
        t = s["rule3"]
        row.update({"rule3_exit": t["exit"], "rule3_ret": None if "ret" not in t else round(t["ret"], 5),
                    "rule3_days": t.get("days")})
        out.append(row)
    return out
