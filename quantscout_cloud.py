# -*- coding: utf-8 -*-
"""
QuantScout PRO TERMINAL (v5.5 - AUTO-START / ALWAYS ON)
"""
import streamlit as st
import pandas as pd
import requests
import yfinance as yf
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

# --- MAIN LOOP (Always Runs) ---
tickers = [t.strip().upper() for t in tickers_txt.split(",") if t.strip()]
rows = []

trading_ready = enable_trading and alpaca_id and alpaca_secret
# Snapshot of currently-held positions for this scan pass. Updated locally as orders
# are submitted below so we don't buy the same name twice or oversell within one pass.
open_positions = get_open_positions(alpaca_id, alpaca_secret) if trading_ready else {}
open_symbols = set(open_positions.keys())

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
                # Oversold mean-reversion buy: only take it when news isn't actively
                # bearish, otherwise this was catching falling knives in confirmed
                # downtrends with negative sentiment (contradicts the SELL rule above).
                elif rsi < 35 and sent >= -0.1: decision, conf = "BUY", 0.5 + max(0.0, sent) * 0.1

            trade_note = "-"
            if decision == "BUY":
                if not trading_ready:
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
                    else:
                        trade_note = f"⚠️ Buy Failed: {e or sc}"
            elif decision == "SELL":
                if not trading_ready:
                    trade_note = "Trading Off"
                elif sym not in open_symbols:
                    trade_note = "No Position"
                else:
                    sc, j, e = submit_market_order(sym, "sell", alpaca_id, alpaca_secret, qty=open_positions.get(sym))
                    if sc in (200, 201):
                        open_symbols.discard(sym)
                        trade_note = "✅ Sold (Closed)"
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
    m5.metric("Open Positions (Paper)", len(open_symbols) if trading_ready else "Off")
    
    st.markdown("---")

    def color_signal(val):
        return 'background-color: #1b4d3e' if val == 'BUY' else 'background-color: #4d1b1b' if val == 'SELL' else ''
    
    st.dataframe(
        df.style.applymap(color_signal, subset=['SIGNAL']),
        use_container_width=True,
        height=600
    )
