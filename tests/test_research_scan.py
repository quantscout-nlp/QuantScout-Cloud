# -*- coding: utf-8 -*-
"""Tests for the scoring/dedup layer and the mechanical gate.

The load-bearing claim of this system is that it is DETERMINISTIC: the same
research file always produces the same ranking, and reordering the file by hand
never changes the result. Several tests below exist only to hold that claim.

Run: python -m pytest tests/ -q   (or: python tests/test_research_scan.py)
"""
import copy
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mechanical_filters as mf
import research_scan as rs

DATASET = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "research", "track_a_2026-09-04.json")


def load():
    with open(DATASET, "r", encoding="utf-8") as fh:
        return json.load(fh)


# --- determinism ---------------------------------------------------------------
def test_ranking_is_stable_across_runs():
    data = load()
    first = [r["symbol"] for r in rs.rank(data)]
    for _ in range(5):
        assert [r["symbol"] for r in rs.rank(load())] == first


def test_ranking_is_independent_of_input_order():
    """Shuffling the JSON's ticker array must not move anything in the output."""
    data = load()
    baseline = [(r["symbol"], r["score"]) for r in rs.rank(data)]
    for seed in range(10):
        shuffled = copy.deepcopy(data)
        random.Random(seed).shuffle(shuffled["tickers"])
        assert [(r["symbol"], r["score"]) for r in rs.rank(shuffled)] == baseline


def test_no_duplicate_symbols_in_output():
    symbols = [r["symbol"] for r in rs.rank(load())]
    assert len(symbols) == len(set(symbols))


# --- dedup ---------------------------------------------------------------------
def test_dedupe_merges_mentions_rather_than_overwriting():
    records = [
        {"symbol": "ABC", "mentions": ["grok"], "flags": [], "universe": ["gainer"]},
        {"symbol": "abc", "mentions": ["claude"], "flags": ["high_beta"], "universe": ["earnings"]},
    ]
    merged = rs.dedupe(records)
    assert len(merged) == 1
    assert merged[0]["mentions"] == ["grok", "claude"]
    assert merged[0]["flags"] == ["high_beta"]
    assert merged[0]["universe"] == ["gainer", "earnings"]


def test_dedupe_prefers_verified_numbers():
    records = [
        {"symbol": "ABC", "mentions": [], "move_pct": None, "verified": False},
        {"symbol": "ABC", "mentions": [], "move_pct": 7.5, "verified": True},
    ]
    assert rs.dedupe(records)[0]["move_pct"] == 7.5


# --- scoring rules -------------------------------------------------------------
def test_consensus_is_concave_and_capped():
    """More agreement must never scale linearly -- correlated sources do not compound."""
    weights = [rs._consensus_multiplier(n) for n in range(0, 8)]
    gains = [b - a for a, b in zip(weights[1:], weights[2:])]
    assert all(earlier >= later for earlier, later in zip(gains, gains[1:]))
    assert weights[-1] == rs.CONSENSUS_CAP
    assert rs._consensus_multiplier(50) == rs.CONSENSUS_CAP


def test_hard_disqualifier_zeroes_score_despite_unanimous_consensus():
    rec = {"symbol": "IPOX", "mentions": ["grok", "claude", "gemini", "chatgpt", "copilot"],
           "universe": ["gainer"], "move_pct": 12.0,
           "catalyst_quality": "company_specific_dated", "flags": ["insufficient_history"]}
    scored = rs.score_ticker(rec)
    assert scored["score"] == 0.0
    assert scored["disqualified"]
    assert scored["raw_score"] > 0  # the opinion was strong; the rule still wins


def test_commentary_mention_of_a_falling_stock_is_not_a_long():
    """A name discussed only because it dropped must not inherit a positive weight."""
    rec = {"symbol": "DOWN", "mentions": ["claude"], "universe": [], "move_pct": -4.0,
           "catalyst_quality": "company_specific_dated", "flags": []}
    assert rs.score_ticker(rec)["direction"] == "SHORT/AVOID"


