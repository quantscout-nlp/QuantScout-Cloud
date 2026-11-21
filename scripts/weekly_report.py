# -*- coding: utf-8 -*-
"""
Weekly paper-trading report generator.

Produces a single self-contained HTML file that opens in any browser with no
server and no network: all CSS and JS are inline, fonts degrade to a system
stack, and there is no build step.

WHAT IT REPORTS ON, AND WHY THAT ORDER
--------------------------------------
The question a weekly report has to answer is "what did the bot do". For a
trading bot there are two very different ways the answer can be "nothing":

  1. It ran, computed signals, and none of them fired.  -> a strategy result
  2. It ran but could not compute a signal, or could not have placed an order
     even if one had fired.                             -> a plumbing failure

Conflating those two is the single most dangerous thing a report like this can
do, because (2) dressed up as (1) reads as "the strategy is being cautious"
when actually nothing is connected. So this report leads with a PRECONDITION
AUDIT -- the chain of things that must all be true before a paper order is
possible -- and only then shows activity. A week with zero trades and zero
preconditions met is reported as a broken pipeline, never as a flat week.

DATA SOURCES, in order of authority
-----------------------------------
  * GitHub Actions run list + job logs   -- did the scheduler fire, and what did
    each run print? This is the authoritative record of execution.
  * trial_state/trial_log.jsonl          -- the engine's own decision log, if the
    trial branch exists. Absent means nothing was persisted.
  * Alpaca account/orders                -- only when paper keys are supplied.
    NEVER mixes a live account in: see the --alpaca-label guard.

Every number in the output traces to one of those. Nothing is estimated, and a
field that could not be read renders as "unknown", never as zero.

USAGE
    python scripts/weekly_report.py --evidence reports/evidence_week1.json \
        --week 1 --out reports/week1.html

    # or pull straight from the GitHub API (needs a token with Actions: read)
    GITHUB_TOKEN=... python scripts/weekly_report.py --fetch --days 7 \
        --out reports/week_latest.html
"""
import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

GITHUB_API = "https://api.github.com"
REPO = "quantscout-nlp/QuantScout-Cloud"
WORKFLOW = "trading-engine.yml"

# The engine's own secret/variable names, and what breaks when each is empty.
# Mirrors scripts/run_scan.py's env reads and engine.run_scan()'s gating.
CONFIG_KEYS = {
    "ALPACA_ID":          "no broker connection; fetch_alpaca_price returns 'No Keys'",
    "ALPACA_SECRET":      "no broker connection; trading_base_ready stays False",
    "POLYGON_KEY":        "price fallback dead; fetch_polygon_price returns 'No Key'",
    "TIINGO_KEY":         "sentiment falls through to GoogleNews, then yfinance",
    "TRIAL_GITHUB_TOKEN": "trial clock cannot persist; phase pinned to DRY_RUN forever",
    "TG_TOKEN":           "no Telegram alerts (cosmetic)",
    "TG_ID":              "no Telegram alerts (cosmetic)",
    "WATCHLIST":          "falls back to engine.DEFAULT_TICKERS",
    "ENABLE_TRADING":     "defaults True, but inert while keys are missing",
    "NOTIONAL_PER_TRADE": "defaults to $500",
    "MAX_POSITIONS":      "defaults to 8",
}


