# -*- coding: utf-8 -*-
"""
Pure-function tests for the QuantScout engine.

Deliberately network-free and credential-free: every test here exercises decision
logic, trial-phase math, log replay, or env parsing, so the suite is safe to run
anywhere. The fetchers (Alpaca/Polygon/Tiingo/yfinance) and the GitHub-backed
persistence layer are not covered — they need live keys and are exercised by
running an actual scan pass.

Several tests pin invariants documented in CLAUDE.md rather than behavior that is
merely current, and say so where that is the point.
"""
import importlib.util
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import engine  # noqa: E402  (must follow the sys.path insert above)


def _load_run_scan():
    """scripts/run_scan.py is a standalone script, not an importable package member."""
    spec = importlib.util.spec_from_file_location("run_scan", REPO_ROOT / "scripts" / "run_scan.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


run_scan = _load_run_scan()


# --- Safety invariants (see CLAUDE.md "Invariants") ---

def test_trade_endpoint_is_hardcoded_to_paper():
    """This is what guarantees the engine cannot place a live order even if it is
    handed live keys. It must stay a module constant — never env-driven."""
    assert engine.ALPACA_TRADE_BASE == "https://paper-api.alpaca.markets"


def test_engine_stays_streamlit_free():
    """engine.py is imported by the headless scheduled job, which has no Streamlit
    runtime; importing streamlit there would break every scan pass."""
    source = (REPO_ROOT / "engine.py").read_text(encoding="utf-8")
    assert "import streamlit" not in source


def test_trial_state_is_not_written_to_main():
    """Committing trial state to the deploy branch would trigger a Streamlit Cloud
    redeploy on every log entry."""
    assert engine.TRIAL_BRANCH == "bot-trial-log"
    assert engine.TRIAL_BRANCH != "main"


def test_shared_requirement_pins_match_across_files():
    """The scheduled engine and the dashboard install from different files; a pin
    that drifts between them means they run different library versions."""
    def pins(filename):
        found = {}
        for line in (REPO_ROOT / filename).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            for separator in ("==", ">="):
                if separator in line:
                    found[line.split(separator)[0].strip()] = line
                    break
        return found

    app_pins, engine_pins = pins("requirements.txt"), pins("requirements-engine.txt")
    for package, spec in engine_pins.items():
        assert app_pins.get(package) == spec, (
            f"{package} is pinned as {spec!r} in requirements-engine.txt but "
            f"{app_pins.get(package)!r} in requirements.txt"
        )


# --- decide() ---

def test_decide_buys_confirmed_uptrend():
    decision, confidence = engine.decide(price=110.0, sma20=100.0, rsi=60.0, sent=0.5)
    assert decision == "BUY"
    assert confidence == pytest.approx(0.85)  # 0.8 + sent * 0.1


def test_decide_sells_confirmed_downtrend():
    decision, confidence = engine.decide(price=90.0, sma20=100.0, rsi=45.0, sent=-0.5)
    assert decision == "SELL"
    assert confidence == pytest.approx(0.8)


def test_decide_holds_when_overbought():
    assert engine.decide(price=110.0, sma20=100.0, rsi=75.0, sent=0.5)[0] == "HOLD"


def test_decide_holds_on_weak_positive_sentiment():
    """Sentiment must clear +0.15, not merely be positive."""
    assert engine.decide(price=110.0, sma20=100.0, rsi=60.0, sent=0.10)[0] == "HOLD"


def test_decide_holds_on_weak_negative_sentiment():
    """Sentiment must fall below -0.2, not merely be negative."""
    assert engine.decide(price=90.0, sma20=100.0, rsi=45.0, sent=-0.1)[0] == "HOLD"


def test_decide_does_not_fire_on_oversold_alone():
    """The RSI<35 mean-reversion branch was removed on purpose — an oversold dip
    with neutral sentiment must not produce a trade."""
    assert engine.decide(price=95.0, sma20=100.0, rsi=32.0, sent=0.0)[0] == "HOLD"


@pytest.mark.parametrize("missing_price", [None, 0, 0.0])
def test_decide_holds_when_price_is_missing(missing_price):
    """Every fetcher can return None/0 when all providers fail."""
    assert engine.decide(price=missing_price, sma20=100.0, rsi=60.0, sent=0.5) == ("HOLD", 0.0)


def test_decide_holds_when_indicators_unavailable():
    """fetch_indicators_hybrid returns rsi=0.0 on total data failure; that must not
    be read as an extremely oversold reading."""
    assert engine.decide(price=110.0, sma20=0.0, rsi=0.0, sent=0.9) == ("HOLD", 0.0)


# --- Trial phase gating ---

def test_trial_phase_fails_safe_to_dry_run_without_a_start_date():
    """With no GitHub token the start date is None, and the engine must not trade."""
    assert engine.get_trial_phase(None) == ("DRY_RUN", None)


@pytest.mark.parametrize(
    "days_elapsed,expected_phase",
    [
        (0, "DRY_RUN"),
        (29, "DRY_RUN"),
        (30, "PAPER"),   # first day orders reach Alpaca
        (59, "PAPER"),
        (60, "REVIEW"),  # frozen
        (400, "REVIEW"),
    ],
)
def test_trial_phase_boundaries(days_elapsed, expected_phase):
    start = date.today() - timedelta(days=days_elapsed)
    assert engine.get_trial_phase(start) == (expected_phase, days_elapsed)


def test_trial_phase_lengths_sum_to_sixty_days():
    assert engine.TRIAL_DRY_RUN_DAYS + engine.TRIAL_PAPER_DAYS == 60


# --- Dry-run position replay ---

def _log_text(*rows):
    return "\n".join(json.dumps(row) for row in rows)


def test_derive_simulated_positions_replays_buys_and_sells():
    text = _log_text(
        {"mode": "DRY_RUN", "action": "BUY", "ticker": "TSLA"},
        {"mode": "DRY_RUN", "action": "BUY", "ticker": "AMD"},
        {"mode": "DRY_RUN", "action": "SELL", "ticker": "TSLA"},
    )
    assert engine.derive_simulated_positions(text) == {"AMD"}


def test_derive_simulated_positions_ignores_paper_rows():
    """Paper fills appear in Alpaca's real position list; counting them here too
    would double-count them."""
    text = _log_text({"mode": "PAPER", "action": "BUY", "ticker": "TSLA"})
    assert engine.derive_simulated_positions(text) == set()


def test_derive_simulated_positions_tolerates_malformed_lines():
    """A truncated or interleaved commit must not wipe out the replayed state."""
    text = "not json at all\n\n" + _log_text({"mode": "DRY_RUN", "action": "BUY", "ticker": "MU"}) + "\n"
    assert engine.derive_simulated_positions(text) == {"MU"}


def test_derive_simulated_positions_selling_unheld_symbol_is_a_noop():
    text = _log_text({"mode": "DRY_RUN", "action": "SELL", "ticker": "NVDA"})
    assert engine.derive_simulated_positions(text) == set()


@pytest.mark.parametrize("empty", [None, "", "\n\n"])
def test_derive_simulated_positions_handles_empty_log(empty):
    assert engine.derive_simulated_positions(empty) == set()


# --- to_float ---

@pytest.mark.parametrize(
    "raw,expected",
    [("1.5", 1.5), (2, 2.0), (2.0, 2.0), (None, None), ("abc", None), ({}, None)],
)
def test_to_float(raw, expected):
    assert engine.to_float(raw) == expected


# --- Env-derived config (see CLAUDE.md "Gotchas") ---

def test_env_helpers_treat_empty_string_as_unset(monkeypatch):
    """An unset GitHub Actions *variable* is injected as "", not as an absent key,
    so a plain os.environ.get(name, default) would never reach its default."""
    for name in ("MAX_POSITIONS", "NOTIONAL_PER_TRADE", "ENABLE_TRADING"):
        monkeypatch.setenv(name, "")
    assert run_scan._env_int("MAX_POSITIONS", 8) == 8
    assert run_scan._env_float("NOTIONAL_PER_TRADE", 500.0) == 500.0
    assert run_scan._env_bool("ENABLE_TRADING", True) is True
    assert run_scan._env_str("WATCHLIST", "fallback") == "fallback"


def test_env_helpers_read_real_values(monkeypatch):
    monkeypatch.setenv("MAX_POSITIONS", "3")
    monkeypatch.setenv("NOTIONAL_PER_TRADE", "250.5")
    assert run_scan._env_int("MAX_POSITIONS", 8) == 3
    assert run_scan._env_float("NOTIONAL_PER_TRADE", 500.0) == pytest.approx(250.5)


@pytest.mark.parametrize(
    "raw,expected",
    [("false", False), ("FALSE", False), ("False", False), ("true", True), ("1", True)],
)
def test_env_bool_only_the_literal_false_disables_trading(monkeypatch, raw, expected):
    monkeypatch.setenv("ENABLE_TRADING", raw)
    assert run_scan._env_bool("ENABLE_TRADING", True) is expected


def test_tickers_from_env_falls_back_to_default_watchlist(monkeypatch):
    monkeypatch.setenv("WATCHLIST", "")
    assert run_scan._tickers_from_env() == engine.DEFAULT_TICKERS


def test_tickers_from_env_parses_trims_and_uppercases(monkeypatch):
    monkeypatch.setenv("WATCHLIST", " tsla, amd ,, mu ")
    assert run_scan._tickers_from_env() == ["TSLA", "AMD", "MU"]


def test_default_watchlist_has_no_duplicates():
    assert len(engine.DEFAULT_TICKERS) == len(set(engine.DEFAULT_TICKERS))
