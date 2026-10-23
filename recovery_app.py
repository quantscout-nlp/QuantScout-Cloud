# -*- coding: utf-8 -*-
"""
Recovery Desk: standalone, read-only dashboard (its own Streamlit app, separate
from the QuantScout engine app).

  streamlit run recovery_app.py

It reads the signed snapshot that the always-on worker (or the GitHub failover
schedule) publishes to the desk's private Drive folder. It never computes signals,
never places orders, and refuses to show a snapshot whose signature doesn't verify.
"Refresh now" only asks the GitHub failover workflow to run one screening cycle.

Its one write: the sidebar's Robinhood positions upload. It saves a signed,
read-only copy of your book (symbol, qty, average cost, lot) to the desk folder.
The desk uses it for the held-name stop block. Nothing is ever sent to Robinhood.
"""
import os
import sys
from datetime import datetime

import pandas as pd
import requests
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from recovery import held
from recovery.desk import dump_signed, load_signed
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


def _secret(k: str) -> str:
    """App secrets on Streamlit Cloud, environment variables for a local run.
    Streamlit raises when no secrets.toml exists at all, so that counts as 'not set'."""
    try:
        if k in st.secrets:
            return str(st.secrets[k])
    except Exception:  # noqa: BLE001 - StreamlitSecretNotFoundError when no secrets file exists
        pass
    return os.environ.get(k, "")


ENV = {k: _secret(k) for k in KEYS}
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
        "rsi0", "rvol_label", "ak_d", "ak_s", "ak_combined", "live_last", "vwap", "stop", "live_flags", "news",
        "rh_qty", "rh_lot"]


def desk_store():
    return open_store(env=ENV, local_dir=ENV["RECOVERY_LOCAL_DIR"] or "recovery_out")


def positions_panel():
    """Sidebar: upload, preview, save or clear the Robinhood book (read-only copy)."""
    st.sidebar.header("Robinhood positions")
    st.sidebar.caption("Upload your rh_book CSV (or any CSV with symbol, qty, avg cost). Read-only: it only "
                       "feeds the held-name stop block. Nothing is sent to Robinhood.")
    try:
        current, integrity = load_signed(desk_store().read_text("held.json"), ENV)
    except Exception as e:  # noqa: BLE001 - storage not configured yet
        st.sidebar.warning(f"Desk storage unavailable ({type(e).__name__}).")
        return
    if integrity == "TAMPERED":
        st.sidebar.error("The saved book failed its signature check and is being ignored. Upload it again.")
    elif current:
        st.sidebar.success(f"{len(current['positions'])} positions saved {current['uploaded_et'][:16]} ET")
    up = st.sidebar.file_uploader("Positions CSV", type=["csv"], accept_multiple_files=False)
    if up is not None:
        try:
            positions, meta, warnings = held.parse_positions(up.getvalue())
        except ValueError as e:
            st.sidebar.error(f"Can't use this file: {e}")
            return
        # Per-name Cap/Mom: start from the file, else last saved book for that symbol, else Auto.
        positions = held.carry_books(positions, current if integrity != "TAMPERED" else None)
        grid = pd.DataFrame([{**p, "book": p.get("book") or "Auto"} for p in positions])
        edited = st.sidebar.data_editor(
            grid, hide_index=True, height=260, key=f"book_editor_{up.name}_{up.size}",
            disabled=[c for c in grid.columns if c != "book"],
            column_config={"book": st.column_config.SelectboxColumn(
                "book", options=list(held.BOOK_CHOICES), required=True,
                help="Cap = stop under SMA50. Mom = stop under SMA20. Auto = desk's book, else Mom.")})
        positions = held.set_books(positions, dict(zip(edited["symbol"], edited["book"])))
        for w in warnings[:10]:
            st.sidebar.caption(f"⚠️ {w}")
        if not ENV["RECOVERY_SNAPSHOT_KEY"]:
            st.sidebar.error("Set RECOVERY_SNAPSHOT_KEY in app secrets first: the desk only trusts signed files.")
        elif st.sidebar.button(f"Save {len(positions)} positions to the desk"):
            doc = {"uploaded_et": datetime.now(ET).isoformat(), "asof_et": meta["asof_et"],
                   "account_mask": meta["account_mask"], "source_name": up.name[:80], "positions": positions}
            desk_store().write_text("held.json", dump_signed(doc, ENV))
            st.cache_data.clear()
            st.sidebar.success("Saved. The stop block updates on the desk's next cycle (within 5 minutes).")
    if current and st.sidebar.button("Clear saved positions"):
        desk_store().write_text("held.json", dump_signed({"uploaded_et": datetime.now(ET).isoformat(),
                                                          "positions": []}, ENV))
        st.sidebar.info("Cleared.")