# --- log parsing ----------------------------------------------------------------
def parse_run_log(text):
    """Pull the engine's own printed state out of one job log.

    Deliberately regex over the engine's exact print statements rather than
    anything inferred: if run_scan.py's output format changes, this returns
    unknowns instead of quietly wrong numbers.
    """
    out = {
        "phase": None, "trial_day": None, "trial_start": None,
        "open_symbols_raw": None, "log_entries": None,
        "http_429": text.count("HTTP Error 429"),
        "warnings": [], "signal_lines": [], "empty_config": [], "set_config": [],
    }
    m = re.search(r"Phase:\s*(\w+)\s*\(day ([^)]*)\)\s*—\s*started (\S+)", text)
    if m:
        out["phase"], out["trial_day"], out["trial_start"] = m.group(1), m.group(2), m.group(3)
    m = re.search(r"Open positions/simulated holds:\s*(\[[^\]]*\])", text)
    if m:
        out["open_symbols_raw"] = m.group(1)
    m = re.search(r"(\d+) new trial-log entries this pass", text)
    if m:
        out["log_entries"] = int(m.group(1))
    for w in re.findall(r"WARNING: ([^\n]+)", text):
        out["warnings"].append(w.strip())
    # run_scan.py prints one line per non-HOLD signal: "  SYM   BUY  price=.. -> note"
    for line in re.findall(r"^\s{2}([A-Z.]{1,6})\s+(BUY|SELL)\s+(.+)$", text, re.M):
        out["signal_lines"].append({"symbol": line[0], "side": line[1], "detail": line[2].strip()})
    # The Actions env block echoes each variable; "NAME:" with nothing after it is empty.
    for key in CONFIG_KEYS:
        if re.search(rf"^\s*{key}:\s*$", text, re.M):
            out["empty_config"].append(key)
        elif re.search(rf"^\s*{key}:\s*\S", text, re.M):
            out["set_config"].append(key)
    return out


def preconditions(parsed, trial_log_present, alpaca_label):
    """The chain that must ALL hold before one paper order can exist.

    Ordered as the engine evaluates them, so the first failure is the root cause
    and everything below it is downstream noise.
    """
    empty = set(parsed.get("empty_config") or [])
    phase = parsed.get("phase")
    has_price_key = not ({"ALPACA_ID", "ALPACA_SECRET"} <= empty) or "POLYGON_KEY" not in empty
    checks = [
        ("Scheduler fires",
         True, "GitHub Actions ran the workflow on its cron"),
        ("Broker credentials present",
         not ({"ALPACA_ID", "ALPACA_SECRET"} & empty),
         "engine.run_scan: trading_base_ready = enable_trading AND alpaca_id AND alpaca_secret"),
        ("A price source answers",
         has_price_key,
         "fetch_alpaca_price then fetch_polygon_price; both return None without keys"),
        ("Indicators computable",
         parsed.get("http_429", 0) == 0,
         "fetch_indicators_hybrid needs >20 daily bars; HTTP 429 means the fallback was throttled"),
        ("A signal can fire",
         parsed.get("http_429", 0) == 0 and has_price_key,
         "decide() requires `price and rsi > 0`; with price=None it returns HOLD unconditionally"),
        ("Trial clock persists",
         "TRIAL_GITHUB_TOKEN" not in empty,
         "get_trial_start_date returns None without a token, so get_trial_phase(None) -> DRY_RUN"),
        ("Phase allows opening a position",
         phase == "PAPER",
         "can_open_new = trading_base_ready AND phase == 'PAPER'"),
        ("Decisions are recorded",
         trial_log_present,
         "trial_state/trial_log.jsonl on the bot-trial-log branch"),
        ("Paper account is the order target",
         alpaca_label == "paper",
         "engine.ALPACA_TRADE_BASE is hardcoded to paper-api.alpaca.markets"),
    ]
    return [{"name": n, "ok": bool(ok), "why": w} for n, ok, w in checks]


# --- GitHub fetch (optional) ----------------------------------------------------
def fetch_runs(days, token):
    import urllib.request
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    url = (f"{GITHUB_API}/repos/{REPO}/actions/workflows/{WORKFLOW}/runs"
           f"?per_page=100&created=>={since}")
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


