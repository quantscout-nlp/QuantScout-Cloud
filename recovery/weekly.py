# -*- coding: utf-8 -*-
"""
Weekly paper report: one self-contained HTML page (no external files or scripts,
opens in any browser, works offline) on what the desk did over a Monday-Friday week.

The desk places no orders, so "paper trading" here is a hypothetical ledger built
from the desk's own end-of-day records (`session_YYYY-MM-DD.json`):
- Entry: a name that is a Paper candidate in a session's record and was not one in
  the previous captured session; bought at the next session's open.
- Exit: the first later session whose record shows the close under that name's stop
  (SMA20 Mom, SMA50 Cap); sold at the next session's open. Otherwise the position
  is marked at the week's last close and reported as open.
- Equal notional per entry, cost per side applied on entry and exit.
Everything the page shows comes from signed desk files and daily bars. A day with
no record is reported as NOT CAPTURED, never filled in.
"""
import html
import json
from datetime import date, timedelta
from typing import Dict, List, Optional

from . import desk

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri")
STATUSES = ("TIER1", "CAP_SETUP", "TIER2", "WATCH_HOLD", "NOT_SCANNED")


def last_completed_week(today: date) -> date:
    """Monday of the most recent week whose Friday has passed (today on a weekend counts)."""
    monday = today - timedelta(days=today.weekday())
    return monday if today.weekday() >= 5 else monday - timedelta(days=7)


def collect(store, monday: date, env) -> List[dict]:
    """The week's end-of-day records and change logs, verified line by line."""
    days = []
    for i in range(5):
        iso = (monday + timedelta(days=i)).isoformat()
        snap, integrity = desk.load_signed(store.read_text(f"session_{iso}.json"), env)
        changes, dropped = [], 0
        for line in (store.read_text(f"changes_{iso}.jsonl") or "").splitlines():
            if line.strip():
                obj, li = desk.load_signed(line, env)
                if obj is None or li == "TAMPERED":
                    dropped += 1
                else:
                    changes.append(obj)
        days.append({"date": iso, "snap": snap, "integrity": integrity, "changes": changes, "dropped": dropped})
    return days


def ledger(days: List[dict], bars: Dict[str, List[dict]], notional: float, cost_bps: float) -> List[dict]:
    """Hypothetical paper trades from the week's Paper candidates (see module docstring)."""
    cost = cost_bps / 10_000.0
    captured = [d for d in days if d["snap"]]
    rows_by_day = [{r["ticker"]: r for r in d["snap"].get("rows", [])} for d in captured]
    trades = []
    for k, d in enumerate(captured):
        prev = rows_by_day[k - 1] if k else {}
        for t, r in rows_by_day[k].items():
            if not r.get("paper_candidate") or prev.get(t, {}).get("paper_candidate"):
                continue
            series = [b for b in bars.get(t, []) if b["date"] > d["date"]]
            trade = {"ticker": t, "book": r.get("book"), "status": r.get("status"), "signal_date": d["date"],
                     "signal_close": r.get("close"), "stop": r.get("stop"), "state": "PENDING_ENTRY"}
            if series:
                entry = series[0]
                trade.update(entry_date=entry["date"], entry=entry["o"], state="OPEN")
                for j in range(k + 1, len(captured)):
                    later = rows_by_day[j].get(t)
                    if later and later.get("stop") is not None and later.get("close") is not None \
                            and later["close"] < later["stop"] and captured[j]["date"] >= entry["date"]:
                        nxt = [b for b in series if b["date"] > captured[j]["date"]]
                        if nxt:
                            trade.update(exit_date=nxt[0]["date"], exit=nxt[0]["o"], state="STOPPED")
                        else:
                            trade.update(state="EXIT_PENDING", exit_flag_date=captured[j]["date"])
                        break
                last = series[-1]
                px = trade.get("exit", last["c"])
                trade["mark"] = px
                trade["mark_date"] = trade.get("exit_date", last["date"])
                trade["ret"] = px / entry["o"] - 1 - (2 * cost if trade["state"] == "STOPPED" else cost)
                trade["pnl"] = notional * trade["ret"]
            trades.append(trade)
    return trades


