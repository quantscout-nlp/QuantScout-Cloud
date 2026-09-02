# -*- coding: utf-8 -*-
"""
QuantScout engine: fetching, decision logic, trade execution, and GitHub-backed
trial persistence. No Streamlit dependency — importable by both the interactive
dashboard (quantscout_cloud.py, read-only for trading) and the headless scheduled
job (scripts/run_scan.py, the sole place that actually submits orders and writes
the trial log) so the two can never race each other or drift apart in logic.
"""
import base64
import json
from datetime import date, datetime
from typing import Any, Dict, Optional

import pandas as pd
import requests

try:
    import yfinance as yf
except ImportError:
    yf = None

try:
    from GoogleNews import GoogleNews
except ImportError:
    GoogleNews = None

try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
except ImportError:
    SentimentIntensityAnalyzer = None

SESSION = requests.Session()
SESSION.headers.update({"user-agent": "QuantScoutCloud/5.6"})

DEFAULT_TICKERS = [
    "TSLA", "SNOW", "DUOL", "ORCL", "RDDT", "PLTR", "CRWV", "VST", "AMD", "AMAT",
    "LYFT", "SMCI", "LEU", "OKLO", "OPEN", "QS", "MU", "CRWD", "LUNR", "SOC",
    "RKLB", "ARM", "HOOD", "COIN", "SHOP", "SOFI", "UBER", "DASH", "CCJ", "TEM",
    "RGTI", "IBIT", "MRVL", "INTC", "RIVN", "TSM", "WULF", "ASM", "HPE", "SMR",
    "UEC", "FIG", "NXE",
]

# Intentionally hardcoded to Alpaca's PAPER trading endpoint (never configurable)
# so this engine can never place a live order regardless of what keys it's given.
ALPACA_TRADE_BASE = "https://paper-api.alpaca.markets"

# --- GitHub-backed durable state (dry-run log, trial clock, alert dedup) ---
# Committed to a dedicated branch, never `main` — committing to the deploy branch
# would trigger a Streamlit Cloud redeploy on every entry.
GITHUB_API = "https://api.github.com"
GITHUB_REPO = "quantscout-nlp/QuantScout-Cloud"
TRIAL_BRANCH = "bot-trial-log"
TRIAL_META_PATH = "trial_state/trial_meta.json"
TRIAL_LOG_PATH = "trial_state/trial_log.jsonl"
ALERT_STATE_PATH = "trial_state/alert_state.json"
TRIAL_DRY_RUN_DAYS = 30
TRIAL_PAPER_DAYS = 30

_trial_branch_ready = False
_trial_start_cache = None


# --- HTTP helpers ---
def to_float(x: Any) -> Optional[float]:
    try: return float(x) if x is not None else None
    except: return None

def http_get_json(url: str, headers: Optional[Dict]=None, params: Optional[Dict]=None):
    try:
        r = SESSION.get(url, headers=headers, params=params, timeout=10.0)
        if r.status_code >= 400: return r.status_code, None, r.text[:200]
        return r.status_code, r.json(), ""
    except Exception as e:
        return 0, None, str(e)[:200]

def http_post_json(url: str, headers: Optional[Dict]=None, json_body: Optional[Dict]=None):
    try:
        r = SESSION.post(url, headers=headers, json=json_body, timeout=10.0)
        if r.status_code >= 400: return r.status_code, None, r.text[:200]
        return r.status_code, r.json(), ""
    except Exception as e:
        return 0, None, str(e)[:200]

def http_put_json(url: str, headers: Optional[Dict]=None, json_body: Optional[Dict]=None):
    try:
        r = SESSION.put(url, headers=headers, json=json_body, timeout=10.0)
        if r.status_code >= 400: return r.status_code, None, r.text[:200]
        return r.status_code, r.json(), ""
    except Exception as e:
        return 0, None, str(e)[:200]


# --- Alpaca / Polygon / yfinance fetchers ---
def fetch_alpaca_price(symbol, kid, sec):
    if not kid or not sec: return None, "No Keys"
    h = {"APCA-API-KEY-ID": kid, "APCA-API-SECRET-KEY": sec}
    sc, j, e = http_get_json(f"https://data.alpaca.markets/v2/stocks/{symbol}/trades/latest", headers=h)
    if j and isinstance(j, dict) and "trade" in j and j["trade"]:
        return to_float(j["trade"]["p"]), "Alpaca"
    return None, e

