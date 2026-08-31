# -*- coding: utf-8 -*-
"""
QuantScout PRO TERMINAL (v5.5 - AUTO-START / ALWAYS ON)
"""
import streamlit as st
import pandas as pd
import requests
import yfinance as yf
import json
import base64
from datetime import datetime, date
import pytz
from typing import Any, Dict, Optional

try:
    from GoogleNews import GoogleNews
except ImportError:
    GoogleNews = None

try:
    from streamlit_autorefresh import st_autorefresh
except ImportError:
    st_autorefresh = None

# --- PAGE CONFIG ---
st.set_page_config(page_title="QuantScout Cloud", layout="wide", page_icon="🦅")

# --- SECRETS MANAGER ---
def get_secret(key_name):
    if key_name in st.secrets:
        return st.secrets[key_name]
    return ""

# =========================
# LOAD KEYS
# =========================
ALPACA_ID = get_secret("ALPACA_ID")
ALPACA_SECRET = get_secret("ALPACA_SECRET")
POLYGON_KEY = get_secret("POLYGON_KEY")
TIINGO_KEY = get_secret("TIINGO_KEY")
TG_TOKEN = get_secret("TG_TOKEN")
TG_ID = get_secret("TG_ID")
GITHUB_TOKEN = get_secret("GITHUB_TOKEN")

# --- UTILS ---
SESSION = requests.Session()
SESSION.headers.update({"user-agent": "QuantScoutCloud/5.5"})

try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
except ImportError:
    SentimentIntensityAnalyzer = None

def to_float(x: Any) -> Optional[float]:
    try: return float(x) if x is not None else None
    except: return None

def http_get_json(url: str, headers: Optional[Dict]=None, params: Optional[Dict]=None):
    try:
        r = SESSION.get(url, headers=headers, params=params, timeout=5.0)
        if r.status_code >= 400: return r.status_code, None, r.text[:200]
        return r.status_code, r.json(), ""
    except Exception as e:
        return 0, None, str(e)[:200]

def http_post_json(url: str, headers: Optional[Dict]=None, json_body: Optional[Dict]=None):
    try:
        r = SESSION.post(url, headers=headers, json=json_body, timeout=5.0)
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

# =========================
# 60-DAY TRIAL: GitHub-backed durable state (dry-run log + phase clock)
# =========================
# Streamlit's session_state and local disk don't survive a redeploy/restart, so the
# trial's start date and its dry-run decision log are committed to a *separate* branch
# of this repo (never `main`) — committing to main would trigger a Streamlit Cloud
# redeploy on every log entry, restarting the app in a loop.
GITHUB_API = "https://api.github.com"
GITHUB_REPO = "quantscout-nlp/QuantScout-Cloud"
TRIAL_BRANCH = "bot-trial-log"
TRIAL_META_PATH = "trial_state/trial_meta.json"
TRIAL_LOG_PATH = "trial_state/trial_log.jsonl"
TRIAL_DRY_RUN_DAYS = 30
TRIAL_PAPER_DAYS = 30

def gh_headers():
    return {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}

def gh_get_branch_sha(branch):
    sc, j, e = http_get_json(f"{GITHUB_API}/repos/{GITHUB_REPO}/git/ref/heads/{branch}", headers=gh_headers())
    return j.get("object", {}).get("sha") if sc == 200 and j else None

@st.cache_resource
def gh_ensure_trial_branch():
    if not GITHUB_TOKEN: return False
    if gh_get_branch_sha(TRIAL_BRANCH): return True
    base_sha = gh_get_branch_sha("main")
    if not base_sha: return False
    sc, j, e = http_post_json(f"{GITHUB_API}/repos/{GITHUB_REPO}/git/refs", headers=gh_headers(),
                               json_body={"ref": f"refs/heads/{TRIAL_BRANCH}", "sha": base_sha})
    return sc in (200, 201)

def gh_get_file(path):
    sc, j, e = http_get_json(f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}", headers=gh_headers(),
                              params={"ref": TRIAL_BRANCH})
    if sc == 200 and j and "content" in j:
        return base64.b64decode(j["content"]).decode("utf-8"), j["sha"]
    return None, None

