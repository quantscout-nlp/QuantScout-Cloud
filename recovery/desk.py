# -*- coding: utf-8 -*-
"""
One desk cycle:

1. Clock: where are we in the US session (Alpaca calendar: holidays, early closes).
2. Daily qualification: rebuilt only when a new session has completed, from
   completed daily bars, then cached in the store. Today's developing bar is
   never used for qualification.
3. Session overlay: from completed 5-minute bars of *today only*. Anti-Knife is
   recomputed from scratch every cycle, so yesterday's AK PASS can't carry over.
4. Freshness: if the feed goes stale during the session, every new eligibility is
   blocked until it recovers.
5. Diff against the last published statuses, append the changes, publish.

Orders are OFF and there is no code path to turn them on.
"""
import csv
import hashlib
import hmac
import io
import json
import os
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional, Tuple

from recovery import rules
from recovery.indicators import session_vwap
from recovery.providers import ET, DataHub

ORDERS = "OFF"
SETTLE = timedelta(minutes=5)          # after the close, before the official bar counts as completed


# ------------------------------------------------------------------ clock
def market_clock(hub: DataHub, now: datetime) -> dict:
    today = now.date()
    cal = hub.alpaca_calendar(today - timedelta(days=14), today + timedelta(days=1))
    sessions = {}
    if cal:
        for d in cal:
            sessions[d["date"]] = (time.fromisoformat(d["open"]), time.fromisoformat(d["close"]))
        source = "alpaca_calendar"
    else:
        # Weekday fallback: blind to holidays and early closes, and labelled as such.
        for i in range(-14, 2):
            d = today + timedelta(days=i)
            if d.weekday() < 5:
                sessions[d.isoformat()] = (time(9, 30), time(16, 0))
        source = "weekday_fallback"
    t = sessions.get(today.isoformat())
    open_dt = datetime.combine(today, t[0], ET) if t else None
    close_dt = datetime.combine(today, t[1], ET) if t else None
    if not t:
        phase = "closed_day"
    elif now < open_dt:
        phase = "premarket"
    elif now < close_dt:
        phase = "open"
    else:
        phase = "postmarket"
    done = [d for d, (_, c) in sessions.items()
            if d < today.isoformat() or (d == today.isoformat() and now >= datetime.combine(today, c, ET) + SETTLE)]
    return {"now": now, "session_date": today if t else None, "open": open_dt, "close": close_dt,
            "phase": phase, "last_completed": date.fromisoformat(max(done)) if done else None,
            "calendar_source": source}


# ------------------------------------------------------------------ inputs
def load_universe(store, env=os.environ) -> List[str]:
    if env.get("RECOVERY_UNIVERSE"):
        return _dedupe(env["RECOVERY_UNIVERSE"].split(","))
    # universe.csv inside the desk's own Drive folder (uploaded by
    # scripts/google_drive_token.py, so the narrow drive.file scope can read it).
    text = store.read_text("universe.csv")
    if text is None and env.get("RECOVERY_UNIVERSE_PATH") and os.path.exists(env["RECOVERY_UNIVERSE_PATH"]):
        with open(env["RECOVERY_UNIVERSE_PATH"], encoding="utf-8-sig") as f:
            text = f.read()
    if not text:
        return []
    rows = list(csv.DictReader(io.StringIO(text)))
    if rows and "ticker" in rows[0]:
        return _dedupe(r["ticker"] for r in rows)
    return _dedupe(text.replace("\n", ",").split(","))


def _dedupe(xs) -> List[str]:
    seen, out = set(), []
    for x in xs:
        x = (x or "").strip().upper()
        if x and x != "TICKER" and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def rule_config(env=os.environ) -> rules.RuleConfig:
    """RECOVERY_WASH_SALE='STX,WDC,AVGO:2026-10-17' blocks those names through that date."""
    wash = {}
    for part in filter(None, env.get("RECOVERY_WASH_SALE", "").split(";")):
        names, _, until = part.partition(":")
        for n in names.split(","):
            if n.strip():
                wash[n.strip().upper()] = until.strip()
    return rules.RuleConfig(wash_sale=wash)


def _r(x, nd=4):
    return round(x, nd) if isinstance(x, float) else x


