"""Tests for automatic trade entry: the monitor-loop cycle, the challenge-halt gate, live-quote pricing,
lot sizing, the shared position cap and the shared dispatch lock.

Everything external is replaced (MT5 quote/symbol info, the bridge snapshot, order dispatch, telemetry,
strategy evaluation), so nothing here talks to a terminal or writes to the database. Run with:
    python -m unittest tests.test_auto_trading -v
"""
import threading
import time
import types
import unittest
from unittest.mock import patch

import MetaTrader5 as mt5
import numpy as np
import pandas as pd

import Enhanced_Derive_AI_Engine as engine

SYMBOLS = list(engine.SUPPORTED_SYMBOLS)
TICK = types.SimpleNamespace(ask=1005.5, bid=1005.0)
SIGNAL = ("BUY", {"regime": "TEST", "consensus_score": 60, "active_paths": ["Path A"], "reasoning": "test"})
STALE_HALT = {"status": "HALTED_MAX_DRAWDOWN", "halt_trading": True, "starting_balance": 200.0, "current_balance": 230.55}
RESUMED = {"status": "ACTIVE", "halt_trading": False, "starting_balance": 200.0, "current_balance": 230.55}
REAL_LOT_FORMULA = engine.compounding_challenge.CompoundingChallengeTracker.calculate_dynamic_lot_size


def candle_frame(rows, seed):
    rng = np.random.default_rng(seed)
    close = 1000.0 + np.cumsum(rng.normal(0, 1.2, rows))
    close = close - close[-1] + 1000.0
    open_ = np.r_[close[0], close[:-1]]
    frame = pd.DataFrame({
        "time": (1731600000 + np.arange(rows) * 300).astype("int64"),
        "open": open_,
        "high": np.maximum(open_, close) + abs(rng.normal(0, 0.5, rows)),
        "low": np.minimum(open_, close) - abs(rng.normal(0, 0.5, rows)),
        "close": close,
    })
    frame.index = pd.to_datetime(frame["time"], unit="s", utc=True)
    return frame


def with_new_candle(frame):
    """Append a candle: the one that was forming is now closed."""
    stamp = int(frame["time"].iloc[-1]) + 300
    last = float(frame["close"].iloc[-1])
    row = pd.DataFrame(
        {"time": [stamp], "open": [last], "high": [last + 0.8], "low": [last - 0.8], "close": [last + 0.2]},
        index=pd.to_datetime([stamp], unit="s", utc=True),
    )
    return pd.concat([frame, row])


class _Tracker:
    """Stands in for the challenge tracker so no database is touched."""
    def __init__(self, *args, **kwargs):
        self.state = {}

    def calculate_dynamic_lot_size(self, **kwargs):
        return REAL_LOT_FORMULA(None, **kwargs)

    def evaluate_day(self, simulated_pnl=0.0):
        return dict(RESUMED)


