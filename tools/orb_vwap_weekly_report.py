# -*- coding: utf-8 -*-
"""
Weekly paper report for the ORB / VWAP-pullback intraday bot.

Reads the bot's daily "post_session_summary_YYYY-MM-DD.html" pages from a folder and
writes one self-contained HTML page per Monday-Friday week (no external files or
scripts; opens in any browser, also offline). Standard library only.

  python orb_vwap_weekly_report.py SUMMARY_FOLDER --week 2026-11-02 [--out FOLDER]
  python orb_vwap_weekly_report.py SUMMARY_FOLDER            # last completed week

What the summaries contain, and so what the page can show: each session's paper
equity, day change, open positions, flattened flag, and every order the bot sent
(time, symbol, strategy, side, qty, entry, stop, target). They do not record exits
or fills, so per-trade results are NOT shown; P/L comes from the day-change line.
A weekday with no summary is shown as MISSING, never estimated.
"""
import argparse
import glob
import html
import json
import os
import re
from datetime import date, datetime, timedelta
from html.parser import HTMLParser

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri")


class _Summary(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paras, self.rows, self._row, self._cell, self._in = [], [], None, None, None

    def handle_starttag(self, tag, attrs):
        if tag == "p":
            self._in = []
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._cell = []

    def handle_endtag(self, tag):
        if tag == "p" and self._in is not None:
            self.paras.append("".join(self._in).strip())
            self._in = None
        elif tag in ("td", "th") and self._cell is not None:
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)
        elif self._in is not None:
            self._in.append(data)


def _num(s):
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


def parse_summary(text: str) -> dict:
    p = _Summary()
    p.feed(text)
    info = " · ".join(p.paras)
    m = lambda rx: (re.search(rx, info) or [None, None])[1]
    header = p.rows[0] if p.rows else []
    orders = []
    for r in p.rows[1:]:
        o = dict(zip([h.lower() for h in header], r))
        orders.append({"time": o.get("time"), "sym": o.get("sym"), "strategy": o.get("strategy"),
                       "side": o.get("side"), "qty": _num(o.get("qty")), "entry": _num(o.get("entry")),
                       "stop": _num(o.get("stop")), "target": _num(o.get("target")),
                       "order": o.get("order"), "dry": o.get("dry")})
    return {"generated": m(r"Generated ([0-9-]+ [0-9:]+ ET)"), "equity": _num(m(r"paper equity ([0-9,.\-]+)")),
            "day_change": _num(m(r"day change ([0-9,.\-]+)")), "signals": _num(m(r"Signals/trades: (\d+)")),
            "open_positions": _num(m(r"open paper positions now: (\d+)")),
            "flattened": m(r"flattened flag: (\w+)"), "sizing": m(r"(Sized on [^.]+)"), "orders": orders}


def load_week(folder: str, monday: date) -> list:
    days = []
    for i in range(5):
        d = (monday + timedelta(days=i)).isoformat()
        path = os.path.join(folder, f"post_session_summary_{d}.html")
        s = None
        if os.path.exists(path):
            with open(path, encoding="utf-8", errors="replace") as f:
                s = parse_summary(f.read())
        days.append({"date": d, "s": s})
    return days


def prior_equity(folder: str, monday: date):
    """Equity at the last summary before this week, for the week's starting point."""
    best = None
    for path in glob.glob(os.path.join(folder, "post_session_summary_*.html")):
        d = os.path.basename(path)[21:31]
        if d < monday.isoformat() and (best is None or d > best[0]):
            best = (d, path)
    if not best:
        return None, None
    with open(best[1], encoding="utf-8", errors="replace") as f:
        return best[0], parse_summary(f.read())["equity"]


def risk_fields(o: dict) -> dict:
    e, s, t, q = o["entry"], o["stop"], o["target"], o["qty"]
    if None in (e, s, t, q) or e == s:
        return {"notional": None, "risk": None, "rr": None}
    return {"notional": e * q, "risk": abs(e - s) * q, "rr": abs(t - e) / abs(e - s)}


def _e(x):
    return html.escape("" if x is None else str(x))


def _money(x):
    return "" if x is None else f"{'+' if x >= 0 else '−'}${abs(x):,.2f}"