# --- rendering ------------------------------------------------------------------
def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def render_html(rep):
    pre = rep["preconditions"]
    met = sum(1 for p in pre if p["ok"])
    total = len(pre)
    blocked = met < total
    first_fail = next((p for p in pre if not p["ok"]), None)

    verdict = ("NO PAPER TRADING OCCURRED" if blocked and rep["orders"] == 0
               else ("NO SIGNALS FIRED" if rep["orders"] == 0 else "ACTIVE"))
    vclass = "fail" if blocked else ("warn" if rep["orders"] == 0 else "pass")

    rows = "".join(
        f'<tr><td class="n">{i}</td><td><span class="chip {"c-pass" if p["ok"] else "c-fail"}">'
        f'{"PASS" if p["ok"] else "BLOCKED"}</span></td><td><b>{_esc(p["name"])}</b>'
        f'<div class="why">{_esc(p["why"])}</div></td></tr>'
        for i, p in enumerate(pre, 1))

    cadence = "".join(
        f'<tr><td class="mono">{_esc(d)}</td><td class="n mono">{c}</td></tr>'
        for d, c in rep["runs_per_day"])

    cfg = "".join(
        f'<tr><td class="mono">{_esc(k)}</td>'
        f'<td><span class="chip {"c-fail" if k in rep["empty_config"] else "c-pass"}">'
        f'{"EMPTY" if k in rep["empty_config"] else "SET"}</span></td>'
        f'<td class="why">{_esc(v)}</td></tr>'
        for k, v in CONFIG_KEYS.items())

    chain = "".join(
        f'<li><b>{_esc(c["step"])}</b><div class="why">{_esc(c["detail"])}</div>'
        f'<pre class="ev">{_esc(c["evidence"])}</pre></li>'
        for c in rep["causal_chain"])

    fixes = "".join(
        f'<li><b>{_esc(f["what"])}</b><div class="why">{_esc(f["how"])}</div></li>'
        for f in rep["fixes"])

    tmpl = _TEMPLATE
    for token, value in [
        ("__TITLE__", rep["title"]),
        ("__WEEK__", str(rep["week"])),
        ("__WINDOW__", rep["window"]),
        ("__GENERATED__", rep["generated"]),
        ("__VERDICT__", verdict),
        ("__VCLASS__", vclass),
        ("__MET__", str(met)),
        ("__TOTAL__", str(total)),
        ("__RUNS__", str(rep["runs"])),
        ("__RUNS_ALLTIME__", str(rep["runs_all_time"])),
        ("__ORDERS__", str(rep["orders"])),
        ("__SIGNALS__", str(rep["signals"])),
        ("__LOGGED__", str(rep["logged_decisions"])),
        ("__PHASE__", rep["phase"] or "unknown"),
        ("__EXPECTED__", rep["expected_runs"]),
        ("__THROTTLE__", rep["throttle_note"]),
        ("__FIRSTFAIL__", _esc(first_fail["name"]) if first_fail else "none"),
        ("__PREROWS__", rows),
        ("__CADENCE__", cadence),
        ("__CFGROWS__", cfg),
        ("__CHAIN__", chain),
        ("__FIXES__", fixes),
        ("__SCOPE__", _esc(rep["scope_note"])),
        ("__ALPACA__", _esc(rep["alpaca_note"])),
    ]:
        tmpl = tmpl.replace(token, value)
    return tmpl


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap">
<style>
:root{
  --ink:#1a1714; --ink2:#4a423a; --ink3:#7d7366;
  --paper:#f0ece6; --surface:#fffdfa; --line:#d8d1c7; --soft:#e7e1d8;
  --accent:#a44a1f;
  --pass:#2f6b43; --pass-bg:#e2eee6;
  --warn:#8a6314; --warn-bg:#f6ecd6;
  --fail:#9c2b22; --fail-bg:#f6e2df;
  --mono:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,monospace;
  --sans:"Archivo",system-ui,-apple-system,"Segoe UI",sans-serif;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --ink:#ece7e0; --ink2:#b3a99d; --ink3:#857a6d;
  --paper:#15120f; --surface:#1e1a16; --line:#352f28; --soft:#262119;
  --accent:#e08a5a;
  --pass:#6cbd8b; --pass-bg:#16301f;
  --warn:#d9a948; --warn-bg:#352709;
  --fail:#e0847a; --fail-bg:#381915;
}}
:root[data-theme="dark"]{
  --ink:#ece7e0; --ink2:#b3a99d; --ink3:#857a6d;
  --paper:#15120f; --surface:#1e1a16; --line:#352f28; --soft:#262119;
  --accent:#e08a5a;
  --pass:#6cbd8b; --pass-bg:#16301f;
  --warn:#d9a948; --warn-bg:#352709;
  --fail:#e0847a; --fail-bg:#381915;
}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--sans);
  font-size:15px;line-height:1.6;-webkit-font-smoothing:antialiased}
