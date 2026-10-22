# -*- coding: utf-8 -*-
"""
Pure indicator maths on lists of floats (oldest first). No pandas, so the rule
layer gives the same numbers on any Python and can be unit-tested without data.

Conventions match the LeanData pack's FIELDS.md (schema 2) so the cloud desk and
the desktop pack agree:
- RSI(14) with Wilder smoothing (seeded with the simple mean of the first 14).
- MACD(12,26,9) with EMA alpha = 2/(n+1), seeded with the first value.
- Drawdown = close / highest HIGH over the window - 1.
"""
from typing import List, Optional, Sequence


def sma(values: Sequence[float], n: int) -> Optional[float]:
    if len(values) < n or n <= 0:
        return None
    return sum(values[-n:]) / n


def ema_series(values: Sequence[float], n: int) -> List[float]:
    if not values:
        return []
    alpha = 2.0 / (n + 1)
    out = [float(values[0])]
    for v in values[1:]:
        out.append(alpha * float(v) + (1 - alpha) * out[-1])
    return out


def wilder_rsi_series(closes: Sequence[float], n: int = 14) -> List[Optional[float]]:
    """RSI per bar; None until n changes exist."""
    out: List[Optional[float]] = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains[:n]) / n
    avg_l = sum(losses[:n]) / n
    out[n] = _rsi(avg_g, avg_l)
    for i in range(n, len(gains)):
        avg_g = (avg_g * (n - 1) + gains[i]) / n
        avg_l = (avg_l * (n - 1) + losses[i]) / n
        out[i + 1] = _rsi(avg_g, avg_l)
    return out


def _rsi(avg_g: float, avg_l: float) -> float:
    if avg_l == 0:
        return 100.0 if avg_g > 0 else 50.0
    rs = avg_g / avg_l
    return 100.0 - 100.0 / (1.0 + rs)


def macd_series(closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9):
    """Returns (macd, signal, hist) lists, same length as closes."""
    ef, es = ema_series(closes, fast), ema_series(closes, slow)
    macd = [a - b for a, b in zip(ef, es)]
    sig = ema_series(macd, signal)
    hist = [m - s for m, s in zip(macd, sig)]
    return macd, sig, hist


def drawdown_pct(closes: Sequence[float], highs: Sequence[float], window: int) -> Optional[float]:
    """Positive percent below the highest HIGH of the last `window` sessions (incl. today)."""
    if not closes or not highs:
        return None
    peak = max(highs[-window:])
    if peak <= 0:
        return None
    return (1.0 - closes[-1] / peak) * 100.0


def session_vwap(bars: Sequence[dict]) -> Optional[float]:
    """VWAP over completed 5-min bars. Uses each bar's own vwap when the feed
    supplies it (Alpaca `vw`), otherwise the typical price (h+l+c)/3."""
    num = den = 0.0
    for b in bars:
        v = float(b.get("v") or 0)
        if v <= 0:
            continue
        px = b.get("vw")
        if px is None:
            px = (float(b["h"]) + float(b["l"]) + float(b["c"])) / 3.0
        num += float(px) * v
        den += v
    return num / den if den > 0 else None
