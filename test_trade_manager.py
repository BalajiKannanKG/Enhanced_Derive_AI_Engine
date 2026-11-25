"""Tests for trade_manager: break-even, trailing stop, take-profit removal and their safety invariants."""
import random
import unittest

from trade_manager import ManagementConfig, best_price_since, config_for_profile, decide

CFG = ManagementConfig()           # be 1.0R, trail from 1.5R at 2.0 ATR, tighten after 4R, step 0.1 ATR
ATR = 2.0
ENTRY = 1000.0
RISK = 3.0                         # a 1.5 ATR stop: R = 3.0 points


def buy(best, bid=None, sl=997.0, tp=1006.0, spread=0.2, **kw):
    bid = ENTRY + (best - ENTRY) * 0.9 if bid is None else bid     # price has pulled back a little from the best
    return decide("BUY", ENTRY, sl, tp, ATR, bid, bid + spread, RISK, best, CFG, **kw)


class RuleTests(unittest.TestCase):
    def test_losing_or_flat_trade_is_left_alone(self):
        for bid in (990.0, 997.5, 1000.0, 1002.0):               # deep loss, near the stop, flat, small profit
            d = buy(best=max(bid, 1002.0), bid=bid)
            self.assertEqual((d.phase, d.new_sl, d.new_tp), ("hold", None, None), f"bid {bid}")

    def test_no_early_exit_ever_requested(self):
        # The manager can only return a stop level or a TP change: there is no 'close' decision to ask for.
        d = buy(best=1001.0, bid=994.0)
        self.assertIsNone(d.new_sl)

    def test_break_even_at_one_r_with_buffer(self):
        d = buy(best=1003.0, bid=1002.9)                          # exactly 1.0R
        self.assertEqual(d.phase, "breakeven")
        self.assertAlmostEqual(d.new_sl, ENTRY + 0.1 * ATR)       # 0.2 buffer
        self.assertIsNone(d.new_tp, "the target stays until trailing starts")

    def test_break_even_buffer_covers_a_wide_spread(self):
        d = buy(best=1003.0, bid=1002.9, spread=0.6)
        self.assertAlmostEqual(d.new_sl, ENTRY + 0.6)

    def test_below_one_r_nothing_happens(self):
        d = buy(best=1002.9, bid=1002.8)
        self.assertEqual(d.phase, "hold")

    def test_trailing_follows_the_best_price(self):
        d = buy(best=1010.0, bid=1009.0)                          # 3.33R
        self.assertEqual(d.phase, "trail")
        self.assertAlmostEqual(d.new_sl, 1010.0 - CFG.trail_atr_mult * ATR)       # 1006.0
        self.assertEqual(d.new_tp, 0.0, "the fixed target is removed so the trail is the exit")

    def test_trail_never_sits_below_break_even(self):
        d = buy(best=1004.6, bid=1004.5)                          # 1.53R: trail level 1000.6 > BE 1000.2
        self.assertEqual(d.phase, "trail")
        self.assertGreaterEqual(d.new_sl, ENTRY + 0.2)

    def test_trail_tightens_after_a_big_run(self):
        d = buy(best=1013.0, bid=1012.5)                          # 4.33R
        self.assertAlmostEqual(d.new_sl, 1013.0 - CFG.trail_atr_mult * CFG.trail_tight_factor * ATR)

    def test_pullback_does_not_loosen_the_stop(self):
        d = buy(best=1010.0, bid=1007.0, sl=1006.0)               # SL already at the trail level
        self.assertIsNone(d.new_sl)
        d = buy(best=1010.0, bid=1007.0, sl=1008.5)               # SL already tighter than the trail
        self.assertIsNone(d.new_sl)

    def test_small_improvements_are_ignored(self):
        d = buy(best=1010.0, bid=1009.5, sl=1005.9)               # would move by 0.1 < step of 0.2
        self.assertIsNone(d.new_sl)

    def test_take_profit_kept_when_configured_to(self):
        cfg = ManagementConfig(remove_tp_when_trailing=False)
        d = decide("BUY", ENTRY, 997.0, 1006.0, ATR, 1009.0, 1009.2, RISK, 1010.0, cfg)
        self.assertIsNone(d.new_tp)

    def test_missing_stop_gets_one(self):
        d = buy(best=1010.0, bid=1009.0, sl=0.0)
        self.assertAlmostEqual(d.new_sl, 1006.0)

    def test_waits_when_the_stop_would_be_too_close_to_the_market(self):
        d = buy(best=1004.0, bid=1000.25, min_stop_distance=0.5)  # BE level 1000.2 is only 0.05 under the market
        self.assertIsNone(d.new_sl)
        self.assertIn("waiting", d.reason)

    def test_respects_the_brokers_minimum_distance(self):
        d = buy(best=1010.0, bid=1006.5, min_stop_distance=1.0)   # trail 1006.0 is 0.5 away: too close, so fall back to BE
        self.assertAlmostEqual(d.new_sl, ENTRY + 0.2)

    def test_falls_back_to_break_even_when_the_price_has_already_dropped_below_the_trail(self):
        d = buy(best=1011.0, bid=1004.0)                          # trail level 1007.0 is above the market
        self.assertEqual(d.phase, "trail")
        self.assertAlmostEqual(d.new_sl, ENTRY + 0.2)
        self.assertIn("beyond the current price", d.reason)
        self.assertEqual(d.new_tp, 0.0)

    def test_does_nothing_when_even_break_even_cannot_be_placed(self):
        d = buy(best=1011.0, bid=1000.1)                          # price is back at the entry: BE level is above it
        self.assertIsNone(d.new_sl)
        self.assertIn("waiting", d.reason)
        self.assertIsNone(d.new_tp, "the target stays: the trade has no protective stop yet")

    def test_target_is_removed_when_the_stop_is_already_beyond_break_even(self):
        d = buy(best=1011.0, bid=1004.0, sl=1005.0, tp=1020.0)    # the stop was already moved up (e.g. by hand)
        self.assertIsNone(d.new_sl)
        self.assertEqual(d.new_tp, 0.0)

    def test_fallback_is_not_repeated_once_applied(self):
        first = buy(best=1011.0, bid=1004.0)
        again = decide("BUY", ENTRY, first.new_sl, 0.0, ATR, 1004.0, 1004.2, RISK, 1011.0, CFG)
        self.assertIsNone(again.new_sl)

    def test_sell_is_the_mirror_image(self):
        d = decide("SELL", ENTRY, 1003.0, 994.0, ATR, 990.8, 991.0, RISK, 990.0, CFG)   # 3.33R in profit
        self.assertEqual(d.phase, "trail")
        self.assertAlmostEqual(d.new_sl, 990.0 + CFG.trail_atr_mult * ATR)
        self.assertEqual(d.new_tp, 0.0)

    def test_invalid_input_holds(self):
        bad = [("HOLD", ENTRY, 997, 1006, ATR, 1009.0, 1009.2, RISK, 1010.0), ("BUY", float("nan"), 997, 1006, ATR, 1009.0, 1009.2, RISK, 1010.0),
               ("BUY", ENTRY, 997, 1006, 0.0, 1009.0, 1009.2, RISK, 1010.0), ("BUY", ENTRY, 997, 1006, ATR, 1009.0, 1009.2, 0.0, 1010.0),
               ("BUY", ENTRY, 997, 1006, ATR, 0.0, 1009.2, RISK, 1010.0), ("BUY", ENTRY, 997, 1006, ATR, 1009.0, 1009.2, RISK, float("inf"))]
        for args in bad:
            d = decide(*args, CFG)
            self.assertEqual((d.phase, d.new_sl, d.new_tp), ("hold", None, None), args)

    def test_applying_the_decision_makes_the_next_one_a_no_op(self):
        d1 = buy(best=1010.0, bid=1009.0)
        d2 = decide("BUY", ENTRY, d1.new_sl, 0.0, ATR, 1009.0, 1009.2, RISK, 1010.0, CFG)
        self.assertIsNone(d2.new_sl)
        self.assertIsNone(d2.new_tp)


