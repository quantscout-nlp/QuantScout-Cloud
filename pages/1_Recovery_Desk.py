# -*- coding: utf-8 -*-
"""
Recovery Desk: read-only view of the automated watchlist.

It reads the snapshot that the scheduled job / always-on worker publishes to
the private Drive folder. It never computes signals and never places orders.
"Refresh now" only asks GitHub Actions to run one more screening cycle.
"""
import json
import os
import sys
from datetime import datetime

import pandas as pd
import requests
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from recovery.providers import ET
from recovery.store import open_store

try:
    from streamlit_autorefresh import st_autorefresh
except ImportError:
    st_autorefresh = None

st.set_page_config(page_title="Recovery Desk", layout="wide", page_icon="🧭")

KEYS = ["RECOVERY_DRIVE_FOLDER_ID", "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET",
        "GOOGLE_OAUTH_REFRESH_TOKEN", "GOOGLE_SERVICE_ACCOUNT_JSON", "RECOVERY_LOCAL_DIR", "GITHUB_TOKEN"]
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
    txt = store.read_text("latest.json")
    return json.loads(txt) if txt else None


def dispatch_refresh() -> str:
    if not ENV["GITHUB_TOKEN"]:
        return "No GITHUB_TOKEN secret with Actions write access. The next scheduled cycle will refresh."
    r = requests.post(f"https://api.github.com/repos/{REPO}/actions/workflows/recovery-desk.yml/dispatches",
                      headers={"Authorization": f"Bearer {ENV['GITHUB_TOKEN']}",
                               "Accept": "application/vnd.github+json"}, json={"ref": "main"}, timeout=15)
    return "Cycle requested. It usually lands in 1-2 minutes." if r.status_code == 204 else \
        f"GitHub refused ({r.status_code}). The token needs Actions: write."


if st_autorefresh:
    st_autorefresh(interval=60_000, key="desk_refresh")

st.title("🧭 Recovery Desk")
st.caption("Automated screening and status updates. Orders OFF — a human submits every order. Not trade advice.")

try:
    snap = load_snapshot()
except Exception as e:  # noqa: BLE001 - show the storage problem instead of a stack trace
    st.error(f"Could not read the desk snapshot: {e}")
    st.stop()
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
c6.metric("Orders", snap["orders"])
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
           f"daily sources: {h['daily_sources']} · daily built {h['daily_built_et']}")
