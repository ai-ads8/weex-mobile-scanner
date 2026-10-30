import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import signal_engine as eng  # noqa: E402
from trade_analyzer import analyze_position  # noqa: E402


def v51_score(ratio, risk_pct, spread_pct, candle_age, quote_age, alignment=3):
    """Verbatim copy of the v5.1 formula (app.py lines 61-68 at commit 7a90eff)."""
    volume = min(25, max(0, (ratio - 1) * 12))
    structure = 25 if alignment == 3 else 10
    freshness = max(0, 20 - candle_age / 12 - max(0, quote_age) / 5)
    execution = max(0, 15 - (spread_pct or 0) * 45)
    risk = max(0, 15 - abs(risk_pct - 1.0) * 6)
    return max(0, min(100, round(volume + structure + freshness + execution + risk)))


def series(n, start, step_ms, base=1.0, drift=0.0, vol=1.0, wiggle=0.001):
    out, p = [], base
    for i in range(n):
        o = p
        c = p * (1 + drift + (wiggle if i % 2 else -wiggle))
        out.append({"t": start + i * step_ms, "o": o, "h": max(o, c) * 1.0005, "l": min(o, c) * 0.9995,
                    "c": c, "v": vol})
        p = c
    return out


def make_market(trigger_close_mult, trigger_volume, now, downtrend=True, bar_size=None):
    """Synthetic market: downtrend on all TFs, last 1m candle breaks below the 20-bar low."""
    drift = -0.0004 if downtrend else 0.0004
    data = {}
    for tf, sec in eng.TIMEFRAMES.items():
        last_open = (int(now) // sec - 1) * sec      # last fully closed candle
        start = (last_open - 119 * sec) * 1000
        data[tf] = series(120, start, sec * 1000, drift=drift)
    a = data["1m"]
    prior_low = min(x["l"] for x in a[-21:-1])
    prev_c = a[-2]["c"]
    c = prior_low * trigger_close_mult
    o = prev_c
    lo = c * 0.9998 if bar_size is None else c * (1 - bar_size * 0.02)
    a[-1] = {"t": a[-1]["t"], "o": o, "h": max(o, c) * 1.0001, "l": min(lo, c), "c": c, "v": trigger_volume}
    info = {tf: eng.timeframe_info(v) for tf, v in data.items()}
    q = {"quoteFresh": True, "lastPrice": c, "markPrice": c, "spreadPct": 0.01,
         "bidPrice": c * 0.99995, "askPrice": c * 1.00005, "quoteAgeSec": 2}
    return data, info, q


class LegacyScore(unittest.TestCase):
    def test_matches_v51_exactly(self):
        for args in [(1.1, 0.4, 0.01, 10, 2, 2), (3.2, 1.0, 0.02, 5, 1, 3), (2.0, 2.5, 0.2, 170, 40, 3),
                     (5.0, 1.05, 0.0, 3, 0, 3), (1.5, 0.15, None, 60, -3, 2)]:
            self.assertEqual(eng.legacy_score(*args), v51_score(*args), args)

    def test_97_requires_volume_spike_full_alignment_and_1pct_stop(self):
        # Volume at least ~2.8x, 3/3 alignment, fresh, tight spread, stop ~1%: the profile of a big impulse candle.
        self.assertEqual(v51_score(3.1, 1.0, 0.01, 6, 2, 3), 99)
        self.assertLess(v51_score(3.1, 1.0, 0.01, 6, 2, 2), 85)   # 2/3 alignment caps the score
        self.assertLess(v51_score(1.5, 1.0, 0.01, 6, 2, 3), 85)   # modest volume caps the score
        self.assertLess(v51_score(3.1, 0.3, 0.01, 6, 2, 3), 95)   # tight stop loses risk points

    def test_execution_and_freshness_are_nearly_constant(self):
        # Typical spreads (0.005-0.05%) give 12.8-14.8 of 15 execution points: almost no discrimination.
        self.assertGreater(min(15 - s * 45 for s in (0.005, 0.01, 0.02, 0.05)), 12.7)


class Evaluate(unittest.TestCase):
    def setUp(self):
        self.now = 1_800_000_000 + 5   # 5 s after a minute boundary

    def test_clean_breakout_is_quality_A_or_B(self):
        data, info, q = make_market(0.9992, 1.6, self.now)
        s, why = eng.evaluate("XRPUSDT", "SCALP", data, info, self.now, q)
        self.assertIsNotNone(s, why)
        self.assertEqual(s["side"], "SHORT")
        self.assertEqual(s["triggerTf"], "1m")
        self.assertEqual(s["quality"], "A", s["warnings"])
        self.assertTrue(s["notes"])  # legacy stop sits far away: informative note

    def test_climax_candle_gets_warning_and_not_A(self):
        data, info, q = make_market(0.985, 6.0, self.now, bar_size=1.0)
        s, why = eng.evaluate("XRPUSDT", "SCALP", data, info, self.now, q)
        self.assertIsNotNone(s, why)
        self.assertTrue(any("climax" in w or "achterna" in w for w in s["warnings"]), s["warnings"])
        self.assertNotEqual(s["quality"], "A")

    def test_stale_scalp_rejected(self):
        later = self.now + 120
        data, info, q = make_market(0.9992, 1.6, self.now)
        s, why = eng.evaluate("XRPUSDT", "SCALP", data, info, later, q)
        self.assertIsNone(s)
        self.assertEqual(why, "signaal te oud")

    def test_late_alert_flagged_by_freshness(self):
        data, info, q = make_market(0.9992, 1.6, self.now)
        s, _ = eng.evaluate("XRPUSDT", "SCALP", data, info, self.now, q)
        risk = abs(s["entry"] - s["stop"])
        moved = s["entry"] - 0.8 * risk      # price ran 0.8R further in the short direction
        f = eng.freshness(s, self.now + 10, moved)
        self.assertAlmostEqual(f["driftR"], 0.8, places=2)
        self.assertFalse(f["actionable"])
        self.assertTrue(eng.freshness(s, self.now + 10, s["entry"])["actionable"])
        self.assertFalse(eng.freshness(s, self.now + 200, s["entry"])["fresh"])


class Paper(unittest.TestCase):
    def rec(self):
        s = {"id": "X", "symbol": "XRPUSDT", "side": "LONG", "mode": "SCALP", "entry": 100.0, "stop": 99.0,
             "tp1": 102.0, "tp2": 103.0, "createdAt": 1000, "score": 90, "quality": "A"}
        return eng.new_paper(s, latency_sec=20, fee_pct=0.12, slip_pct=0.0)

    def c(self, t, o, h, l, c):
        return {"t": t * 1000, "o": o, "h": h, "l": l, "c": c, "v": 1}

    def test_no_fill_before_latency_and_stop_wins_same_candle(self):
        r = self.rec()
        eng.step_paper(r, [self.c(1000, 100, 103, 98, 100)])   # opens before fill time: ignored
        self.assertEqual(r["status"], "PENDING")
        eng.step_paper(r, [self.c(1020, 100, 103, 98.5, 100)])  # both stop and TP1 inside: stop wins
        self.assertEqual(r["exitReason"], "SL")
        self.assertAlmostEqual(r["grossR"], -1.0)
        self.assertAlmostEqual(r["netR"], -1.0 - 0.12, places=3)  # 0.12% of 100 / risk 1 = 0.12R

    def test_target_and_mfe(self):
        r = self.rec()
        eng.step_paper(r, [self.c(1020, 100, 101, 99.5, 100.8), self.c(1080, 100.8, 102.5, 100.5, 102.2)])
        self.assertEqual(r["exitReason"], "TP1")
        self.assertAlmostEqual(r["grossR"], 2.0)
        self.assertGreaterEqual(r["mfeR"], 2.5)
        self.assertAlmostEqual(r["maeR"], -0.5)

    def test_skip_when_gap_beyond_stop(self):
        r = self.rec()
        eng.step_paper(r, [self.c(1020, 98.5, 99, 98, 98.7)])
        self.assertEqual(r["status"], "SKIPPED")

    def test_time_stop(self):
        r = self.rec()
        candles = [self.c(1020 + 60 * i, 100, 100.3, 99.8, 100.1) for i in range(70)]
        eng.step_paper(r, candles)
        self.assertEqual(r["exitReason"], "TIME")

    def test_summary(self):
        recs = [{"netR": x} for x in [2, -1, -1, 2, -1]]
        m = eng.summarize(recs)
        self.assertEqual(m["n"], 5)
        self.assertAlmostEqual(m["expectancyR"], 0.2)
        self.assertEqual(m["profitFactor"], round(4 / 3, 2))
        self.assertEqual(m["maxDrawdownR"], -2.0)
        self.assertFalse(m["reliable"])


class Analyzer(unittest.TestCase):
    def test_xrp_screenshot(self):
        # WEEX: notional 1945.632 at mark 1.2472 -> qty 1560; PNL -15.74, ROI -127.24% (on entry margin)
        res = analyze_position("SHORT", 1.2371, 1.2472, 156, notional=1560 * 1.2371)
        self.assertAlmostEqual(res["estimatedPnl"], -15.76, delta=0.05)
        self.assertAlmostEqual(res["estimatedRoiPct"], -127.4, delta=0.5)
        self.assertEqual(res["status"], "REDUCE EXPOSURE")       # 156x without stop

    def test_xrp_with_one_percent_stop_is_beyond_margin(self):
        res = analyze_position("SHORT", 1.2371, 1.2400, 156, margin=12.37, stop=1.2371 * 1.01)
        self.assertGreater(res["lossAtStopPctOfMargin"], 150)
        self.assertEqual(res["status"], "REDUCE EXPOSURE")

    def test_op_screenshot_profit(self):
        res = analyze_position("LONG", 0.11225, 0.11412, 96, notional=9081.5 * 0.11225)
        self.assertGreater(res["estimatedPnl"], 15)
        self.assertEqual(res["status"], "REDUCE EXPOSURE")       # 96x without stop still flagged

    def test_invalidated_and_partial(self):
        self.assertEqual(analyze_position("LONG", 100, 98.9, 5, margin=10, stop=99)["status"], "SETUP INVALIDATED")
        self.assertEqual(analyze_position("LONG", 100, 102.1, 5, margin=10, stop=99)["status"],
                         "PARTIAL EXIT CONDITION MET")
        self.assertEqual(analyze_position("LONG", 100, 101.2, 5, margin=10, stop=99)["status"], "PROTECT PROFITS")
        self.assertEqual(analyze_position("LONG", 100, 100.2, 5, margin=10, stop=99)["status"], "HOLD / MONITOR")

    def test_stop_beyond_liquidation(self):
        res = analyze_position("LONG", 100, 100, 50, margin=10, stop=97, liquidation=98.2)
        self.assertTrue(res["stopBeyondLiquidation"])
        self.assertEqual(res["status"], "REDUCE EXPOSURE")


if __name__ == "__main__":
    unittest.main()
