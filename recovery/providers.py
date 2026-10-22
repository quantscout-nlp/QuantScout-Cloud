# -*- coding: utf-8 -*-
"""
Market-data feeds behind one rate-limited client, in a fixed fallback order.

Role of each paid subscription (see docs/RECOVERY_DESK_CLOUD.md for the reasoning):
- Alpaca Algo Trader Plus (SIP, 10,000 REST calls/min, unlimited stream symbols):
  primary for daily bars, live 5-minute bars, RTH volume, market calendar and
  Benzinga news. Bulk multi-symbol endpoints keep a full 232-name cycle at a
  handful of calls, far below the limit.
- Massive (formerly Polygon) Stocks Developer: fallback daily bars per symbol and
  fundamentals. Its live data is 15-minute delayed on that tier, so it is never
  used for the intraday Anti-Knife session gate.
- Tiingo: fallback daily bars.
- yfinance: last resort daily bars only. It scrapes Yahoo, is not an API, and
  datacenter IPs are often throttled. A Yahoo Finance Gold subscription does not
  change that.
- Seeking Alpha Pro: no public API, and its terms forbid scraping. Not used.

Every result says which source it came from, so the desk can label the data
and never pass off a fallback as SIP.
"""
import os
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

import requests

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

ALPACA_DATA = "https://data.alpaca.markets"
# Calendar lives on the trading host. Paper host on purpose: nothing here trades,
# and paper keys are what this repo's secrets already hold.
ALPACA_TRADE = "https://paper-api.alpaca.markets"
MASSIVE_BASE = os.environ.get("MASSIVE_API_BASE", "https://api.polygon.io")
TIINGO_BASE = "https://api.tiingo.com"


class RateLimiter:
    """Spaces calls so a provider never sees more than `per_minute` of them."""

    def __init__(self, per_minute: int):
        self.interval = 60.0 / max(per_minute, 1)
        self._next = 0.0

    def wait(self):
        now = time.monotonic()
        if now < self._next:
            time.sleep(self._next - now)
        self._next = max(now, self._next) + self.interval


class Http:
    def __init__(self, stats: dict):
        self.s = requests.Session()
        self.s.headers.update({"user-agent": "QuantScoutRecoveryDesk/1.0"})
        self.stats = stats

    def get(self, url, limiter: RateLimiter, headers=None, params=None, tries=4):
        delay = 1.0
        err = ""
        for _ in range(tries):
            limiter.wait()
            try:
                r = self.s.get(url, headers=headers, params=params, timeout=20)
            except requests.RequestException as e:
                err = str(e)[:200]
                time.sleep(delay)
                delay *= 2
                continue
            self.stats["calls"] = self.stats.get("calls", 0) + 1
            if r.status_code == 429 or r.status_code >= 500:
                if r.status_code == 429:
                    self.stats["rate_limited"] = self.stats.get("rate_limited", 0) + 1
                wait = r.headers.get("Retry-After")
                time.sleep(float(wait) if wait and wait.replace(".", "", 1).isdigit() else delay)
                delay *= 2
                err = f"HTTP {r.status_code}"
                continue
            if r.status_code >= 400:
                return r.status_code, None, r.text[:300]
            try:
                return r.status_code, r.json(), ""
            except ValueError:
                return r.status_code, None, "bad json"
        return 0, None, err


def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(ET)


def _bar(t: datetime, o, h, l, c, v, vw=None) -> dict:
    return {"t": t.isoformat(), "date": t.date().isoformat(), "o": float(o), "h": float(h),
            "l": float(l), "c": float(c), "v": float(v or 0), "vw": (float(vw) if vw is not None else None)}


def _chunks(xs: List[str], n: int) -> Iterable[List[str]]:
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