class PropertyTests(unittest.TestCase):
    def random_case(self, rng):
        direction = rng.choice(("BUY", "SELL"))
        atr = rng.uniform(0.2, 6.0)
        risk = atr * rng.uniform(0.8, 3.0)
        d = 1 if direction == "BUY" else -1
        entry = rng.uniform(500, 3000)
        best = entry + d * rng.uniform(-2.0, 8.0) * risk
        mark = best - d * rng.uniform(0.0, 3.0) * atr
        spread = rng.uniform(0.0, 0.4) * atr
        bid = mark if d > 0 else mark - spread
        ask = bid + spread
        sl = entry - d * risk * rng.uniform(0.0, 1.0) if rng.random() < 0.85 else (entry + d * rng.uniform(0.0, 2.0) * risk if rng.random() < 0.5 else 0.0)
        tp = entry + d * risk * rng.uniform(1.0, 4.0) if rng.random() < 0.7 else 0.0
        return direction, d, entry, sl, tp, atr, bid, ask, risk, best, rng.uniform(0.0, 0.5) * atr

    def test_invariants_over_many_random_trades(self):
        rng = random.Random(20261122)
        acted = 0
        for _ in range(20000):
            side, d, entry, sl, tp, atr, bid, ask, risk, best, min_dist = self.random_case(rng)
            dec = decide(side, entry, sl, tp, atr, bid, ask, risk, best, CFG, min_stop_distance=min_dist)
            mark = bid if d > 0 else ask
            if dec.new_sl is not None:
                acted += 1
                self.assertTrue(sl <= 0 or d * (dec.new_sl - sl) > 0, "the stop must only move in the profitable direction")
                self.assertGreater(d * (mark - dec.new_sl), 0, "the stop must sit on the safe side of the market")
                self.assertGreaterEqual(d * (mark - dec.new_sl) + 1e-9, min_dist, "the broker's minimum distance must be respected")
                self.assertGreaterEqual(d * (dec.new_sl - entry), -1e-9, "a managed stop is never worse than break-even")
            if dec.phase == "hold":
                self.assertIsNone(dec.new_sl)
                self.assertIsNone(dec.new_tp)
            if dec.new_tp is not None:
                self.assertEqual(dec.new_tp, 0.0)
                self.assertEqual(dec.phase, "trail")
                protected = dec.new_sl is not None or (sl > 0 and d * (sl - entry) >= 0)
                self.assertTrue(protected, "the target may only be dropped once a protective stop is in place")
        self.assertGreater(acted, 1000, "the property test must actually exercise stop moves")

    def test_sell_decisions_mirror_buy_decisions(self):
        rng = random.Random(7)
        k = 5000.0
        for _ in range(5000):
            side, d, entry, sl, tp, atr, bid, ask, risk, best, min_dist = self.random_case(rng)
            a = decide(side, entry, sl, tp, atr, bid, ask, risk, best, CFG, min_stop_distance=min_dist)
            mirror = "SELL" if side == "BUY" else "BUY"
            msl = (k - sl) if sl > 0 else 0.0
            mtp = (k - tp) if tp > 0 else 0.0
            b = decide(mirror, k - entry, msl, mtp, atr, k - ask, k - bid, risk, k - best, CFG, min_stop_distance=min_dist)
            self.assertEqual(a.phase, b.phase)
            self.assertEqual(a.new_sl is None, b.new_sl is None)
            if a.new_sl is not None:
                self.assertAlmostEqual(k - a.new_sl, b.new_sl, places=6)
            self.assertEqual(a.new_tp, b.new_tp)

    def test_decisions_are_repeatable_and_settle(self):
        rng = random.Random(99)
        for _ in range(3000):
            side, d, entry, sl, tp, atr, bid, ask, risk, best, min_dist = self.random_case(rng)
            first = decide(side, entry, sl, tp, atr, bid, ask, risk, best, CFG, min_stop_distance=min_dist)
            self.assertEqual(first, decide(side, entry, sl, tp, atr, bid, ask, risk, best, CFG, min_stop_distance=min_dist))
            sl2 = first.new_sl if first.new_sl is not None else sl
            tp2 = first.new_tp if first.new_tp is not None else tp
            second = decide(side, entry, sl2, tp2, atr, bid, ask, risk, best, CFG, min_stop_distance=min_dist)
            self.assertIsNone(second.new_sl, "after applying a stop move the same inputs must not ask for another")
            self.assertIsNone(second.new_tp)


