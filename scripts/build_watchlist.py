# -*- coding: utf-8 -*-
"""
Build a ranked, deduped, cluster-capped watchlist from a Track A research file, and
optionally put every surviving candidate through the mechanical gate against real
bars.

    python scripts/build_watchlist.py                          # rank only (offline)
    python scripts/build_watchlist.py --live                   # + mechanical gate
    python scripts/build_watchlist.py --live --top 15
    python scripts/build_watchlist.py --markdown report.md
    python scripts/build_watchlist.py --watchlist-var          # WATCHLIST repo-var line
    python scripts/build_watchlist.py --size 25000 1.5 84.30 82.10

--live needs ALPACA_ID / ALPACA_SECRET in the environment. Without it the gate does
not run, and the output says so rather than implying names were cleared. A name is
tradeable only when it is ranked AND gated -- ranking alone is never sufficient.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mechanical_filters as mf
import research_scan as rs

DEFAULT_DATASET = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "research", "track_a_2026-09-04.json")


def load(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def print_ranking(rows, top, show_all):
    print(f"{'#':>3}  {'SYM':<6} {'SCORE':>6}  {'DIRECTION':<12} {'SRC':>3}  "
          f"{'CLUSTER':<20} {'MOVE':>7}  NOTE")
    print("-" * 108)
    for row in rows[:None if show_all else top]:
        if row["disqualified"]:
            note = "DQ: " + (row["disqualify_reasons"][0][:52] if row["disqualify_reasons"] else "")
        elif row.get("cluster_capped"):
            note = f"cluster-capped (slot {row['cluster_slot']}) -- alternate only"
        else:
            note = ", ".join(row["flags"])[:52] or "-"
        move = f"{row['move_pct']:+.1f}%" if row["move_pct"] is not None else "   n/a"
        print(f"{row['rank']:>3}  {row['symbol']:<6} {row['score']:>6.2f}  "
              f"{row['direction']:<12} {row['n_sources']:>3}  {row['cluster']:<20} "
              f"{move:>7}  {note}")


def run_gate(rows, top):
    alpaca_id = os.environ.get("ALPACA_ID", "")
    alpaca_secret = os.environ.get("ALPACA_SECRET", "")
    if not alpaca_id:
        print("\n[gate] ALPACA_ID/ALPACA_SECRET not set -- falling back to yfinance. "
              "Spread checks will fail closed without a quote source.", file=sys.stderr)

    candidates = [r for r in rows
                  if r["direction"] == "LONG" and not r["disqualified"]
                  and not r.get("cluster_capped")][:top]

    print(f"\nMECHANICAL GATE -- {len(candidates)} candidates against live bars")
    print("-" * 108)
    results = []
    for row in candidates:
        sym = row["symbol"]
        bars = mf.fetch_daily_bars(sym, alpaca_id, alpaca_secret)
        if not bars:
            print(f"  {sym:<6} REJECT   no bars retrieved (fail-closed)")
            results.append({"symbol": sym, "passed": False, "reject_reason": "no data"})
            continue
        quote = mf.fetch_quote(sym, alpaca_id, alpaca_secret)
        verdict = mf.screen(sym, bars, quote)
        results.append(verdict)
        if verdict["passed"]:
            m = verdict["metrics"]
            print(f"  {sym:<6} PASS     px {m['price']:.2f} | SMA20 {m['sma20']:.2f} > "
                  f"SMA50 {m['sma50']:.2f} > SMA200 {m['sma200']:.2f} | "
                  f"ATR {m['atr_pct']:.1f}% | ext {m['extension_atr']:.1f} ATR")
        else:
            print(f"  {sym:<6} REJECT   {verdict['reject_reason']}")
    return results


def to_markdown(dataset, rows, top):
    ctx = dataset["market_context"]
    out = [f"# Track A ranked watchlist -- session {dataset['as_of_session']}", ""]
    out.append(f"Next regular session: **{dataset['next_regular_session']}** "
               f"({dataset['note_next_session']})")
    out.append("")
    out.append(f"S&P 500 {ctx['spx_close']} ({ctx['spx_pct']:+.2f}%) | "
               f"Nasdaq {ctx['nasdaq_comp_close']} ({ctx['nasdaq_comp_pct']:+.2f}%) | "
               f"Dow {ctx['dow_close']} ({ctx['dow_pct']:+.2f}%)")
    out.append("")
    out.append(f"Regime: **{ctx['rate_regime']}** -- {ctx['rate_regime_note']}")
    out.append("")
    out.append(f"> {ctx['dispersion_note']}")
    out.append("")
    out.append("| # | Ticker | Score | Direction | Sources | Cluster | Move | Flags |")
    out.append("|---|--------|-------|-----------|---------|---------|------|-------|")
    for row in rows[:top]:
        move = f"{row['move_pct']:+.1f}%" if row["move_pct"] is not None else "n/a"
        flags = ", ".join(row["flags"]) or "-"
        if row["disqualified"]:
            flags = "**DQ** -- " + flags
        elif row.get("cluster_capped"):
            flags = "*cluster-capped* -- " + flags
        out.append(f"| {row['rank']} | {row['symbol']} | {row['score']:.2f} | "
                   f"{row['direction']} | {row['n_sources']} | {row['cluster']} | "
                   f"{move} | {flags} |")
    out.append("")
    out.append("## Cluster exposure")
    out.append("")
    for cluster, count in rs.cluster_exposure(rows).items():
        out.append(f"- `{cluster}`: {count} long candidate(s)")
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=DEFAULT_DATASET)
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--all", action="store_true", help="print every ranked row")
    ap.add_argument("--max-per-cluster", type=int, default=2,
                    help="concurrent long candidates allowed per correlation cluster")
    ap.add_argument("--live", action="store_true", help="run the mechanical gate on real bars")
    ap.add_argument("--markdown", metavar="PATH", help="write a markdown report")
    ap.add_argument("--watchlist-var", action="store_true",
                    help="print a comma-separated WATCHLIST repo-variable line")
    ap.add_argument("--size", nargs=4, metavar=("EQUITY", "RISK_PCT", "ENTRY", "STOP"),
                    type=float, help="position-size one trade and exit")
    args = ap.parse_args()

    if args.size:
        equity, risk_pct, entry, stop = args.size
        result = mf.position_size(equity, risk_pct, entry, stop)
        for key, value in result.items():
            print(f"  {key:<24} {value}")
        return

    dataset = load(args.dataset)
    rows = rs.apply_cluster_cap(rs.rank(dataset), args.max_per_cluster)

    print(f"Track A -- session {dataset['as_of_session']} -> next open "
          f"{dataset['next_regular_session']}")
    print(f"Regime: {dataset['market_context']['rate_regime']} | "
          f"{len(rows)} unique tickers after dedup | "
          f"{sum(1 for r in rows if r['disqualified'])} disqualified")
    print(f"Consensus computed over "
          f"{sum(1 for s in dataset['chat_sources'] if s['transcript_supplied'])} "
          f"of {len(dataset['chat_sources'])} declared chat sources\n")
    print_ranking(rows, args.top, args.all)

    print("\nCLUSTER EXPOSURE (long candidates per correlation group)")
    for cluster, count in rs.cluster_exposure(rows).items():
        marker = "  <-- CONCENTRATION RISK" if count >= 4 else ""
        print(f"  {cluster:<22} {count}{marker}")

    if args.watchlist_var:
        tradeable = [r["symbol"] for r in rows
                     if r["direction"] == "LONG" and not r["disqualified"]
                     and not r.get("cluster_capped")][:args.top]
        print(f"\nWATCHLIST={','.join(tradeable)}")

    if args.live:
        run_gate(rows, args.top)

    if args.markdown:
        with open(args.markdown, "w", encoding="utf-8") as fh:
            fh.write(to_markdown(dataset, rows, args.top))
        print(f"\nWrote {args.markdown}")


if __name__ == "__main__":
    main()
