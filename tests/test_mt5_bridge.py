import unittest
from types import SimpleNamespace
from unittest.mock import patch

import mt5_bridge


class FakeMetaTrader5:
    POSITION_TYPE_BUY = 0
    DEAL_TYPE_BUY = 0
    DEAL_TYPE_SELL = 1
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_SLTP = 6
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_DONE_PARTIAL = 10010
    TRADE_RETCODE_PLACED = 10008
    TIMEFRAME_M5 = 5

    def __init__(self):
        self.order_send_calls = []
        self.history_calls = []
        self.positions = ()

    def last_error(self):
        return (0, "ok")

    def initialize(self, **kwargs):
        return True

    def shutdown(self):
        pass

    def terminal_info(self):
        return SimpleNamespace(
            connected=True, trade_allowed=True, tradeapi_disabled=False
        )

    def account_info(self):
        return SimpleNamespace(
            balance=1000.0,
            equity=1010.0,
            profit=10.0,
            margin_free=900.0,
            currency="USD",
            login=123,
            server="Demo",
            trade_allowed=True,
        )

    def positions_get(self, **kwargs):
        return self.positions

    def history_deals_get(self, start, end):
        return (
            SimpleNamespace(profit=-5.0, commission=-0.5, swap=0.0, fee=0.0),
        )

    def symbol_select(self, symbol, selected):
        return True

    def symbol_info_tick(self, symbol):
        symbol_offset = {
            "Volatility 10 Index": 10.0,
            "Volatility 25 Index": 0.0,
            "Volatility 50 Index": 50.0,
            "Volatility 100 Index": 100.0,
        }[symbol]
        return SimpleNamespace(
            ask=100.1 + symbol_offset, bid=100.0 + symbol_offset, time=0
        )

    def copy_rates_from_pos(self, symbol, timeframe, start, count):
        self.history_calls.append((symbol, timeframe, start, count))
        return [
            {
                "time": 300,
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
            }
        ]

    def symbol_info(self, symbol):
        symbol_minimum = {
            "Volatility 10 Index": 0.1,
            "Volatility 25 Index": 0.1,
            "Volatility 50 Index": 0.3,
            "Volatility 100 Index": 0.4,
        }[symbol]
        return SimpleNamespace(
            volume_min=symbol_minimum,
            volume_max=10.0,
            volume_step=0.1,
            filling_mode=1,
            trade_stops_level=0,
            point=0.01,
            digits=2,
        )

    def order_calc_profit(self, order_type, symbol, volume, price, stop):
        return -50.0 * volume

    def order_calc_margin(self, order_type, symbol, volume, price):
        return 50.0 * volume

    def order_check(self, request):
        return SimpleNamespace(retcode=0, comment="Done")

    def order_send(self, request):
        self.order_send_calls.append(request)
        return SimpleNamespace(retcode=self.TRADE_RETCODE_DONE, comment="Done", order=1)