def stop_line(l: dict) -> str:
    if l["flag"] == "STOP_NOT_COMPUTED":
        return f"{l['ticker']} | STOP_NOT_COMPUTED | {l.get('reason', '')} | RH {l['qty']} {l['lot']}"
    cost = f" @ {l['avg_cost']:.2f}" if l.get("avg_cost") else ""
    rel = "<" if l["flag"] == "EXIT_FLAG" else ">="
    return (f"{l['ticker']} | {l['book']} ({l.get('book_basis', 'file')}) | {l['flag']} | close {l['close']:.2f} {rel} {l['stop_name']} "
            f"{l['stop']:.2f} | RH {l['qty']}{cost} {l['lot']} | as-of {l['asof']}"
            + (f" | {l['live_note']}" if l.get("live_note") else ""))


@st.cache_data(ttl=600, show_spinner=False)
def load_backtest():
    return load_signed(desk_store().read_text("backtest_report.json"), ENV)


def _pct(x):
    return None if x is None else round(100 * x, 2)


def evidence_rows(blocks: dict) -> pd.DataFrame:
    out = []
    for name, b in blocks.items():
        ex, fw, r3 = b["excess"]["10"], b["fwd"]["10"], b["rule3"]
        ci = ex.get("ci95")
        out.append({"status": name, "signals": b["signals"],
                    "excess 10d %": _pct(ex.get("mean")),
                    "95% range %": f"{_pct(ci[0])} to {_pct(ci[1])}" if ci else "too few months",
                    "hit 10d %": _pct(fw.get("hit")), "Rule 3 trade %": _pct(r3.get("mean")),
                    "Rule 3 days": None if r3.get("avg_days") is None else round(r3["avg_days"], 1)})
    return pd.DataFrame(out)


@st.cache_data(ttl=55, show_spinner=False)
def load_snapshot():
    return load_signed(desk_store().read_text("latest.json"), ENV)


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

positions_panel()
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

st.subheader("Held-name stop block")
hb = snap.get("held") or {}
if not hb.get("lines"):
    st.caption("No Robinhood positions saved. Upload your book in the sidebar to get EXIT_FLAG / "
               "STOP_NOT_COMPUTED lines.")
else:
    if hb["status"] == "BOOK_STALE":
        st.warning(f"The saved book is {hb['age_days']} days old. Upload a fresh one: stops are only as good as it is.")
    st.caption(f"Mom stop = official close under SMA20 · Cap stop = official close under SMA50 · audit flags, "
               f"human decides, never an automatic flatten · book uploaded {hb['uploaded_et'][:16]} ET "
               f"{hb.get('account_mask') or ''} · " + ", ".join(f"{k} {v}" for k, v in hb["counts"].items()))
    flagged = [l for l in hb["lines"] if l["flag"] != "OK"]
    st.code("\n".join(stop_line(l) for l in flagged) or "No held name is under its stop.", language=None)
    with st.expander(f"All held names ({len(hb['lines'])})"):
        st.dataframe(pd.DataFrame(hb["lines"]), use_container_width=True, hide_index=True)

st.subheader("Backtest evidence")
try:
    bt, bt_integrity = load_backtest()
except Exception:  # noqa: BLE001 - storage problems are already shown above
    bt, bt_integrity = None, "MISSING"
if bt_integrity == "TAMPERED":
    st.error("backtest_report.json failed its signature check and is not shown. Run the backtest again.")
elif not bt:
    st.caption("No backtest yet. On the worker VM run scripts/run_backtest.py (see docs, Tradability test).")
else:
    st.caption(f"{bt['period']['start']} to {bt['period']['end']} · {bt['symbols']} names · "
               f"{bt['name_days']:,} name-days · entry at the next open · {bt['cost_bps_per_side']} bps per side · "
               f"excess = return minus the same-day average of every scanned name · generated {bt['generated_et'][:16]} ET")
    st.markdown("**Is there an edge?** A status earns its keep when its excess return is above 0 "
                "and the whole 95% range sits above 0.")
    st.dataframe(evidence_rows(bt["statuses"]), use_container_width=True, hide_index=True)
    with st.expander("RVOL20 floor sensitivity (Tier 1)"):
        st.dataframe(evidence_rows(bt["rvol_floor_sensitivity"]).rename(columns={"status": "floor"}),
                     use_container_width=True, hide_index=True)
    with st.expander("Known differences from the live desk"):
        for n in bt["notes"]:
            st.caption(f"• {n}")

st.subheader("Classification changes today")
if snap["changes_today"]:
    st.dataframe(pd.DataFrame(snap["changes_today"][::-1]), use_container_width=True, hide_index=True)
else:
    st.caption("No changes recorded yet this session.")
st.caption(f"Provider calls this cycle: {h['provider_calls']} · 429 responses: {h['rate_limited']} · "
           f"daily sources: {h['daily_sources']} · intraday sources: {h.get('intraday_sources', {})} · "
           f"RVOL basis: {h.get('rvol_basis', 'SIP')} first · daily built {h['daily_built_et']} · "
           f"snapshot {integrity}")