def test_small_move_does_not_flip_commentary_direction():
    rec = {"symbol": "FLAT", "mentions": ["claude"], "universe": [], "move_pct": -0.2,
           "catalyst_quality": "company_specific_dated", "flags": []}
    assert rs.score_ticker(rec)["direction"] == "LONG"


def test_soft_penalties_compound():
    base = {"symbol": "X", "mentions": ["grok"], "universe": ["gainer"], "move_pct": 8.0,
            "catalyst_quality": "company_specific_dated", "flags": []}
    one = dict(base, flags=["high_beta"])
    two = dict(base, flags=["high_beta", "gap_extended"])
    assert rs.score_ticker(two)["score"] < rs.score_ticker(one)["score"] < rs.score_ticker(base)["score"]


def test_known_disqualified_names_are_actually_disqualified():
    """CBRS and EQPT are recent IPOs; no 200-day history means no trend gate."""
    rows = {r["symbol"]: r for r in rs.rank(load())}
    for symbol in ("CBRS", "EQPT"):
        assert rows[symbol]["disqualified"], f"{symbol} should be disqualified"
        assert rows[symbol]["score"] == 0.0
    assert rows["FICO"]["disqualified"]  # regulatory repricing, no technical floor


# --- cluster concentration -----------------------------------------------------
def test_cluster_cap_limits_concurrent_longs_per_group():
    rows = rs.apply_cluster_cap(rs.rank(load()), max_per_cluster=2)
    uncapped = {}
    for row in rows:
        if row["direction"] == "LONG" and not row["disqualified"] and not row["cluster_capped"]:
            uncapped[row["cluster"]] = uncapped.get(row["cluster"], 0) + 1
    assert all(count <= 2 for count in uncapped.values()), uncapped


def test_cluster_cap_keeps_highest_ranked_member():
    rows = rs.apply_cluster_cap(rs.rank(load()), max_per_cluster=1)
    by_cluster = {}
    for row in rows:
        if row["direction"] == "LONG" and not row["disqualified"]:
            by_cluster.setdefault(row["cluster"], []).append(row)
    for members in by_cluster.values():
        assert not members[0]["cluster_capped"]
        assert all(m["cluster_capped"] for m in members[1:])


# --- weekend deltas and triage -------------------------------------------------
def test_headwind_cuts_conviction_on_a_long():
    base = {"symbol": "L", "mentions": ["grok"], "universe": ["gainer"], "move_pct": 8.0,
            "catalyst_quality": "company_specific_dated", "flags": []}
    plain = rs.score_ticker(base)["score"]
    head = rs.score_ticker(dict(base, weekend_delta="headwind"))["score"]
    tail = rs.score_ticker(dict(base, weekend_delta="tailwind"))["score"]
    assert head < plain < tail


def test_headwind_deepens_conviction_on_an_avoid_rather_than_softening_it():
    """The sign trap: a deteriorating name must not score BETTER for deteriorating."""
    base = {"symbol": "S", "mentions": ["grok"], "universe": ["loser"], "move_pct": -12.0,
            "catalyst_quality": "company_specific_dated", "flags": []}
    plain = rs.score_ticker(base)["score"]
    head = rs.score_ticker(dict(base, weekend_delta="headwind"))["score"]
    assert plain < 0 and head < plain, (plain, head)


def test_triage_assigns_exactly_one_action_per_ticker():
    rows = rs.triage_all(rs.apply_cluster_cap(rs.rank(load())))
    assert len(rows) == len(load()["tickers"])
    assert all(r["action"] in rs.TRIAGE_ACTIONS for r in rows)


def test_disqualified_never_reaches_a_tradeable_action():
    for row in rs.triage_all(rs.apply_cluster_cap(rs.rank(load()))):
        if row["disqualified"]:
            assert row["action"] == "DISQUALIFIED", row["symbol"]


def test_adverse_weekend_delta_stands_a_long_down():
    row = rs.triage(rs.score_ticker({
        "symbol": "ADV", "mentions": ["grok"], "universe": ["gainer"], "move_pct": 7.0,
        "catalyst_quality": "company_specific_dated", "flags": [],
        "weekend_delta": "adverse"}))
    assert row["action"] == "STAND_ASIDE"


