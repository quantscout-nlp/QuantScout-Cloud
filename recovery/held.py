# -*- coding: utf-8 -*-
"""
Robinhood positions (read-only) and the held-name stop block.

Robinhood has no official equities API, so positions come in as a CSV you upload
on the dashboard. It accepts the HITL `rh_book_7190_*.csv` layouts (symbol or
ticker, qty, avg_cost, SEED/WHOLE in seed_or_whole / type / seed_whole / note)
and Robinhood-style headers (Symbol, Quantity, Average Cost). Only symbol, qty,
average cost, lot and an optional Cap/Mom book are kept. No account number is
stored beyond a masked tail.

Stop block, from the locked rule text (v1.6.1, unchanged in v1.6.2):
  Mom stop = last official close under SMA20. Cap stop = last official close under
  SMA50. EXIT_FLAG if under the stop. STOP_NOT_COMPUTED if the held name was not
  scanned. Flags are audit output. Human decides. Not an automatic flatten. Do not
  invent a flag if the official close is not under the stop.
"""
import csv
import io
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple

MAX_BYTES = 2_000_000
MAX_ROWS = 2000
SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
STALE_DAYS = 3

SYMBOL_COLS = ("symbol", "ticker", "instrument")
QTY_COLS = ("qty", "quantity", "shares", "rh_qty")
COST_COLS = ("avg_cost", "average_cost", "average cost", "avg cost", "rh_avg_cost", "average buy price", "cost_basis_per_share")
LOT_COLS = ("seed_or_whole", "seed_whole", "rh_lot", "type", "note", "lot")
BOOK_COLS = ("book", "sleeve")
ASOF_COLS = ("asof_et", "as_of", "asof", "date")
MASK_COLS = ("account_mask", "account")


def _pick(row: Dict[str, str], names) -> Optional[str]:
    for n in names:
        v = row.get(n)
        if v not in (None, ""):
            return v.strip()
    return None


def _num(s: Optional[str]) -> Optional[float]:
    if s is None:
        return None
    try:
        return float(s.replace("$", "").replace(",", "").strip())
    except ValueError:
        return None


def parse_positions(raw: bytes) -> Tuple[List[dict], dict, List[str]]:
    """Returns (positions, meta, warnings). Raises ValueError when the file can't be used."""
    if len(raw) > MAX_BYTES:
        raise ValueError("file is larger than 2 MB")
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("no header row")
    reader.fieldnames = [f.strip().lower() for f in reader.fieldnames]
    if not any(c in reader.fieldnames for c in SYMBOL_COLS) or not any(c in reader.fieldnames for c in QTY_COLS):
        raise ValueError("need a symbol/ticker column and a qty/quantity column")
    merged: Dict[str, dict] = {}
    warnings: List[str] = []
    meta = {"asof_et": None, "account_mask": None}
    for i, row in enumerate(reader, start=2):
        if i > MAX_ROWS + 1:
            warnings.append(f"stopped after {MAX_ROWS} rows")
            break
        sym = (_pick(row, SYMBOL_COLS) or "").upper()
        qty = _num(_pick(row, QTY_COLS))
        if not sym and qty is None:
            continue
        if not SYMBOL_RE.match(sym):
            warnings.append(f"row {i}: skipped, symbol not recognised")
            continue
        if qty is None or qty <= 0:
            warnings.append(f"row {i}: {sym} skipped, quantity missing or not positive")
            continue
        cost = _num(_pick(row, COST_COLS))
        lot_raw = next((v.strip().upper() for v in (row.get(c) for c in LOT_COLS) if v and v.strip().upper() in ("SEED", "WHOLE")), None)
        book_raw = (_pick(row, BOOK_COLS) or "").lower()
        book = "Cap" if book_raw.startswith(("cap", "rec")) else "Mom" if book_raw.startswith("mom") else None
        meta["asof_et"] = meta["asof_et"] or _pick(row, ASOF_COLS)
        mask = _pick(row, MASK_COLS)
        if mask and not meta["account_mask"]:
            meta["account_mask"] = "••••" + re.sub(r"\D", "", mask)[-4:]
        if sym in merged:                       # same symbol twice: combine into one lot
            p = merged[sym]
            total = p["qty"] + qty
            if p["avg_cost"] is not None and cost is not None:
                p["avg_cost"] = round((p["avg_cost"] * p["qty"] + cost * qty) / total, 6)
            p["qty"] = round(total, 6)
            continue
        merged[sym] = {"symbol": sym, "qty": round(qty, 6), "avg_cost": cost,
                       "lot": lot_raw or ("SEED" if qty < 1 else "WHOLE"), "book": book}
    if not merged:
        raise ValueError("no usable positions found")
    return list(merged.values()), meta, warnings


