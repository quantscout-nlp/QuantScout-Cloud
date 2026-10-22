# -*- coding: utf-8 -*-
"""
The locked rule text, as code. Source: Drive `SKILL.md` "capitulation-momentum-rules"
v1.6.2 (locked 2026-10-22 15:05 ET; v1.6.2 changes only the RVOL20 source: SIP RTH
first, Webull RTH fallback, one source per ratio). Thresholds live in RuleConfig so a future
lock changes one place. Where the rule text leaves a gap, the choice made here is
marked INTERPRETATION so it can be reviewed.

Everything here is a pure function of completed bars. Nothing here fetches data,
and nothing here can trade.
"""
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Sequence

from recovery import indicators as ind


@dataclass(frozen=True)
class RuleConfig:
    dd_window: int = 90                 # high_peak_90td, same basis as the desktop engine
    dd_min: float = 5.0                 # Rule 1: exclude under 5 percent ...
    dd_shallow_max: float = 15.0        # ... 5-15 shallow quality ...
    dd_max: float = 60.0                # ... 15-60 classic cap, exclude over 60
    rsi_low: float = 40.0
    rsi_high: float = 65.0
    rsi_watch_low: float = 30.0
    rsi_late_high: float = 70.0
    rvol_floor: float = 0.80            # exactly 0.80 passes
    rvol_border_low: float = 0.72
    rvol_border_high: float = 0.88
    ak_s_hold_bars: int = 3
    wash_sale: Dict[str, str] = field(default_factory=dict)   # ticker -> last blocked date (ISO)


# ---------------------------------------------------------------- daily metrics
def daily_metrics(bars: Sequence[dict], rth_volumes: Optional[Sequence[float]], cfg: RuleConfig,
                  rth_source: str = "SIP_RTH", daily_source: str = "SIP_DAILY") -> dict:
    """bars: completed daily sessions, oldest first, keys date/o/h/l/c/v.
    rth_volumes: regular-hours volume of the same trailing sessions (oldest first,
    last item = the scored session), or None when no intraday store is available."""
    closes = [float(b["c"]) for b in bars]
    highs = [float(b["h"]) for b in bars]
    lows = [float(b["l"]) for b in bars]
    rsi = ind.wilder_rsi_series(closes)
    macd, signal, hist = ind.macd_series(closes) if closes else ([], [], [])
    m = {
        "asof": bars[-1]["date"] if bars else None,
        "bars": len(bars),
        "close": closes[-1] if closes else None,
        "prior_close": closes[-2] if len(closes) > 1 else None,
        "sma9": ind.sma(closes, 9), "sma20": ind.sma(closes, 20),
        "sma50": ind.sma(closes, 50), "sma200": ind.sma(closes, 200),
        "rsi0": rsi[-1] if rsi else None,
        "rsi1": rsi[-2] if len(rsi) > 1 else None,
        "rsi_last5": [r for r in rsi[-5:] if r is not None],
        # MACD needs ~35 sessions before its histogram means anything.
        "macd": macd[-1] if len(closes) >= 35 else None,
        "macd_signal": signal[-1] if len(closes) >= 35 else None,
        "hist": list(reversed(hist[-5:])) if len(closes) >= 35 else [],   # hist[0] = Hist0 (latest)
        "dd_pct": ind.drawdown_pct(closes, highs, cfg.dd_window),
        "ref_low": lows[-1] if lows else None,       # last completed RTH daily low
        "prior_low": lows[-2] if len(lows) > 1 else None,
    }
    m["chg_pct"] = ((m["close"] / m["prior_close"] - 1) * 100) if m["prior_close"] else None
    m["dd_band"] = dd_band(m["dd_pct"], cfg)
    m["rvol20"], m["vol_source"] = rvol20(rth_volumes, [float(b["v"]) for b in bars], rth_source, daily_source)
    return m


def dd_band(dd: Optional[float], cfg: RuleConfig) -> str:
    if dd is None:
        return "n/a"
    if dd < cfg.dd_min or dd > cfg.dd_max:
        return "out"
    return "shallow" if dd <= cfg.dd_shallow_max else "classic"


def rvol20(rth_volumes: Optional[Sequence[float]], daily_volumes: Sequence[float],
           rth_source: str = "SIP_RTH", daily_source: str = "SIP_DAILY"):
    """RVOL20 = scored session volume / mean of the previous 20 (scored session excluded).
    Numerator and denominator always come from ONE source, never mixed. Regular-hours
    volume first (SIP RTH by default, Webull RTH as fallback, set by
    RECOVERY_RVOL_BASIS), then that symbol's daily-bar volume. The label names the
    source, and the 0.72-0.88 VOL_BORDERLINE band absorbs the basis gap (e.g. ZS
    0.817 Webull vs 0.763 SIP)."""
    for vols, source in ((rth_volumes, rth_source), (daily_volumes, daily_source)):
        if vols and len(vols) >= 21:
            prev = vols[-21:-1]
            base = sum(prev) / 20.0
            if base > 0:
                return vols[-1] / base, source
    return None, "NONE"


