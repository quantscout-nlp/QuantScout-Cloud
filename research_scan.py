# -*- coding: utf-8 -*-
"""
Deterministic dedup + cross-source consensus scoring for the Track A research set.

WHAT THIS IS FOR
----------------
Several AI chat sessions (Grok, Claude, Gemini, ...) were each asked to filter the
same session's movers. This module collapses those overlapping lists into one
ranked universe, weighting a ticker up when it shows up in more than one of them.

WHAT THE SCORE IS NOT
---------------------
It is NOT independent confirmation. Those chat sessions read the same wire feeds
and the same mover tables, so five mentions is closer to one observation repeated
five times than to five samples. The score therefore measures *salience* -- how
loudly the research pipeline is pointing at a name -- and nothing else.

Three design choices follow directly from that, and they are the whole point:

  1. Consensus weight has DIMINISHING returns and a HARD CAP (see CONSENSUS_WEIGHT).
     The 4th and 5th mention are worth almost nothing because they are almost
     certainly not new information.
  2. Consensus can only ever RANK candidates. It can never admit one. Admission is
     decided by mechanical_filters.py against real price data, which holds a veto.
  3. Hard disqualifiers zero the score outright, no matter how many sources agree.
     A name every model loves but that has no 200-day history is still untradeable
     by a rule that needs a 200-day average.

Everything here is a pure function of the input dict: no clock, no network, no RNG,
no set iteration order leaking into output. Same JSON in, same ranking out, forever.
"""
from typing import Any, Dict, List, Optional

# Distinct chat sources mentioning a ticker -> multiplier. Concave and capped on
# purpose: correlated sources do not compound. Going 1->2 sources is the only step
# that carries real information (an independent second look); past 3 it is echo.
CONSENSUS_WEIGHT = {0: 0.60, 1: 1.00, 2: 1.80, 3: 2.40, 4: 2.80}
CONSENSUS_CAP = 3.00

# What KIND of evidence the mention is. Signs carry the directional bias, so a
# "loser" mention scores negative and sorts to the short/avoid end of the book.
BUCKET_WEIGHT = {
    "earnings_pop":  1.50,   # post-earnings-announcement drift: best-documented edge here
    "earnings_drop": -1.50,
    "gainer":         1.00,  # session strength, but crowded by definition
    "loser":         -1.00,
    "theme":          0.50,  # sector/thematic mention only
    "watchlist":      0.25,  # on somebody's list with no catalyst attached
}

# How much to trust the catalyst behind the move.
CATALYST_QUALITY = {
    "company_specific_dated": 1.00,  # a dated, single-name, verifiable event
    "sector_sympathy":        0.70,  # it moved because its group moved
    "unverified":             0.40,  # mentioned, but the move could not be confirmed
}

# Flags that ZERO the score. These are structural, not opinions -- each one names a
# reason the name cannot be traded by this system's own rules.
HARD_DISQUALIFIERS = {
    "insufficient_history":
        "fewer than 200 sessions of history -- SMA200 is undefined, so the trend gate cannot run",
    "regulatory_headline":
        "repricing on a political/regulatory decision with no technical floor and open headline risk",
    "unverified_date":
        "the catalyst could not be dated to this session; it may be a stale print",
    "stale_catalyst":
        "catalyst belongs to an earlier reporting period, not this session",
    "earnings_ahead":
        "binary event still in front of it -- that is an event trade, not a momentum trade",
    "leveraged_etf":
        "daily-reset leveraged product; use as a group gauge, not a position",
    "etf_reference":
        "index/ETF used as a regime gauge, not a single-name candidate",
}