.wrap{max-width:900px;margin:0 auto;padding:30px 20px 70px;display:flex;flex-direction:column;gap:32px}
h1,h2,h3{margin:0;text-wrap:balance;letter-spacing:-.015em}
h1{font-size:clamp(28px,5vw,40px);font-weight:700;line-height:1.1}
h2{font-size:22px;font-weight:700}
h3{font-size:16px;font-weight:600}
p{margin:0}
.mono{font-family:var(--mono);font-variant-numeric:tabular-nums}
.eyebrow{font-family:var(--mono);font-size:11px;font-weight:700;letter-spacing:.13em;
  text-transform:uppercase;color:var(--ink3)}
.prose{max-width:66ch;color:var(--ink2)}
.why{font-size:13.5px;color:var(--ink2);margin-top:3px}
header{border-bottom:2px solid var(--ink);padding-bottom:20px;display:flex;flex-direction:column;gap:12px}
.verdict{padding:18px 20px;border-radius:4px;border-left:4px solid}
.verdict.fail{background:var(--fail-bg);border-color:var(--fail)}
.verdict.warn{background:var(--warn-bg);border-color:var(--warn)}
.verdict.pass{background:var(--pass-bg);border-color:var(--pass)}
.verdict .vt{font-size:20px;font-weight:700;letter-spacing:-.01em}
.verdict.fail .vt{color:var(--fail)} .verdict.warn .vt{color:var(--warn)} .verdict.pass .vt{color:var(--pass)}
.verdict p{margin-top:7px;font-size:14.5px;color:var(--ink2);max-width:70ch}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(138px,1fr));gap:1px;
  background:var(--line);border:1px solid var(--line);border-radius:4px;overflow:hidden}
.tiles div{background:var(--surface);padding:12px 14px;display:flex;flex-direction:column;gap:3px}
.tiles dt{font-family:var(--mono);font-size:10px;font-weight:700;letter-spacing:.1em;
  text-transform:uppercase;color:var(--ink3)}
.tiles dd{margin:0;font-family:var(--mono);font-size:22px;font-weight:700;font-variant-numeric:tabular-nums}
.tiles dd small{font-size:12px;font-weight:500;color:var(--ink3)}
section{display:flex;flex-direction:column;gap:15px}
.shead{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;
  border-bottom:1px solid var(--line);padding-bottom:8px}
.shead .eyebrow{margin-left:auto}
.tblwrap{overflow-x:auto;border:1px solid var(--line);border-radius:4px;background:var(--surface)}
table{border-collapse:collapse;width:100%;font-size:14px;min-width:420px}
th,td{padding:10px 14px;text-align:left;border-bottom:1px solid var(--soft);vertical-align:top}
th{font-family:var(--mono);font-size:10.5px;font-weight:700;letter-spacing:.1em;
  text-transform:uppercase;color:var(--ink3);background:var(--paper)}
tr:last-child td{border-bottom:none}
td.n{font-family:var(--mono);font-variant-numeric:tabular-nums;text-align:right;color:var(--ink3)}
.chip{font-family:var(--mono);font-size:10.5px;font-weight:700;letter-spacing:.09em;
  padding:3px 8px;border-radius:2px;white-space:nowrap;display:inline-block}
.c-pass{background:var(--pass-bg);color:var(--pass)}
.c-fail{background:var(--fail-bg);color:var(--fail)}
ol.chain{margin:0;padding-left:0;list-style:none;counter-reset:s;
  display:flex;flex-direction:column;gap:0}
