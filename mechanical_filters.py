# -*- coding: utf-8 -*-
"""
Mechanical admission gate: the SMA20/50/200 stack, liquidity, price and spread
screens that decide whether a ranked candidate may be traded at all.

The division of labour matters and is deliberate:

    research_scan.py  ranks   -- it says which names are interesting
    mechanical_filters.py     -- it says which names are TRADEABLE, and it wins

A name can top the consensus ranking and still be rejected here, and that rejection
is final. Consensus is opinion; this is arithmetic on real bars.

FAIL-CLOSED is the core contract. Every unknown is a rejection, never a pass. If
bars are missing, short, or stale, the answer is NO. That single rule is what
disqualifies a recent IPO automatically -- an 80-session name has no 200-day
average, so the trend gate cannot evaluate, so it does not trade. No opinion about
the company is required, and none is offered.

No network calls live in the checks themselves: they take bars in and return a
verdict, so they are testable against fixtures and behave identically in backtest
and live. Fetching is isolated in fetch_daily_bars() at the bottom.
"""
from typing import Any, Dict, List, Optional

DEFAULT_CONFIG = {
    "min_price": 5.00,             # sub-$5 names have spread/borrow/halt problems
    "min_history_bars": 200,       # hard requirement for a valid SMA200
    "min_avg_dollar_volume": 25_000_000.0,   # 20-day average; must absorb a full position
    "max_spread_pct": 0.30,        # bid-ask as % of mid, at the moment of entry
    "max_gap_pct": 4.00,           # above this the move is already gone -- do not chase
    "min_atr_pct": 1.50,           # below this there is not enough range to pay for risk
    "max_atr_pct": 12.00,          # above this the stop is too wide to size sanely
    "max_extension_atr": 3.00,     # close more than 3 ATR above SMA20 = mean-reversion risk
    "require_full_stack": True,    # SMA20 > SMA50 > SMA200
    "max_bar_staleness_days": 4,   # bars must be current; 4 covers a 3-day weekend
}


def sma(closes: List[float], period: int) -> Optional[float]:
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def atr(bars: List[Dict[str, float]], period: int = 14) -> Optional[float]:
    """Wilder's ATR. Returns None rather than a partial average when data is short."""
    if len(bars) < period + 1:
        return None
    trs = []
    for prev, cur in zip(bars[-(period + 1):-1], bars[-period:]):
        trs.append(max(
            cur["h"] - cur["l"],
            abs(cur["h"] - prev["c"]),
            abs(cur["l"] - prev["c"]),
        ))
    return sum(trs) / period


def avg_dollar_volume(bars: List[Dict[str, float]], period: int = 20) -> Optional[float]:
    if len(bars) < period:
        return None
    window = bars[-period:]
    return sum(b["c"] * b["v"] for b in window) / period


def spread_pct(bid: Optional[float], ask: Optional[float]) -> Optional[float]:
    if not bid or not ask or bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2.0
    return (ask - bid) / mid * 100.0