def _sig(body: str, key: str) -> str:
    return hmac.new(key.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()


def dump_signed(obj: dict, env=os.environ) -> str:
    """JSON with an HMAC over the body when RECOVERY_SNAPSHOT_KEY is set, so a file
    edited by anything else with access to the Drive folder (another bot, an AI
    agent, a leaked token) is detected instead of trusted."""
    body = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    key = env.get("RECOVERY_SNAPSHOT_KEY", "")
    return json.dumps({"body": body, "sig": _sig(body, key)}) if key else body


def load_signed(text: Optional[str], env=os.environ) -> Tuple[Optional[dict], str]:
    """Returns (object or None, integrity) with integrity VERIFIED / UNSIGNED / TAMPERED."""
    if not text:
        return None, "MISSING"
    try:
        obj = json.loads(text)
    except ValueError:
        return None, "TAMPERED"
    key = env.get("RECOVERY_SNAPSHOT_KEY", "")
    if isinstance(obj, dict) and set(obj) == {"body", "sig"}:
        if key and hmac.compare_digest(obj["sig"], _sig(obj["body"], key)):
            return json.loads(obj["body"]), "VERIFIED"
        return None, "TAMPERED"
    return (None, "TAMPERED") if key else (obj, "UNSIGNED")


def _load_json(store, name, default, env=os.environ, errors: Optional[List[str]] = None):
    obj, integrity = load_signed(store.read_text(name), env)
    if integrity == "TAMPERED" and errors is not None:
        errors.append(f"{name} failed its integrity check and was ignored")
    return obj if obj is not None else default


# ------------------------------------------------------------------ daily qualification
def news_token(item: Optional[dict], searched: bool, since_confirmed: datetime) -> dict:
    if not searched:
        return {"news": "NEWS_NOT_SEARCHED"}
    if not item:
        return {"news": "NEWS_ABSENT"}
    ts = datetime.fromisoformat(item["ts"].replace("Z", "+00:00")).astimezone(ET)
    tok = "NEWS_CONFIRMED" if ts >= since_confirmed else "NEWS_STALE"
    return {"news": tok, "news_asof": ts.isoformat(), "news_note": item["headline"]}


def build_daily(hub: DataHub, universe: List[str], last_completed: date, cfg: rules.RuleConfig,
                now: datetime, env=os.environ) -> dict:
    bars, src = hub.daily_bars(universe, last_completed - timedelta(days=420), last_completed)
    rth, rth_src = hub.rth_volumes(universe, last_completed - timedelta(days=40), last_completed,
                                   env.get("RECOVERY_RVOL_BASIS", "SIP"))
    news = hub.alpaca_news(universe, now - timedelta(days=7))
    # "Fresh" = since the open of the last completed session (covers that session,
    # its after-hours, and today's premarket).
    since = datetime.combine(last_completed, time(9, 30), ET)
    fundamentals = env.get("RECOVERY_FUNDAMENTALS", "1") == "1" and bool(hub.massive_key)
    tickers = {}
    for sym in universe:
        b = [x for x in bars.get(sym, []) if x["date"] <= last_completed.isoformat()]
        if len(b) < 60:
            tickers[sym] = {"status": "NOT_SCANNED", "reason": "insufficient daily bars"}
            continue
        dates = [x["date"] for x in b[-21:]]
        vols = rth.get(sym, {})
        rth_list = [vols[d] for d in dates] if len(dates) == 21 and all(d in vols for d in dates) else None
        daily_label = "WEBULL_DAILY" if src.get(sym) == "webull" else f"{(src.get(sym) or 'other').upper()}_DAILY"
        m = rules.daily_metrics(b, rth_list, cfg, rth_source=rth_src.get(sym, "SIP_RTH"), daily_source=daily_label)
        c = rules.classify_daily(m, cfg)
        row = {**{k: _r(v) for k, v in m.items() if k not in ("hist", "rsi_last5")},
               "hist": [_r(h) for h in m["hist"]], "rsi_last5": [_r(r, 2) for r in m["rsi_last5"]],
               **{k: _r(v) for k, v in c.items()}, "daily_source": src.get(sym)}
        row.update(news_token((news or {}).get(sym), news is not None, since))
        if fundamentals and c["status"] in ("TIER1", "CAP_SETUP", "TIER2"):
            row.update(hub.massive_eps_ttm(sym) or {})
        tickers[sym] = row
    counts: Dict[str, int] = {}
    for s in src.values():
        counts[s] = counts.get(s, 0) + 1
    return {"as_of_session": last_completed.isoformat(), "built_et": now.isoformat(),
            "universe_hash": _uhash(universe), "daily_sources": counts, "tickers": tickers}


def _uhash(universe: List[str]) -> str:
    return hashlib.sha1(",".join(sorted(universe)).encode()).hexdigest()[:12]


# ------------------------------------------------------------------ session overlay
def completed_today(rows: List[dict], session_date: date, now: datetime, minutes: int = 5) -> List[dict]:
    out = []
    for b in rows:
        t = datetime.fromisoformat(b["t"])
        if t.date() == session_date and t + timedelta(minutes=minutes) <= now:
            out.append(b)
    return out


def is_rth(b: dict, open_dt: datetime, close_dt: datetime) -> bool:
    t = datetime.fromisoformat(b["t"])
    return open_dt <= t < close_dt


def overlay(daily: dict, clock: dict, intraday: Dict[str, List[dict]], cfg: rules.RuleConfig,
            feed_stale: bool) -> List[dict]:
    phase, now = clock["phase"], clock["now"]
    in_session_day = phase in ("premarket", "open")
    out = []
    for sym, d in daily["tickers"].items():
        row = {"ticker": sym, **d, "ak_d": "NOT_SCORED", "ak_s": "NOT_SCORED", "ak_combined": "NOT_SCORED",
               "vwap": None, "live_last": None, "today_low": None, "low_held_bars": None, "live_flags": [],
               "paper_candidate": False, "blocked_by": []}
        base = d.get("status")
        if base == "NOT_SCANNED":
            out.append(row)
            continue
        bars = completed_today(intraday.get(sym, []), clock["session_date"], now) if in_session_day else []
        rth = [b for b in bars if is_rth(b, clock["open"], clock["close"])] if bars else []
        if bars:
            row["today_low"] = min(b["l"] for b in bars)           # premarket + RTH so far
            row["live_last"] = bars[-1]["c"]
            if row["stop"] is not None and row["live_last"] < row["stop"]:
                row["live_flags"].append(f"LIVE below {row['stop_name']} (intraday, not official)")
        status = base
        if base == "CAP_SETUP":
            if in_session_day:
                row["ak_d"] = rules.ak_d(row["today_low"], d.get("ref_low"), d.get("prior_low"))
                s = rules.ak_s(rth, phase == "open", cfg)
                if phase == "open" and feed_stale:
                    s = {**s, "ak_s": "NO_5MIN_FEED"}
                row.update({"ak_s": s["ak_s"], "vwap": _r(s["vwap"]), "low_held_bars": s["low_held_bars"]})
                row["ak_combined"] = rules.ak_combined(row["ak_d"], row["ak_s"])
                status = {"PASS": "CAP_PAPER", "FAIL": "CAP_AK_BLOCK"}.get(row["ak_combined"], "CAP_WAIT_AK")
            else:
                status = "CAP_SETUP"                                 # next session; AK not scored yet
        elif rth:
            row["vwap"] = _r(session_vwap(rth))
        row["status"] = status

        if status in ("TIER1", "CAP_PAPER"):
            if phase == "closed_day" or now.weekday() >= 5:
                row["blocked_by"].append("no new Paper on non-session prints")
            if feed_stale:
                row["blocked_by"].append("feed stale")
            if rules.wash_sale_blocked(sym, now.date(), cfg):
                row["blocked_by"].append("wash-sale list")
            row["paper_candidate"] = not row["blocked_by"]
        tier_text = {"TIER1": "TIER 1 Paper candidate", "CAP_PAPER": "CAP Paper candidate (AK PASS)",
                     "TIER2": "TIER 2"}.get(status, "WATCH")
        m_view = {**d, "hist": d.get("hist", [])}
        row["card"] = rules.card_lines(0, sym, m_view, d, tier_text)
        out.append(row)
    order = {"TIER1": 0, "CAP_PAPER": 1, "CAP_WAIT_AK": 2, "CAP_SETUP": 3, "TIER2": 4,
             "CAP_AK_BLOCK": 5, "WATCH_HOLD": 6, "NOT_SCANNED": 7}
    out.sort(key=lambda r: (order.get(r["status"], 9), -(r.get("battery") or -9), r["ticker"]))
    for i, r in enumerate(out, 1):
        if r.get("card"):
            r["card"][0] = f"{i}." + r["card"][0].split(".", 1)[1]
    return out


# ------------------------------------------------------------------ cycle
def _worker_is_fresh(state: dict, now: datetime, env) -> bool:
    """The free GitHub schedule is the failover: it stands down while the always-on
    worker has published within RECOVERY_FAILOVER_AFTER_S (default 10 min)."""
    if env.get("RECOVERY_ROLE") != "failover" or env.get("RECOVERY_FORCE") == "1":
        return False
    last = state.get("last_success_et")
    if state.get("writer") != "worker" or not last:
        return False
    age = now - datetime.fromisoformat(last)
    return age < timedelta(seconds=int(env.get("RECOVERY_FAILOVER_AFTER_S", "600")))


def _feed_label(sources: Dict[str, str], hub) -> str:
    names = {"alpaca_sip": "SIP", "alpaca_iex": "IEX", "webull": "WEBULL"}
    used = sorted({names.get(v, v.upper()) for v in sources.values()})
    if used:
        return "+".join(used)
    if getattr(hub, "has_alpaca", True):
        return hub.feed.upper()
    return "WEBULL" if getattr(hub, "has_webull", False) else "NONE"


def run_cycle(store, hub: DataHub, now: Optional[datetime] = None, env=os.environ) -> Optional[dict]:
    """One cycle. Returns the published snapshot, or None when a failover run stands
    down because the worker is healthy."""
    now = (now or datetime.now(ET)).astimezone(ET)
    errors: List[str] = []
    state = _load_json(store, "state.json", {}, env, errors)
    if _worker_is_fresh(state, now, env):
        return None
    role = env.get("RECOVERY_ROLE", "primary")
    cfg = rule_config(env)
    clock = market_clock(hub, now)
    universe = load_universe(store, env)
    if not universe:
        errors.append("universe is empty: set RECOVERY_UNIVERSE or upload universe.csv to the desk folder")

    daily = _load_json(store, "daily_cache.json", {}, env, errors)
    lc = clock["last_completed"]
    if universe and lc and (daily.get("as_of_session") != lc.isoformat()
                            or daily.get("universe_hash") != _uhash(universe)):
        daily = build_daily(hub, universe, lc, cfg, now, env)
        store.write_text("daily_cache.json", dump_signed(daily, env))
    daily.setdefault("tickers", {})
    for sym in universe:
        daily["tickers"].setdefault(sym, {"status": "NOT_SCANNED", "reason": "not in daily cache"})

    intraday: Dict[str, List[dict]] = {}
    sources: Dict[str, str] = {}
    last_bar_end = None
    feed_stale = False
    max_age = timedelta(seconds=int(env.get("RECOVERY_MAX_BAR_AGE_S", "600")))
    if clock["phase"] in ("premarket", "open") and universe:
        start = datetime.combine(now.date(), time(4, 0), ET)
        intraday = hub.intraday_bars(universe, start, now)
        sources = dict(getattr(hub, "intraday_sources", {}))
        ends = [datetime.fromisoformat(b["t"]) + timedelta(minutes=5)
                for rows in intraday.values() for b in rows if datetime.fromisoformat(b["t"]) + timedelta(minutes=5) <= now]
        last_bar_end = max(ends) if ends else None
        if clock["phase"] == "open" and now - clock["open"] > timedelta(minutes=10):
            feed_stale = last_bar_end is None or now - last_bar_end > max_age

    rows = overlay(daily, clock, intraday, cfg, feed_stale)
    for r in rows:
        r["intraday_source"] = sources.get(r["ticker"])

    # ---- diff vs last publish
    prev = state.get("statuses", {})
    changes = []
    for r in rows:
        key = f"{r['status']}{'*' if r['paper_candidate'] else ''}"
        if prev.get(r["ticker"]) not in (None, key):
            reason = r.get("fail_gate") or r.get("rule1_token") or ""
            if r["status"].startswith("CAP"):
                reason = f"AK-D {r['ak_d']} / AK-S {r['ak_s']}"
            if r["blocked_by"]:
                reason += f" blocked: {', '.join(r['blocked_by'])}"
            changes.append({"ts": now.isoformat(), "ticker": r["ticker"], "from": prev[r["ticker"]],
                            "to": key, "reason": reason.strip(), "writer": role})
    log_day = (clock["session_date"] or now.date()).isoformat()
    log_name = f"changes_{log_day}.jsonl"
    if changes:
        existing = store.read_text(log_name) or ""
        store.write_text(log_name, existing + "".join(dump_signed(c, env) + "\n" for c in changes),
                         "application/x-ndjson")
    todays = []
    for line in (store.read_text(log_name) or "").splitlines():
        obj, integrity = load_signed(line, env)
        if obj is None and line.strip():
            errors.append(f"{log_name}: a line failed its integrity check and was dropped")
        elif obj is not None:
            todays.append(obj)

    scanned = sum(1 for r in rows if r["status"] != "NOT_SCANNED")
    errors += hub.stats["errors"][:10]
    if not universe or scanned == 0:
        health = "NO_DATA"
    elif feed_stale:
        health = "STALE"
    elif (errors or clock["calendar_source"] != "alpaca_calendar" or scanned < len(universe)
          or "alpaca_iex" in sources.values()):
        health = "DEGRADED"
    else:
        health = "OK"
    counts: Dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    src_counts: Dict[str, int] = {}
    for v in sources.values():
        src_counts[v] = src_counts.get(v, 0) + 1
    snap = {
        "schema": 2, "generated_et": now.isoformat(), "phase": clock["phase"],
        "session_date": clock["session_date"].isoformat() if clock["session_date"] else None,
        "qualified_from_session": daily.get("as_of_session"),
        "orders": ORDERS,
        "health": {
            "status": health, "feed": _feed_label(sources, hub), "feed_note": hub.feed_note,
            "intraday_sources": src_counts, "writer": role,
            "integrity": "SIGNED" if env.get("RECOVERY_SNAPSHOT_KEY") else "UNSIGNED",
            "last_bar_end_et": last_bar_end.isoformat() if last_bar_end else None,
            "data_delay_s": int((now - last_bar_end).total_seconds()) if last_bar_end else None,
            "calendar": clock["calendar_source"], "provider_calls": hub.stats["calls"],
            "rate_limited": hub.stats["rate_limited"], "daily_sources": daily.get("daily_sources", {}),
            "rvol_basis": (env.get("RECOVERY_RVOL_BASIS") or "SIP").upper(),
            "scanned": f"{scanned} of {len(universe)} scanned", "errors": errors[:10],
            "daily_built_et": daily.get("built_et"),
        },
        "counts": counts, "rows": rows, "changes_today": todays[-100:],
    }
    store.write_text("latest.json", dump_signed(snap, env))
    store.write_text("latest.csv", to_csv(rows), "text/csv")
    if clock["phase"] == "postmarket" and state.get("saved_session") != log_day:
        store.write_text(f"session_{log_day}.json", dump_signed(snap, env))   # end-of-day record
        state["saved_session"] = log_day
    state.update({"statuses": {r["ticker"]: f"{r['status']}{'*' if r['paper_candidate'] else ''}" for r in rows},
                  "last_success_et": now.isoformat(), "session_date": snap["session_date"], "writer": role})
    store.write_text("state.json", dump_signed(state, env))
    return snap


CSV_COLS = ["ticker", "status", "paper_candidate", "blocked_by", "book", "fail_gate", "rule1_token", "dd_pct",
            "dd_band", "close", "chg_pct", "asof", "sma9", "sma20", "sma50", "sma200", "stack", "macd", "battery",
            "rsi0", "rsi1", "rvol_label", "vol_source", "graduate_rule2", "exit_flags", "stop_name", "stop",
            "ak_d", "ak_s", "ak_combined", "vwap", "live_last", "today_low", "ref_low", "prior_low",
            "low_held_bars", "live_flags", "news", "news_note", "eps_ttm", "daily_source", "intraday_source"]


def to_csv(rows: List[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLS, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: ("|".join(map(str, r[k])) if isinstance(r.get(k), list) else r.get(k)) for k in CSV_COLS})
    return buf.getvalue()
