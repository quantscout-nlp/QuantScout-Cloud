# -*- coding: utf-8 -*-
"""
Recovery Desk: standalone, read-only dashboard (its own Streamlit app, separate
from the QuantScout engine app).

  streamlit run recovery_app.py

It reads the signed snapshot that the always-on worker (or the GitHub failover
schedule) publishes to the desk's private Drive folder. It never computes signals,
never places orders, and refuses to show a snapshot whose signature doesn't verify.
"Refresh now" only asks the GitHub failover workflow to run one screening cycle.
"""
import os
import sys
from datetime import datetime

import pandas as pd
import requests
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from recovery.desk import load_signed
from recovery.providers import ET
from recovery.store import open_store

try:
    from streamlit_autorefresh import st_autorefresh
except ImportError:
    st_autorefresh = None

st.set_page_config(page_title="Recovery Desk", layout="wide", page_icon="🧭")

KEYS = ["RECOVERY_DRIVE_FOLDER_ID", "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET",
        "GOOGLE_OAUTH_REFRESH_TOKEN", "GOOGLE_SERVICE_ACCOUNT_JSON", "RECOVERY_LOCAL_DIR", "GITHUB_TOKEN",
        "RECOVERY_SNAPSHOT_KEY"]
ENV = {k: (st.secrets[k] if k in st.secrets else os.environ.get(k, "")) for k in KEYS}
REPO = "quantscout-nlp/QuantScout-Cloud"
SECTIONS = [
    ("TIER1", "Tier 1 — Top Daily Tradable Winners (Strong Buy/Buy) — Paper candidate only"),
    ("CAP_PAPER", "Cap / Rule 1 — AK Combined PASS — Paper candidate only"),
    ("CAP_WAIT_AK", "Cap / Rule 1 — waiting on Anti-Knife this session"),
    ("CAP_SETUP", "Cap / Rule 1 — next-session setups (AK not scored yet)"),
    ("TIER2", "Tier 2 — Potential Buys — stack met, another gate fails"),
    ("CAP_AK_BLOCK", "Cap — Anti-Knife FAIL (falling knife, no new Paper)"),
    ("WATCH_HOLD", "Watch / Hold — further development; no new buy"),
    ("NOT_SCANNED", "NOT_SCANNED — no qualifying data (never a silent pass)"),
]
COLS = ["ticker", "paper_candidate", "blocked_by", "fail_gate", "dd_pct", "close", "chg_pct", "battery",
        "rsi0", "rvol_label", "ak_d", "ak_s", "ak_combined", "live_last", "vwap", "stop", "live_flags", "news"]


@st.cache_data(ttl=55, show_spinner=False)
def load_snapshot():
    store = open_store(env=ENV, local_dir=ENV["RECOVERY_LOCAL_DIR"] or "recovery_out")
    return load_signed(store.read_text("latest.json"), ENV)


def dispatch_refresh() -> str:
    if not ENV["GITHUB_TOKEN"]:
        return "No GITHUB_TOKEN secret with Actions write access. The next scheduled cycle will refresh."
    r = requests.post(f"https://api.github.com/repos/{REPO}/actions/workflows/recovery-desk-failover.yml/dispatches",
                      headers={"Authorization": f"Bearer {ENV['GITHUB_TOKEN']}",
                               "Accept": "application/vnd.github+json"}, json={"ref": "main", "inputs": {"force": "true"}}, timeout=15)
    return "Cycle requested. It usually lands in 1-2 minutes." if r.status_code == 204 else \
        f"GitHub refused ({r.status_code}). The token needs Actions: write."


if st_autorefresh:
    st_autorefresh(interval=60_000, key="desk_refresh")

st.title("🧭 Recovery Desk")
st.caption("Automated screening and status updates. Orders OFF — a human submits every order. Not trade advice.")

try:
    snap, integrity = load_snapshot()
except Exception as e:  # noqa: BLE001 - show the storage problem instead of a stack trace
    st.error(f"Could not read the desk snapshot: {type(e).__name__}")
    st.stop()
if integrity == "TAMPERED":
    st.error("The snapshot in Drive failed its signature check. It was written or edited by something other "
             "than the desk, so it is not shown. The next desk cycle will overwrite it.")
    st.stop()
if integrity == "UNSIGNED":
    st.warning("Snapshot is unsigned. Set the same RECOVERY_SNAPSHOT_KEY on the worker, the failover workflow "
               "and this app so tampering can be detected.")
if not snap:
    st.warning("No snapshot yet. Set RECOVERY_DRIVE_FOLDER_ID and Google credentials in app secrets, "
               "and make sure the Recovery Desk workflow or worker has run once.")
    st.stop()

h = snap["health"]
gen = datetime.fromisoformat(snap["generated_et"])
age_min = (datetime.now(ET) - gen).total_seconds() / 60
badge = {"OK": "🟢", "DEGRADED": "🟡", "STALE": "🔴", "NO_DATA": "⚫"}.get(h["status"], "⚪")
c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric("Health", f"{badge} {h['status']}")
c2.metric("Last update (ET)", gen.strftime("%a %H:%M"), f"{age_min:.0f} min ago", delta_color="off")
c3.metric("Feed", h["feed"], f"delay {h['data_delay_s']}s" if h["data_delay_s"] is not None else "no live bars",
          delta_color="off")
c4.metric("Coverage", h["scanned"])
c5.metric("Phase", snap["phase"], f"qualified from {snap['qualified_from_session']}", delta_color="off")
c6.metric("Orders", snap["orders"], f"writer: {h.get('writer', 'n/a')}", delta_color="off")
if snap["phase"] == "open" and age_min > 12:
    st.error(f"Snapshot is {age_min:.0f} minutes old during market hours. Treat every status as stale.")
if h["feed_note"]:
    st.warning(h["feed_note"])
if h["calendar"] != "alpaca_calendar":
    st.warning("Market calendar unavailable: holidays and early closes are not known this cycle.")
for e in h["errors"]:
    st.caption(f"⚠️ {e}")

b1, b2 = st.columns([1, 5])
if b1.button("🔄 Refresh now"):
    st.cache_data.clear()
    b2.info(dispatch_refresh())

rows = pd.DataFrame(snap["rows"])
for status, title in SECTIONS:
    part = rows[rows["status"] == status] if not rows.empty else rows
    if part.empty:
        continue
    with st.expander(f"{title} ({len(part)})", expanded=status in ("TIER1", "CAP_PAPER", "CAP_WAIT_AK")):
        st.dataframe(part[[c for c in COLS if c in part.columns]], use_container_width=True, hide_index=True)
        if status != "NOT_SCANNED":
            st.code("\n".join(line for card in part["card"].dropna() for line in card), language=None)

st.subheader("Classification changes today")
if snap["changes_today"]:
    st.dataframe(pd.DataFrame(snap["changes_today"][::-1]), use_container_width=True, hide_index=True)
else:
    st.caption("No changes recorded yet this session.")
st.caption(f"Provider calls this cycle: {h['provider_calls']} · 429 responses: {h['rate_limited']} · "
           f"daily sources: {h['daily_sources']} · intraday sources: {h.get('intraday_sources', {})} · "
           f"RVOL basis: {h.get('rvol_basis', 'SIP')} first · daily built {h['daily_built_et']} · "
           f"snapshot {integrity}")