def fetch_polygon_price(symbol, key):
    if not key: return None, "No Key"
    sc, j, e = http_get_json(f"https://api.polygon.io/v2/last/trade/{symbol}", params={"apiKey": key})
    if j and isinstance(j, dict) and "results" in j and j["results"]:
        return to_float(j["results"]["p"]), "Polygon"
    return None, e

def fetch_indicators_hybrid(symbol, kid, sec):
    rsi, sma20 = 0.0, 0.0
    if kid and sec:
        h = {"APCA-API-KEY-ID": kid, "APCA-API-SECRET-KEY": sec}
        params = {"timeframe": "1Day", "limit": 50, "feed": "iex"}
        sc, j, e = http_get_json(f"https://data.alpaca.markets/v2/stocks/{symbol}/bars", headers=h, params=params)
        if j and isinstance(j, dict) and "bars" in j and j["bars"]:
            bars = j["bars"]
            if len(bars) > 20:
                closes = pd.Series([b["c"] for b in bars])
                delta = closes.diff()
                up, down = delta.clip(lower=0), -delta.clip(upper=0)
                # adjust=False reproduces Wilder's original recursive smoothing;
                # pandas' default (adjust=True) is a different, biased average.
                rs = up.ewm(alpha=1/14, adjust=False).mean() / down.ewm(alpha=1/14, adjust=False).mean()
                rsi = 100 - (100/(1+rs)).iloc[-1]
                sma20 = closes.rolling(20).mean().iloc[-1]
                return float(sma20), float(rsi), ""
    if yf is not None:
        try:
            hist = yf.Ticker(symbol).history(period="3mo")
            if not hist.empty and len(hist) > 20:
                closes = hist["Close"]
                delta = closes.diff()
                up, down = delta.clip(lower=0), -delta.clip(upper=0)
                rs = up.ewm(alpha=1/14, adjust=False).mean() / down.ewm(alpha=1/14, adjust=False).mean()
                rsi = 100 - (100/(1+rs)).iloc[-1]
                sma20 = closes.rolling(20).mean().iloc[-1]
                return float(sma20), float(rsi), ""
        except: pass
    return 0.0, 0.0, "No Data"

def fetch_news_hybrid(symbol, t_key):
    if not SentimentIntensityAnalyzer: return 0.0, "VADER Missing"
    analyzer = SentimentIntensityAnalyzer()
    if t_key:
        sc, j, e = http_get_json("https://api.tiingo.com/tiingo/news", params={"tickers": symbol, "limit": 1, "token": t_key})
        if j and isinstance(j, list) and len(j) > 0:
            title = j[0].get("title", "")
            return analyzer.polarity_scores(title).get("compound", 0.0), f"[Tiingo] {title}"
    if GoogleNews:
        try:
            goog = GoogleNews(lang='en', period='1d')
            goog.search(f"{symbol} stock news")
            results = goog.result()
            if results and len(results) > 0:
                title = results[0].get("title", "")
                return analyzer.polarity_scores(title).get("compound", 0.0), f"[Google] {title}"
        except: pass
    return 0.0, "No Data"


# --- Alpaca trading (paper only) ---
def get_open_positions(kid, sec):
    if not kid or not sec: return {}
    h = {"APCA-API-KEY-ID": kid, "APCA-API-SECRET-KEY": sec}
    sc, j, e = http_get_json(f"{ALPACA_TRADE_BASE}/v2/positions", headers=h)
    if sc == 200 and isinstance(j, list):
        return {p["symbol"]: p.get("qty", "0") for p in j}
    return {}

def submit_market_order(symbol, side, kid, sec, notional=None, qty=None):
    h = {"APCA-API-KEY-ID": kid, "APCA-API-SECRET-KEY": sec}
    body = {"symbol": symbol, "side": side, "type": "market", "time_in_force": "day"}
    if qty is not None:
        body["qty"] = str(qty)
    else:
        body["notional"] = str(notional)
    return http_post_json(f"{ALPACA_TRADE_BASE}/v2/orders", headers=h, json_body=body)