# Flags that SCALE the score down but leave the name tradeable. Multiplicative and
# compounding: three soft warnings should hurt roughly like one hard one.
SOFT_PENALTIES = {
    "gap_extended":     0.60,  # already gapped hard; most of the move is gone
    "extended_group":   0.70,  # its whole group is late in a vertical run
    "high_beta":        0.75,
    "microcap":         0.50,  # cannot absorb size without moving your own stop
    "knife":            0.50,  # falling on a guide-down; catching it is a different trade
    "index_inclusion_bid": 0.70,  # mechanical demand with a known expiry date
    "beat_and_fade":    0.80,  # good news sold -- informative, but a warning
    "sell_the_news":    0.80,
    "dual_driver":      0.85,  # correlation flips depending on which story is driving
    "guide_down":       0.70,
    "below_ipo_price":  0.80,
    "move_date_ambiguous": 0.70,
    "wrong_sector_tag": 1.00,  # a data-hygiene flag; surfaced, not punished
    "thesis_decelerating": 0.70,  # the driver still works, but its RATE OF CHANGE is falling
    "move_size_disputed": 0.85,   # sources disagree on how far it actually moved
    "gap_risk_high":     0.80,    # a live catalyst likely to gap the entry away overnight
}

# How a post-close development rescales conviction. Applied to the score's MAGNITUDE,
# with the sign handled separately -- see _weekend_multiplier for why that matters.
WEEKEND_DELTA_WEIGHT = {
    "tailwind":  1.20,   # a new catalyst pushing the existing thesis forward
    "unchanged": 1.00,
    "mixed":     0.85,   # new information cuts both ways
    "headwind":  0.75,   # the thesis still holds but conditions turned against it
    "adverse":   0.50,   # the new information argues against the thesis outright
}


def _weekend_multiplier(delta: str, raw_sign: int) -> float:
    """Scale conviction by what happened after the close.

    The subtlety: 'headwind' describes the STOCK, not the trade. On a long candidate
    a headwind cuts conviction. On a name already scored SHORT/AVOID, that same
    headwind *deepens* the reason to avoid it -- so the magnitude must grow, not
    shrink. Naively multiplying a negative score by 0.75 would make a deteriorating
    name look more attractive, which is exactly backwards.
    """
    weight = WEEKEND_DELTA_WEIGHT.get(delta, 1.0)
    if raw_sign < 0 and weight:
        return 1.0 / weight
    return weight


def _buckets_for(rec: Dict[str, Any]) -> List[str]:
    """Map a record's universe membership + move direction onto scoring buckets.

    'earnings' alone is ambiguous, so the sign of move_pct decides pop vs drop. A
    ticker with an earnings tag but no recorded move gets the neutral 'watchlist'
    bucket rather than a guessed direction.
    """
    universe = rec.get("universe") or []
    move = rec.get("move_pct")
    buckets: List[str] = []

    if "earnings" in universe:
        if move is not None and move > 0:
            buckets.append("earnings_pop")
        elif move is not None and move < 0:
            buckets.append("earnings_drop")
        else:
            buckets.append("watchlist")
    if "gainer" in universe:
        buckets.append("gainer")
    if "loser" in universe:
        buckets.append("loser")
    if not buckets:
        # No membership in the session screens: it came in purely as chat commentary.
        buckets.append("theme" if rec.get("mentions") else "watchlist")
    return buckets


# Below this, a move is noise and should not flip a commentary mention's direction.
DIRECTION_THRESHOLD_PCT = 1.0


def _direction_sign(rec, bucket_names):
    """Sign correction for commentary-only mentions.

    A ticker that arrived purely as chat commentary gets a positive 'theme' or
    'watchlist' weight by default -- but a name discussed *because it fell* is not
    a long candidate. When the only buckets are commentary buckets and the record
    carries a verified move beyond the noise threshold, that move's sign wins.
    Explicit gainer/loser/earnings buckets already encode direction, so they are
    left alone.
    """
    if not set(bucket_names) <= {"theme", "watchlist"}:
        return 1.0
    move = rec.get("move_pct")
    if move is None or abs(move) < DIRECTION_THRESHOLD_PCT:
        return 1.0
    return 1.0 if move > 0 else -1.0


