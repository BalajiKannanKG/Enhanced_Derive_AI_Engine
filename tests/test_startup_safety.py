import http.client
import json
import threading
import unittest
from email.message import Message
from http.server import ThreadingHTTPServer
from unittest.mock import Mock, patch

import Enhanced_Derive_AI_Engine as engine
import pandas as pd


class StartupSafetyTests(unittest.TestCase):
    def setUp(self):
        self.live_trading_enabled = engine.LIVE_TRADING_ENABLED
        self.auto_trading_enabled = engine.AUTO_TRADING_ENABLED

    def tearDown(self):
        engine.LIVE_TRADING_ENABLED = self.live_trading_enabled
        engine.AUTO_TRADING_ENABLED = self.auto_trading_enabled

    def test_trade_management_does_not_dispatch_without_opt_in(self):
        engine.LIVE_TRADING_ENABLED = False
        account = {
            "current_price": 100.0,
            "mt5_account": {
                "positions": [{"type": "BUY", "price": 70.0, "sl": 60.0}]
            },
            "timeframes": {"5m": {"atr": 1.0}},
        }

        with patch.object(engine.os, "makedirs") as make_directories:
            engine.check_and_auto_trigger_trade(account)
            engine.manage_active_trades(account)

        make_directories.assert_not_called()

    def test_decision_audit_updates_in_data_only_mode_without_dispatching(self):
        state = {
            "current_price": 100.0,
            "cpr": {},
            "timeframes": {
                "5m": {
                    "atr": 1.0,
                    "adx": 20.0,
                    "rsi": 10.0,
                    "stoch_k_history": [10.0],
                    "ema20": 99.0,
                    "bb_upper": [110.0],
                    "bb_lower": [90.0],
                    "candles": [{
                        "open": 100.0,
                        "high": 100.5,
                        "low": 99.5,
                        "close": 100.0,
                    }],
                },
                "h1": {"trend": "FLAT"},
            },
        }

        with (
            patch.object(engine, "LIVE_TRADING_ENABLED", False),
            patch.object(engine, "agent_status", "DATA ONLY"),
            patch.object(engine.mt5_bridge, "place_market_order") as place_order,
        ):
            engine.check_and_auto_trigger_trade(state)

        self.assertEqual(state["decision_audit"]["consensus_score"], 35)
        self.assertIn("Path D (Extreme Oversold Reversal)", state["decision_audit"]["active_paths"])
        place_order.assert_not_called()

    def test_manual_live_mode_does_not_send_automatic_strategy_orders(self):
        state = {
            "current_price": 100.0,
            "cpr": {},
            "timeframes": {
                "3m": {"trend": "BULLISH"},
                "5m": {
                    "data_ready": True,
                    "trend": "BULLISH",
                    "atr": 1.0,
                    "adx": 20.0,
                    "rsi": 10.0,
                    "stoch_k_history": [10.0],
                    "ema20": 99.0,
                    "bb_upper": [110.0],
                    "bb_lower": [90.0],
                    "candles": [{
                        "open": 100.0,
                        "high": 100.5,
                        "low": 99.5,
                        "close": 100.0,
                    }],
                },
                "15m": {"trend": "BULLISH"},
                "h1": {"data_ready": True, "trend": "BULLISH"},
            },
        }

        with (
            patch.object(engine, "LIVE_TRADING_ENABLED", True),
            patch.object(engine, "AUTO_TRADING_ENABLED", False),
            patch.object(engine, "agent_status", "RUNNING"),
            patch.object(engine.mt5_bridge, "place_market_order") as place_order,
        ):
            engine.check_and_auto_trigger_trade(state)

        self.assertEqual(state["decision_audit"]["consensus_score"], 65)
        place_order.assert_not_called()

    def test_auto_live_mode_dispatches_threshold_signal_with_quote_relative_stops(self):
        state = {
            "current_price": 100.0,
            "cpr": {},
            "mt5_account": {
                "open_positions": 0,
                "positions": [],
                "daily_start_balance": 1000.0,
                "daily_pnl": 0.0,
                "balance": 1000.0,
                "symbols": {"Volatility 10 Index": {"available": True}},
            },
            "timeframes": {
                "3m": {"trend": "BULLISH"},
                "5m": {
                    "data_ready": True,
                    "trend": "BULLISH",
                    "atr": 1.0,
                    "adx": 20.0,
                    "rsi": 10.0,
                    "stoch_k_history": [10.0],
                    "ema20": 99.0,
                    "bb_upper": [110.0],
                    "bb_lower": [90.0],
                    "candles": [{
                        "time": 300,
                        "open": 100.0,
                        "high": 100.5,
                        "low": 99.5,
                        "close": 100.0,
                    }, {
                        "time": 600,
                        "open": 100.0,
                        "high": 101.0,
                        "low": 99.0,
                        "close": 100.0,
                    }],
                    "bb_upper": [110.0, 110.0],
                    "bb_middle": [100.0, 100.0],
                    "bb_lower": [90.0, 90.0],
                    "ema20_history": [99.0, 110.0],
                    "ema30_history": [101.0, 90.0],
                    "ema100_history": [100.0, 100.0],
                    "atr_history": [1.0, 2.0],
                    "adx_history": [20.0, 30.0],
                    "rsi_history": [10.0, 90.0],
                    "stoch_k_history": [10.0, 90.0],
                    "stoch_d_history": [10.0, 90.0],
                },
                "15m": {"trend": "BULLISH"},
                "h1": {"data_ready": True, "trend": "BULLISH"},
            },
        }
        place_order = Mock(return_value=(True, "Done", {"order": 1, "volume": 0.2}))
        account_snapshot = Mock(return_value=(
            state["mt5_account"],
            {"trade_allowed": True},
        ))

        with (
            patch.object(engine, "LIVE_TRADING_ENABLED", True),
            patch.object(engine, "AUTO_TRADING_ENABLED", True),
            patch.object(engine, "agent_status", "RUNNING"),
            patch.object(engine, "last_auto_evaluated_candle_times", {"Volatility 10 Index": "0"}),
            patch.object(engine, "latest_mt5_status", {"trade_allowed": True}),
            patch.object(engine.mt5_bridge, "account_snapshot", account_snapshot),
            patch.object(engine.mt5_bridge, "place_market_order", place_order),
        ):
            engine.check_and_auto_trigger_trade(state, "Volatility 10 Index")
            closed_score = state["decision_audit"]["consensus_score"]
            engine.check_and_auto_trigger_trade(state, "Volatility 10 Index")

        self.assertEqual(closed_score, 65)
        payload = place_order.call_args.args[0]
        self.assertEqual(payload["action"], "BUY")
        self.assertEqual(payload["symbol"], "Volatility 10 Index")
        self.assertEqual(payload["sl_distance"], 1.5)
        self.assertEqual(payload["tp_distance"], 3.0)
        self.assertTrue(payload["risk_sized"])
        self.assertNotIn("sl", payload)
        self.assertNotIn("tp", payload)
        place_order.assert_called_once()

    def test_auto_trading_waits_for_next_closed_candle_after_startup(self):
        state = {
            "current_price": 100.0,
            "cpr": {},
            "mt5_account": {
                "open_positions": 0,
                "positions": [],
                "daily_start_balance": 1000.0,
                "daily_pnl": 0.0,
            },
            "timeframes": {
                "5m": {
                    "data_ready": True,
                    "trend": "BULLISH",
                    "atr": 1.0,
                    "adx": 20.0,
                    "rsi": 50.0,
                    "stoch_k_history": [50.0, 50.0],
                    "candles": [
                        {"time": 300, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0},
                        {"time": 600, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0},
                    ],
                    "bb_upper": [110.0, 110.0],
                    "bb_middle": [100.0, 100.0],
                    "bb_lower": [90.0, 90.0],
                    "ema20_history": [100.0, 100.0],
                    "ema30_history": [100.0, 100.0],
                    "ema100_history": [100.0, 100.0],
                    "atr_history": [1.0, 1.0],
                    "adx_history": [20.0, 20.0],
                    "rsi_history": [50.0, 50.0],
                    "stoch_d_history": [50.0, 50.0],
                },
                "h1": {"data_ready": True, "trend": "FLAT"},
            },
        }
        with (
            patch.object(engine, "LIVE_TRADING_ENABLED", True),
            patch.object(engine, "AUTO_TRADING_ENABLED", True),
            patch.object(engine, "agent_status", "RUNNING"),
            patch.object(engine, "last_auto_evaluated_candle_times", {}),
            patch.object(engine, "latest_mt5_status", {"trade_allowed": True}),
            patch.object(engine.mt5_bridge, "place_market_order") as place_order,
        ):
            engine.check_and_auto_trigger_trade(state)

        self.assertIn("waiting for the next completed 5m candle", state["decision_audit"]["reasoning"])
        place_order.assert_not_called()

    def test_manual_order_endpoint_dispatches_only_in_live_mode(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), engine.CustomHTTPRequestHandler)
        port = server.server_address[1]
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        bridge = Mock()
        bridge.account_snapshot.return_value = (
            {
                "open_positions": 0,
                "positions": [],
                "daily_start_balance": 1000.0,
                "daily_pnl": 0.0,
            },
            {"trade_allowed": True, "detail": "Connected"},
        )
        bridge.place_market_order.return_value = (True, "Done", {"order": 1})

        try:
            with (
                patch.object(engine, "DASHBOARD_PORT", port),
                patch.object(engine, "LIVE_TRADING_ENABLED", True),
                patch.object(engine, "agent_status", "RUNNING"),
                patch.object(engine, "mt5_bridge", bridge),
            ):
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                payload = json.dumps({
                    "symbol": "Volatility 25 Index",
                    "action": "BUY",
                    "volume": 0.5,
                    "entry": 100.0,
                    "sl": 90.0,
                    "tp": 120.0,
                })
                connection.request(
                    "POST",
                    "/api/execute-trade",
                    body=payload,
                    headers={
                        "Host": f"localhost:{port}",
                        "Origin": f"http://localhost:{port}",
                        "Content-Type": "application/json",
                    },
                )
                response = connection.getresponse()
                response_data = json.loads(response.read())
                connection.close()

            self.assertEqual(response.status, 200)
            self.assertEqual(response_data["status"], "SUCCESS")
            bridge.place_market_order.assert_called_once()
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)

    def test_decision_audit_reports_waiting_for_required_market_data(self):
        state = {"current_price": 100.0, "timeframes": {"5m": {}, "h1": {}}}

        engine.check_and_auto_trigger_trade(state)

        self.assertEqual(state["decision_audit"]["regime"], "SCANNING")
        self.assertIn("Waiting for complete 5m and 1h", state["decision_audit"]["reasoning"])

    def test_non_default_symbol_tick_updates_its_own_candle_cache(self):
        symbol = "Volatility 10 Index"
        cache = engine.create_timeframe_cache()
        cache["5m"] = pd.DataFrame(
            [{"time": 300, "open": 200.0, "high": 201.0, "low": 199.0, "close": 200.0}],
            index=pd.DatetimeIndex([pd.Timestamp(300, unit="s", tz="UTC")]),
        )
        prices = {
            "Volatility 10 Index": 200.0,
            "Volatility 25 Index": 100.0,
            "Volatility 50 Index": 500.0,
            "Volatility 100 Index": 1000.0,
        }

        with (
            patch.object(engine, "symbol_tf_caches", {symbol: cache}),
            patch.object(engine, "current_prices", prices),
            patch.object(engine, "current_price", 100.0),
            patch.object(engine, "broadcast_state_update") as broadcast,
        ):
            with patch.object(engine.mt5_bridge, "market_data_snapshot", return_value=(
                {
                    symbol: {
                        "quote": {"bid": 210.0, "ask": 210.2, "time": 600},
                        "history": {},
                    }
                },
                {"status": "CONNECTED"},
            )):
                engine.poll_mt5_market_data()

        self.assertEqual(cache["5m"].iloc[-1]["open"], 210.0)
        self.assertEqual(cache["5m"].iloc[-1]["close"], 210.0)
        self.assertEqual(prices["Volatility 25 Index"], 100.0)
        broadcast.assert_called_once()

    def test_mt5_history_populates_each_symbol_timeframe_cache(self):
        symbol = "Volatility 10 Index"
        rates = [{
            "time": 600,
            "open": 200.0,
            "high": 202.0,
            "low": 199.0,
            "close": 201.0,
        }]
        snapshot = {
            supported_symbol: {
                "quote": {"bid": 201.0, "ask": 201.2, "time": 600},
                "history": {
                    timeframe: rates for timeframe in engine.MT5_TIMEFRAMES
                },
            }
            for supported_symbol in engine.SUPPORTED_SYMBOLS
        }
        caches = {
            supported_symbol: engine.create_timeframe_cache()
            for supported_symbol in engine.SUPPORTED_SYMBOLS
        }
        prices = {supported_symbol: 0.0 for supported_symbol in engine.SUPPORTED_SYMBOLS}

        with (
            patch.object(engine, "symbol_tf_caches", caches),
            patch.object(engine, "current_prices", prices),
            patch.object(engine, "current_price", 0.0),
            patch.object(engine.mt5_bridge, "market_data_snapshot", return_value=(
                snapshot,
                {"status": "CONNECTED"},
            )),
            patch.object(engine, "broadcast_state_update"),
        ):
            history_loaded = engine.poll_mt5_market_data(include_history=True)

        candle = caches[symbol]["5m"].iloc[0]
        self.assertTrue(history_loaded)
        self.assertEqual(candle["open"], 200.0)
        self.assertEqual(candle["high"], 202.0)
        self.assertEqual(candle["low"], 199.0)
        self.assertEqual(candle["close"], 201.0)

    def test_short_history_does_not_invent_atr_for_trade_levels(self):
        index = pd.date_range("2026-01-01", periods=5, freq="5min", tz="UTC")
        candles = pd.DataFrame(
            [
                {"time": int(timestamp.timestamp()), "open": 100 + i, "high": 102 + i, "low": 99 + i, "close": 101 + i}
                for i, timestamp in enumerate(index)
            ],
            index=index,
        )

        timeframe = engine.process_dataframe(candles, "Volatility 10 Index")

        self.assertFalse(timeframe["data_ready"])
        self.assertEqual(timeframe["atr"], 0.0)

    def test_initial_dashboard_state_exposes_all_supported_symbols(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), engine.CustomHTTPRequestHandler)
        port = server.server_address[1]
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            with patch.object(engine, "latest_state_payload", None):
                connection = http.client.HTTPConnection("127.0.0.1", port)
                connection.request("GET", "/agent_state.json", headers={"Host": f"localhost:{engine.DASHBOARD_PORT}"})
                response = connection.getresponse()
                state = json.loads(response.read())
                connection.close()

            self.assertEqual(set(state["symbols"]), set(engine.SUPPORTED_SYMBOLS))
            self.assertEqual(state["default_symbol"], engine.DEFAULT_SYMBOL)
            self.assertEqual(state["market_data_source"], "MT5")
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)

    def test_only_local_dashboard_origins_are_allowed_for_posts(self):
        handler = object.__new__(engine.CustomHTTPRequestHandler)
        handler.headers = Message()
        handler.headers["Host"] = "localhost:8080"
        handler.headers["Origin"] = "http://localhost:8080"
        self.assertTrue(handler._is_local_dashboard_request())

        handler.headers = Message()
        handler.headers["Host"] = "localhost:8080"
        handler.headers["Origin"] = "http://attacker.example"
        self.assertFalse(handler._is_local_dashboard_request())

    def test_daily_loss_limit_fails_closed_when_metrics_are_missing(self):
        allowed, message = engine.check_daily_loss_limit({"balance": 100.0})

        self.assertFalse(allowed)
        self.assertIn("daily_start_balance and daily_pnl", message)

    def test_daily_loss_limit_blocks_at_configured_threshold(self):
        allowed, message = engine.check_daily_loss_limit({
            "daily_start_balance": 100.0,
            "daily_pnl": -2.0,
        })

        self.assertFalse(allowed)
        self.assertIn("Daily loss limit reached", message)

    def test_requested_risk_cannot_exceed_configured_limit(self):
        risk_percent, message = engine.validate_requested_risk_percent({"risk_pct": 0.0101})

        self.assertIsNone(risk_percent)
        self.assertIn("exceeds the configured", message)


if __name__ == "__main__":
    unittest.main()