# --- Telegram ---
def send_telegram_alert_smart(message, token, chat_id):
    if not token or not chat_id: return
    import pytz
    try:
        est = pytz.timezone('US/Eastern')
        now = datetime.now(est)
    except:
        now = datetime.now()
    if now.hour >= 23 or now.hour < 7:
        return  # Quiet hours 11pm-7am ET
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try: requests.post(url, json={"chat_id": chat_id, "text": message}, timeout=5)
    except: pass


# --- Decision logic ---
# Only the two primary, trend-confirmed signals fire trades: BUY requires an actual
# uptrend + healthy momentum + positive sentiment; SELL requires an actual downtrend
# + momentum breaking down + negative sentiment. The oversold mean-reversion "buy
# the shallow dip" branch (RSI<35 alone) was removed on request — it's a real,
# independently-legitimate signal type, but a weaker/secondary one than the two
# trend-confirmed rules, and shouldn't be firing trades alongside them.
def decide(price, sma20, rsi, sent):
    decision, conf = "HOLD", 0.0
    if price and rsi > 0:
        if price > sma20 and rsi < 70 and sent > 0.15:
            decision, conf = "BUY", 0.8 + (sent * 0.1)
        elif price < sma20 and rsi > 30 and sent < -0.2:
            decision, conf = "SELL", 0.8
    return decision, conf


# --- GitHub-backed trial persistence ---
def gh_headers(github_token):
    return {"Authorization": f"Bearer {github_token}", "Accept": "application/vnd.github+json"}

def gh_get_branch_sha(branch, github_token):
    sc, j, e = http_get_json(f"{GITHUB_API}/repos/{GITHUB_REPO}/git/ref/heads/{branch}", headers=gh_headers(github_token))
    return j.get("object", {}).get("sha") if sc == 200 and j else None

def gh_ensure_trial_branch(github_token):
    global _trial_branch_ready
    if not github_token: return False
    if _trial_branch_ready: return True
    if gh_get_branch_sha(TRIAL_BRANCH, github_token):
        _trial_branch_ready = True
        return True
    base_sha = gh_get_branch_sha("main", github_token)
    if not base_sha: return False
    sc, j, e = http_post_json(f"{GITHUB_API}/repos/{GITHUB_REPO}/git/refs", headers=gh_headers(github_token),
                               json_body={"ref": f"refs/heads/{TRIAL_BRANCH}", "sha": base_sha})
    _trial_branch_ready = sc in (200, 201)
    return _trial_branch_ready

def gh_get_file(path, github_token):
    sc, j, e = http_get_json(f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}", headers=gh_headers(github_token),
                              params={"ref": TRIAL_BRANCH})
    if sc == 200 and j and "content" in j:
        return base64.b64decode(j["content"]).decode("utf-8"), j["sha"]
    return None, None

def gh_put_file(path, content_text, sha, message, github_token):
    body = {
        "message": message,
        "content": base64.b64encode(content_text.encode("utf-8")).decode("ascii"),
        "branch": TRIAL_BRANCH,
    }
    if sha: body["sha"] = sha
    sc, j, e = http_put_json(f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}", headers=gh_headers(github_token), json_body=body)
    return sc in (200, 201), e

def get_trial_start_date(github_token, force_refresh=False):
    """Bootstraps the trial start date to today on first-ever call. Memoized for the
    life of the process so repeated calls don't keep hitting the GitHub API."""
    global _trial_start_cache
    if not github_token: return None
    if _trial_start_cache is not None and not force_refresh:
        return _trial_start_cache
    gh_ensure_trial_branch(github_token)
    content, _ = gh_get_file(TRIAL_META_PATH, github_token)
    if content:
        try:
            _trial_start_cache = date.fromisoformat(json.loads(content)["start_date"])
            return _trial_start_cache
        except Exception:
            pass
    today = date.today()
    gh_put_file(TRIAL_META_PATH, json.dumps({"start_date": today.isoformat()}), None,
                "Init 60-day trial start date", github_token)
    _trial_start_cache = today
    return today