class AutoTradingTests(unittest.TestCase):
    def setUp(self):
        self.orders = []
        self.positions = []
        self.halt = dict(RESUMED)
        profile = engine.CURRENT_PROFILE
        self.caches = {
            symbol: {tf: candle_frame(300, hash((symbol, tf)) % 997) for tf in ("1m", "3m", "5m", "15m", "30m", "h1", "h4", "d1")}
            for symbol in SYMBOLS
        }
        self.primary_tf, self.confluence_tf = profile["primary_tf"], profile["confluence_tf"]

        def snapshot():
            account = {
                "balance": 200.0, "equity": 200.0, "positions": list(self.positions),
                "daily_start_balance": 200.0, "daily_pnl": 0.0, "quote": {"bid": TICK.bid, "ask": TICK.ask},
                "symbols": {s: {"available": True, "volume_min": 0.01, "volume_max": 100, "volume_step": 0.01, "digits": 3} for s in SYMBOLS},
            }
            return account, {"trade_allowed": True, "connection_verified": True}

        def dispatch(symbol, order_type, lot_size, price, sl, tp, max_allowed_spread_multiplier=1.5):
            ticket = 7000 + len(self.orders)
            self.orders.append({"symbol": symbol, "order_type": order_type, "lot": lot_size, "price": price, "sl": sl, "tp": tp})
            self.positions.append({"ticket": ticket, "magic": engine.MT5_MAGIC_NUMBER, "symbol": symbol})
            # A real MT5 result is an object with attributes, not a dict.
            return True, "Order executed successfully.", types.SimpleNamespace(order=ticket, volume=lot_size, price=price)

        patches = [
            patch.object(engine, "LIVE_TRADING_ENABLED", True),
            patch.object(engine, "AUTO_TRADING_ENABLED", True),
            patch.object(engine, "CHALLENGE_HALTED", False),
            patch.object(engine, "agent_status", "RUNNING"),
            patch.object(engine, "symbol_tf_caches", self.caches),
            patch.object(engine, "current_prices", {s: float(self.caches[s]["5m"]["close"].iloc[-1]) for s in SYMBOLS}),
            patch.object(engine, "mt5_bridge", types.SimpleNamespace(account_snapshot=snapshot)),
            patch.object(engine, "latest_mt5_account", snapshot()[0]),
            patch.object(engine, "latest_mt5_status", snapshot()[1]),
            patch.object(engine, "execute_guarded_order", dispatch),
            patch.object(engine, "log_trade_event", lambda **kw: self.orders[-1].update(logged=True)),
            patch.object(engine, "evaluate_multi_path_strategy", lambda *a, **k: SIGNAL),
            patch.object(engine, "last_auto_evaluated_candle_times", {}),
            patch.object(engine, "latest_symbol_states", {}),
            patch.object(engine.compounding_challenge, "CompoundingChallengeTracker", _Tracker),
            patch.object(engine.compounding_challenge, "get_current_challenge_status", lambda: dict(self.halt)),
            patch.object(mt5, "symbol_info_tick", lambda symbol: TICK, create=True),
            patch.object(mt5, "symbol_info", lambda symbol: types.SimpleNamespace(
                spread=2, point=0.001, volume_min=0.01, volume_max=100.0, volume_step=0.01), create=True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    # -- helpers -------------------------------------------------------------------------------
    def broadcast(self):
        engine.broadcast_state_update()

    def new_candle(self):
        for symbol in SYMBOLS:
            for tf in (self.primary_tf, self.confluence_tf):
                self.caches[symbol][tf] = with_new_candle(self.caches[symbol][tf])
            engine.current_prices[symbol] = float(self.caches[symbol][self.primary_tf]["close"].iloc[-1])

    def arm_then_close_a_candle(self):
        self.broadcast()
        engine.run_auto_trading_cycle()          # first cycle only records the candle that is already open
        self.new_candle()
        self.broadcast()
        engine.run_auto_trading_cycle()

    # -- tests ---------------------------------------------------------------------------------
    def test_first_cycle_only_arms_then_a_closed_candle_trades(self):
        self.broadcast()
        engine.run_auto_trading_cycle()
        self.assertEqual(self.orders, [])
        self.new_candle()
        self.broadcast()
        engine.run_auto_trading_cycle()
        self.assertEqual(len(self.orders), len(SYMBOLS))
        self.assertTrue(all(order.get("logged") for order in self.orders), "telemetry must be written after the order")

    def test_order_is_priced_from_the_live_quote(self):
        self.arm_then_close_a_candle()
        order = self.orders[0]
        self.assertEqual(order["price"], TICK.ask)
        self.assertLess(order["sl"], order["price"])
        self.assertGreater(order["tp"], order["price"])
        self.assertAlmostEqual((order["tp"] - order["price"]) / (order["price"] - order["sl"]), 2.0, places=9)

    def test_lot_size_follows_the_configured_risk_percent(self):
        self.arm_then_close_a_candle()
        order = self.orders[0]
        atr = engine.latest_symbol_states[order["symbol"]]["timeframes"][self.primary_tf]["atr_history"][-2]
        # The R:R helper scales risk by 1.0x or 1.2x depending on whether tp/sl lands at or just under 2.0
        # in floating point, so either multiplier is correct. What must never happen is ~100x the intended risk.
        allowed = [max(0.50, 200.0 * (engine.MAX_RISK_PER_TRADE_PERCENT * multiplier / 100.0) / (2.0 * atr)) for multiplier in (1.0, 1.2)]
        self.assertTrue(any(abs(order["lot"] - round(lot, 2)) <= 0.02 for lot in allowed), f"lot {order['lot']} not in {allowed}")
        self.assertLess(order["lot"], 5.0, "a lot size near 100x the intended risk would be a unit error")

    def test_stale_halt_does_not_switch_auto_trading_off(self):
        self.halt = dict(STALE_HALT)             # the database still says halted, but the tracker resumes
        self.broadcast()
        self.assertTrue(engine.AUTO_TRADING_ENABLED)
        self.assertFalse(engine.CHALLENGE_HALTED)

    def test_real_halt_blocks_entries_and_clears_by_itself(self):
        halted = dict(STALE_HALT, current_balance=150.0)
        self.halt = halted

        class HaltedTracker(_Tracker):
            def evaluate_day(self, simulated_pnl=0.0):
                return dict(halted)

        with patch.object(engine.compounding_challenge, "CompoundingChallengeTracker", HaltedTracker):
            self.arm_then_close_a_candle()
        self.assertTrue(engine.CHALLENGE_HALTED)
        self.assertEqual(self.orders, [])
        self.halt = dict(RESUMED)
        self.broadcast()
        self.assertFalse(engine.CHALLENGE_HALTED)
        self.assertTrue(engine.AUTO_TRADING_ENABLED)
        engine.run_auto_trading_cycle()           # first cycle after the halt only records the open candle
        self.new_candle()
        self.broadcast()
        engine.run_auto_trading_cycle()
        self.assertGreater(len(self.orders), 0)

    def test_nothing_trades_when_a_gate_is_closed(self):
        for name, value in (("AUTO_TRADING_ENABLED", False), ("LIVE_TRADING_ENABLED", False), ("agent_status", "PAUSED")):
            with self.subTest(gate=name), patch.object(engine, name, value):
                self.arm_then_close_a_candle()
                self.assertEqual(self.orders, [])
                engine.last_auto_evaluated_candle_times.clear()

    def test_position_cap_counts_every_open_position(self):
        self.positions.extend({"ticket": i, "magic": 0, "symbol": SYMBOLS[0]} for i in range(3))   # opened by hand
        self.arm_then_close_a_candle()
        self.assertEqual(len(self.orders), engine.MAX_OPEN_POSITIONS - 3)
        self.assertLessEqual(len(self.positions), engine.MAX_OPEN_POSITIONS)

    def test_an_error_on_one_symbol_does_not_stop_the_others(self):
        good_dispatch = engine.execute_guarded_order

        def flaky(symbol, *args, **kwargs):
            if symbol == SYMBOLS[0]:
                raise RuntimeError("simulated broker error")
            return good_dispatch(symbol, *args, **kwargs)

        with patch.object(engine, "execute_guarded_order", flaky):
            self.arm_then_close_a_candle()
        self.assertEqual(len(self.orders), len(SYMBOLS) - 1)

    def test_auto_entries_wait_for_an_in_flight_manual_order(self):
        self.broadcast()
        engine.run_auto_trading_cycle()
        self.new_candle()
        self.broadcast()
        engine.order_dispatch_lock.acquire()      # a manual order is being processed
        worker = threading.Thread(target=engine.run_auto_trading_cycle)
        try:
            worker.start()
            time.sleep(0.3)
            self.assertEqual(self.orders, [], "auto entry must not run while the dispatch lock is held")
        finally:
            engine.order_dispatch_lock.release()
        worker.join(5)
        self.assertGreater(len(self.orders), 0)

    def test_monitor_loop_runs_the_auto_cycle_every_iteration(self):
        calls = {"cycle": 0, "sleep": 0}

        class Stop(Exception):
            pass

        def spy():
            calls["cycle"] += 1

        def stop_after_three(_seconds):
            calls["sleep"] += 1
            if calls["sleep"] >= 3:
                raise Stop()

        with patch.object(engine, "refresh_mt5_state", lambda: None), \
                patch.object(engine, "check_and_sync_closed_telemetry_trades", lambda: None), \
                patch.object(engine, "poll_mt5_market_data", lambda include_history=False: True), \
                patch.object(engine.compounding_challenge, "sync_closed_challenge_trades", lambda: None), \
                patch.object(engine, "run_auto_trading_cycle", spy), \
                patch.object(engine, "time", types.SimpleNamespace(sleep=stop_after_three, monotonic=time.monotonic, time=time.time)):
            with self.assertRaises(Stop):
                engine.mt5_monitor_loop()
        self.assertEqual(calls["cycle"], 3)


if __name__ == "__main__":
    unittest.main()
