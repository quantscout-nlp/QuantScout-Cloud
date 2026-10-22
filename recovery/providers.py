# -*- coding: utf-8 -*-
"""
Market-data feeds behind one rate-limited client, in a fixed fallback order.

| Data                     | 1st                 | 2nd (fallback)          | then                       |
|--------------------------|---------------------|-------------------------|----------------------------|
| Live 5-min bars (AK-S/D) | Alpaca SIP          | Webull OpenAPI (live)   | none -> NO_5MIN_FEED       |
| Daily bars (qualify)     | Alpaca SIP          | Webull OpenAPI          | Massive, Tiingo, yfinance* |
| RVOL20 volume            | SIP RTH (30-min)    | Webull RTH daily        | daily SIP volume (labelled)|
| News token               | Alpaca (Benzinga)   | -                       | NEWS_NOT_SEARCHED          |

*yfinance only if installed; it scrapes Yahoo and is not part of the cloud install.
Massive Stocks Developer is 15-minute delayed, so it never feeds the live gates.
Seeking Alpha Pro has no API and is not used. Robinhood has no official equities API.

Every series carries its source, and RVOL20 never mixes sources within one ratio.
Nothing here can trade: only market-data hosts are called, plus Alpaca's calendar.
"""
import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

ALPACA_DATA = "https://data.alpaca.markets"
# The calendar lives on the trading host. Paper host on purpose: nothing here trades.
ALPACA_TRADE = "https://paper-api.alpaca.markets"
MASSIVE_BASE = os.environ.get("MASSIVE_API_BASE", "https://api.polygon.io")
TIINGO_BASE = "https://api.tiingo.com"
WEBULL_HOST = os.environ.get("WEBULL_API_HOST", "api.webull.com")
WEBULL_BARS = "/market-data/stocks/bars/list"


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
        self.s.headers.update({"user-agent": "QuantScoutRecoveryDesk/1.1"})
        self.stats = stats

    def request(self, method, url, limiter: RateLimiter, tries=4, headers_fn=None, **kw):
        """headers_fn rebuilds headers per attempt (Webull signatures carry a nonce)."""
        delay = 1.0
        err = ""
        for _ in range(tries):
            limiter.wait()
            if headers_fn:
                kw["headers"] = headers_fn()
            try:
                r = self.s.request(method, url, timeout=20, **kw)
            except requests.RequestException as e:
                err = type(e).__name__
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

    def get(self, url, limiter, headers=None, params=None, tries=4):
        return self.request("GET", url, limiter, tries=tries, headers=headers, params=params)


def _parse_ts(s: str) -> datetime:
    s = s.replace("Z", "+00:00")
    if s.endswith("+0000"):
        s = s[:-5] + "+00:00"
    return datetime.fromisoformat(s).astimezone(ET)


def _bar(t: datetime, o, h, l, c, v, vw=None) -> dict:
    return {"t": t.isoformat(), "date": t.date().isoformat(), "o": float(o), "h": float(h),
            "l": float(l), "c": float(c), "v": float(v or 0), "vw": (float(vw) if vw is not None else None)}


def _chunks(xs: List[str], n: int) -> Iterable[List[str]]:
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def webull_signed_headers(app_key: str, app_secret: str, uri: str, body: str, host: str = WEBULL_HOST,
                          timestamp: Optional[str] = None, nonce: Optional[str] = None) -> Dict[str, str]:
    """HMAC-SHA256 request signature, same algorithm as Webull's official
    webull-openapi-python-sdk 3.0.4 (default_signature_composer). Reimplemented so
    the desk installs no trading SDK."""
    sign = {
        "x-app-key": app_key,
        "x-timestamp": timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "x-signature-version": "1.0",
        "x-signature-algorithm": "HMAC-SHA256",
        "x-signature-nonce": nonce or str(uuid.uuid4()),
    }
    params = {**sign, "host": host}
    parts = "&".join(f"{k}={params[k]}" for k in sorted(params))
    to_sign = f"{uri}&{parts}"
    if body:
        to_sign += "&" + hashlib.sha256(body.encode("utf-8")).hexdigest().upper()
    digest = hmac.new((app_secret + "&").encode("utf-8"), quote(to_sign, safe="").encode("utf-8"), hashlib.sha256)
    return {**sign, "x-signature": base64.b64encode(digest.digest()).decode("ascii")}


