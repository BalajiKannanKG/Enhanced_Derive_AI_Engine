import unittest
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pandas as pd

import backtest
from backtest import TIMEFRAMES, _cpr_from_daily, _get_histories, resolve_bar_exit


class BacktestTests(unittest.TestCase):
    def test_long_candle_hitting_stop_and_target_uses_stop(self):
        result = resolve_bar_exit(
            "BUY",
            stop_loss=95.0,
            take_profit=110.0,
            open_bid=100.0,
            high_bid=112.0,
            low_bid=94.0,
            spread=0.5,
        )

        self.assertEqual(result, (95.0, "stop_loss"))

    def test_short_exit_checks_ask_side_with_historical_spread(self):
        result = resolve_bar_exit(
            "SELL",
            stop_loss=105.0,
            take_profit=95.0,
            open_bid=100.0,
            high_bid=104.8,
            low_bid=94.0,
            spread=0.3,
        )

        self.assertEqual(result, (105.0, "stop_loss"))

    def test_daily_cpr_uses_the_selected_completed_day(self):
        daily = pd.DataFrame([{
            "high": 120.0,
            "low": 90.0,
            "close": 105.0,
        }])

        cpr = _cpr_from_daily(daily, 0)

        self.assertEqual(cpr["pivot"], 105.0)
        self.assertEqual(cpr["bc"], 105.0)
        self.assertEqual(cpr["tc"], 105.0)
        self.assertEqual(cpr["r1"], 120.0)
        self.assertEqual(cpr["s1"], 90.0)

    def test_history_uses_available_window_and_only_completed_bars(self):
        symbol = "Volatility 25 Index"
        available_start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        requested_end = datetime(2026, 2, 5, 0, 2, tzinfo=timezone.utc)
        bars_by_timeframe = {}
        for timeframe, (mt5_timeframe, duration) in TIMEFRAMES.items():
            end_timestamp = int(requested_end.timestamp())
            times = [
                int(available_start.timestamp()),
                end_timestamp - duration * 2,
                end_timestamp - duration // 2,
                end_timestamp + 86400,
            ]
            bars_by_timeframe[mt5_timeframe] = pd.DataFrame({
                "time": times,
                "open": [100.0] * len(times),
                "high": [101.0] * len(times),
                "low": [99.0] * len(times),
                "close": [100.0] * len(times),
                "spread": [5] * len(times),
            })

        with (
            patch.object(backtest.mt5, "symbol_select", return_value=True),
            patch.object(
                backtest.mt5,
                "copy_rates_range",
                side_effect=lambda requested_symbol, timeframe, start, end:
                    bars_by_timeframe[timeframe],
            ),
        ):
            histories, coverage, actual_start, actual_end = _get_histories(
                [symbol],
                datetime(2024, 1, 1, tzinfo=timezone.utc),
                requested_end,
            )

        self.assertEqual(
            actual_start,
            int((available_start + timedelta(days=30)).timestamp()),
        )
        m5_duration = TIMEFRAMES["5m"][1]
        self.assertEqual(
            actual_end,
            int(requested_end.timestamp()) - m5_duration * 2 + m5_duration,
        )
        for timeframe, (_, duration) in TIMEFRAMES.items():
            frame = histories[symbol][timeframe]
            self.assertTrue((frame["time"] + duration <= actual_end).all())
            self.assertEqual(coverage[symbol][timeframe]["bars"], len(frame))

    def test_replay_opens_at_next_bar_and_keeps_one_portfolio_position(self):
        symbol = "Volatility 25 Index"
        m5_start = 1_700_000_000
        histories = {
            symbol: {
                "3m": self._bars(m5_start - 300, 180, 1600, 100),
                "5m": self._bars(m5_start, 300, 600, 100),
                "15m": self._bars(m5_start - 900, 900, 300, 100),
                "h1": self._bars(m5_start - 300 * 3600, 3600, 300, 100),
                "d1": self._bars(m5_start - 40 * 86400, 86400, 60, 100),
            }
        }
        mt5 = backtest.mt5
        with (
            patch.object(mt5, "symbol_info", return_value=SimpleNamespace(
                volume_min=0.1, volume_max=1.0, volume_step=0.1, point=0.01
            )),
            patch.object(mt5, "order_calc_profit", side_effect=lambda order_type, symbol, volume, opened, closed:
                         (closed - opened) * 10 * volume if order_type == mt5.ORDER_TYPE_BUY
                         else (opened - closed) * 10 * volume),
            patch.object(mt5, "order_calc_margin", side_effect=lambda order_type, symbol, volume, price: volume * 10),
            patch.object(
                backtest.engine,
                "evaluate_multi_path_strategy",
                return_value=("BUY", {"consensus_score": 70}),
            ),
        ):
            result = backtest.run_backtest(
                histories,
                m5_start + 300 * 300,
                m5_start + 300 * 310,
                1000,
            )

        self.assertEqual(result["summary"]["trades"], 1)
        trade = result["trades"][0]
        self.assertEqual(trade["action"], "BUY")
        self.assertEqual(
            trade["entry_time"],
            pd.to_datetime(m5_start + 300 * 301, unit="s", utc=True).isoformat(),
        )
        self.assertEqual(trade["exit_reason"], "end_of_test")

    @staticmethod
    def _bars(start: int, step: int, count: int, price: float) -> pd.DataFrame:
        times = [start + step * i for i in range(count)]
        return pd.DataFrame({
            "time": times,
            "open": [price] * count,
            "high": [price + 1] * count,
            "low": [price - 1] * count,
            "close": [price] * count,
            "spread": [5] * count,
        })


if __name__ == "__main__":
    unittest.main()
