# -*- coding: utf-8 -*-
"""Rule and cycle tests. Run: python -m unittest discover -s tests"""
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from recovery import desk, indicators as ind, rules
from recovery.providers import ET
from recovery.store import LocalStore

CFG = rules.RuleConfig()


def bar(t: datetime, o, h, l, c, v=1000.0, vw=None):
    return {"t": t.isoformat(), "date": t.date().isoformat(), "o": o, "h": h, "l": l, "c": c, "v": v, "vw": vw}


class Indicators(unittest.TestCase):
    def test_sma_and_rsi_bounds(self):
        xs = [float(i) for i in range(1, 31)]
        self.assertEqual(ind.sma(xs, 5), 28.0)
        self.assertIsNone(ind.sma(xs, 31))
        self.assertEqual(ind.wilder_rsi_series(xs)[-1], 100.0)   # only gains
        self.assertIsNone(ind.wilder_rsi_series(xs)[13])

    def test_vwap_prefers_bar_vwap(self):
        bs = [{"h": 11, "l": 9, "c": 10, "v": 100, "vw": 10.5}, {"h": 12, "l": 10, "c": 11, "v": 300, "vw": None}]
        self.assertAlmostEqual(ind.session_vwap(bs), (10.5 * 100 + 11 * 300) / 400)


class Battery(unittest.TestCase):
    def test_levels(self):
        self.assertEqual(rules.battery([5, 4, 3, 2, 1]), 4)
        self.assertEqual(rules.battery([5, 4, 3, 2, -1]), 3)
        self.assertEqual(rules.battery([5, 4, 6]), 2)
        self.assertEqual(rules.battery([1, 2, 3]), 0)          # fading three days
        self.assertEqual(rules.battery([2, 3, 1]), 1)          # positive, not expanding
        self.assertEqual(rules.battery([-0.1, 2, 3]), -1)
        self.assertEqual(rules.battery([0.0, 1, 1]), -1)        # at or below zero is RED


class AntiKnife(unittest.TestCase):
    # Worked examples from candidates_latest.csv (2026-10-21) and the 09-09 workbook.
    def test_ak_d_examples(self):
        self.assertEqual(rules.ak_d(434.53, 425.11, 424.74), "PASS")    # AMGN higher low
        self.assertEqual(rules.ak_d(263.97, 263.28, 264.07), "HOLD")    # IQV: prior was a lower low
        self.assertEqual(rules.ak_d(354.38, 354.11, 365.90), "HOLD")    # MDB
        self.assertEqual(rules.ak_d(164.19, 167.22, 160.75), "FAIL")    # CRDO took the low
        self.assertEqual(rules.ak_d(None, 1, 1), "NOT_SCORED")

    def _session(self, closes, lows, vw):
        t0 = datetime(2026, 10, 21, 9, 30, tzinfo=ET)
        return [bar(t0 + timedelta(minutes=5 * i), c, c + 0.1, l, c, 1000, vw) for i, (c, l) in enumerate(zip(closes, lows))]

    def test_ak_s_works_on_partial_session(self):
        # Four bars in: low printed on bar 0, held 3 bars, last close above VWAP -> PASS.
        s = self._session([10, 10.2, 10.4, 10.6], [9.5, 10.0, 10.1, 10.3], None)
        self.assertEqual(rules.ak_s(s, True, CFG)["ak_s"], "PASS")

    def test_ak_s_fail_and_hold(self):
        falling = self._session([10, 9.8, 9.6], [9.9, 9.7, 9.5], 10.0)
        self.assertEqual(rules.ak_s(falling, True, CFG)["ak_s"], "FAIL")
        mixed = self._session([10, 10.5], [9.0, 10.2], 9.8)       # above VWAP, low held only 1 bar
        self.assertEqual(rules.ak_s(mixed, True, CFG)["ak_s"], "HOLD")
        self.assertEqual(rules.ak_s(mixed, False, CFG)["ak_s"], "PREMARKET_NOT_APPLICABLE")
        self.assertEqual(rules.ak_s([], True, CFG)["ak_s"], "NO_5MIN_FEED")

    def test_combined_never_promotes(self):
        self.assertEqual(rules.ak_combined("PASS", "PASS"), "PASS")
        self.assertEqual(rules.ak_combined("PASS", "HOLD"), "HOLD")
        self.assertEqual(rules.ak_combined("HOLD", "FAIL"), "FAIL")
        self.assertEqual(rules.ak_combined("PASS", "PREMARKET_NOT_APPLICABLE"), "NOT_SCORED")