def test_gap_risk_flag_routes_to_gap_watch_not_primary():
    row = rs.triage(rs.score_ticker({
        "symbol": "GAPR", "mentions": ["grok"], "universe": ["gainer"], "move_pct": 11.0,
        "catalyst_quality": "company_specific_dated", "flags": ["gap_risk_high"],
        "weekend_delta": "tailwind"}))
    assert row["action"] == "GAP_WATCH"


def test_weekend_reweighting_is_still_deterministic():
    first = [(r["symbol"], r["score"], r["action"])
             for r in rs.triage_all(rs.apply_cluster_cap(rs.rank(load())))]
    for seed in range(5):
        shuffled = copy.deepcopy(load())
        random.Random(seed).shuffle(shuffled["tickers"])
        assert [(r["symbol"], r["score"], r["action"])
                for r in rs.triage_all(rs.apply_cluster_cap(rs.rank(shuffled)))] == first


# --- mechanical gate -----------------------------------------------------------
def _bars(n, start=50.0, step=0.20, rng=1.6, vol=4_000_000):
    bars, price = [], start
    for _ in range(n):
        price += step
        bars.append({"o": price - rng * 0.3, "h": price + rng * 0.6,
                     "l": price - rng * 0.6, "c": price, "v": vol, "t": "2026-09-04"})
    return bars


def test_gate_passes_a_clean_stacked_uptrend():
    verdict = mf.screen("OK", _bars(260), {"bid": 101.90, "ask": 102.00})
    assert verdict["passed"], verdict["failed"]


def test_gate_rejects_short_history_before_evaluating_anything_else():
    verdict = mf.screen("IPOX", _bars(80), {"bid": 65.90, "ask": 66.00})
    assert not verdict["passed"]
    assert "SMA200 undefined" in verdict["reject_reason"]


def test_missing_quote_fails_closed_rather_than_passing():
    verdict = mf.screen("NOQ", _bars(260), quote=None)
    assert not verdict["passed"]
    assert "spread" in verdict["failed"]


def test_gate_rejects_a_downtrend():
    falling = _bars(260, start=150.0, step=-0.20)
    assert not mf.screen("DOWN", falling, {"bid": 99.90, "ask": 100.00})["passed"]


def test_gate_rejects_illiquid_names():
    thin = _bars(260, vol=1_000)
    verdict = mf.screen("THIN", thin, {"bid": 101.90, "ask": 102.00})
    assert "dollar_volume" in verdict["failed"]


def test_gate_rejects_wide_spread():
    verdict = mf.screen("WIDE", _bars(260), {"bid": 101.00, "ask": 103.00})
    assert "spread" in verdict["failed"]


def test_gap_check_blocks_chasing():
    assert not mf.gap_ok(100.0, 107.0)["ok"]
    assert mf.gap_ok(100.0, 101.5)["ok"]


# --- sizing --------------------------------------------------------------------
def test_position_size_is_risk_based():
    result = mf.position_size(equity=25_000, risk_pct=1.5, entry=100.0, stop=98.0)
    assert result["risk_dollars"] == 375.0
    assert result["shares"] == 62  # 375 / 2.00 = 187 shares, but the 25% notional cap binds
    assert result["notional_capped"]


def test_position_size_uncapped_when_stop_is_wide_enough():
    result = mf.position_size(equity=25_000, risk_pct=1.5, entry=50.0, stop=44.0)
    assert result["shares"] == 62  # 375 / 6.00 = 62, under the $6,250 notional cap
    assert not result["notional_capped"]
    assert result["actual_risk"] == 372.0


def test_position_size_rejects_an_inverted_stop():
    assert mf.position_size(25_000, 1.5, 100.0, 105.0)["shares"] == 0


def test_notional_cap_bounds_gap_risk():
    """A very tight stop implies a huge share count; the cap must bound it."""
    result = mf.position_size(equity=25_000, risk_pct=1.5, entry=100.0, stop=99.90)
    assert result["notional"] <= 25_000 * 0.25 + 100
    assert result["notional_capped"]


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                passed += 1
                print(f"  PASS  {name}")
            except AssertionError as exc:
                failed += 1
                print(f"  FAIL  {name}: {exc}")
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