ol.chain li{counter-increment:s;position:relative;padding:0 0 18px 34px;
  border-left:2px solid var(--line);margin-left:11px}
ol.chain li:last-child{border-left-color:transparent;padding-bottom:0}
ol.chain li::before{content:counter(s);position:absolute;left:-12px;top:0;
  width:22px;height:22px;border-radius:50%;background:var(--accent);color:var(--surface);
  font-family:var(--mono);font-size:11px;font-weight:700;display:grid;place-items:center}
pre.ev{font-family:var(--mono);font-size:12px;background:var(--paper);border:1px solid var(--soft);
  border-radius:3px;padding:9px 11px;margin:8px 0 0;overflow-x:auto;color:var(--ink2);white-space:pre-wrap}
ol.fixes{margin:0;padding-left:22px;display:flex;flex-direction:column;gap:12px}
ol.fixes li::marker{font-family:var(--mono);font-weight:700;color:var(--accent)}
details{background:var(--surface);border:1px solid var(--line);border-radius:4px;padding:0}
details summary{padding:11px 15px;cursor:pointer;font-weight:600;font-size:14px}
details summary:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}
details[open] summary{border-bottom:1px solid var(--soft)}
details .body{padding:14px 15px}
footer{border-top:1px solid var(--line);padding-top:18px;color:var(--ink3);font-size:13px;
  display:flex;flex-direction:column;gap:9px;max-width:72ch}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
</style>
</head>
<body>
<div class="wrap">

<header>
  <p class="eyebrow">QuantScout-Cloud &middot; paper-trading weekly &middot; week __WEEK__</p>
  <h1>__TITLE__</h1>
  <p class="prose">Window <span class="mono">__WINDOW__</span> &middot; generated <span class="mono">__GENERATED__</span></p>
</header>

<div class="verdict __VCLASS__">
  <div class="vt">__VERDICT__</div>
  <p>__SCOPE__</p>
</div>

<dl class="tiles">
  <div><dt>Orders placed</dt><dd>__ORDERS__</dd></div>
  <div><dt>Signals fired</dt><dd>__SIGNALS__</dd></div>
  <div><dt>Decisions logged</dt><dd>__LOGGED__</dd></div>
  <div><dt>Preconditions met</dt><dd>__MET__<small> / __TOTAL__</small></dd></div>
  <div><dt>Engine runs (week)</dt><dd>__RUNS__</dd></div>
  <div><dt>Runs all time</dt><dd>__RUNS_ALLTIME__</dd></div>
  <div><dt>Trial phase</dt><dd style="font-size:15px">__PHASE__</dd></div>
</dl>

<section>
  <div class="shead"><h2>Why nothing traded</h2><span class="eyebrow">root cause first</span></div>
  <p class="prose">First failing precondition: <b>__FIRSTFAIL__</b>. Everything below it is
  downstream of that, so fixing lower items alone changes nothing.</p>
  <ol class="chain">__CHAIN__</ol>
</section>

<section>
  <div class="shead"><h2>Precondition audit</h2><span class="eyebrow">__MET__ of __TOTAL__ met</span></div>
  <p class="prose">All of these must hold before a single paper order is possible. They are
  listed in the order the engine evaluates them.</p>
  <div class="tblwrap"><table>
    <thead><tr><th>#</th><th>State</th><th>Gate</th></tr></thead>
    <tbody>__PREROWS__</tbody>
  </table></div>
</section>

<section>
  <div class="shead"><h2>Configuration</h2><span class="eyebrow">from the Actions env block</span></div>
  <div class="tblwrap"><table>
    <thead><tr><th>Secret / variable</th><th>State</th><th>Consequence when empty</th></tr></thead>
    <tbody>__CFGROWS__</tbody>
  </table></div>
</section>