class Gates(unittest.TestCase):
    def test_rvol_floor_and_borderline(self):
        self.assertEqual(rules.rvol_label(0.80, CFG), "RVOL20 0.80 VOL_BORDERLINE")
        self.assertEqual(rules.rvol_label(0.95, CFG), "RVOL20 0.95")
        rv, src = rules.rvol20([100.0] * 20 + [80.0], [])
        self.assertAlmostEqual(rv, 0.8)
        self.assertEqual(src, "SIP_RTH")

    def test_dd_band(self):
        self.assertEqual(rules.dd_band(1.9, CFG), "out")
        self.assertEqual(rules.dd_band(10.7, CFG), "shallow")
        self.assertEqual(rules.dd_band(30, CFG), "classic")
        self.assertEqual(rules.dd_band(61, CFG), "out")

    def test_rsi_tokens(self):
        base = {"rsi0": 50, "rsi1": 48}
        self.assertIsNone(rules.rsi_token(base, CFG))
        self.assertEqual(rules.rsi_token({"rsi0": 67, "rsi1": 60}, CFG), "RSI LATE")
        self.assertEqual(rules.rsi_token({"rsi0": 35, "rsi1": 33}, CFG), "RSI WATCH")
        self.assertEqual(rules.rsi_token({"rsi0": 72, "rsi1": 70}, CFG), "RSI FAIL")
        self.assertEqual(rules.rsi_token({"rsi0": 50, "rsi1": 51}, CFG), "RSI NOT RISING")


def uptrend_bars(n=260, end=date(2026, 10, 21)):
    """Steady uptrend with an accelerating finish: stack Y, MACD > 0, expanding histogram."""
    out, d, px = [], end - timedelta(days=int(n * 1.5)), 50.0
    while len(out) < n:
        d += timedelta(days=1)
        if d.weekday() >= 5:
            continue
        px *= 1.004 if len(out) < n - 5 else 1.02
        t = datetime(d.year, d.month, d.day, tzinfo=ET)
        out.append(bar(t, px * 0.995, px * 1.01, px * 0.99, px, 1_000_000))
    return out


def cap_bars(end=date(2026, 10, 21)):
    """Flat, a 30% slide, then a choppy recovery: stack N, Rule 1 passes (RSI ~59 rising)."""
    pxs = [100.0] * 190 + [100 - 0.75 * i for i in range(1, 41)]
    p = pxs[-1]
    for i in range(35):
        p *= 1.009 if i % 3 else 0.99
        pxs.append(p)
    days, d = [], end
    while len(days) < len(pxs):
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return [bar(datetime(x.year, x.month, x.day, tzinfo=ET), px, px * 1.005, px * 0.995, px, 1e6)
            for x, px in zip(reversed(days), pxs)]


class Classify(unittest.TestCase):
    def test_tier1_needs_rvol(self):
        bs = uptrend_bars()
        m = rules.daily_metrics(bs, [1_000_000.0] * 21, CFG)
        c = rules.classify_daily(m, CFG)
        self.assertEqual((c["stack"], c["status"]), ("Y", "TIER1"))
        self.assertGreaterEqual(c["battery"], 2)
        low = rules.daily_metrics(bs, [1_000_000.0] * 20 + [500_000.0], CFG)
        c2 = rules.classify_daily(low, CFG)
        self.assertEqual(c2["status"], "TIER2")
        self.assertIn("< 0.80", c2["fail_gate"])

    def test_short_history_is_watch(self):
        m = rules.daily_metrics(uptrend_bars(120), None, CFG)
        self.assertEqual(rules.classify_daily(m, CFG)["fail_gate"], "no SMA200")

    def test_card_format(self):
        bs = uptrend_bars()
        m = rules.daily_metrics(bs, [1_000_000.0] * 21, CFG)
        c = rules.classify_daily(m, CFG)
        pipe, close = rules.card_lines(1, "TEST", m, c, "TIER 1 Paper candidate")
        self.assertTrue(pipe.startswith("1. TEST | Mom | DD "))
        self.assertIn("| STACK Y |", pipe)
        self.assertIn("EXIT under SMA20", pipe)
        self.assertTrue(close.startswith("CLOSE "))


class FakeHub:
    """Stands in for DataHub: a fixed calendar, daily bars and 5-minute bars."""
    feed, feed_note, massive_key = "sip", "", ""

    def __init__(self, daily, five):
        self.daily, self.five = daily, five
        self.stats = {"calls": 0, "rate_limited": 0, "errors": []}

    def alpaca_calendar(self, start, end):
        out, d = [], start
        while d <= end:
            if d.weekday() < 5:
                out.append({"date": d.isoformat(), "open": "09:30", "close": "16:00"})
            d += timedelta(days=1)
        return out

    def daily_bars(self, symbols, start, end, min_bars=60):
        got = {s: self.daily[s] for s in symbols if s in self.daily}
        return got, {s: "alpaca_sip" for s in got}

    def rth_volumes(self, symbols, start, end):
        return {s: {b["date"]: b["v"] for b in self.daily[s][-25:]} for s in symbols if s in self.daily}

    def alpaca_news(self, symbols, start):
        return {}

    def alpaca_bars(self, symbols, tf, start, end):
        return {s: self.five.get(s, []) for s in symbols}