def get_trial_phase(start_date):
    """DRY_RUN (days 0-29): signals computed and logged, no orders touch Alpaca at all.
    PAPER (days 30-59): real orders against Alpaca's paper account.
    REVIEW (day 60+): frozen — no new positions opened; existing ones may still be
    closed. Promotion to live trading is never automatic; it's a separate decision."""
    if start_date is None: return "DRY_RUN", None
    days = (date.today() - start_date).days
    if days < TRIAL_DRY_RUN_DAYS: return "DRY_RUN", days
    if days < TRIAL_DRY_RUN_DAYS + TRIAL_PAPER_DAYS: return "PAPER", days
    return "REVIEW", days

def derive_simulated_positions(existing_log_text):
    """Replays the dry-run log to recover which symbols are 'simulated-held', since
    dry-run trades never touch Alpaca's real (paper) position list."""
    held = set()
    if not existing_log_text: return held
    for line in existing_log_text.splitlines():
        line = line.strip()
        if not line: continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if row.get("mode") != "DRY_RUN": continue
        if row.get("action") == "BUY": held.add(row.get("ticker"))
        elif row.get("action") == "SELL": held.discard(row.get("ticker"))
    return held

def load_alert_state(github_token):
    if not github_token: return {}, None
    content, sha = gh_get_file(ALERT_STATE_PATH, github_token)
    if content:
        try:
            return json.loads(content), sha
        except Exception:
            pass
    return {}, sha