def pnl_by_day(trades: List[dict], bars: Dict[str, List[dict]], dates: List[str], notional: float,
               cost_bps: float) -> List[Optional[float]]:
    """Cumulative paper P/L at each weekday's close (realized + marked), None before the first entry."""
    cost = cost_bps / 10_000.0
    out = []
    for d in dates:
        total, any_open = 0.0, False
        for t in trades:
            if "entry_date" not in t or t["entry_date"] > d:
                continue
            any_open = True
            if t.get("exit_date") and t["exit_date"] <= d:
                total += notional * (t["exit"] / t["entry"] - 1 - 2 * cost)
                continue
            closes = [b["c"] for b in bars.get(t["ticker"], []) if t["entry_date"] <= b["date"] <= d]
            if closes:
                total += notional * (closes[-1] / t["entry"] - 1 - cost)
        out.append(round(total, 2) if any_open else None)
    return out


def _e(x) -> str:
    return html.escape("" if x is None else str(x))


def _money(x) -> str:
    return "" if x is None else f"{'+' if x >= 0 else '−'}${abs(x):,.2f}"


def _pct(x) -> str:
    return "" if x is None else f"{'+' if x >= 0 else '−'}{abs(x) * 100:.2f}%"


def _chart(dates: List[str], series: List[Optional[float]]) -> str:
    pts = [(i, v) for i, v in enumerate(series) if v is not None]
    if not pts:
        return '<p class="muted">No paper position was open this week, so there is no P/L line to draw.</p>'
    w, h, pl, pr, pt, pb = 960, 260, 96, 56, 20, 40
    lo, hi = min(0.0, *(v for _, v in pts)), max(0.0, *(v for _, v in pts))
    span = (hi - lo) or 1.0
    x = lambda i: pl + i * (w - pl - pr) / 4
    y = lambda v: pt + (hi - v) / span * (h - pt - pb)
    path = " ".join(f"{'M' if k == 0 else 'L'}{x(i):.1f},{y(v):.1f}" for k, (i, v) in enumerate(pts))
    ticks = "".join(f'<text x="{x(i):.1f}" y="{h - 10}" text-anchor="middle" class="axis">{WEEKDAYS[i]} {d[5:]}</text>'
                    for i, d in enumerate(dates))
    dots = "".join(f'<circle cx="{x(i):.1f}" cy="{y(v):.1f}" r="4" class="dot"><title>{dates[i]}: {_money(v)}</title></circle>'
                   for i, v in pts)
    return (f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Cumulative paper P/L by day">'
            f'<line x1="{pl}" x2="{w - pr}" y1="{y(0):.1f}" y2="{y(0):.1f}" class="zero"/>'
            f'<text x="{pl - 8}" y="{y(hi) + 4:.1f}" text-anchor="end" class="axis">{_money(hi)}</text>'
            f'<text x="{pl - 8}" y="{y(lo) + 4:.1f}" text-anchor="end" class="axis">{_money(lo)}</text>'
            f'<path d="{path}" class="line"/>{dots}{ticks}</svg>')