class DataHub:
    def __init__(self, env=os.environ):
        self.stats: dict = {"calls": 0, "rate_limited": 0, "errors": []}
        self.http = Http(self.stats)
        self.alpaca_h = {"APCA-API-KEY-ID": env.get("ALPACA_ID", ""),
                         "APCA-API-SECRET-KEY": env.get("ALPACA_SECRET", "")}
        self.massive_key = env.get("MASSIVE_KEY") or env.get("POLYGON_KEY", "")
        self.tiingo_key = env.get("TIINGO_KEY", "")
        self.webull_key = env.get("WEBULL_APP_KEY", "")
        self.webull_secret = env.get("WEBULL_APP_SECRET", "")
        self.webull_token = env.get("WEBULL_ACCESS_TOKEN", "")   # only if your account enforces 2FA tokens
        # Ceilings set below the published limits to leave headroom.
        self.lim = {
            "alpaca": RateLimiter(int(env.get("ALPACA_CALLS_PER_MIN", "1000"))),
            "webull": RateLimiter(int(env.get("WEBULL_CALLS_PER_MIN", "50"))),
            "massive": RateLimiter(int(env.get("MASSIVE_CALLS_PER_MIN", "300"))),
            "tiingo": RateLimiter(int(env.get("TIINGO_CALLS_PER_MIN", "60"))),
        }
        self.feed = env.get("ALPACA_FEED", "sip").lower()
        self.feed_note = ""
        self.intraday_sources: Dict[str, str] = {}

    @property
    def has_alpaca(self):
        return bool(self.alpaca_h["APCA-API-KEY-ID"] and self.alpaca_h["APCA-API-SECRET-KEY"])

    @property
    def has_webull(self):
        return bool(self.webull_key and self.webull_secret)

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
                    self.feed_note = "Alpaca IEX fallback: plan does not permit recent SIP (partial volume)"
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

    # ------------------------------------------------------------ Webull OpenAPI (market data only)
    def webull_bars(self, symbols: List[str], timespan: str, count: int, sessions: Optional[str] = None,
                    start: Optional[datetime] = None) -> Dict[str, List[dict]]:
        """POST /market-data/stocks/bars/list, 20 symbols per call. ETFs need category
        US_ETF, so names missing under US_STOCK are retried once as US_ETF."""
        out: Dict[str, List[dict]] = {}
        if not self.has_webull or not symbols:
            return out
        todo = list(symbols)
        for category in ("US_STOCK", "US_ETF"):
            for chunk in _chunks(todo, 20):
                body = {"symbols": chunk, "category": category, "timespan": timespan, "count": str(count)}
                if sessions:
                    body["trading_sessions"] = sessions
                if start is not None:
                    body["start_time"] = int(start.timestamp() * 1000)
                raw = json.dumps(body, ensure_ascii=False, separators=(",", ":"))

                def headers(raw=raw):
                    h = webull_signed_headers(self.webull_key, self.webull_secret, WEBULL_BARS, raw)
                    h.update({"x-version": "v3", "Content-Type": "application/json"})
                    if self.webull_token:
                        h["x-access-token"] = self.webull_token
                    return h

                sc, j, e = self.http.request("POST", f"https://{WEBULL_HOST}{WEBULL_BARS}", self.lim["webull"],
                                             headers_fn=headers, data=raw.encode("utf-8"))
                if j is None:
                    self._err(f"webull bars {timespan}/{category}: {sc} {e}")
                    continue
                for item in parse_webull_bars(j):
                    if item["bars"]:
                        out[item["symbol"]] = item["bars"]
                        if item["delay_minutes"]:
                            self.stats.setdefault("webull_delay_minutes", item["delay_minutes"])
            todo = [s for s in todo if s not in out]
            if not todo:
                break
        return out

    # ------------------------------------------------------------ Massive / Tiingo / yfinance (daily only)
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
        except ImportError:
            return []                      # not part of the cloud install, by design
        try:
            hist = yf.Ticker(sym).history(start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(),
                                          auto_adjust=False)
        except Exception as e:  # noqa: BLE001 - last-resort source, any failure just means "no data"
            self._err(f"yfinance {sym}: {type(e).__name__}")
            return []
        return [_bar(datetime(ix.year, ix.month, ix.day, tzinfo=ET), r["Open"], r["High"], r["Low"],
                     r["Close"], r["Volume"]) for ix, r in hist.iterrows()]

    # ------------------------------------------------------------ cascades
    def daily_bars(self, symbols: List[str], start: date, end: date, min_bars: int = 60
                   ) -> Tuple[Dict[str, List[dict]], Dict[str, str]]:
        s_dt = datetime(start.year, start.month, start.day, tzinfo=ET)
        e_dt = datetime(end.year, end.month, end.day, 23, 59, tzinfo=ET)
        got = self.alpaca_bars(symbols, "1Day", s_dt, e_dt)
        bars = {s: got[s] for s in got if len(got[s]) >= min_bars}
        source = {s: f"alpaca_{self.feed}" for s in bars}
        missing = [s for s in symbols if s not in bars]
        if missing:
            count = min(1200, (end - start).days)
            for s, rows in self.webull_bars(missing, "D", count).items():
                rows = [b for b in rows if b["date"] <= end.isoformat()]
                if len(rows) >= min_bars:
                    bars[s], source[s] = rows, "webull"
        for sym in [s for s in symbols if s not in bars]:
            for name, fn in (("massive", self.massive_daily), ("tiingo", self.tiingo_daily), ("yfinance", self.yf_daily)):
                rows = fn(sym, start, end)
                if len(rows) >= min_bars:
                    bars[sym], source[sym] = rows, name
                    break
        return bars, source

    def rth_volumes(self, symbols: List[str], start: date, end: date, basis: str = "SIP"
                    ) -> Tuple[Dict[str, Dict[str, float]], Dict[str, str]]:
        """Regular-hours volume per session and its source, one source per symbol.
        SIP: 30-min SIP bars 09:30-16:00 ET. WEBULL: Webull RTH daily bars (the v1.6.2
        fallback; it was the v1.6.1 basis). `basis` picks which one is tried first; the other is the fallback."""
        def sip(syms):
            s_dt = datetime(start.year, start.month, start.day, 9, 30, tzinfo=ET)
            e_dt = datetime(end.year, end.month, end.day, 16, 0, tzinfo=ET)
            vols: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
            for sym, rows in self.alpaca_bars(syms, "30Min", s_dt, e_dt).items():
                for b in rows:
                    t = datetime.fromisoformat(b["t"])
                    if (t.hour, t.minute) >= (9, 30) and t.hour < 16:
                        vols[sym][b["date"]] += b["v"]
            return vols if self.feed == "sip" else {}       # IEX volume is partial: never an RVOL basis

        def webull(syms):
            return {s: {b["date"]: b["v"] for b in rows}
                    for s, rows in self.webull_bars(syms, "D", (end - start).days + 5).items()}

        order = [("WEBULL_RTH", webull), ("SIP_RTH", sip)] if basis.upper() == "WEBULL" else \
                [("SIP_RTH", sip), ("WEBULL_RTH", webull)]
        out: Dict[str, Dict[str, float]] = {}
        src: Dict[str, str] = {}
        for label, fn in order:
            todo = [s for s in symbols if s not in out]
            if not todo:
                break
            for s, v in fn(todo).items():
                if v:
                    out[s], src[s] = dict(v), label
        return out, src

    def intraday_bars(self, symbols: List[str], start: datetime, now: datetime) -> Dict[str, List[dict]]:
        """Today's 5-min bars incl. premarket. Alpaca SIP first; Webull (real time) for
        anything Alpaca did not return, or for everything if Alpaca fell back to IEX."""
        got = dict(self.alpaca_bars(symbols, "5Min", start, now))
        self.intraday_sources = {s: f"alpaca_{self.feed}" for s in got if got[s]}
        missing = [s for s in symbols if not got.get(s)]
        if self.feed != "sip":
            missing = list(symbols)
        if missing and self.has_webull:
            count = min(1200, int((now - start).total_seconds() // 300) + 2)
            for s, rows in self.webull_bars(missing, "M5", count, sessions="PRE,RTH", start=start).items():
                rows = [b for b in rows if b["t"] >= start.isoformat()]
                if rows:
                    got[s], self.intraday_sources[s] = rows, "webull"
        return got


def parse_webull_bars(payload) -> List[dict]:
    """Normalise a bars/list response: [{symbol, result: [bars newest first], delay_minutes}]."""
    items = payload.get("result", payload.get("data", [])) if isinstance(payload, dict) else payload
    out = []
    for it in items or []:
        bars = []
        for b in it.get("result") or []:
            bars.append(_bar(_parse_ts(b["time"]), b["open"], b["high"], b["low"], b["close"], b.get("volume")))
        bars.sort(key=lambda b: b["t"])
        out.append({"symbol": it.get("symbol"), "bars": bars, "delay_minutes": int(it.get("delay_minutes") or 0)})
    return out