# ---------------------------------------------------------------- gates
def battery(hist: Sequence[float]) -> Optional[int]:
    """MACD histogram battery on the official daily histogram, Hist0 first."""
    if not hist:
        return None
    h = list(hist) + [None] * 5
    h0, h1, h2, h3, h4 = h[:5]
    if h0 is None or h0 <= 0:
        return -1                      # Hist0 at or below 0 is RED (Rule 3)
    if None not in (h1, h2, h3, h4) and h0 > h1 > h2 > h3 > h4 and h4 > 0:
        return 4
    if None not in (h1, h2) and h0 > h1 > h2 and h2 > 0:
        return 3
    if h1 is not None and h0 > h1:
        return 2
    if None not in (h1, h2) and h0 < h1 < h2:
        return 0                       # fading three days: trim watch
    return 1                           # positive but not expanding


def stack_state(m: dict) -> str:
    if m["sma200"] is None:
        return "N/A"                   # fewer than 200 sessions: "no SMA200"
    return "Y" if m["sma9"] > m["sma20"] > m["sma50"] > m["sma200"] else "N"


def rsi_token(m: dict, cfg: RuleConfig) -> Optional[str]:
    """None when Rule 1's RSI clause passes, else the token explaining why not."""
    r0, r1 = m["rsi0"], m["rsi1"]
    if r0 is None or r1 is None:
        return "RSI n/a"
    if r0 < cfg.rsi_watch_low or r0 > cfg.rsi_late_high:
        return "RSI FAIL"
    if r0 > cfg.rsi_high:
        return "RSI LATE"
    if r0 < cfg.rsi_low:
        return "RSI WATCH" if r0 > r1 else "RSI FAIL"
    if r0 <= r1:
        return "RSI NOT RISING"
    return None


def rule1(m: dict, cfg: RuleConfig):
    """Cap / recovery. Returns (passes, first failing token)."""
    if m["dd_band"] not in ("shallow", "classic"):
        return False, f"DD {m['dd_band']}"
    if None in (m["sma20"], m["sma50"]):
        return False, "no SMA50"
    if not (m["close"] > m["sma20"] and m["close"] > m["sma50"]):
        return False, "failed-reclaim"
    if not m["sma20"] > m["sma50"]:
        return False, "SMA20 <= SMA50"
    if not m["hist"] or m["hist"][0] <= 0:
        return False, "Hist0 <= 0"
    tok = rsi_token(m, cfg)
    return (tok is None), tok


def rsi_strong(m: dict) -> bool:
    """Five-day strict RSI climb is Strong-only."""
    r = m["rsi_last5"]
    return len(r) == 5 and all(a < b for a, b in zip(r, r[1:]))


def rvol_label(rv: Optional[float], cfg: RuleConfig) -> str:
    if rv is None:
        return "RVOL20 n/a"
    s = f"RVOL20 {rv:.2f}"
    if cfg.rvol_border_low <= rv <= cfg.rvol_border_high:
        s += " VOL_BORDERLINE"
    return s


def exit_flags(m: dict, book: str) -> List[str]:
    """Rule 3. Audit flags only; a human decides. Never an automatic flatten."""
    flags = []
    if None in (m["sma9"], m["sma20"], m["sma50"]):
        return flags
    if m["sma9"] < m["sma20"]:
        flags.append("SMA9 lost SMA20")
    if m["close"] < m["sma20"] and m["close"] < m["sma50"]:
        flags.append("lost SMA20 then SMA50")
    if book == "Mom" and m["sma200"] is not None and m["sma50"] < m["sma200"]:
        flags.append("SMA50 below SMA200")
    if m["hist"] and m["hist"][0] <= 0:
        flags.append("RED (Hist0 <= 0)")
    return flags


def classify_daily(m: dict, cfg: RuleConfig) -> dict:
    """Daily qualification from the last completed session.

    Precedence (INTERPRETATION where the text is silent):
    TIER1 (Rule 2 + battery +2..+4 + RVOL20) > CAP_SETUP (Rule 1, still needs
    Anti-Knife in the live session) > TIER2 (stack met, another gate fails) >
    WATCH_HOLD (everything else, STACK N lives here)."""
    stack = stack_state(m)
    bat = battery(m["hist"])
    h0 = m["hist"][0] if m["hist"] else None
    rule2_core = stack == "Y" and (m["macd"] or 0) > 0 and (h0 or 0) > 0
    r1_ok, r1_tok = rule1(m, cfg)
    rv = m["rvol20"]
    rv_ok = rv is not None and rv >= cfg.rvol_floor - 1e-9

    gate = None
    if stack == "Y":
        if bat is not None and bat < 2:
            gate = f"BAT {bat:+d}" if bat else "BAT 0"
        elif not (m["macd"] or 0) > 0:
            gate = "MACD <= 0"
        elif m["close"] < m["sma20"]:
            # INTERPRETATION: a new Paper can't open below its own EXIT level.
            gate = "failed-reclaim"
        elif not rv_ok:
            gate = f"{rvol_label(rv, cfg)} < {cfg.rvol_floor:.2f}" if rv is not None else "RVOL20 n/a"

    if rule2_core and gate is None:
        status, book = "TIER1", "Mom"
    elif r1_ok:
        status, book = "CAP_SETUP", "Cap"
    elif stack == "Y":
        status, book = "TIER2", "Mom"
    else:
        status, book = "WATCH_HOLD", "Watch"
        gate = "no SMA200" if stack == "N/A" else "STACK N"

    graduate = book == "Cap" and None not in (m["sma50"], m["sma200"]) \
        and m["sma50"] > m["sma200"] and (m["macd"] or 0) > 0
    stop_level = m["sma50"] if book == "Cap" else m["sma20"]
    return {
        "status": status, "book": book, "stack": stack, "battery": bat,
        "rule1": r1_ok, "rule1_token": r1_tok, "rule2": rule2_core,
        "fail_gate": gate, "rsi_strong": rsi_strong(m), "graduate_rule2": graduate,
        "rvol_label": rvol_label(rv, cfg), "exit_flags": exit_flags(m, book),
        "stop_name": "SMA50" if book == "Cap" else "SMA20", "stop": stop_level,
    }