def gh_put_file(path, content_text, sha, message):
    body = {
        "message": message,
        "content": base64.b64encode(content_text.encode("utf-8")).decode("ascii"),
        "branch": TRIAL_BRANCH,
    }
    if sha: body["sha"] = sha
    sc, j, e = http_put_json(f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}", headers=gh_headers(), json_body=body)
    return sc in (200, 201), e

@st.cache_resource
def get_trial_start_date():
    """Returns the trial's start date, bootstrapping it to today on first run ever.
    Cached per-container-lifetime so this only hits the GitHub API once, not every pass."""
    if not GITHUB_TOKEN: return None
    gh_ensure_trial_branch()
    content, _ = gh_get_file(TRIAL_META_PATH)
    if content:
        try:
            return date.fromisoformat(json.loads(content)["start_date"])
        except Exception:
            pass
    today = date.today()
    gh_put_file(TRIAL_META_PATH, json.dumps({"start_date": today.isoformat()}), None,
                "Init 60-day trial start date")
    return today

def get_trial_phase(start_date):
    """DRY_RUN (days 0-29): signals computed and logged, no orders touch Alpaca at all.
    PAPER (days 30-59): real orders against Alpaca's paper account (already-built path).
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

# Intentionally hardcoded to Alpaca's PAPER trading endpoint (not configurable via
# secrets) so this bot can never place a live order. ALPACA_ID/ALPACA_SECRET must be
# keys generated from the Alpaca *paper* dashboard — live-account keys will simply
# get a 401 here and orders will fail safely (visible in the TRADE column).
ALPACA_TRADE_BASE = "https://paper-api.alpaca.markets"

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

# --- SMART ALERTS (DND PROTOCOL) ---
def send_telegram_alert_smart(message, token, chat_id):
    if not token or not chat_id: return

    # 1. Force US/Eastern Time
    try:
        est = pytz.timezone('US/Eastern')
        now = datetime.now(est)
    except:
        now = datetime.now() 

    # 2. Quiet Hours (11 PM - 7 AM EST)
    if now.hour >= 23 or now.hour < 7:
        return # Silence

    # 3. Send Message
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try: requests.post(url, json={"chat_id": chat_id, "text": message}, timeout=3)
    except: pass

# --- FETCHERS ---
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

@st.cache_data(ttl=60)
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
        sc, j, e = http_get_json("https://api.tiingo.com/tiingo/news", params={"tickers":symbol,"limit":1,"token":t_key})
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

# --- UI ---
st.title("🦅 QuantScout Cloud v5.5 (Auto-Pilot)")

# Non-blocking client-side refresh (replaces the old blocking time.sleep()+st.rerun()
# loop, which held the server thread/session open indefinitely and, combined with the
# unbounded alert_key growth below, was the likely cause of the long-running crash).
if st_autorefresh:
    st_autorefresh(interval=60_000, key="auto_refresh")
else:
    st.warning("⚠️ streamlit-autorefresh not installed — page will not auto-update.")

# Force Dark Mode Style for Metrics
st.markdown("""
<style>
    .stMetric { background-color: #0e1117; border: 1px solid #303030; padding: 10px; border-radius: 5px; }
</style>
""", unsafe_allow_html=True)

with st.sidebar:
    st.header("⚙️ Settings")
    if not ALPACA_ID:
        st.warning("⚠️ Enter Keys Manually")
        alpaca_id = st.text_input("Alpaca ID", type="password")
        alpaca_secret = st.text_input("Alpaca Secret", type="password")
        polygon_key = st.text_input("Polygon Key", type="password")
        tiingo_key = st.text_input("Tiingo Key", type="password")
        tg_token = st.text_input("Telegram Token", type="password")
        tg_id = st.text_input("Telegram ID", type="password")
    else:
        st.success("🔒 Keys Loaded")
        alpaca_id, alpaca_secret = ALPACA_ID, ALPACA_SECRET
        polygon_key, tiingo_key = POLYGON_KEY, TIINGO_KEY
        tg_token, tg_id = TG_TOKEN, TG_ID

    default_tickers = "TSLA, SNOW, DUOL, ORCL, RDDT, PLTR, CRWV, VST, AMD, AMAT, LYFT, SMCI, LEU, OKLO, OPEN, QS, MU, CRWD, LUNR, SOC, RKLB, ARM, HOOD, COIN, SHOP, SOFI, UBER, DASH, CCJ, TEM, RGTI, IBIT, MRVL, INTC, RIVN, MU, TSM, WULF, ASM, MRVL, HPE, SMR, UEC, FIG, NXE"
    tickers_txt = st.text_area("Watchlist", value=default_tickers, height=300)

    # NO BUTTON - AUTO START LOGIC
    st.info("System is Scanning (Auto-Pilot)")

    st.markdown("---")
    st.subheader("📈 Auto-Trading")
    st.caption("🧪 PAPER mode only — simulated orders, no real capital at risk.")
    enable_trading = st.checkbox("Enable Order Execution", value=True)
    notional_per_trade = st.number_input("Capital per Trade ($)", min_value=10.0, value=500.0, step=50.0)
    max_positions = st.number_input("Max Concurrent Positions", min_value=1, value=8, step=1)

    st.markdown("---")
    st.subheader("🧪 60-Day Trial")
    trial_start = get_trial_start_date()
    phase, trial_day = get_trial_phase(trial_start)
    if trial_start is None:
        st.error("No GITHUB_TOKEN secret — trial clock/log can't persist. Forcing DRY RUN (no orders will be placed) until it's configured.")
    elif phase == "DRY_RUN":
        st.info(f"Day {trial_day + 1} of {TRIAL_DRY_RUN_DAYS} — **DRY RUN**\nSignals logged, no orders placed.\nStarted {trial_start.isoformat()}")
    elif phase == "PAPER":
        st.success(f"Day {trial_day + 1 - TRIAL_DRY_RUN_DAYS} of {TRIAL_PAPER_DAYS} — **PAPER TRADING**\nReal orders against Alpaca's paper account.")
    else:
        st.warning(f"Day {trial_day + 1} — **TRIAL COMPLETE**\nNo new positions will open. Existing paper positions can still be closed. Review the 60-day log before deciding on live trading.")

# --- MAIN LOOP (Always Runs) ---
tickers = [t.strip().upper() for t in tickers_txt.split(",") if t.strip()]
rows = []

trading_base_ready = enable_trading and alpaca_id and alpaca_secret
can_open_new = trading_base_ready and phase == "PAPER"
can_close = trading_base_ready and phase in ("PAPER", "REVIEW")

# Snapshot of currently-held positions for this scan pass. Updated locally as orders
# are submitted below so we don't buy the same name twice or oversell within one pass.
trial_log_text, trial_log_sha = (gh_get_file(TRIAL_LOG_PATH) if GITHUB_TOKEN else (None, None))
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

# Progress spinner
with st.spinner(f"Scanning {len(tickers)} tickers..."):
    for sym in tickers:
        try:
            price, src = fetch_alpaca_price(sym, alpaca_id, alpaca_secret)
            if not price: price, src = fetch_polygon_price(sym, polygon_key)
            sma20, rsi, err = fetch_indicators_hybrid(sym, alpaca_id, alpaca_secret)
            sent, headline = fetch_news_hybrid(sym, tiingo_key)

            decision, conf = "HOLD", 0.0
            if price and rsi > 0:
                if price > sma20 and rsi < 70 and sent > 0.15: decision, conf = "BUY", 0.8 + (sent * 0.1)
                elif price < sma20 and rsi > 30 and sent < -0.2: decision, conf = "SELL", 0.8
                # Oversold mean-reversion buy: only a short-term dip within an intact
                # trend (price no more than 3% below its 20-day average), and only when
                # news isn't actively bearish. Without the price check this still fired
                # on a confirmed downtrend (price well below SMA20) any time sentiment
                # was merely neutral/unavailable — a falling knife, just not on bad news.
                elif rsi < 35 and price >= sma20 * 0.97 and sent >= -0.1: decision, conf = "BUY", 0.5 + max(0.0, sent) * 0.1

            trade_note = "-"
            if decision == "BUY":
                if phase == "DRY_RUN":
                    if sym in open_symbols:
                        trade_note = "Already Held (sim)"
                    elif len(open_symbols) >= max_positions:
                        trade_note = "Max Positions (sim)"
                    else:
                        open_symbols.add(sym)
                        trade_note = "🧪 Would BUY (dry run)"
                        new_log_rows.append({"ts": datetime.utcnow().isoformat(), "mode": "DRY_RUN", "action": "BUY",
                                              "ticker": sym, "price": price, "rsi": round(rsi, 2), "sma20": round(sma20, 2),
                                              "sentiment": round(sent, 3), "conf": round(conf, 2)})
                elif phase == "REVIEW":
                    trade_note = "⏸ Trial Frozen (Review)"
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
                        trade_note = f"✅ Bought ${notional_per_trade:.0f}"
                        new_log_rows.append({"ts": datetime.utcnow().isoformat(), "mode": "PAPER", "action": "BUY",
                                              "ticker": sym, "price": price, "rsi": round(rsi, 2), "sma20": round(sma20, 2),
                                              "sentiment": round(sent, 3), "conf": round(conf, 2), "notional": notional_per_trade})
                    else:
                        trade_note = f"⚠️ Buy Failed: {e or sc}"
            elif decision == "SELL":
                if phase == "DRY_RUN":
                    if sym not in open_symbols:
                        trade_note = "No Position (sim)"
                    else:
                        open_symbols.discard(sym)
                        trade_note = "🧪 Would SELL (dry run)"
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
                        trade_note = "✅ Sold (Closed)"
                        new_log_rows.append({"ts": datetime.utcnow().isoformat(), "mode": "PAPER", "action": "SELL",
                                              "ticker": sym, "price": price, "rsi": round(rsi, 2), "sma20": round(sma20, 2),
                                              "sentiment": round(sent, 3), "conf": round(conf, 2)})
                    else:
                        trade_note = f"⚠️ Sell Failed: {e or sc}"

            if decision != "HOLD":
                alert_log = st.session_state.setdefault("alert_log", {})
                alert_key = f"{sym}_{decision}"
                today = date.today().isoformat()
                if alert_log.get(alert_key) != today:
                    msg = f"🦅 CLOUD ALERT\n{decision} {sym}\n${price} | RSI: {rsi:.1f} | Conf: {conf:.2f}\nTrade: {trade_note}\n{headline}"
                    send_telegram_alert_smart(msg, tg_token, tg_id)
                    alert_log[alert_key] = today

            rows.append({"TICKER": sym, "PRICE": price, "RSI": round(rsi,1), "SIGNAL": decision, "CONF": round(conf,2), "TRADE": trade_note, "NEWS": headline})
        except: pass

# One commit per scan pass (not per ticker) for every new dry-run/paper log entry,
# to keep commit volume on the trial branch reasonable.
if new_log_rows and GITHUB_TOKEN:
    new_lines = "\n".join(json.dumps(r) for r in new_log_rows)
    updated_text = (trial_log_text.rstrip("\n") + "\n" + new_lines) if trial_log_text else new_lines
    gh_put_file(TRIAL_LOG_PATH, updated_text + "\n", trial_log_sha,
                f"Trial log: {len(new_log_rows)} new entr{'y' if len(new_log_rows) == 1 else 'ies'}")

# --- DISPLAY HUD ---
if rows:
    df = pd.DataFrame(rows)
    
    buys = len(df[df["SIGNAL"] == "BUY"])
    sells = len(df[df["SIGNAL"] == "SELL"])
    avg_rsi = df["RSI"].mean() if "RSI" in df.columns else 0.0
    
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Active Tickers", len(tickers))
    m2.metric("Buy Signals", buys)
    m3.metric("Sell Signals", sells)
    m4.metric("Market RSI (Avg)", round(avg_rsi, 1))
    m5.metric(f"Open Positions ({phase})", len(open_symbols))
    
    st.markdown("---")

    def color_signal(val):
        return 'background-color: #1b4d3e' if val == 'BUY' else 'background-color: #4d1b1b' if val == 'SELL' else ''
    
    st.dataframe(
        df.style.applymap(color_signal, subset=['SIGNAL']),
        use_container_width=True,
        height=600
    )