class HelperTests(unittest.TestCase):
    CANDLES = [{"time": 100, "high": 1050.0, "low": 940.0}, {"time": 160, "high": 1004.0, "low": 998.0},
               {"time": 220, "high": 1010.0, "low": 1001.0}, {"time": 280, "high": None, "low": 1003.0}, {"high": 9999.0, "low": 1.0}]

    def test_best_price_ignores_candles_that_started_before_the_entry(self):
        self.assertEqual(best_price_since("BUY", self.CANDLES, 150, mark=1006.0), 1010.0)
        self.assertEqual(best_price_since("SELL", self.CANDLES, 150, mark=1006.0), 998.0)

    def test_best_price_always_includes_the_current_price(self):
        self.assertEqual(best_price_since("BUY", [], 0, mark=1234.5), 1234.5)
        self.assertEqual(best_price_since("BUY", self.CANDLES, 150, mark=1500.0), 1500.0)

    def test_config_from_profile(self):
        cfg = config_for_profile({"break_even_trigger_r": 1.2, "trail_atr_mult": 3.0})
        self.assertEqual((cfg.be_trigger_r, cfg.trail_atr_mult, cfg.trail_start_r), (1.2, 3.0, ManagementConfig().trail_start_r))


if __name__ == "__main__":
    unittest.main()