# ---------------------------------------------------------------- Anti-Knife
def ak_d(today_low: Optional[float], ref_low: Optional[float], prior_low: Optional[float]) -> str:
    """AK-D Daily. FAIL if today's (or premarket developing) low <= last completed
    RTH daily low. HOLD if yesterday was itself a lower low but today has not
    taken it (HOLD is not PASS). Otherwise PASS."""
    if today_low is None or ref_low is None:
        return "NOT_SCORED"
    if today_low <= ref_low:
        return "FAIL"
    if prior_low is not None and ref_low < prior_low:
        return "HOLD"
    return "PASS"


def ak_s(rth_bars: Sequence[dict], session_open: bool, cfg: RuleConfig) -> dict:
    """AK-S Session on the completed 5-min bars of today's RTH session *so far*.
    No full 78-bar session is needed: it is scored from the first completed bar on.
    PASS only if last close > VWAP and the session low has held >= 3 bars.
    FAIL if last close < VWAP and the low is still expanding (new low in last 3)."""
    if not session_open:
        return {"ak_s": "PREMARKET_NOT_APPLICABLE", "vwap": None, "low_held_bars": None}
    if not rth_bars:
        return {"ak_s": "NO_5MIN_FEED", "vwap": None, "low_held_bars": None}
    vwap = ind.session_vwap(rth_bars)
    lows = [float(b["l"]) for b in rth_bars]
    low_idx = lows.index(min(lows))                 # first bar that printed the session low
    held = len(rth_bars) - 1 - low_idx
    last = float(rth_bars[-1]["c"])
    if vwap is None:
        verdict = "NO_5MIN_FEED"
    elif last > vwap and held >= cfg.ak_s_hold_bars:
        verdict = "PASS"
    elif last < vwap and held < cfg.ak_s_hold_bars:
        verdict = "FAIL"
    else:
        verdict = "HOLD"                            # mixed: close vs VWAP and low-hold disagree
    return {"ak_s": verdict, "vwap": vwap, "low_held_bars": held}


def ak_combined(d: str, s: str) -> str:
    if "FAIL" in (d, s):
        return "FAIL"
    if d == "PASS" and s == "PASS":
        return "PASS"
    if "HOLD" in (d, s):
        return "HOLD"
    return "NOT_SCORED"


def wash_sale_blocked(ticker: str, today: date, cfg: RuleConfig) -> bool:
    until = cfg.wash_sale.get(ticker)
    return bool(until) and today.isoformat() <= until


# ---------------------------------------------------------------- card (Format 3)
def _f(x, nd=2, sign=False):
    if x is None:
        return "n/a"
    return f"{x:+.{nd}f}" if sign else f"{x:.{nd}f}"


def card_lines(n: int, ticker: str, m: dict, c: dict, tier_text: str) -> List[str]:
    """Standard Bot Trading Card pipe line + CLOSE line, field order per the rulebook."""
    h = (m["hist"] + [None, None])[:2]
    rsi = f"{_f(m['rsi0'], 1)}/{_f(m['rsi1'], 1)}"
    bat = "n/a" if c["battery"] is None else f"{c['battery']:+d}"
    pipe = (f"{n}. {ticker} | {c['book']} | DD {_f(m['dd_pct'], 1)}% | "
            f"SMA9 {_f(m['sma9'])} / 20 {_f(m['sma20'])} / 50 {_f(m['sma50'])} / 200 {_f(m['sma200'])} | "
            f"STACK {c['stack'] if c['stack'] != 'N/A' else 'N'} | MACD {_f(m['macd'], sign=True)} | "
            f"H0 {_f(h[0], sign=True)} H1 {_f(h[1], sign=True)} | BAT {bat} | RSI {rsi} | "
            f"{tier_text} | EXIT under {c['stop_name']} {_f(c['stop'])}")
    close = f"CLOSE {_f(m['close'])} ({_f(m['chg_pct'], sign=True)}% vs prior) as-of {m['asof']}"
    return [pipe, close]