def _consensus_multiplier(n_sources: int) -> float:
    return CONSENSUS_WEIGHT.get(n_sources, CONSENSUS_CAP)


def _soft_multiplier(flags: List[str]) -> float:
    mult = 1.0
    for flag in flags:
        mult *= SOFT_PENALTIES.get(flag, 1.0)
    return mult


def score_ticker(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Score one ticker record. Pure: no I/O, no globals mutated."""
    # dict.fromkeys dedups while preserving first-seen order -- a set() here would
    # make n_sources stable but the echoed source list order arbitrary.
    sources = list(dict.fromkeys(rec.get("mentions") or []))
    flags = list(rec.get("flags") or [])

    consensus = _consensus_multiplier(len(sources))
    quality = CATALYST_QUALITY.get(rec.get("catalyst_quality", "unverified"), 0.40)
    bucket_names = _buckets_for(rec)
    # Sum rather than max: a name that is BOTH an earnings drop and a session loser
    # is more decisively one-directional than a name that is only one of the two.
    bucket = sum(BUCKET_WEIGHT.get(b, 0.0) for b in bucket_names)
    bucket *= _direction_sign(rec, bucket_names)
    soft = _soft_multiplier(flags)

    disqualifiers = [f for f in flags if f in HARD_DISQUALIFIERS]
    pre_weekend = consensus * bucket * quality * soft

    delta = rec.get("weekend_delta", "unchanged")
    weekend_mult = _weekend_multiplier(delta, 1 if pre_weekend >= 0 else -1)
    raw = pre_weekend * weekend_mult
    score = 0.0 if disqualifiers else raw

    return {
        "symbol": rec["symbol"],
        "name": rec.get("name", ""),
        "cluster": rec.get("cluster", "unclassified"),
        "score": round(score, 3),
        "raw_score": round(raw, 3),
        "direction": "LONG" if raw > 0 else ("SHORT/AVOID" if raw < 0 else "NEUTRAL"),
        "n_sources": len(sources),
        "sources": sources,
        "buckets": bucket_names,
        "consensus_mult": consensus,
        "bucket_weight": round(bucket, 3),
        "quality_mult": quality,
        "soft_mult": round(soft, 3),
        "weekend_delta": delta,
        "weekend_mult": round(weekend_mult, 3),
        "weekend_note": rec.get("weekend_note", ""),
        "pre_weekend_score": round(pre_weekend, 3),
        "disqualified": bool(disqualifiers),
        "disqualify_reasons": [HARD_DISQUALIFIERS[f] for f in disqualifiers],
        "flags": flags,
        "move_pct": rec.get("move_pct"),
        "close": rec.get("close"),
        "catalyst": rec.get("catalyst", ""),
        "flag_note": rec.get("flag_note", ""),
        "url": rec.get("url"),
    }


def dedupe(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse duplicate symbols, merging their mentions/flags/universe.

    The research set is assembled by hand from several transcripts, so the same
    ticker genuinely can be entered twice. Merging (rather than last-wins) means a
    duplicate entry ADDS its sources instead of silently discarding the earlier
    ones -- which is exactly the consensus signal we are trying to measure.
    """
    merged: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for rec in records:
        sym = rec["symbol"].strip().upper()
        if sym not in merged:
            clone = dict(rec)
            clone["symbol"] = sym
            clone["mentions"] = list(rec.get("mentions") or [])
            clone["flags"] = list(rec.get("flags") or [])
            clone["universe"] = list(rec.get("universe") or [])
            merged[sym] = clone
            order.append(sym)
            continue
        tgt = merged[sym]
        for key in ("mentions", "flags", "universe"):
            tgt[key] = list(dict.fromkeys(tgt[key] + list(rec.get(key) or [])))
        # A verified record always beats an unverified one for the numeric fields.
        if rec.get("verified") and not tgt.get("verified"):
            for key in ("move_pct", "close", "catalyst", "catalyst_quality", "url", "verified"):
                if rec.get(key) is not None:
                    tgt[key] = rec[key]
    return [merged[s] for s in order]


def rank(dataset: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Dedup, score, and rank the whole dataset.

    Sort key is (-score, -n_sources, symbol). The trailing symbol makes ties break
    alphabetically instead of by input order, so re-ordering the JSON file by hand
    can never change the output ranking.
    """
    scored = [score_ticker(r) for r in dedupe(dataset["tickers"])]
    scored.sort(key=lambda r: (-r["score"], -r["n_sources"], r["symbol"]))
    for i, row in enumerate(scored, start=1):
        row["rank"] = i
    return scored


def cluster_exposure(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    """Count admitted LONG candidates per correlation cluster.

    This is the concentration check. On this particular session most of the
    strength sat in one AI-capex trade wearing eleven different tickers, so
    'five positions' would have been one bet at five times the intended size.
    """
    counts: Dict[str, int] = {}
    for row in rows:
        if row["direction"] == "LONG" and not row["disqualified"]:
            counts[row["cluster"]] = counts.get(row["cluster"], 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def apply_cluster_cap(rows: List[Dict[str, Any]], max_per_cluster: int = 2) -> List[Dict[str, Any]]:
    """Mark all but the top-N ranked longs in each cluster as cluster-capped.

    Deliberately does not drop them -- the alternates stay visible and ranked so a
    capped name can be promoted if one ahead of it fails its mechanical gate.
    """
    seen: Dict[str, int] = {}
    out = []
    for row in rows:
        row = dict(row)
        if row["direction"] == "LONG" and not row["disqualified"]:
            n = seen.get(row["cluster"], 0)
            row["cluster_capped"] = n >= max_per_cluster
            row["cluster_slot"] = n + 1
            seen[row["cluster"]] = n + 1
        else:
            row["cluster_capped"] = False
            row["cluster_slot"] = None
        out.append(row)
    return out


# --- Tuesday triage -------------------------------------------------------------
# A single deterministic decision tree mapping a scored row to one action. Order is
# load-bearing: each branch is strictly more permissive than the one above it, so a
# name can only ever be downgraded by an earlier check, never rescued by a later one.
TRIAGE_ACTIONS = {
    "DISQUALIFIED": "Never trade under these rules. The reason is structural, not an opinion.",
    "AVOID_LONG":   "Do not buy. Guide-down / repricing drift runs WITH the surprise, not against it.",
    "STAND_ASIDE":  "Weekend development argues against the thesis. No position until it resolves.",
    "GAP_WATCH":    "Live catalyst likely to gap the entry away. Trade ONLY if the open gap clears the cap.",
    "ALTERNATE":    "Cluster-capped. Promote only if a primary in the same cluster fails its gate.",
    "PRIMARY":      "Eligible. Trade if the regime gate, mechanical gate and entry trigger all clear.",
}


def triage(row: Dict[str, Any]) -> Dict[str, Any]:
    """Map one scored row to exactly one Tuesday action."""
    if row["disqualified"]:
        action, why = "DISQUALIFIED", (row["disqualify_reasons"] or ["structural"])[0]
    elif row["direction"] == "SHORT/AVOID":
        action, why = "AVOID_LONG", "scored negative -- deteriorating, not a dip"
    elif row.get("weekend_delta") == "adverse":
        action, why = "STAND_ASIDE", row.get("weekend_note", "adverse weekend development")
    elif "gap_risk_high" in row["flags"]:
        action, why = "GAP_WATCH", "live weekend catalyst -- the move may already be gone at the open"
    elif row.get("cluster_capped"):
        action, why = "ALTERNATE", f"{row['cluster']} slot {row['cluster_slot']} -- concentration cap"
    else:
        action, why = "PRIMARY", "clears ranking; still subject to every gate"
    return {**row, "action": action, "action_why": why,
            "action_meaning": TRIAGE_ACTIONS[action]}


def triage_all(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [triage(r) for r in rows]