# --- Orchestration: the ONE place that fetches, decides, trades, and logs ---
def run_scan(tickers, creds: Dict, config: Dict):
    """Runs one full scan-decide-trade-log pass. This is the only function that
    submits Alpaca orders or writes to the trial log/alert state — call it from
    exactly one place at a time (the scheduled headless job), never concurrently
    from an interactive dashboard, or you risk double-submitted orders.
    """
    alpaca_id = creds.get("alpaca_id")
    alpaca_secret = creds.get("alpaca_secret")
    polygon_key = creds.get("polygon_key")
    tiingo_key = creds.get("tiingo_key")
    tg_token = creds.get("tg_token")
    tg_id = creds.get("tg_id")
    github_token = creds.get("github_token")

    enable_trading = config.get("enable_trading", True)
    notional_per_trade = config.get("notional_per_trade", 500.0)
    max_positions = config.get("max_positions", 8)

    trial_start = get_trial_start_date(github_token)
    phase, trial_day = get_trial_phase(trial_start)
    trading_base_ready = bool(enable_trading and alpaca_id and alpaca_secret)
    can_open_new = trading_base_ready and phase == "PAPER"
    can_close = trading_base_ready and phase in ("PAPER", "REVIEW")

    trial_log_text, trial_log_sha = (gh_get_file(TRIAL_LOG_PATH, github_token) if github_token else (None, None))
    alert_state, alert_state_sha = load_alert_state(github_token) if github_token else ({}, None)

    if phase == "DRY_RUN":
        open_positions = {}
        open_symbols = derive_simulated_positions(trial_log_text)
    elif trading_base_ready:
        open_positions = get_open_positions(alpaca_id, alpaca_secret)
        open_symbols = set(open_positions.keys())
    else:
        open_positions = {}
        open_symbols = set()

    new_log_rows = []
    rows = []
    today_iso = date.today().isoformat()

    for sym in tickers:
        try:
            price, src = fetch_alpaca_price(sym, alpaca_id, alpaca_secret)
            if not price: price, src = fetch_polygon_price(sym, polygon_key)
            sma20, rsi, err = fetch_indicators_hybrid(sym, alpaca_id, alpaca_secret)
            sent, headline = fetch_news_hybrid(sym, tiingo_key)

            decision, conf = decide(price, sma20, rsi, sent)

            trade_note = "-"
            if decision == "BUY":
                if phase == "DRY_RUN":
                    if sym in open_symbols:
                        trade_note = "Already Held (sim)"
                    elif len(open_symbols) >= max_positions:
                        trade_note = "Max Positions (sim)"
                    else:
                        open_symbols.add(sym)
                        trade_note = "Would BUY (dry run)"
                        new_log_rows.append({"ts": datetime.utcnow().isoformat(), "mode": "DRY_RUN", "action": "BUY",
                                              "ticker": sym, "price": price, "rsi": round(rsi, 2), "sma20": round(sma20, 2),
                                              "sentiment": round(sent, 3), "conf": round(conf, 2)})
                elif phase == "REVIEW":
                    trade_note = "Trial Frozen (Review)"
                elif not can_open_new:
                    trade_note = "Trading Off"
                elif sym in open_symbols:
                    trade_note = "Already Held"
                elif len(open_symbols) >= max_positions:
                    trade_note = "Max Positions"
                else:
                    sc, j, e = submit_market_order(sym, "buy", alpaca_id, alpaca_secret, notional=notional_per_trade)
                    if sc in (200, 201):
                        open_symbols.add(sym)
                        trade_note = f"Bought ${notional_per_trade:.0f}"
                        new_log_rows.append({"ts": datetime.utcnow().isoformat(), "mode": "PAPER", "action": "BUY",
                                              "ticker": sym, "price": price, "rsi": round(rsi, 2), "sma20": round(sma20, 2),
                                              "sentiment": round(sent, 3), "conf": round(conf, 2), "notional": notional_per_trade})
                    else:
                        trade_note = f"Buy Failed: {e or sc}"
            elif decision == "SELL":
                if phase == "DRY_RUN":
                    if sym not in open_symbols:
                        trade_note = "No Position (sim)"
                    else:
                        open_symbols.discard(sym)
                        trade_note = "Would SELL (dry run)"
                        new_log_rows.append({"ts": datetime.utcnow().isoformat(), "mode": "DRY_RUN", "action": "SELL",
                                              "ticker": sym, "price": price, "rsi": round(rsi, 2), "sma20": round(sma20, 2),
                                              "sentiment": round(sent, 3), "conf": round(conf, 2)})
                elif not can_close:
                    trade_note = "Trading Off"
                elif sym not in open_symbols:
                    trade_note = "No Position"
                else:
                    sc, j, e = submit_market_order(sym, "sell", alpaca_id, alpaca_secret, qty=open_positions.get(sym))
                    if sc in (200, 201):
                        open_symbols.discard(sym)
                        trade_note = "Sold (Closed)"
                        new_log_rows.append({"ts": datetime.utcnow().isoformat(), "mode": "PAPER", "action": "SELL",
                                              "ticker": sym, "price": price, "rsi": round(rsi, 2), "sma20": round(sma20, 2),
                                              "sentiment": round(sent, 3), "conf": round(conf, 2)})
                    else:
                        trade_note = f"Sell Failed: {e or sc}"

            if decision != "HOLD":
                alert_key = f"{sym}_{decision}"
                if alert_state.get(alert_key) != today_iso:
                    msg = f"QuantScout Cloud [{phase}]\n{decision} {sym}\n${price} | RSI: {rsi:.1f} | Conf: {conf:.2f}\nTrade: {trade_note}\n{headline}"
                    send_telegram_alert_smart(msg, tg_token, tg_id)
                    alert_state[alert_key] = today_iso

            rows.append({"TICKER": sym, "PRICE": price, "RSI": round(rsi, 1), "SIGNAL": decision,
                         "CONF": round(conf, 2), "TRADE": trade_note, "NEWS": headline})
        except Exception:
            pass

    if github_token:
        if new_log_rows:
            new_lines = "\n".join(json.dumps(r) for r in new_log_rows)
            updated_text = (trial_log_text.rstrip("\n") + "\n" + new_lines) if trial_log_text else new_lines
            gh_put_file(TRIAL_LOG_PATH, updated_text + "\n", trial_log_sha,
                        f"Trial log: {len(new_log_rows)} new entr{'y' if len(new_log_rows) == 1 else 'ies'}", github_token)
        gh_put_file(ALERT_STATE_PATH, json.dumps(alert_state), alert_state_sha, "Update alert dedup state", github_token)

    return {
        "rows": rows,
        "new_log_rows": new_log_rows,
        "phase": phase,
        "trial_day": trial_day,
        "trial_start": trial_start,
        "open_symbols": open_symbols,
    }