class DataHub:
    def __init__(self, env=os.environ):
        self.stats: dict = {"calls": 0, "rate_limited": 0, "errors": []}
        self.http = Http(self.stats)
        self.alpaca_h = {"APCA-API-KEY-ID": env.get("ALPACA_ID", ""),
                         "APCA-API-SECRET-KEY": env.get("ALPACA_SECRET", "")}
        self.massive_key = env.get("MASSIVE_KEY") or env.get("POLYGON_KEY", "")
        self.tiingo_key = env.get("TIINGO_KEY", "")
        # Per-plan ceilings, set below the published limits to leave headroom.
        self.lim = {
            "alpaca": RateLimiter(int(env.get("ALPACA_CALLS_PER_MIN", "1000"))),
            "massive": RateLimiter(int(env.get("MASSIVE_CALLS_PER_MIN", "300"))),
            "tiingo": RateLimiter(int(env.get("TIINGO_CALLS_PER_MIN", "60"))),
        }
        self.feed = env.get("ALPACA_FEED", "sip").lower()
        self.feed_note = ""

    @property
    def has_alpaca(self):
        return bool(self.alpaca_h["APCA-API-KEY-ID"] and self.alpaca_h["APCA-API-SECRET-KEY"])

    def _err(self, msg):
        self.stats["errors"].append(msg[:200])

    # ------------------------------------------------------------ Alpaca
    def alpaca_bars(self, symbols: List[str], timeframe: str, start: datetime, end: datetime,
                    adjustment: str = "split") -> Dict[str, List[dict]]:
        out: Dict[str, List[dict]] = defaultdict(list)
        if not self.has_alpaca or not symbols:
            return out
        for chunk in _chunks(symbols, 100):
            token = None
            while True:
                params = {"symbols": ",".join(chunk), "timeframe": timeframe, "limit": 10000,
                          "start": start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                          "end": end.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                          "adjustment": adjustment, "feed": self.feed, "sort": "asc"}
                if token:
                    params["page_token"] = token
                sc, j, e = self.http.get(f"{ALPACA_DATA}/v2/stocks/bars", self.lim["alpaca"],
                                         headers=self.alpaca_h, params=params)
                if sc in (403, 422) and self.feed == "sip" and "subscription" in (e or "").lower():
                    # Plan doesn't cover recent SIP: drop to IEX and say so on every row.
                    self.feed = "iex"
                    self.feed_note = "IEX fallback: plan does not permit recent SIP (partial volume, VWAP approximate)"
                    continue
                if not j:
                    self._err(f"alpaca bars {timeframe}: {sc} {e}")
                    break
                for sym, rows in (j.get("bars") or {}).items():
                    for b in rows:
                        out[sym].append(_bar(_parse_ts(b["t"]), b["o"], b["h"], b["l"], b["c"], b["v"], b.get("vw")))
                token = j.get("next_page_token")
                if not token:
                    break
        return out

    def alpaca_calendar(self, start: date, end: date) -> Optional[List[dict]]:
        if not self.has_alpaca:
            return None
        sc, j, e = self.http.get(f"{ALPACA_TRADE}/v2/calendar", self.lim["alpaca"], headers=self.alpaca_h,
                                 params={"start": start.isoformat(), "end": end.isoformat()})
        if not isinstance(j, list):
            self._err(f"alpaca calendar: {sc} {e}")
            return None
        return j

    def alpaca_news(self, symbols: List[str], start: datetime) -> Optional[Dict[str, dict]]:
        """Latest Benzinga headline per symbol since `start`. None if the search failed."""
        if not self.has_alpaca:
            return None
        latest: Dict[str, dict] = {}
        for chunk in _chunks(symbols, 50):
            token, pages = None, 0
            while pages < 10:
                params = {"symbols": ",".join(chunk), "limit": 50, "sort": "desc",
                          "start": start.astimezone(UTC).isoformat().replace("+00:00", "Z")}
                if token:
                    params["page_token"] = token
                sc, j, e = self.http.get(f"{ALPACA_DATA}/v1beta1/news", self.lim["alpaca"],
                                         headers=self.alpaca_h, params=params)
                if not j:
                    self._err(f"alpaca news: {sc} {e}")
                    return None
                for n in j.get("news", []):
                    for s in n.get("symbols", []):
                        if s in chunk and s not in latest:
                            latest[s] = {"ts": n.get("created_at"), "headline": n.get("headline", "")[:160]}
                token, pages = j.get("next_page_token"), pages + 1
                if not token:
                    break
        return latest

    # ------------------------------------------------------------ Massive / Tiingo / yfinance
    def massive_daily(self, sym: str, start: date, end: date) -> List[dict]:
        if not self.massive_key:
            return []
        sc, j, e = self.http.get(f"{MASSIVE_BASE}/v2/aggs/ticker/{sym}/range/1/day/{start}/{end}",
                                 self.lim["massive"], params={"adjusted": "true", "sort": "asc",
                                                              "limit": 50000, "apiKey": self.massive_key})
        if not j:
            self._err(f"massive {sym}: {sc} {e}")
            return []
        return [_bar(datetime.fromtimestamp(r["t"] / 1000, ET), r["o"], r["h"], r["l"], r["c"], r.get("v"), r.get("vw"))
                for r in j.get("results") or []]

    def massive_eps_ttm(self, sym: str) -> Optional[dict]:
        if not self.massive_key:
            return None
        sc, j, e = self.http.get(f"{MASSIVE_BASE}/vX/reference/financials", self.lim["massive"],
                                 params={"ticker": sym, "timeframe": "ttm", "limit": 1, "apiKey": self.massive_key})
        try:
            r = j["results"][0]
            eps = r["financials"]["income_statement"]["diluted_earnings_per_share"]["value"]
            return {"eps_ttm": eps, "fund_asof": r.get("end_date"), "fund_source": "massive"}
        except (TypeError, KeyError, IndexError):
            return None

    def tiingo_daily(self, sym: str, start: date, end: date) -> List[dict]:
        if not self.tiingo_key:
            return []
        sc, j, e = self.http.get(f"{TIINGO_BASE}/tiingo/daily/{sym}/prices", self.lim["tiingo"],
                                 params={"startDate": start.isoformat(), "endDate": end.isoformat(),
                                         "token": self.tiingo_key})
        if not isinstance(j, list):
            self._err(f"tiingo {sym}: {sc} {e}")
            return []
        out = []
        for r in j:
            d = datetime.fromisoformat(r["date"].replace("Z", "+00:00")).date()
            out.append(_bar(datetime(d.year, d.month, d.day, tzinfo=ET), r["adjOpen"], r["adjHigh"],
                            r["adjLow"], r["adjClose"], r.get("adjVolume")))
        return out

    def yf_daily(self, sym: str, start: date, end: date) -> List[dict]:
        try:
            import yfinance as yf
            hist = yf.Ticker(sym).history(start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(),
                                          auto_adjust=False)
        except Exception as e:  # noqa: BLE001 - last-resort source, any failure just means "no data"
            self._err(f"yfinance {sym}: {e}")
            return []
        return [_bar(datetime(ix.year, ix.month, ix.day, tzinfo=ET), r["Open"], r["High"], r["Low"],
                     r["Close"], r["Volume"]) for ix, r in hist.iterrows()]

    # ------------------------------------------------------------ cascades
    def daily_bars(self, symbols: List[str], start: date, end: date, min_bars: int = 60
                   ) -> Tuple[Dict[str, List[dict]], Dict[str, str]]:
        s_dt = datetime(start.year, start.month, start.day, tzinfo=ET)
        e_dt = datetime(end.year, end.month, end.day, 23, 59, tzinfo=ET)
        bars = dict(self.alpaca_bars(symbols, "1Day", s_dt, e_dt))
        source = {s: f"alpaca_{self.feed}" for s in bars if len(bars[s]) >= min_bars}
        for sym in symbols:
            if sym in source:
                continue
            for name, fn in (("massive", self.massive_daily), ("tiingo", self.tiingo_daily), ("yfinance", self.yf_daily)):
                got = fn(sym, start, end)
                if len(got) >= min_bars:
                    bars[sym], source[sym] = got, name
                    break
        return {s: bars[s] for s in source}, source

    def rth_volumes(self, symbols: List[str], start: date, end: date) -> Dict[str, Dict[str, float]]:
        """Regular-hours (09:30-16:00 ET) volume per session, from 30-min SIP bars."""
        s_dt = datetime(start.year, start.month, start.day, 9, 30, tzinfo=ET)
        e_dt = datetime(end.year, end.month, end.day, 16, 0, tzinfo=ET)
        out: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for sym, rows in self.alpaca_bars(symbols, "30Min", s_dt, e_dt).items():
            for b in rows:
                t = datetime.fromisoformat(b["t"])
                if (t.hour, t.minute) >= (9, 30) and t.hour < 16:
                    out[sym][b["date"]] += b["v"]
        return out