class Cycle(unittest.TestCase):
    def test_session_reset_and_staleness(self):
        daily = {"AAA": uptrend_bars(end=date(2026, 10, 21))}
        session = date(2026, 10, 22)
        t0 = datetime(2026, 10, 22, 9, 30, tzinfo=ET)
        five = {"AAA": [bar(t0 + timedelta(minutes=5 * i), 100, 101, 99, 100.5) for i in range(6)]}
        env = {"RECOVERY_UNIVERSE": "AAA,ZZZ"}
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalStore(tmp)
            pre = desk.run_cycle(store, FakeHub(daily, five), datetime(2026, 10, 22, 8, 30, tzinfo=ET), env)
            self.assertEqual(pre["phase"], "premarket")
            self.assertEqual(pre["qualified_from_session"], "2026-10-21")
            self.assertEqual(pre["health"]["scanned"], "1 of 2 scanned")
            rows = {r["ticker"]: r for r in pre["rows"]}
            self.assertEqual(rows["ZZZ"]["status"], "NOT_SCANNED")
            self.assertEqual(rows["AAA"]["status"], "TIER1")

            live = desk.run_cycle(store, FakeHub(daily, five), datetime(2026, 10, 22, 10, 2, tzinfo=ET), env)
            self.assertEqual(live["health"]["status"], "DEGRADED")     # ZZZ not scanned
            self.assertIsNotNone(live["health"]["last_bar_end_et"])
            # 10:02 with bars ending 10:00 is fresh; at 10:30 the same bars are stale.
            stale = desk.run_cycle(store, FakeHub(daily, five), datetime(2026, 10, 22, 10, 30, tzinfo=ET), env)
            self.assertEqual(stale["health"]["status"], "STALE")
            aaa = {r["ticker"]: r for r in stale["rows"]}["AAA"]
            self.assertFalse(aaa["paper_candidate"])
            self.assertIn("feed stale", aaa["blocked_by"])
            self.assertTrue(any(c["ticker"] == "AAA" for c in stale["changes_today"]))
            self.assertTrue(os.path.exists(os.path.join(tmp, f"changes_{session}.jsonl")))

            post = desk.run_cycle(store, FakeHub(daily, five), datetime(2026, 10, 22, 16, 20, tzinfo=ET), env)
            self.assertEqual(post["phase"], "postmarket")
            self.assertTrue(os.path.exists(os.path.join(tmp, f"session_{session}.json")))

    def test_cap_names_get_ak_from_today_only(self):
        daily = {"CAP": cap_bars()}
        m = rules.daily_metrics(daily["CAP"], None, CFG)
        self.assertEqual(rules.classify_daily(m, CFG)["status"], "CAP_SETUP")
        ref_low = m["ref_low"]
        t0 = datetime(2026, 10, 22, 9, 30, tzinfo=ET)

        def session(first_low):
            # Low printed on the first bar, then higher closes, bar VWAP under the last close.
            return [bar(t0 + timedelta(minutes=5 * i), 77, 78, first_low if i == 0 else 76.8,
                        76.5 + 0.1 * i, 1000, 76.5) for i in range(12)]

        env = {"RECOVERY_UNIVERSE": "CAP"}
        at_1030 = datetime(2026, 10, 22, 10, 32, tzinfo=ET)
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalStore(tmp)
            ok = desk.run_cycle(store, FakeHub(daily, {"CAP": session(ref_low + 0.1)}), at_1030, env)["rows"][0]
            self.assertEqual((ok["ak_d"], ok["ak_s"], ok["status"]), ("PASS", "PASS", "CAP_PAPER"))
            self.assertTrue(ok["paper_candidate"])

            knife = desk.run_cycle(store, FakeHub(daily, {"CAP": session(ref_low - 0.1)}), at_1030, env)["rows"][0]
            self.assertEqual((knife["ak_d"], knife["status"]), ("FAIL", "CAP_AK_BLOCK"))

            nofeed = desk.run_cycle(store, FakeHub(daily, {}), at_1030, env)
            self.assertEqual(nofeed["health"]["status"], "STALE")
            self.assertEqual(nofeed["rows"][0]["status"], "CAP_WAIT_AK")      # no 5-min feed never passes

            # Next morning: yesterday's PASS must not carry into the new session.
            daily["CAP"] = cap_bars(date(2026, 10, 22))
            pre = desk.run_cycle(store, FakeHub(daily, {}), datetime(2026, 10, 23, 8, 30, tzinfo=ET), env)["rows"][0]
            self.assertEqual(pre["ak_s"], "PREMARKET_NOT_APPLICABLE")
            self.assertNotEqual(pre["status"], "CAP_PAPER")
            with open(os.path.join(tmp, "latest.json")) as f:
                self.assertEqual(json.load(f)["orders"], "OFF")


if __name__ == "__main__":
    unittest.main()
