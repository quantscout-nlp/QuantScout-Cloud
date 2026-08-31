# -*- coding: utf-8 -*-
"""
QuantScout Cloud Dashboard (v6.0 - READ-ONLY VIEWER)

This dashboard shows a live signal preview and the authoritative trial state, but
it does NOT place orders or write to the trial log itself. The scheduled GitHub
Actions job (scripts/run_scan.py, using engine.py) is the sole place that submits
Alpaca orders and advances the trial log — running trading logic here too would
let this dashboard and that scheduled job race each other into double-submitted
orders. See engine.py for the shared fetch/decide/trade logic.
"""
import streamlit as st
import pandas as pd
import json

import engine

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

ALPACA_ID = get_secret("ALPACA_ID")
ALPACA_SECRET = get_secret("ALPACA_SECRET")
POLYGON_KEY = get_secret("POLYGON_KEY")
TIINGO_KEY = get_secret("TIINGO_KEY")
GITHUB_TOKEN = get_secret("GITHUB_TOKEN")

# --- UI ---
st.title("🦅 QuantScout Cloud (Live Signal Viewer)")

if st_autorefresh:
    st_autorefresh(interval=60_000, key="auto_refresh")
else:
    st.warning("⚠️ streamlit-autorefresh not installed — page will not auto-update.")

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
    else:
        st.success("🔒 Keys Loaded")
        alpaca_id, alpaca_secret = ALPACA_ID, ALPACA_SECRET
        polygon_key, tiingo_key = POLYGON_KEY, TIINGO_KEY

    tickers_txt = st.text_area("Watchlist (preview only)", value=", ".join(engine.DEFAULT_TICKERS), height=300)
    st.caption(
        "This only changes what's previewed below. The actual traded watchlist is "
        "set via the WATCHLIST repository variable on the scheduled GitHub Actions job."
    )

    st.markdown("---")
    st.subheader("📈 Trading")
    st.info(
        "This dashboard is **read-only** — it never places orders. All trading runs "
        "on a schedule via GitHub Actions (`.github/workflows/trading-engine.yml`), "
        "configured with its own repository variables (ENABLE_TRADING, "
        "NOTIONAL_PER_TRADE, MAX_POSITIONS)."
    )

    st.markdown("---")
    st.subheader("🧪 60-Day Trial")
    trial_start = engine.get_trial_start_date(GITHUB_TOKEN)
    phase, trial_day = engine.get_trial_phase(trial_start)
    if trial_start is None:
        st.error("No GITHUB_TOKEN secret — trial clock/log can't be read here (the scheduled engine fails safe into DRY_RUN until it's configured).")
    elif phase == "DRY_RUN":
        st.info(f"Day {trial_day + 1} of {engine.TRIAL_DRY_RUN_DAYS} — **DRY RUN**\nSignals logged, no orders placed.\nStarted {trial_start.isoformat()}")
    elif phase == "PAPER":
        st.success(f"Day {trial_day + 1 - engine.TRIAL_DRY_RUN_DAYS} of {engine.TRIAL_PAPER_DAYS} — **PAPER TRADING**\nReal orders against Alpaca's paper account.")
    else:
        st.warning(f"Day {trial_day + 1} — **TRIAL COMPLETE**\nNo new positions open. Existing paper positions can still be closed. Review the 60-day log before deciding on live trading.")

# --- Authoritative state (read-only) ---
trial_log_text, _ = (engine.gh_get_file(engine.TRIAL_LOG_PATH, GITHUB_TOKEN) if GITHUB_TOKEN else (None, None))
if phase == "DRY_RUN":
    open_symbols = engine.derive_simulated_positions(trial_log_text)
elif alpaca_id and alpaca_secret:
    open_symbols = set(engine.get_open_positions(alpaca_id, alpaca_secret).keys())
else:
    open_symbols = set()

# --- Live signal preview (read-only: fetches + decides, submits nothing, logs nothing) ---
tickers = [t.strip().upper() for t in tickers_txt.split(",") if t.strip()]
rows = []
with st.spinner(f"Scanning {len(tickers)} tickers..."):
    for sym in tickers:
        try:
            price, src = engine.fetch_alpaca_price(sym, alpaca_id, alpaca_secret)
            if not price: price, src = engine.fetch_polygon_price(sym, polygon_key)
            sma20, rsi, err = engine.fetch_indicators_hybrid(sym, alpaca_id, alpaca_secret)
            sent, headline = engine.fetch_news_hybrid(sym, tiingo_key)
            decision, conf = engine.decide(price, sma20, rsi, sent)
            held_note = "Held" if sym in open_symbols else "-"
            rows.append({"TICKER": sym, "PRICE": price, "RSI": round(rsi, 1), "SIGNAL": decision,
                         "CONF": round(conf, 2), "POSITION": held_note, "NEWS": headline})
        except Exception:
            pass

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
    st.caption("Live signal preview — recomputed every refresh, purely informational. The scheduled engine (not this page) is what actually trades.")

    def color_signal(val):
        return 'background-color: #1b4d3e' if val == 'BUY' else 'background-color: #4d1b1b' if val == 'SELL' else ''

    st.dataframe(
        df.style.applymap(color_signal, subset=['SIGNAL']),
        use_container_width=True,
        height=500
    )

# --- Recent trial log (authoritative record of what the engine actually did) ---
st.markdown("---")
st.subheader("📜 Recent Trial Log")
if not GITHUB_TOKEN:
    st.caption("No GITHUB_TOKEN secret — can't read the trial log.")
elif trial_log_text:
    log_rows = []
    for line in trial_log_text.strip().splitlines()[-25:]:
        try:
            log_rows.append(json.loads(line))
        except Exception:
            continue
    if log_rows:
        st.dataframe(pd.DataFrame(log_rows[::-1]), use_container_width=True, height=400)
    else:
        st.caption("Log exists but couldn't be parsed.")
else:
    st.caption("No trades logged yet.")
