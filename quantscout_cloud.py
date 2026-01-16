# -*- coding: utf-8 -*-
"""
QuantScout PRO TERMINAL (v5.4 - AUTO-START / ALWAYS ON)
"""
import streamlit as st
import pandas as pd
import requests
import time
import yfinance as yf
from GoogleNews import GoogleNews
from datetime import datetime
import pytz 
from typing import Any, Dict, Optional

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
SESSION.headers.update({"user-agent": "QuantScoutCloud/5.4"})

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
                rs = up.ewm(alpha=1/14).mean() / down.ewm(alpha=1/14).mean()
                rsi = 100 - (100/(1+rs)).iloc[-1]
                sma20 = closes.rolling(20).mean().iloc[-1]
                return float(sma20), float(rsi), ""
    try:
        hist = yf.Ticker(symbol).history(period="3mo")
        if not hist.empty and len(hist) > 20:
            closes = hist["Close"]
            delta = closes.diff()
            up, down = delta.clip(lower=0), -delta.clip(upper=0)
            rs = up.ewm(alpha=1/14).mean() / down.ewm(alpha=1/14).mean()
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
st.title("🦅 QuantScout Cloud v5.4 (Auto-Pilot)")

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

# --- MAIN LOOP (Always Runs) ---
tickers = [t.strip().upper() for t in tickers_txt.split(",") if t.strip()]
rows = []

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
                elif rsi < 35: decision, conf = "BUY", 0.5

            if decision != "HOLD":
                alert_key = f"{sym}_{decision}_{datetime.now().strftime('%H:%M')}"
                if alert_key not in st.session_state:
                    msg = f"🦅 CLOUD ALERT\n{decision} {sym}\n${price} | RSI: {rsi:.1f}\n{headline}"
                    send_telegram_alert_smart(msg, tg_token, tg_id) 
                    st.session_state[alert_key] = True

            rows.append({"TICKER": sym, "PRICE": price, "RSI": round(rsi,1), "SIGNAL": decision, "NEWS": headline})
        except: pass

# --- DISPLAY HUD ---
if rows:
    df = pd.DataFrame(rows)
    
    buys = len(df[df["SIGNAL"] == "BUY"])
    sells = len(df[df["SIGNAL"] == "SELL"])
    avg_rsi = df["RSI"].mean() if "RSI" in df.columns else 0.0
    
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Active Tickers", len(tickers))
    m2.metric("Buy Signals", buys)
    m3.metric("Sell Signals", sells)
    m4.metric("Market RSI (Avg)", round(avg_rsi, 1))
    
    st.markdown("---")

    def color_signal(val):
        return 'background-color: #1b4d3e' if val == 'BUY' else 'background-color: #4d1b1b' if val == 'SELL' else ''
    
    st.dataframe(
        df.style.applymap(color_signal, subset=['SIGNAL']), 
        use_container_width=True, 
        height=600
    )

# --- AUTO REFRESH ---
time.sleep(60)
st.rerun()