def held_block(doc: Optional[dict], tickers: Dict[str, dict], live: Dict[str, float], now: datetime) -> dict:
    """One line per held name. `tickers` = daily qualification rows (official close based)."""
    if not doc or not doc.get("positions"):
        return {"status": "NO_POSITIONS", "lines": [], "counts": {}}
    uploaded = datetime.fromisoformat(doc["uploaded_et"])
    age_days = (now - uploaded).total_seconds() / 86400
    lines = []
    for p in doc["positions"]:
        d = tickers.get(p["symbol"]) or {}
        line = {"ticker": p["symbol"], "qty": p["qty"], "avg_cost": p["avg_cost"], "lot": p["lot"]}
        if d.get("status") in (None, "NOT_SCANNED") or d.get("close") is None:
            line.update({"flag": "STOP_NOT_COMPUTED", "reason": d.get("reason", "not scanned")})
            lines.append(line)
            continue
        # Book: the file's Cap/Mom column wins, then the desk's own book. INTERPRETATION:
        # a Watch name defaults to Mom (SMA20), the tighter stop, so a flag comes sooner.
        book = p.get("book") or (d.get("book") if d.get("book") in ("Cap", "Mom") else "Mom")
        stop_name = "SMA50" if book == "Cap" else "SMA20"
        stop = d.get("sma50") if book == "Cap" else d.get("sma20")
        close = d["close"]
        line.update({"book": book, "book_basis": "file" if p.get("book") else
                     ("desk" if d.get("book") in ("Cap", "Mom") else "default"),
                     "stop_name": stop_name, "stop": stop, "close": close, "asof": d.get("asof"),
                     "status": d.get("status"), "rule3": d.get("exit_flags", [])})
        if stop is None:
            line.update({"flag": "STOP_NOT_COMPUTED", "reason": f"no {stop_name}"})
        else:
            line["flag"] = "EXIT_FLAG" if close < stop else "OK"
        if p["avg_cost"]:
            line["unrealized_pct"] = round((close / p["avg_cost"] - 1) * 100, 2)
        if p["symbol"] in live:
            line["live_last"] = live[p["symbol"]]
            if stop is not None and live[p["symbol"]] < stop:
                line["live_note"] = f"LIVE under {stop_name} (intraday, not official)"
        lines.append(line)
    order = {"EXIT_FLAG": 0, "STOP_NOT_COMPUTED": 1, "OK": 2}
    lines.sort(key=lambda l: (order[l["flag"]], l["ticker"]))
    counts: Dict[str, int] = {}
    for l in lines:
        counts[l["flag"]] = counts.get(l["flag"], 0) + 1
    return {"status": "BOOK_STALE" if age_days > STALE_DAYS else "OK", "uploaded_et": doc["uploaded_et"],
            "asof_et": doc.get("asof_et"), "account_mask": doc.get("account_mask"),
            "age_days": round(age_days, 1), "lines": lines, "counts": counts}


BOOK_CHOICES = ("Auto", "Cap", "Mom")


def carry_books(positions: List[dict], previous: Optional[dict]) -> List[dict]:
    """Keep the Cap/Mom you set last time: a position with no book in the new file
    takes the book saved for the same symbol in the previous held.json."""
    prev = {p.get("symbol"): p.get("book") for p in (previous or {}).get("positions", [])
            if p.get("book") in ("Cap", "Mom")}
    return [{**p, "book": p.get("book") or prev.get(p["symbol"])} for p in positions]


def set_books(positions: List[dict], choices: Dict[str, Optional[str]]) -> List[dict]:
    """Apply per-name choices from the dashboard editor. 'Auto' (or blank) clears the
    book so the desk decides (its own Cap/Mom book, else Mom)."""
    out = []
    for p in positions:
        c = choices.get(p["symbol"], p.get("book"))
        out.append({**p, "book": c if c in ("Cap", "Mom") else None})
    return out


def rh_fields(doc: Optional[dict]) -> Dict[str, dict]:
    """ticker -> {rh_qty, rh_avg_cost, rh_lot} for the Tier 2 EXTRA 'RH qty @ cost or SEED'."""
    out = {}
    for p in (doc or {}).get("positions", []):
        out[p["symbol"]] = {"rh_qty": p["qty"], "rh_avg_cost": p["avg_cost"], "rh_lot": p["lot"]}
    return out
