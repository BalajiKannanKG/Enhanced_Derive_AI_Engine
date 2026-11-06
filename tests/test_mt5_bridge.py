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
    SYMBOL_FILLING_FOK = 1
    SYMBOL_FILLING_IOC = 2
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_DONE_PARTIAL = 10010
    TRADE_RETCODE_PLACED = 10008

    def __init__(self):
        self.order_send_calls = []

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
        return ()

    def history_deals_get(self, start, end):
        return (
            SimpleNamespace(profit=-5.0, commission=-0.5, swap=0.0, fee=0.0),
        )

    def symbol_select(self, symbol, selected):
        return True

    def symbol_info_tick(self, symbol):
        return SimpleNamespace(ask=100.1, bid=100.0)

    def symbol_info(self, symbol):
        return SimpleNamespace(
            volume_min=0.1,
            volume_max=10.0,
            volume_step=0.1,
            filling_mode=self.SYMBOL_FILLING_FOK,
            trade_stops_level=0,
            point=0.01,
            digits=2,
        )

    def order_calc_profit(self, order_type, symbol, volume, price, stop):
        return -5.0

    def order_calc_margin(self, order_type, symbol, volume, price):
        return 50.0

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
        self.assertTrue(status["connection_verified"])
        self.assertTrue(status["trade_allowed"])

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
