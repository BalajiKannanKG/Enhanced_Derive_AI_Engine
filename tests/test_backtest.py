import unittest
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pandas as pd

import backtest
from backtest import (
    TIMEFRAMES,
    Position,
    _confirmation_matches,
    _cpr_from_daily,
    _get_histories,
    _updated_stop_loss,
    resolve_bar_exit,
)


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

    def test_higher_timeframe_confirmation_must_match_trade_direction(self):
        states = {
            "15m": {"trend": "BULLISH"},
            "h1": {"trend": "BEARISH"},
        }

        self.assertFalse(
            _confirmation_matches("BUY", states, ("15m", "h1"))
        )
        states["h1"]["trend"] = "BULLISH"
        self.assertTrue(
            _confirmation_matches("BUY", states, ("15m", "h1"))
        )

    def test_five_minute_profile_confluence_does_not_depend_on_three_minute_data(self):
        state = {
            "atr": 1.0,
            "adx": 10.0,
            "rsi": 50.0,
            "stoch_k_history": [50.0],
            "ema20": 100.0,
            "bb_upper": [110.0],
            "bb_lower": [90.0],
            "candles": [],
        }
        timeframes = {
            "3m": {"trend": "BEARISH"},
            "5m": {"trend": "BULLISH"},
            "15m": {"trend": "BULLISH"},
            "h1": {"trend": "BULLISH"},
        }

        _, standard_audit = backtest.engine.evaluate_multi_path_strategy(
            state, timeframes["h1"], 100.0, {}, timeframes
        )
        _, five_minute_audit = backtest.engine.evaluate_multi_path_strategy(
            state,
            timeframes["h1"],
            100.0,
            {},
            timeframes,
            include_sub_5m_confluence=False,
        )

        self.assertEqual(standard_audit["consensus_score"], 0)
        self.assertEqual(five_minute_audit["consensus_score"], 35)
        self.assertIn(
            "Path E (MTF Bullish Alignment)",
            five_minute_audit["active_paths"],
        )

    def test_scalp_trailing_moves_to_breakeven_then_tightens_with_atr(self):
        position = Position(
            symbol="Volatility 25 Index",
            action="BUY",
            volume=1.0,
            entry_time=1_700_000_000,
            entry_price=100.0,
            stop_loss=90.0,
            take_profit=120.0,
            risk_amount=10.0,
            entry_spread_points=0,
            consensus_score=65,
            initial_stop_loss=90.0,
            initial_take_profit=120.0,
            planned_rr=2.0,
            higher_timeframe_confirmation="15m=BULLISH;h1=BULLISH",
        )

        self.assertIsNone(
            _updated_stop_loss(position, 109.9, 5.0, "breakeven_then_atr")
        )
        self.assertEqual(
            _updated_stop_loss(position, 110.0, 5.0, "breakeven_then_atr"),
            100.0,
        )
        position.stop_loss = 100.0
        self.assertEqual(
            _updated_stop_loss(position, 115.0, 5.0, "breakeven_then_atr"),
            110.0,
        )

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
                execution_timeframe="3m",
            )

        self.assertEqual(
            actual_start,
            int((available_start + timedelta(days=30)).timestamp()),
        )
        self.assertEqual(
            actual_end,
            int(requested_end.timestamp()) - TIMEFRAMES["3m"][1],
        )
        for timeframe, (_, duration) in TIMEFRAMES.items():
            frame = histories[symbol][timeframe]
            self.assertTrue((frame["time"] + duration <= actual_end).all())
            self.assertEqual(coverage[symbol][timeframe]["bars"], len(frame))

    def test_history_can_skip_sub_five_minute_data(self):
        symbol = "Volatility 25 Index"
        requested_end = datetime(2026, 2, 5, tzinfo=timezone.utc)
        bars_by_timeframe = {}
        available_start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for timeframe, (mt5_timeframe, duration) in TIMEFRAMES.items():
            end_timestamp = int(requested_end.timestamp())
            times = [
                int(available_start.timestamp()),
                end_timestamp - duration * 3,
                end_timestamp - duration * 2,
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
            ) as copy_rates,
        ):
            histories, _, _, _ = _get_histories(
                [symbol],
                datetime(2025, 1, 1, tzinfo=timezone.utc),
                requested_end,
                execution_timeframe="5m",
                include_sub_5m_confluence=False,
            )

        self.assertNotIn(TIMEFRAMES["3m"][0], [
            call.args[1] for call in copy_rates.call_args_list
        ])
        self.assertNotIn("3m", histories[symbol])

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
                return_value=(None, {"consensus_score": 45}),
            ),
            patch.object(backtest, "_trend_at", return_value="BULLISH"),
        ):
            result = backtest.run_backtest(
                histories,
                m5_start + 300 * 300,
                m5_start + 300 * 310,
                1000,
                min_consensus_score=40,
                confirmation_timeframes=("15m", "h1"),
            )

        self.assertEqual(result["summary"]["trades"], 1)
        trade = result["trades"][0]
        self.assertEqual(trade["action"], "BUY")
        self.assertEqual(trade["consensus_score"], 45)
        self.assertEqual(trade["planned_rr"], 2.0)
        self.assertEqual(
            trade["higher_timeframe_confirmation"],
            "15m=BULLISH;h1=BULLISH",
        )
        self.assertAlmostEqual(
            abs(trade["initial_take_profit"] - trade["entry_price"])
            / abs(trade["entry_price"] - trade["initial_stop_loss"]),
            2.0,
        )
        self.assertAlmostEqual(
            trade["realized_r_multiple"],
            trade["pnl"] / trade["risk_amount"],
        )
        self.assertEqual(
            trade["entry_time"],
            pd.to_datetime(m5_start + 300 * 301, unit="s", utc=True).isoformat(),
        )
        self.assertEqual(trade["exit_reason"], "end_of_test")

    def test_planned_reward_risk_cannot_be_less_than_two(self):
        with self.assertRaisesRegex(backtest.BacktestError, "at least 1:2"):
            backtest.run_backtest(
                {}, 1, 2, 1000, target_rr=1.99
            )

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