def render(monday: date, days: list, start, generated: str, source_note: str) -> str:
    friday = monday + timedelta(days=4)
    have = [d for d in days if d["s"]]
    orders = [dict(o, date=d["date"], **risk_fields(o)) for d in have for o in d["s"]["orders"]]
    week_pnl = sum(d["s"]["day_change"] or 0 for d in have) if have else None
    end_eq = have[-1]["s"]["equity"] if have else None
    start_date, start_eq = start
    win_days = sum(1 for d in have if (d["s"]["day_change"] or 0) > 0)
    risks = [o["risk"] for o in orders if o["risk"] is not None]
    rrs = [o["rr"] for o in orders if o["rr"] is not None]

    kpis = [("Sessions recorded", f"{len(have)} of 5"), ("Orders sent", str(len(orders))),
            ("Week P/L (day changes)", _money(week_pnl) if have else "—"),
            ("Up days", f"{win_days} of {len(have)}" if have else "—"),
            ("Ending paper equity", f"${end_eq:,.2f}" if end_eq else "—"),
            ("Avg planned risk / order", f"${sum(risks) / len(risks):,.0f}" if risks else "—"),
            ("Avg reward:risk planned", f"{sum(rrs) / len(rrs):.2f}" if rrs else "—")]
    kpi_html = "".join(f'<div class="kpi"><div class="kv">{_e(v)}</div><div class="kl">{_e(k)}</div></div>' for k, v in kpis)

    # equity line: prior close, then each weekday close
    pts = [("prior " + (start_date[5:] if start_date else ""), start_eq)] + \
          [(f"{WEEKDAYS[i]} {d['date'][5:]}", d["s"]["equity"] if d["s"] else None) for i, d in enumerate(days)]
    vals = [(i, v) for i, (_, v) in enumerate(pts) if v is not None]
    if len(vals) >= 2:
        w, h, pl, pr, pt, pb = 960, 260, 120, 40, 20, 40
        lo, hi = min(v for _, v in vals), max(v for _, v in vals)
        span = (hi - lo) or 1.0
        X = lambda i: pl + i * (w - pl - pr) / (len(pts) - 1)
        Y = lambda v: pt + (hi - v) / span * (h - pt - pb)
        path = " ".join(f"{'M' if k == 0 else 'L'}{X(i):.1f},{Y(v):.1f}" for k, (i, v) in enumerate(vals))
        dots = "".join(f'<circle cx="{X(i):.1f}" cy="{Y(v):.1f}" r="4" class="dot"><title>{_e(pts[i][0])}: ${v:,.2f}</title></circle>' for i, v in vals)
        ticks = "".join(f'<text x="{X(i):.1f}" y="{h - 12}" text-anchor="middle" class="axis">{_e(l)}</text>' for i, (l, _) in enumerate(pts))
        chart = (f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Paper equity at each session close">'
                 f'<text x="{pl - 10}" y="{Y(hi) + 4:.1f}" text-anchor="end" class="axis">${hi:,.0f}</text>'
                 f'<text x="{pl - 10}" y="{Y(lo) + 4:.1f}" text-anchor="end" class="axis">${lo:,.0f}</text>'
                 f'<path d="{path}" class="line"/>{dots}{ticks}</svg>')
    else:
        chart = '<p class="muted">Not enough sessions with an equity figure to draw a line.</p>'

    day_rows = []
    for i, d in enumerate(days):
        s = d["s"]
        if not s:
            day_rows.append([f"{WEEKDAYS[i]} {d['date']}", '<span class="bad">MISSING</span>', "", "", "", "", ""])
            continue
        day_rows.append([f"{WEEKDAYS[i]} {d['date']}", _e(f"${s['equity']:,.2f}" if s["equity"] else ""),
                         f'<span class="{"good" if (s["day_change"] or 0) >= 0 else "bad"}">{_e(_money(s["day_change"]))}</span>',
                         _e(len(s["orders"])), _e(int(s["open_positions"]) if s["open_positions"] is not None else ""),
                         _e(s["flattened"]), _e(s["generated"])])

    order_rows = [[_e(o["date"]), _e(o["time"]), _e(o["sym"]), _e(o["strategy"]), _e(o["side"]),
                   _e(f"{o['qty']:.0f}" if o["qty"] is not None else ""), _e(o["entry"]), _e(o["stop"]), _e(o["target"]),
                   _e(f"${o['notional']:,.0f}" if o["notional"] else ""), _e(f"${o['risk']:,.0f}" if o["risk"] else ""),
                   _e(f"{o['rr']:.2f}" if o["rr"] else ""), _e(o["dry"])] for o in orders]

    def breakdown(key):
        groups = {}
        for o in orders:
            g = groups.setdefault(o[key] or "?", {"n": 0, "buy": 0, "sell": 0, "risk": 0.0, "rr": []})
            g["n"] += 1
            g["buy" if o["side"] == "BUY" else "sell"] += 1
            g["risk"] += o["risk"] or 0
            if o["rr"]:
                g["rr"].append(o["rr"])
        return [[_e(k), _e(g["n"]), _e(g["buy"]), _e(g["sell"]), _e(f"${g['risk']:,.0f}"),
                 _e(f"{sum(g['rr']) / len(g['rr']):.2f}" if g["rr"] else "")] for k, g in
                sorted(groups.items(), key=lambda kv: -kv[1]["n"])]

    notes = [source_note,
             "The summaries record orders sent, not exits or fills, so this page does not show a result per trade. "
             "Week P/L is the sum of each session's day-change line.",
             "Planned risk = |entry − stop| × qty; reward:risk = |target − entry| ÷ |entry − stop|, both as sent.",
             f"Week starting equity: {'$' + format(start_eq, ',.2f') + ' at the ' + start_date + ' close' if start_eq else 'no earlier summary'}."]
    missing = [d["date"] for d in days if not d["s"]]
    if missing:
        notes.append(f"No summary for {', '.join(missing)}: shown as MISSING, nothing estimated. "
                     "If the bot ran that day, its summary was not saved.")
    sizing = {d["s"]["sizing"] for d in have if d["s"]["sizing"]}
    if sizing and end_eq:
        notes.append(f"The summaries say “{'; '.join(sorted(sizing))}” while paper equity is about ${end_eq:,.0f}: "
                     "position sizes follow the $100k spec, not the account size.")
    if start_eq and end_eq and have:
        drift = (end_eq - start_eq) - week_pnl
        if abs(drift) > 1:
            notes.append(f"Equity moved {_money(end_eq - start_eq)} over the week but day changes sum to "
                         f"{_money(week_pnl)} (gap {_money(drift)}), which a missing session or cash movement would explain.")

    T = lambda tid, cols, rows, empty, num=(): ('<p class="muted">' + _e(empty) + '</p>') if not rows else (
        f'<table id="{tid}" class="sortable"><thead><tr>' +
        "".join(f'<th data-k="{i}"{" class=num" if i in num else ""}>{_e(c)}</th>' for i, c in enumerate(cols)) +
        "</tr></thead><tbody>" + "".join("<tr>" + "".join(f'<td{" class=num" if i in num else ""}>{c}</td>'
                                                          for i, c in enumerate(r)) + "</tr>" for r in rows) + "</tbody></table>")

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ORB / VWAP bot weekly paper report {monday.isoformat()}</title>
<style>
:root{{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e2e1dc;--accent:#2f6fde;--good:#1f8a4c;--bad:#c0392b}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151513;--card:#1f1f1c;--ink:#ecebe6;--muted:#a3a29c;--line:#34332f;--accent:#7aa7ff;--good:#4cc27e;--bad:#ff7b6e}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}}
main{{max-width:1080px;margin:0 auto;padding:24px 16px 48px}}h1{{font-size:24px;margin:0 0 4px}}h2{{font-size:18px;margin:28px 0 10px}}
.muted,.sub{{color:var(--muted)}}.banner{{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--accent);padding:10px 14px;border-radius:8px;margin:16px 0}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin:16px 0}}
.kpi{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}}.kv{{font-size:21px;font-weight:650}}.kl{{color:var(--muted);font-size:13px}}
.tabs{{display:flex;gap:6px;flex-wrap:wrap;margin-top:20px}}.tabs button{{border:1px solid var(--line);background:var(--card);color:var(--ink);padding:7px 12px;border-radius:999px;cursor:pointer;font:inherit}}
.tabs button[aria-selected=true]{{background:var(--accent);border-color:var(--accent);color:#fff}}section[hidden]{{display:none}}
.wrap{{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}}table{{border-collapse:collapse;width:100%;font-size:14px}}
th,td{{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}}th{{cursor:pointer;user-select:none;background:var(--card)}}
th:after{{content:" ↕";color:var(--muted);font-size:11px}}.num{{text-align:right;font-variant-numeric:tabular-nums}}
input[type=search]{{width:100%;max-width:320px;padding:7px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink);font:inherit;margin:8px 0}}
.bad{{color:var(--bad);font-weight:600}}.good{{color:var(--good);font-weight:600}}svg{{width:100%;max-height:300px;height:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}}
.axis{{fill:var(--muted);font-size:13px}}.line{{fill:none;stroke:var(--accent);stroke-width:2.5}}.dot{{fill:var(--accent)}}
footer{{margin-top:32px;color:var(--muted);font-size:13px}}
</style></head><body><main>
<h1>ORB / VWAP-pullback bot — weekly paper report</h1>
<div class="sub">Week of {monday:%a %b %d} – {friday:%a %b %d, %Y} · generated {_e(generated)}</div>
<div class="banner"><b>Paper account only.</b> Built from the bot's own post-session summaries. Not trade advice.</div>
<div class="kpis">{kpi_html}</div>
<h2>Paper equity at each session close</h2>{chart}
<div class="tabs" role="tablist">
<button role="tab" aria-selected="true" data-t="days">Daily</button><button role="tab" aria-selected="false" data-t="orders">Orders</button>
<button role="tab" aria-selected="false" data-t="strat">By strategy</button><button role="tab" aria-selected="false" data-t="sym">By symbol</button>
<button role="tab" aria-selected="false" data-t="notes">Data notes</button></div>
<section id="days"><h2>Each session</h2><div class="wrap">{T("t-days", ["Day", "Equity", "Day change", "Orders", "Open at close", "Flattened", "Summary generated"], day_rows, "", (1, 2, 3, 4))}</div></section>
<section id="orders" hidden><h2>Orders sent</h2><input type="search" placeholder="Filter by symbol, strategy, side…" data-f="t-orders">
<div class="wrap">{T("t-orders", ["Date", "Time", "Symbol", "Strategy", "Side", "Qty", "Entry", "Stop", "Target", "Notional", "Planned risk", "R:R", "Dry run"], order_rows, "No orders were recorded this week.", (5, 6, 7, 8, 9, 10, 11))}</div></section>
<section id="strat" hidden><h2>By strategy</h2><div class="wrap">{T("t-strat", ["Strategy", "Orders", "Buys", "Sells", "Planned risk", "Avg R:R"], breakdown("strategy"), "No orders.", (1, 2, 3, 4, 5))}</div></section>
<section id="sym" hidden><h2>By symbol</h2><div class="wrap">{T("t-sym", ["Symbol", "Orders", "Buys", "Sells", "Planned risk", "Avg R:R"], breakdown("sym"), "No orders.", (1, 2, 3, 4, 5))}</div></section>
<section id="notes" hidden><h2>Data notes</h2><ul>{"".join(f"<li>{_e(n)}</li>" for n in notes)}</ul></section>
<footer>Only what the summaries record is shown; a missing day is never estimated.</footer>
</main>
<script type="application/json" id="report-data">{html.escape(json.dumps({"week": monday.isoformat(), "orders": len(orders)}))}</script>
<script>
document.querySelectorAll('.tabs button').forEach(b=>b.addEventListener('click',()=>{{
 document.querySelectorAll('.tabs button').forEach(x=>x.setAttribute('aria-selected',x===b));
 document.querySelectorAll('main>section').forEach(s=>s.hidden=s.id!==b.dataset.t);}}));
document.querySelectorAll('table.sortable th').forEach(th=>th.addEventListener('click',()=>{{
 const tb=th.closest('table').tBodies[0],k=+th.dataset.k,asc=th.dataset.dir!=='asc';th.dataset.dir=asc?'asc':'desc';
 const v=t=>{{const s=t.cells[k].textContent.replace(/[$,%+]/g,'').replace('−','-');const n=parseFloat(s);return isNaN(n)?s:n}};
 [...tb.rows].sort((a,b)=>{{const x=v(a),y=v(b);return (x>y?1:x<y?-1:0)*(asc?1:-1)}}).forEach(r=>tb.appendChild(r));}}));
document.querySelectorAll('input[data-f]').forEach(i=>i.addEventListener('input',()=>{{
 const q=i.value.toLowerCase();document.querySelectorAll('#'+i.dataset.f+' tbody tr').forEach(r=>r.hidden=!r.textContent.toLowerCase().includes(q));}}));
</script></body></html>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", help="folder holding post_session_summary_YYYY-MM-DD.html files")
    ap.add_argument("--week", help="any date in the week (default: last completed Mon-Fri week)")
    ap.add_argument("--out", help="output folder (default: the summary folder)")
    args = ap.parse_args()
    today = date.today()
    if args.week:
        d = date.fromisoformat(args.week)
        monday = d - timedelta(days=d.weekday())
    else:
        monday = today - timedelta(days=today.weekday())
        monday = monday if today.weekday() >= 5 else monday - timedelta(days=7)
    days = load_week(args.folder, monday)
    page = render(monday, days, prior_equity(args.folder, monday), datetime.now().strftime("%Y-%m-%d %H:%M"),
                  f"Source: {sum(1 for d in days if d['s'])} post_session_summary pages from {os.path.basename(os.path.abspath(args.folder))}.")
    out = os.path.join(args.out or args.folder, f"orb_vwap_weekly_report_{monday.isoformat()}.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"week {monday}: {sum(1 for d in days if d['s'])}/5 sessions, written {out}")


if __name__ == "__main__":
    main()