class MT5BridgeTests(unittest.TestCase):
    def setUp(self):
        self.fake_mt5 = FakeMetaTrader5()
        self.mt5_patch = patch.object(mt5_bridge, "mt5", self.fake_mt5)
        self.mt5_patch.start()
        self.bridge = mt5_bridge.MT5Bridge(symbol="Volatility 25 Index")

    def tearDown(self):
        self.bridge.disconnect()
        self.mt5_patch.stop()

    def test_account_snapshot_reads_terminal_account_positions_and_daily_pnl(self):
        account, status = self.bridge.account_snapshot()

        self.assertEqual(account["balance"], 1000.0)
        self.assertEqual(account["daily_start_balance"], 1005.5)
        self.assertEqual(account["daily_pnl"], 4.5)
        self.assertEqual(account["open_positions"], 0)
        self.assertEqual(account["quote"], {"bid": 100.0, "ask": 100.1, "time": 0})
        self.assertTrue(status["connection_verified"])
        self.assertTrue(status["trade_allowed"])

    def test_account_snapshot_includes_each_supported_symbol_quote_and_volume_rules(self):
        bridge = mt5_bridge.MT5Bridge(
            symbol="Volatility 25 Index",
            symbols=[
                "Volatility 10 Index",
                "Volatility 50 Index",
                "Volatility 100 Index",
            ],
        )
        try:
            account, _ = bridge.account_snapshot()
        finally:
            bridge.disconnect()

        self.assertEqual(
            set(account["symbols"]),
            {
                "Volatility 10 Index",
                "Volatility 25 Index",
                "Volatility 50 Index",
                "Volatility 100 Index",
            },
        )
        self.assertEqual(account["symbols"]["Volatility 10 Index"]["quote"]["bid"], 110.0)
        self.assertEqual(account["symbols"]["Volatility 100 Index"]["volume_min"], 0.4)

    def test_market_data_snapshot_reads_mt5_quotes_and_requested_candles(self):
        snapshot, status = self.bridge.market_data_snapshot(
            {"5m": "TIMEFRAME_M5"},
            history_count=300,
            include_history=True,
        )

        self.assertEqual(snapshot["Volatility 25 Index"]["quote"]["bid"], 100.0)
        self.assertEqual(
            snapshot["Volatility 25 Index"]["history"]["5m"][0]["close"],
            100.5,
        )
        self.assertEqual(self.fake_mt5.history_calls, [
            ("Volatility 25 Index", self.fake_mt5.TIMEFRAME_M5, 0, 300)
        ])
        self.assertTrue(status["connection_verified"])

    def test_order_rejects_symbol_outside_configured_list(self):
        succeeded, message, _ = self.bridge.place_market_order({
            "symbol": "Unsupported Index",
            "action": "BUY",
            "volume": 0.1,
            "sl": 90.0,
            "tp": 120.0,
        }, max_risk_percent=1.0)

        self.assertFalse(succeeded)
        self.assertIn("not in the configured", message)
        self.assertEqual(self.fake_mt5.order_send_calls, [])

    def test_order_rejects_when_any_portfolio_position_is_open(self):
        self.fake_mt5.positions = (SimpleNamespace(),)

        succeeded, message, _ = self.bridge.place_market_order({
            "symbol": "Volatility 10 Index",
            "action": "BUY",
            "volume": 0.1,
            "sl": 90.0,
            "tp": 120.0,
        }, max_risk_percent=1.0)

        self.assertFalse(succeeded)
        self.assertIn("position is already open", message)
        self.assertEqual(self.fake_mt5.order_send_calls, [])

    def test_close_position_omits_comment_rejected_by_mt5_argument_validation(self):
        self.fake_mt5.positions = (
            SimpleNamespace(
                symbol="Volatility 25 Index",
                type=mt5_bridge.mt5.POSITION_TYPE_BUY,
                volume=0.1,
                magic=123456,
            ),
        )

        succeeded, _, _ = self.bridge.close_position(5827511697)

        self.assertTrue(succeeded)
        request = self.fake_mt5.order_send_calls[0]
        self.assertEqual(request["position"], 5827511697)
        self.assertNotIn("comment", request)

    def test_order_is_rejected_when_monetary_risk_exceeds_cap(self):
        succeeded, message, _ = self.bridge.place_market_order({
            "symbol": "Volatility 25 Index",
            "action": "BUY",
            "volume": 0.1,
            "sl": 90.0,
            "tp": 120.0,
        }, max_risk_percent=0.1)

        self.assertFalse(succeeded)
        self.assertIn("exceeds", message)
        self.assertEqual(self.fake_mt5.order_send_calls, [])

    def test_valid_market_order_is_checked_and_sent_with_attached_protection(self):
        succeeded, _, result = self.bridge.place_market_order({
            "symbol": "Volatility 25 Index",
            "action": "BUY",
            "volume": 0.1,
            "sl": 90.0,
            "tp": 120.0,
        }, max_risk_percent=1.0)

        self.assertTrue(succeeded)
        self.assertEqual(result["order"], 1)
        self.assertEqual(len(self.fake_mt5.order_send_calls), 1)
        request = self.fake_mt5.order_send_calls[0]
        self.assertEqual(request["sl"], 90.0)
        self.assertEqual(request["tp"], 120.0)

    def test_manual_order_builds_protection_from_fresh_mt5_quote(self):
        succeeded, _, result = self.bridge.place_market_order({
            "symbol": "Volatility 25 Index",
            "action": "BUY",
            "volume": 0.1,
            "sl": 200.0,
            "tp": 220.0,
            "sl_distance": 10.0,
            "tp_distance": 20.0,
        }, max_risk_percent=1.0)

        self.assertTrue(succeeded)
        request = self.fake_mt5.order_send_calls[0]
        self.assertEqual(request["price"], 100.1)
        self.assertEqual(request["sl"], 90.1)
        self.assertEqual(request["tp"], 120.1)
        self.assertEqual(result["quote_price"], 100.1)

    def test_risk_sized_order_uses_largest_volume_within_risk_cap(self):
        succeeded, _, result = self.bridge.place_market_order({
            "symbol": "Volatility 25 Index",
            "action": "BUY",
            "risk_sized": True,
            "sl_distance": 10.0,
            "tp_distance": 20.0,
        }, max_risk_percent=1.0)

        self.assertTrue(succeeded)
        request = self.fake_mt5.order_send_calls[0]
        self.assertEqual(request["volume"], 0.2)
        self.assertEqual(result["risk_amount"], 10.0)
        self.assertEqual(result["risk_cap"], 10.0)

    def test_risk_sized_order_rejects_when_minimum_volume_exceeds_risk_cap(self):
        succeeded, message, _ = self.bridge.place_market_order({
            "symbol": "Volatility 25 Index",
            "action": "BUY",
            "risk_sized": True,
            "sl_distance": 10.0,
            "tp_distance": 20.0,
        }, max_risk_percent=0.1)

        self.assertFalse(succeeded)
        self.assertIn("Minimum broker volume", message)
        self.assertEqual(self.fake_mt5.order_send_calls, [])

    def test_order_rejects_malformed_volume_without_sending(self):
        succeeded, message, _ = self.bridge.place_market_order({
            "symbol": "Volatility 25 Index",
            "action": "BUY",
            "volume": "not-a-number",
            "sl": 90.0,
            "tp": 120.0,
        }, max_risk_percent=1.0)

        self.assertFalse(succeeded)
        self.assertIn("must be numbers", message)
        self.assertEqual(self.fake_mt5.order_send_calls, [])


if __name__ == "__main__":
    unittest.main()
