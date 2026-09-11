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


def print_size_table(dataset, stops=(1.0, 2.0, 3.0, 4.0, 6.0)):
    """Shares and actual risk per candidate across plausible stop distances.

    Only tickers carrying a VERIFIED close appear -- sizing off an unconfirmed price
    is how a position ends up twice the size you meant.
    """
    eq, risk, cap = mf.DEFAULT_EQUITY, mf.DEFAULT_RISK_PCT, mf.DEFAULT_MAX_NOTIONAL_PCT
    rows = rs.triage_all(rs.apply_cluster_cap(rs.rank(dataset)))
    sized = [r for r in rows if r.get("close")
             and r["action"] in ("PRIMARY", "GAP_WATCH", "ALTERNATE")]

    print(f"\nSIZING -- equity ${eq:,.0f}, risk {risk}% (${eq * risk / 100:.0f}/trade), "
          f"notional cap {cap:.0f}% (${eq * cap / 100:,.0f})")
    print(f"A full R is only reachable at a stop {mf.full_r_stop_pct(risk, cap):.2f}% away. "
          f"Below that the CAP sizes the trade, not the risk number.")
    print("-" * 108)
    print(f"{'TICKER':<8}{'CLOSE':>9}  " + "".join(f"{str(s) + '% stop':>17}" for s in stops))
    for row in sized:
        cells = []
        for sp in stops:
            res = mf.position_size(eq, risk, row["close"], row["close"] * (1 - sp / 100))
            cells.append(f"{res['shares']:>5}sh ${res['actual_risk']:>6.0f}"
                         f"{'*' if res['notional_capped'] else ' '}")
        print(f"{row['symbol']:<8}{row['close']:>9.2f}  " + "".join(f"{c:>17}" for c in cells))
    print("\n* = cap binding; actual risk is BELOW the nominal R. Size expectancy on the "
          "dollar figure shown, never on the nominal risk %.")


ACTION_ORDER = ["PRIMARY", "GAP_WATCH", "ALTERNATE", "STAND_ASIDE", "AVOID_LONG", "DISQUALIFIED"]


def print_triage(dataset, rows):
    """Per-ticker action card, grouped by action rather than by rank.

    Grouped deliberately: at 9:25am you need to know what to DO with a name, and
    scanning a rank-ordered list for that is how a disqualified ticker ends up in a
    live order.
    """
    triaged = rs.triage_all(rows)
    weekend = dataset.get("weekend_developments", {})

    print("\n" + "=" * 108)
    print(f"TRIAGE FOR {dataset['next_regular_session']}")
    print("=" * 108)
    if weekend.get("net_read"):
        print("\n" + weekend["net_read"])
    if weekend.get("items"):
        print("\nPost-close developments folded in:")
        for item in weekend["items"]:
            print(f"  [{item['date']}] {item['headline']}")

    by_action = {}
    for row in triaged:
        by_action.setdefault(row["action"], []).append(row)

    for action in ACTION_ORDER:
        group = by_action.get(action)
        if not group:
            continue
        print(f"\n--- {action} ({len(group)}) -- {rs.TRIAGE_ACTIONS[action]}")
        for row in group:
            move = f"{row['move_pct']:+.1f}%" if row["move_pct"] is not None else "n/a"
            print(f"  {row['symbol']:<6} score {row['score']:>6.2f}  {move:>7}  "
                  f"{row['cluster']:<20} weekend={row['weekend_delta']}")
            print(f"         {row['action_why']}")
            if row.get("weekend_note") and row["weekend_delta"] != "unchanged":
                print(f"         weekend: {row['weekend_note']}")


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
    ap.add_argument("--triage", action="store_true",
                    help="print the per-ticker action card for the next session")
    ap.add_argument("--markdown", metavar="PATH", help="write a markdown report")
    ap.add_argument("--watchlist-var", action="store_true",
                    help="print a comma-separated WATCHLIST repo-variable line")
    ap.add_argument("--size", nargs=4, metavar=("EQUITY", "RISK_PCT", "ENTRY", "STOP"),
                    type=float, help="position-size one trade and exit")
    ap.add_argument("--size-table", action="store_true",
                    help=f"shares/risk grid at ${mf.DEFAULT_EQUITY:,.0f} / "
                         f"{mf.DEFAULT_RISK_PCT}%% for tickers with a verified close")
    args = ap.parse_args()

    if args.size_table:
        print_size_table(load(args.dataset))
        return

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

    if args.triage:
        print_triage(dataset, rows)

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