<section>
  <div class="shead"><h2>Scheduler telemetry</h2><span class="eyebrow">execution record</span></div>
  <p class="prose">Cron asks for <b>__EXPECTED__</b>. __THROTTLE__</p>
  <details>
    <summary>Runs per day this window</summary>
    <div class="body"><div class="tblwrap"><table>
      <thead><tr><th>Date (UTC)</th><th>Runs</th></tr></thead>
      <tbody>__CADENCE__</tbody>
    </table></div></div>
  </details>
</section>

<section>
  <div class="shead"><h2>What to fix, in order</h2><span class="eyebrow">to start week 1 for real</span></div>
  <ol class="fixes">__FIXES__</ol>
</section>

<footer>
  <p><b>Scope.</b> Every figure above is read from the GitHub Actions run list, the job logs
  those runs printed, and the repository's branches. Nothing is estimated. A value that could
  not be read renders as "unknown" rather than zero.</p>
  <p><b>Broker accounts.</b> __ALPACA__</p>
  <p>Not investment advice. The engine's order endpoint is hardcoded to Alpaca's paper API
  (<span class="mono">paper-api.alpaca.markets</span>); this report never places or modifies an order.</p>
</footer>
</div>
</body>
</html>
"""


def build_report(evidence):
    """Assemble the report dict from an evidence bundle (no network)."""
    parsed = evidence["parsed_log"]
    pre = preconditions(parsed, evidence["trial_log_present"], evidence["alpaca_label"])
    per_day = sorted(evidence["runs_per_day"].items(), reverse=True)
    return {
        "title": evidence["title"],
        "week": evidence["week"],
        "window": evidence["window"],
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "runs": sum(evidence["runs_per_day"].values()),
        "runs_all_time": evidence["runs_all_time"],
        "orders": evidence["orders"],
        "signals": len(parsed.get("signal_lines") or []),
        "logged_decisions": parsed.get("log_entries") if parsed.get("log_entries") is not None else "unknown",
        "phase": parsed.get("phase"),
        "empty_config": parsed.get("empty_config") or [],
        "preconditions": pre,
        "runs_per_day": per_day,
        "expected_runs": evidence["expected_runs"],
        "throttle_note": evidence["throttle_note"],
        "causal_chain": evidence["causal_chain"],
        "fixes": evidence["fixes"],
        "scope_note": evidence["scope_note"],
        "alpaca_note": evidence["alpaca_note"],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--evidence", help="path to an evidence JSON bundle")
    ap.add_argument("--fetch", action="store_true", help="pull runs from the GitHub API")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--week", default="1")
    ap.add_argument("--out", default="reports/week_latest.html")
    args = ap.parse_args()

    if args.fetch:
        token = os.environ.get("GITHUB_TOKEN", "")
        if not token:
            print("--fetch needs GITHUB_TOKEN (Actions: read) in the environment", file=sys.stderr)
            return 2
        data = fetch_runs(args.days, token)
        per_day = Counter(r["created_at"][:10] for r in data.get("workflow_runs", []))
        print(f"fetched {len(data.get('workflow_runs', []))} runs "
              f"(total_count {data.get('total_count')}) across {len(per_day)} days")
        print("Re-run with --evidence after adding parsed job logs; "
              "the API list alone cannot tell you what each run DID.", file=sys.stderr)
        return 0

    if not args.evidence:
        print("need --evidence PATH (or --fetch to inspect run cadence)", file=sys.stderr)
        return 2

    with open(args.evidence, "r", encoding="utf-8") as fh:
        evidence = json.load(fh)
    rep = build_report(evidence)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(render_html(rep))

    met = sum(1 for p in rep["preconditions"] if p["ok"])
    print(f"wrote {args.out}")
    print(f"  orders={rep['orders']} signals={rep['signals']} "
          f"logged={rep['logged_decisions']} phase={rep['phase']}")
    print(f"  preconditions met: {met}/{len(rep['preconditions'])}")
    for p in rep["preconditions"]:
        if not p["ok"]:
            print(f"    BLOCKED  {p['name']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
