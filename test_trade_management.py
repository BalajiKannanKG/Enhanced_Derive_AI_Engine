"""Engine-level tests for break-even / trailing-stop management of open positions.

A fake broker records every stop change and every close request. MT5, the position snapshot, candles and the
telemetry database are all replaced, so nothing here talks to a terminal. Run with:
    python -m unittest tests.test_trade_management -v
"""
import sqlite3
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import MetaTrader5 as mt5

import Enhanced_Derive_AI_Engine as engine

SYMBOL = engine.SUPPORTED_SYMBOLS[0]
OPEN_TIME = 10_000
ATR = 2.0                    # INTRADAY: stop 1.5 ATR = 3.0 points = 1R; trail 2.0 ATR; trail starts at 1.5R


def candle(minute, high, low):
    return {"time": OPEN_TIME + 60 * minute, "high": high, "low": low, "open": low, "close": high}


class FakeBroker:
    """Records requests and applies stop changes to the shared position list."""
    def __init__(self, positions):
        self.positions, self.modifications, self.closes, self.refuse = positions, [], [], False

    def modify_position(self, ticket, stop_loss, take_profit):
        self.modifications.append((int(ticket), float(stop_loss), float(take_profit)))
        if self.refuse:
            return False, "Invalid stops", {}
        for position in self.positions:
            if position["ticket"] == ticket:
                position["sl"], position["tp"] = float(stop_loss), float(take_profit)
        return True, "ok", {}

    def close_position(self, *args, **kwargs):
        self.closes.append((args, kwargs))
        return True, "closed", {}


class ManagementTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / "telemetry.sqlite")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("CREATE TABLE trade_telemetry (id INTEGER PRIMARY KEY AUTOINCREMENT, ticket INTEGER, entry_price REAL, sl_price REAL)")
            conn.execute("INSERT INTO trade_telemetry (ticket, entry_price, sl_price) VALUES (501, 1000.0, 997.0)")   # 1R = 3.0
        self.position = {"ticket": 501, "symbol": SYMBOL, "type": "BUY", "price": 1000.0, "volume": 0.5, "sl": 997.0,
                         "tp": 1006.0, "profit": 0.0, "magic": engine.MT5_MAGIC_NUMBER, "time": OPEN_TIME}
        self.positions = [self.position]
        self.broker = FakeBroker(self.positions)
        self.market = {"bid": 1000.0, "spread": 0.2}
        self.candles = []
        self.states = {SYMBOL: {"timeframes": {"5m": {"atr": ATR}, "1m": {"candles": []}}}}
        self.account = {"positions": self.positions}
        for target, value in (
            ("LIVE_TRADING_ENABLED", True), ("TRADE_MANAGEMENT_ENABLED", True), ("agent_status", "RUNNING"),
            ("DB_PATH", self.db_path), ("mt5_bridge", self.broker),
            ("trade_management_last_attempt", {}), ("trade_management_tp_removed", set()), ("trade_initial_risk_cache", {}),
            ("active_trade_evolution_logs", {}),
        ):
            patcher = patch.object(engine, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, fn in (
            ("symbol_info_tick", lambda symbol: types.SimpleNamespace(bid=self.market["bid"], ask=self.market["bid"] + self.market["spread"])),
            ("symbol_info", lambda symbol: types.SimpleNamespace(point=0.001, trade_stops_level=0, trade_freeze_level=0, digits=3)),
        ):
            patcher = patch.object(mt5, name, fn, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)

    # -- helpers -------------------------------------------------------------------------------
    def run_cycle(self, bid=None, candles=None):
        if bid is not None:
            self.market["bid"] = bid
        if candles is not None:
            self.candles = candles
        self.states[SYMBOL]["timeframes"]["1m"]["candles"] = self.candles
        engine.manage_active_trades({"mt5_account": self.account, "symbols": self.states})

    def run_after_cooldown(self, **kw):
        engine.trade_management_last_attempt.clear()
        self.run_cycle(**kw)


class TradeManagementTests(ManagementTestBase):
    def test_a_losing_trade_is_never_touched_or_closed(self):
        for minute, bid in enumerate((999.0, 996.0, 994.0, 997.4, 995.0), start=1):
            self.run_after_cooldown(bid=bid, candles=[candle(minute, 1000.5, bid)])
        self.assertEqual(self.broker.modifications, [])
        self.assertEqual(self.broker.closes, [])
        self.assertEqual((self.position["sl"], self.position["tp"]), (997.0, 1006.0), "stop and target stay exactly as opened")

    def test_break_even_once_the_trade_has_earned_one_r(self):
        self.run_cycle(bid=1003.1, candles=[candle(1, 1003.2, 1000.5)])
        self.assertEqual(len(self.broker.modifications), 1)
        ticket, sl, tp = self.broker.modifications[0]
        self.assertAlmostEqual(sl, 1000.0 + max(0.1 * ATR, 0.2))
        self.assertEqual(tp, 1006.0, "the target is kept until trailing starts")

    def test_trailing_follows_the_best_price_and_removes_the_target_once(self):
        self.run_cycle(bid=1010.0, candles=[candle(1, 1010.2, 1000.5)])
        self.assertAlmostEqual(self.position["sl"], 1010.2 - 2.0 * ATR)
        self.assertEqual(self.position["tp"], 0.0)
        self.position["tp"] = 1020.0                       # the trader adds a target back by hand
        self.run_after_cooldown(bid=1011.0, candles=[candle(1, 1011.2, 1000.5)])      # 3.7R: still below the 4R tightening
        self.assertEqual(self.position["tp"], 1020.0, "a target added back later is respected")
        self.assertAlmostEqual(self.position["sl"], 1011.2 - 2.0 * ATR)

    def test_the_stop_never_loosens_on_a_pullback(self):
        self.run_cycle(bid=1010.0, candles=[candle(1, 1010.2, 1000.5)])
        locked = self.position["sl"]
        for bid in (1008.0, 1006.5, 1005.0, 1004.2):
            self.run_after_cooldown(bid=bid, candles=[candle(1, 1010.2, 1000.5)])
            self.assertGreaterEqual(self.position["sl"], locked)
        self.assertEqual(self.broker.closes, [])

    def test_a_spike_between_two_cycles_still_locks_break_even(self):
        # The peak happened between two cycles and the price has already fallen back below where the trail would be.
        self.run_cycle(bid=1004.0, candles=[candle(1, 1011.0, 1001.0), candle(2, 1005.0, 1003.5)])
        self.assertAlmostEqual(self.position["sl"], 1000.0 + 0.2, msg="falls back to break-even rather than leaving -1R in place")
        self.assertEqual(self.position["tp"], 0.0)

    def test_best_price_comes_from_candles_even_if_the_price_has_since_dropped(self):
        # Peak 1011.0 between cycles; the price (1009.0) is still above the trail level, so the trail is placed.
        self.run_cycle(bid=1009.0, candles=[candle(1, 1011.0, 1001.0), candle(2, 1010.0, 1008.5)])
        self.assertAlmostEqual(self.position["sl"], 1011.0 - 2.0 * ATR)

    def test_falls_back_to_other_timeframes_when_one_minute_candles_are_missing(self):
        self.states[SYMBOL]["timeframes"] = {"5m": {"atr": ATR, "candles": [candle(1, 1011.0, 1001.0), candle(6, 1010.0, 1008.5)]}}
        engine.manage_active_trades({"mt5_account": self.account, "symbols": self.states})   # bypass run_cycle: it fills the 1m list
        self.assertEqual(len(self.broker.modifications), 0, "bid is still at the entry price, below the trail and break-even rooms")
        self.market["bid"] = 1009.0
        engine.manage_active_trades({"mt5_account": self.account, "symbols": self.states})
        self.assertAlmostEqual(self.position["sl"], 1011.0 - 2.0 * ATR)

    def test_candle_that_was_running_at_entry_is_ignored(self):
        old = {"time": OPEN_TIME - 30, "high": 1050.0, "low": 990.0, "open": 1000.0, "close": 1000.0}
        self.run_cycle(bid=1001.0, candles=[old, candle(1, 1001.5, 1000.5)])
        self.assertEqual(self.broker.modifications, [], "a spike that came before the entry must not count")

    def test_original_risk_is_used_after_the_stop_has_moved(self):
        self.position["sl"] = 1000.2                         # already at break-even
        self.run_cycle(bid=1004.4, candles=[candle(1, 1004.6, 1000.5)])    # 1.53R of the ORIGINAL 3.0 risk
        self.assertEqual(len(self.broker.modifications), 1)
        self.assertGreater(self.position["sl"], 1000.2)

    def test_sell_positions_are_managed_symmetrically(self):
        self.position.update(type="SELL", sl=1003.0, tp=994.0)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE trade_telemetry SET sl_price = 1003.0")
        self.market.update(bid=989.6, spread=0.2)
        self.run_cycle(candles=[candle(1, 1000.0, 989.4)])
        self.assertAlmostEqual(self.position["sl"], 989.4 + 2.0 * ATR)
        self.assertEqual(self.position["tp"], 0.0)

    def test_only_engine_positions_on_supported_symbols_are_managed(self):
        other = dict(self.position, ticket=502, magic=999)
        foreign = dict(self.position, ticket=503, symbol="EURUSD")
        self.positions[:] = [other, foreign]
        self.run_cycle(bid=1010.0, candles=[candle(1, 1010.2, 1000.5)])
        self.assertEqual(self.broker.modifications, [])

    def test_management_can_be_switched_off(self):
        for name, value in (("TRADE_MANAGEMENT_ENABLED", False), ("LIVE_TRADING_ENABLED", False), ("agent_status", "STOPPED")):
            with self.subTest(setting=name), patch.object(engine, name, value):
                self.run_after_cooldown(bid=1010.0, candles=[candle(1, 1010.2, 1000.5)])
                self.assertEqual(self.broker.modifications, [])

    def test_a_paused_engine_still_protects_open_trades(self):
        with patch.object(engine, "agent_status", "PAUSED"):
            self.run_cycle(bid=1010.0, candles=[candle(1, 1010.2, 1000.5)])
        self.assertEqual(len(self.broker.modifications), 1)

    def test_a_refusing_broker_is_not_hammered(self):
        self.broker.refuse = True
        for _ in range(5):
            self.run_cycle(bid=1010.0, candles=[candle(1, 1010.2, 1000.5)])
        self.assertEqual(len(self.broker.modifications), 1, "one attempt, then a cool-down")
        self.broker.refuse = False
        engine.trade_management_last_attempt.clear()
        self.run_cycle()
        self.assertEqual(len(self.broker.modifications), 2)

    def test_repeating_the_cycle_does_not_resend_the_same_change(self):
        self.run_cycle(bid=1010.0, candles=[candle(1, 1010.2, 1000.5)])
        for _ in range(4):
            self.run_after_cooldown()
        self.assertEqual(len(self.broker.modifications), 1)

    def test_every_change_is_logged_for_the_trade(self):
        self.run_cycle(bid=1010.0, candles=[candle(1, 1010.2, 1000.5)])
        self.assertTrue(engine.active_trade_evolution_logs[501])

    def test_unknown_trade_falls_back_to_its_current_unmoved_stop(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM trade_telemetry")
        self.run_cycle(bid=1003.1, candles=[candle(1, 1003.2, 1000.5)])      # risk = |1000 - 997| = 3.0 -> 1.0R
        self.assertEqual(len(self.broker.modifications), 1)

    def test_profiles_define_a_trail(self):
        for name, profile in engine.TRADING_PROFILES.items():
            cfg = engine.trade_manager.config_for_profile(profile)
            self.assertGreater(cfg.trail_atr_mult, 0, name)
            self.assertGreaterEqual(cfg.trail_start_r, cfg.be_trigger_r, name)

    def test_the_monitor_loop_runs_trade_management_every_iteration(self):
        calls = {"manage": 0, "sleep": 0}

        class Stop(Exception):
            pass

        def spy():
            calls["manage"] += 1

        def stop_after_three(_seconds):
            calls["sleep"] += 1
            if calls["sleep"] >= 3:
                raise Stop()

        with patch.object(engine, "refresh_mt5_state", lambda: None), \
                patch.object(engine, "check_and_sync_closed_telemetry_trades", lambda: None), \
                patch.object(engine, "poll_mt5_market_data", lambda include_history=False: True), \
                patch.object(engine.compounding_challenge, "sync_closed_challenge_trades", lambda: None), \
                patch.object(engine, "run_auto_trading_cycle", lambda: None), \
                patch.object(engine, "run_trade_management_cycle", spy), \
                patch.object(engine, "time", types.SimpleNamespace(sleep=stop_after_three, monotonic=time.monotonic, time=time.time)):
            with self.assertRaises(Stop):
                engine.mt5_monitor_loop()
        self.assertEqual(calls["manage"], 3)

    def test_the_cycle_wrapper_survives_errors(self):
        with patch.object(engine, "manage_active_trades", side_effect=RuntimeError("boom")):
            engine.run_trade_management_cycle()          # must not raise


class LifeOfATradeTests(ManagementTestBase):
    """Replay a price path bar by bar through the real management code with a broker that fills stops."""
    def replay(self, closes, spread=0.2, wick=0.6):
        candles, exit_price, sl_history = [], None, []
        for minute, close in enumerate(closes, start=1):
            high, low = close + wick, close - wick
            candles.append(candle(minute, high, low))
            bid = close
            sl = self.position["sl"]
            if bid <= sl or low <= sl:                         # the broker stops the trade out
                exit_price = sl
                break
            engine.trade_management_last_attempt.clear()
            self.run_cycle(bid=bid, candles=list(candles))
            sl_history.append(self.position["sl"])
        return exit_price, sl_history

    def test_a_strong_trend_is_ridden_and_exited_by_the_trail(self):
        path = [1000 + 0.5 * i for i in range(60)] + [1030 - 0.9 * i for i in range(60)]
        exit_price, history = self.replay(path)
        self.assertIsNotNone(exit_price)
        self.assertGreater(exit_price, 1020.0, "most of the move is kept")
        self.assertEqual(history, sorted(history), "the stop only ever moves up")
        self.assertEqual(self.broker.closes, [])
        self.assertGreater((exit_price - 1000.0) / 3.0, 6.0, "more than six times the original risk")

    def test_a_loser_runs_to_its_original_stop_and_no_further(self):
        path = [1000 - 0.15 * i for i in range(60)]
        exit_price, history = self.replay(path)
        self.assertEqual(exit_price, 997.0, "the loss is exactly the planned 1R")
        self.assertEqual(self.broker.modifications, [])

    def test_a_winner_that_reverses_is_protected_at_break_even(self):
        path = [1000 + 0.4 * i for i in range(10)] + [1004 - 0.4 * i for i in range(40)]
        exit_price, _ = self.replay(path)
        self.assertGreaterEqual(exit_price, 1000.0, "it can no longer end as a loss")


if __name__ == "__main__":
    unittest.main()
