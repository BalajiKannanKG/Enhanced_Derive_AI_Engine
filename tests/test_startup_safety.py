import unittest
from email.message import Message
from unittest.mock import patch

import Enhanced_Derive_AI_Engine as engine


class StartupSafetyTests(unittest.TestCase):
    def setUp(self):
        self.live_trading_enabled = engine.LIVE_TRADING_ENABLED

    def tearDown(self):
        engine.LIVE_TRADING_ENABLED = self.live_trading_enabled

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

    def test_decision_audit_reports_waiting_for_required_market_data(self):
        state = {"current_price": 100.0, "timeframes": {"5m": {}, "h1": {}}}

        engine.check_and_auto_trigger_trade(state)

        self.assertEqual(state["decision_audit"]["regime"], "SCANNING")
        self.assertIn("Waiting for complete 5m and 1h", state["decision_audit"]["reasoning"])

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