def _table(tid: str, cols: List[str], rows: List[List[str]], empty: str, numeric=()) -> str:
    if not rows:
        return f'<p class="muted">{_e(empty)}</p>'
    head = "".join(f'<th data-k="{i}"{" class=num" if i in numeric else ""}>{_e(c)}</th>' for i, c in enumerate(cols))
    body = "".join("<tr>" + "".join(f'<td{" class=num" if i in numeric else ""}>{c}</td>' for i, c in enumerate(r))
                   + "</tr>" for r in rows)
    return f'<table id="{tid}" class="sortable"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def render(monday: date, days: List[dict], trades: List[dict], pnl: List[Optional[float]], generated_et: str,
           notional: float, cost_bps: float, notes: List[str], prices_note: Optional[str]) -> str:
    friday = monday + timedelta(days=4)
    dates = [d["date"] for d in days]
    captured = [d for d in days if d["snap"]]
    entered = [t for t in trades if "entry" in t]
    closed = [t for t in entered if t["state"] == "STOPPED"]
    wins = [t for t in entered if t["ret"] > 0]
    week_pnl = pnl[-1] if pnl and pnl[-1] is not None else (0.0 if not entered else None)

    kpis = [("Sessions captured", f"{len(captured)} of 5"),
            ("Paper entries", str(len(entered))),
            ("Stopped out", str(len(closed))),
            ("Still open", str(sum(t["state"] in ("OPEN", "EXIT_PENDING") for t in entered))),
            ("Winning", f"{len(wins)} of {len(entered)}" if entered else "—"),
            ("Week paper P/L", _money(week_pnl) if entered else "—")]
    kpi_html = "".join(f'<div class="kpi"><div class="kv">{_e(v)}</div><div class="kl">{_e(k)}</div></div>' for k, v in kpis)

    day_rows = []
    for i, d in enumerate(days):
        s = d["snap"]
        if not s:
            day_rows.append([f"{WEEKDAYS[i]} {d['date']}", '<span class="bad">NOT CAPTURED</span>', "", "", "", "", "",
                             _e("no end-of-day record" if d["integrity"] == "MISSING" else f"record {d['integrity']}")])
            continue
        c, h = s.get("counts", {}), s.get("health", {})
        cand = sum(1 for r in s.get("rows", []) if r.get("paper_candidate"))
        day_rows.append([f"{WEEKDAYS[i]} {d['date']}", _e(h.get("status")), _e(h.get("scanned")),
                         _e(c.get("TIER1", 0)), _e(c.get("CAP_SETUP", 0)), _e(c.get("TIER2", 0)), _e(cand),
                         _e(f"{len(d['changes'])} changes" + (f", {d['dropped']} lines dropped" if d["dropped"] else ""))])

    trade_rows = [[_e(t["ticker"]), _e(t.get("book")), _e(t["signal_date"]), _e(t.get("entry_date", "")),
                   _e("" if "entry" not in t else f"{t['entry']:.2f}"), _e(t["state"]),
                   _e(t.get("mark_date", "")), _e("" if "mark" not in t else f"{t['mark']:.2f}"),
                   _e(_pct(t.get("ret"))), _e(_money(t.get("pnl")))] for t in trades]
    change_rows = [[_e(c.get("ts", "")[:16].replace("T", " ")), _e(c.get("ticker")), _e(c.get("from")), _e(c.get("to"))]
                   for d in days for c in d["changes"]]
    flags = []
    for d in captured:
        for line in (d["snap"].get("held") or {}).get("lines", []):
            if line.get("flag") != "OK":
                flags.append([_e(d["date"]), _e(line.get("ticker")), _e(line.get("book")), _e(line.get("flag")),
                              _e(line.get("close")), _e(line.get("stop_name")), _e(line.get("stop"))])

    health_notes = list(notes)
    if prices_note:
        health_notes.append(prices_note)
    if len(captured) < 5:
        health_notes.append(f"{5 - len(captured)} of 5 sessions have no end-of-day record. Those days are shown as "
                            "NOT CAPTURED; nothing is estimated for them.")
    notes_html = "".join(f"<li>{_e(n)}</li>" for n in health_notes) or "<li>No issues recorded.</li>"
    data = json.dumps({"week": monday.isoformat(), "trades": len(trades)})

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Recovery Desk weekly paper report {monday.isoformat()}</title>
<style>
:root{{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e2e1dc;--accent:#2f6fde;--good:#1f8a4c;--bad:#c0392b}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151513;--card:#1f1f1c;--ink:#ecebe6;--muted:#a3a29c;--line:#34332f;--accent:#7aa7ff;--good:#4cc27e;--bad:#ff7b6e}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}}
main{{max-width:1080px;margin:0 auto;padding:24px 16px 48px}}h1{{font-size:24px;margin:0 0 4px}}h2{{font-size:18px;margin:28px 0 10px}}
.muted,.sub{{color:var(--muted)}}.banner{{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--accent);padding:10px 14px;border-radius:8px;margin:16px 0}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:16px 0}}
.kpi{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}}.kv{{font-size:22px;font-weight:650}}.kl{{color:var(--muted);font-size:13px}}
.tabs{{display:flex;gap:6px;flex-wrap:wrap;margin-top:20px}}.tabs button{{border:1px solid var(--line);background:var(--card);color:var(--ink);padding:7px 12px;border-radius:999px;cursor:pointer;font:inherit}}
.tabs button[aria-selected=true]{{background:var(--accent);border-color:var(--accent);color:#fff}}section[hidden]{{display:none}}
.wrap{{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}}table{{border-collapse:collapse;width:100%;font-size:14px}}
th,td{{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}}th{{cursor:pointer;user-select:none;position:sticky;top:0;background:var(--card)}}
th:after{{content:" ↕";color:var(--muted);font-size:11px}}.num{{text-align:right;font-variant-numeric:tabular-nums}}
input[type=search]{{width:100%;max-width:320px;padding:7px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink);font:inherit;margin:8px 0}}
.bad{{color:var(--bad);font-weight:600}}svg{{width:100%;max-height:300px;height:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}}
.axis{{fill:var(--muted);font-size:12px}}.zero{{stroke:var(--line);stroke-width:1}}.line{{fill:none;stroke:var(--accent);stroke-width:2.5}}.dot{{fill:var(--accent)}}
footer{{margin-top:32px;color:var(--muted);font-size:13px}}
</style></head><body><main>
<h1>Recovery Desk — weekly paper report</h1>
<div class="sub">Week of {monday:%a %b %d} – {friday:%a %b %d, %Y} · generated {_e(generated_et)} ET</div>
<div class="banner"><b>Paper only.</b> Orders are OFF; the desk never trades. The ledger below is hypothetical: entries at the next open
after a Paper candidate first appears, exits at the next open after the close falls under its stop, ${notional:,.0f} per entry,
{cost_bps:g} bps per side. Not trade advice.</div>
<div class="kpis">{kpi_html}</div>
<h2>Cumulative paper P/L</h2>{_chart(dates, pnl)}
<div class="tabs" role="tablist">
<button role="tab" aria-selected="true" data-t="days">Daily</button><button role="tab" aria-selected="false" data-t="ledger">Paper ledger</button>
<button role="tab" aria-selected="false" data-t="changes">Status changes</button><button role="tab" aria-selected="false" data-t="stops">Held-name stops</button>
<button role="tab" aria-selected="false" data-t="health">Data health</button></div>
<section id="days"><h2>Each session</h2><div class="wrap">{_table("t-days", ["Day", "Health", "Scanned", "Tier 1", "Cap setups", "Tier 2", "Paper candidates", "Log"], day_rows, "", numeric=(3, 4, 5, 6))}</div></section>
<section id="ledger" hidden><h2>Paper ledger</h2><input type="search" placeholder="Filter rows…" data-f="t-ledger">
<div class="wrap">{_table("t-ledger", ["Ticker", "Book", "Signal", "Entry date", "Entry", "State", "Marked", "Mark", "Return", "P/L"], trade_rows, "No Paper candidate appeared in a captured session this week, so there were no paper entries.", numeric=(4, 7, 8, 9))}</div></section>
<section id="changes" hidden><h2>Status changes</h2><input type="search" placeholder="Filter rows…" data-f="t-changes">
<div class="wrap">{_table("t-changes", ["Time (ET)", "Ticker", "From", "To"], change_rows, "No status changes were recorded this week.")}</div></section>
<section id="stops" hidden><h2>Held-name stop flags</h2><div class="wrap">{_table("t-stops", ["Session", "Ticker", "Book", "Flag", "Close", "Stop", "Level"], flags, "No held name was under its stop or uncomputed in a captured session.", numeric=(4, 6))}</div></section>
<section id="health" hidden><h2>Data health and notes</h2><ul>{notes_html}</ul></section>
<footer>Built from signed desk records only; a missing day is never estimated. Recovery Desk, rulebook v1.6.2.</footer>
</main>
<script id="report-data" type="application/json">{html.escape(data)}</script>
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
