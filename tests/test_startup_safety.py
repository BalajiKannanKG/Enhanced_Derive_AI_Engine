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