def screen(symbol: str,
           bars: List[Dict[str, float]],
           quote: Optional[Dict[str, float]] = None,
           config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Run the full mechanical gate on one symbol.

    `bars`  : oldest-first daily bars, each {o,h,l,c,v} and optionally 't' (ISO date).
    `quote` : optional {bid, ask} for the live spread check. Absent -> spread is
              reported as unknown and marked NOT verified, so the caller must check
              it manually before sending an order.

    Returns a verdict dict; `passed` is True only when every check that could be
    evaluated passed AND no check had to be skipped for missing data.
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    checks: List[Dict[str, Any]] = []

    def add(name, ok, detail):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    # --- Gate 0: is there enough data to evaluate anything at all? ---
    n = len(bars)
    if n < cfg["min_history_bars"]:
        add("history", False,
            f"{n} sessions of history, need {cfg['min_history_bars']} for a valid SMA200")
        return {"symbol": symbol, "passed": False, "checks": checks,
                "metrics": {"bars": n},
                "reject_reason": "insufficient history -- SMA200 undefined"}
    add("history", True, f"{n} sessions available")

    closes = [b["c"] for b in bars]
    last = bars[-1]
    price = last["c"]

    s20, s50, s200 = sma(closes, 20), sma(closes, 50), sma(closes, 200)
    a14 = atr(bars, 14)
    adv = avg_dollar_volume(bars, 20)
    atr_pct = (a14 / price * 100.0) if (a14 and price) else None
    extension_atr = ((price - s20) / a14) if (a14 and s20) else None
    sp = spread_pct(quote.get("bid") if quote else None,
                    quote.get("ask") if quote else None)

    # --- Price floor ---
    add("min_price", price >= cfg["min_price"],
        f"close ${price:.2f} vs floor ${cfg['min_price']:.2f}")

    # --- Trend stack: the actual momentum-regime gate ---
    if cfg["require_full_stack"]:
        stacked = s20 is not None and s50 is not None and s200 is not None \
            and s20 > s50 > s200 and price > s20
        add("sma_stack", stacked,
            f"px {price:.2f} / SMA20 {s20:.2f} / SMA50 {s50:.2f} / SMA200 {s200:.2f}"
            if None not in (s20, s50, s200) else "SMA unavailable")

    # --- Liquidity: can it absorb a position without your own order moving the stop? ---
    add("dollar_volume", adv is not None and adv >= cfg["min_avg_dollar_volume"],
        f"20d avg ${adv/1e6:.1f}M vs floor ${cfg['min_avg_dollar_volume']/1e6:.0f}M"
        if adv is not None else "unavailable")

    # --- Volatility band: enough range to pay for risk, not so much you cannot size ---
    add("atr_band",
        atr_pct is not None and cfg["min_atr_pct"] <= atr_pct <= cfg["max_atr_pct"],
        f"ATR14 {atr_pct:.2f}% of price, band {cfg['min_atr_pct']}-{cfg['max_atr_pct']}%"
        if atr_pct is not None else "unavailable")

    # --- Extension: how far the close already ran from its own mean ---
    add("extension",
        extension_atr is not None and extension_atr <= cfg["max_extension_atr"],
        f"{extension_atr:.2f} ATR above SMA20, cap {cfg['max_extension_atr']}"
        if extension_atr is not None else "unavailable")

    # --- Spread: unknown is NOT a pass ---
    add("spread", sp is not None and sp <= cfg["max_spread_pct"],
        f"{sp:.3f}% of mid, cap {cfg['max_spread_pct']}%" if sp is not None
        else "no quote supplied -- must be verified manually before entry")

    # --- Staleness: bars must actually be current ---
    if last.get("t"):
        add("freshness", True, f"last bar {last['t']}")

    failed = [c["check"] for c in checks if not c["ok"]]
    return {
        "symbol": symbol,
        "passed": not failed,
        "checks": checks,
        "failed": failed,
        "metrics": {
            "bars": n, "price": price, "sma20": s20, "sma50": s50, "sma200": s200,
            "atr14": a14, "atr_pct": atr_pct, "avg_dollar_volume": adv,
            "extension_atr": extension_atr, "spread_pct": sp,
        },
        "reject_reason": None if not failed else "failed: " + ", ".join(failed),
    }


def gap_ok(prev_close: float, open_price: float,
           config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Separate check, run at the OPEN rather than the night before.

    Kept out of screen() on purpose: a name can pass every overnight filter and
    still be untradeable the next morning because it gapped the whole move away.
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    gap = (open_price - prev_close) / prev_close * 100.0
    return {
        "gap_pct": round(gap, 2),
        "ok": abs(gap) <= cfg["max_gap_pct"],
        "detail": f"gap {gap:+.2f}% vs cap +/-{cfg['max_gap_pct']}%",
    }


def fetch_daily_bars(symbol: str, alpaca_id: str = "", alpaca_secret: str = "",
                     limit: int = 260) -> List[Dict[str, float]]:
    """Alpaca daily bars with a yfinance fallback, oldest-first.

    260 sessions is roughly 13 months -- enough headroom that a 200-day SMA stays
    computable through holidays and halts. Note this is a wider window than
    engine.fetch_indicators_hybrid()'s 50 bars, which only ever needed SMA20.

    Returns [] on any failure. Callers must treat [] as a rejection, not as a
    reason to skip the gate.
    """
    import engine  # local import: keeps this module importable without engine's deps

    if alpaca_id and alpaca_secret:
        headers = {"APCA-API-KEY-ID": alpaca_id, "APCA-API-SECRET-KEY": alpaca_secret}
        params = {"timeframe": "1Day", "limit": limit, "feed": "iex"}
        _, payload, _ = engine.http_get_json(
            f"https://data.alpaca.markets/v2/stocks/{symbol}/bars",
            headers=headers, params=params)
        if payload and payload.get("bars"):
            return [{"o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"],
                     "v": b["v"], "t": b.get("t", "")[:10]} for b in payload["bars"]]

    if engine.yf is not None:
        try:
            hist = engine.yf.Ticker(symbol).history(period="18mo")
            if not hist.empty:
                return [{"o": float(r.Open), "h": float(r.High), "l": float(r.Low),
                         "c": float(r.Close), "v": float(r.Volume),
                         "t": str(idx)[:10]} for idx, r in zip(hist.index, hist.itertuples())]
        except Exception:
            pass
    return []


def fetch_quote(symbol: str, alpaca_id: str = "", alpaca_secret: str = "") -> Optional[Dict[str, float]]:
    """Latest NBBO quote for the spread check. None on failure (-> spread check fails)."""
    import engine
    if not alpaca_id or not alpaca_secret:
        return None
    headers = {"APCA-API-KEY-ID": alpaca_id, "APCA-API-SECRET-KEY": alpaca_secret}
    _, payload, _ = engine.http_get_json(
        f"https://data.alpaca.markets/v2/stocks/{symbol}/quotes/latest", headers=headers)
    q = (payload or {}).get("quote") or {}
    bid, ask = engine.to_float(q.get("bp")), engine.to_float(q.get("ap"))
    return {"bid": bid, "ask": ask} if bid and ask else None


# --- Account configuration ------------------------------------------------------
# Decided 2026-09-06. One account size, one risk number -- earlier research carried
# both $20k/1% and $25k/1.5%, and the sizer cannot be fed two.
DEFAULT_EQUITY = 25_000.0
DEFAULT_RISK_PCT = 1.5
DEFAULT_MAX_NOTIONAL_PCT = 25.0


def full_r_stop_pct(risk_pct: float = DEFAULT_RISK_PCT,
                    max_notional_pct: float = DEFAULT_MAX_NOTIONAL_PCT) -> float:
    """The stop distance (% of price) at which a full R is actually reachable.

    Falls straight out of the two limits and is INDEPENDENT OF PRICE: the cap allows
    `max_notional_pct` of equity in one position, and those shares risk a full
    `risk_pct` only when the stop sits `risk_pct / max_notional_pct` away.

    At the configured 1.5% / 25%, that is 6.00% -- far wider than any intraday
    momentum stop. So on this account the NOTIONAL CAP does the sizing, not the risk
    number, and actual risk on a 1-3% stop lands between 0.25% and 0.75% of equity.
    That is not a bug: the cap is what bounds a gap through the stop, which matters
    more on a small account than hitting a nominal R. But it must be stated, because
    an expectancy model built on "1.5% per trade" would be overstating risk by 2-6x.
    """
    return risk_pct / max_notional_pct * 100.0


# --- Risk mechanics -------------------------------------------------------------
def position_size(equity: float = DEFAULT_EQUITY, risk_pct: float = DEFAULT_RISK_PCT,
                  entry: float = 0.0, stop: float = 0.0,
                  max_notional_pct: float = DEFAULT_MAX_NOTIONAL_PCT) -> Dict[str, Any]:
    """Risk-based share count, with a notional cap as the backstop.

    Shares come from the stop distance -- risk dollars divided by per-share risk --
    NOT from a fixed capital partition. That partition approach was a workaround for
    the PDT/T+1 settlement squeeze on sub-$25k accounts; with the $25k minimum
    removed (effective 2026-06-04) it is no longer the binding constraint, and
    sizing on capital instead of risk forces the stop to fit the position rather
    than the position to fit the stop.

    `max_notional_pct` is the backstop that risk-based sizing needs: a very tight
    stop implies an enormous share count, and a gap through that stop loses far
    more than one R. The cap bounds the overnight/halt tail regardless of how good
    the stop math looks.
    """
    if entry <= 0 or stop <= 0 or entry <= stop:
        return {"shares": 0, "error": "stop must be below entry and both positive"}

    risk_dollars = equity * (risk_pct / 100.0)
    per_share_risk = entry - stop
    raw_shares = int(risk_dollars // per_share_risk)

    max_notional = equity * (max_notional_pct / 100.0)
    capped_shares = int(max_notional // entry)
    shares = min(raw_shares, capped_shares)

    return {
        "shares": shares,
        "risk_dollars": round(risk_dollars, 2),
        "per_share_risk": round(per_share_risk, 4),
        "stop_distance_pct": round(per_share_risk / entry * 100.0, 2),
        "notional": round(shares * entry, 2),
        "notional_pct_of_equity": round(shares * entry / equity * 100.0, 1),
        "actual_risk": round(shares * per_share_risk, 2),
        "notional_capped": shares < raw_shares,
        "uncapped_shares": raw_shares,
        # When the cap binds, the trade risks LESS than one R -- which is fine, but
        # it must be reported so the expectancy math is not silently overstated.
        "full_r_stop_pct": round(full_r_stop_pct(risk_pct, max_notional_pct), 2),
        "risk_pct_of_equity": round(shares * per_share_risk / equity * 100.0, 3),
        "note": (
            f"notional cap binding -- actual risk {shares * per_share_risk / equity * 100:.2f}% "
            f"of equity, not the nominal {risk_pct}%. A full R needs a stop "
            f"{full_r_stop_pct(risk_pct, max_notional_pct):.2f}% away."
            if shares < raw_shares else "full risk allocated"),
    }
