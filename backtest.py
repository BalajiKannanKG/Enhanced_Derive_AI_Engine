"""Historical, candle-based replay of the current multi-symbol strategy."""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

import Enhanced_Derive_AI_Engine as engine
from mt5_bridge import mt5


TIMEFRAMES = {
    "3m": (mt5.TIMEFRAME_M3, 180),
    "5m": (mt5.TIMEFRAME_M5, 300),
    "15m": (mt5.TIMEFRAME_M15, 900),
    "h1": (mt5.TIMEFRAME_H1, 3600),
    "d1": (mt5.TIMEFRAME_D1, 86400),
}


@dataclass
class Position:
    symbol: str
    action: str
    volume: float
    entry_time: int
    entry_price: float
    stop_loss: float
    take_profit: float
    risk_amount: float
    entry_spread_points: int
    consensus_score: int
    initial_stop_loss: float
    initial_take_profit: float
    planned_rr: float
    higher_timeframe_confirmation: str


@dataclass
class Trade:
    symbol: str
    action: str
    entry_time: str
    exit_time: str
    volume: float
    entry_price: float
    exit_price: float
    stop_loss: float
    take_profit: float
    pnl: float
    risk_amount: float
    exit_reason: str
    entry_spread_points: int
    exit_spread_points: int
    consensus_score: int
    initial_stop_loss: float
    initial_take_profit: float
    planned_rr: float
    realized_r_multiple: float
    higher_timeframe_confirmation: str


class BacktestError(RuntimeError):
    pass


def _utc_datetime(value: str | None, fallback: datetime) -> datetime:
    if value is None:
        return fallback
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _rates_frame(rates: Any) -> pd.DataFrame:
    if rates is None or len(rates) == 0:
        raise BacktestError(f"MT5 returned no historical bars: {mt5.last_error()}")
    frame = pd.DataFrame(rates)
    frame["time"] = frame["time"].astype("int64")
    frame = frame.drop_duplicates("time", keep="last").sort_values("time")
    frame = frame.reset_index(drop=True)
    required = {"time", "open", "high", "low", "close", "spread"}
    if not required.issubset(frame.columns):
        raise BacktestError(f"MT5 rates omitted required fields: {sorted(required - set(frame.columns))}")
    return frame


def _add_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame
    close, high, low = result["close"], result["high"], result["low"]
    indicators = engine.ta_indicators
    result["ema20"] = indicators.ema(close, length=20)
    result["ema30"] = indicators.ema(close, length=30)
    result["ema100"] = indicators.ema(close, length=100)
    result["rsi"] = indicators.rsi(close, length=14)
    result["atr"] = indicators.atr(high, low, close, length=14)

    adx = indicators.adx(high, low, close, length=14)
    result["adx"] = adx["ADX_14"] if adx is not None and not adx.empty else 20.0
    bands = indicators.bbands(close, length=16, std=1.986)
    if bands is None:
        result["bb_lower"] = close
        result["bb_middle"] = close
        result["bb_upper"] = close
    else:
        result["bb_lower"] = bands.iloc[:, 0]
        result["bb_middle"] = bands.iloc[:, 1]
        result["bb_upper"] = bands.iloc[:, 2]

    stochastic = indicators.stoch(high, low, close, k=14, d=3)
    if stochastic is None:
        result["stoch_k"] = 50.0
        result["stoch_d"] = 50.0
    else:
        result["stoch_k"] = stochastic.iloc[:, 0]
        result["stoch_d"] = stochastic.iloc[:, 1]
    return result


def _value(frame: pd.DataFrame, column: str, index: int, default: float) -> float:
    value = frame[column].iat[index]
    return float(value) if pd.notna(value) else default


def _timeframe_snapshot(
    frame: pd.DataFrame, index: int, duration: int, include_candles: bool = True
) -> dict[str, Any]:
    start = max(0, index - 2)
    candles = [
        {
            "time": int(frame["time"].iat[i]),
            "open": float(frame["open"].iat[i]),
            "high": float(frame["high"].iat[i]),
            "low": float(frame["low"].iat[i]),
            "close": float(frame["close"].iat[i]),
        }
        for i in range(start, index + 1)
    ] if include_candles else []
    close = float(frame["close"].iat[index])
    ema30 = _value(frame, "ema30", index, close)
    ema100 = _value(frame, "ema100", index, close)
    return {
        "data_ready": index + 1 >= 200,
        "trend": "BULLISH" if ema30 > ema100 else "BEARISH",
        "ema20": _value(frame, "ema20", index, close),
        "ema30": ema30,
        "ema100": ema100,
        "rsi": _value(frame, "rsi", index, 50.0),
        "atr": _value(frame, "atr", index, 0.0),
        "adx": _value(frame, "adx", index, 20.0),
        "bb_lower": [_value(frame, "bb_lower", index, close)],
        "bb_middle": [_value(frame, "bb_middle", index, close)],
        "bb_upper": [_value(frame, "bb_upper", index, close)],
        "stoch_k_history": [_value(frame, "stoch_k", index, 50.0)],
        "stoch_d_history": [_value(frame, "stoch_d", index, 50.0)],
        "candles": candles,
        "duration": duration,
    }


def _trend_at(frame: pd.DataFrame, index: int) -> str:
    close = float(frame["close"].iat[index])
    return (
        "BULLISH"
        if _value(frame, "ema30", index, close) > _value(frame, "ema100", index, close)
        else "BEARISH"
    )


def _confirmation_matches(
    action: str,
    timeframe_states: dict[str, dict[str, Any]],
    confirmation_timeframes: tuple[str, ...],
) -> bool:
    required_trend = "BULLISH" if action == "BUY" else "BEARISH"
    return all(
        timeframe_states[timeframe]["trend"] == required_trend
        for timeframe in confirmation_timeframes
    )


def _cpr_from_daily(frame: pd.DataFrame, index: int) -> dict[str, float]:
    if index < 0:
        return {}
    high = float(frame["high"].iat[index])
    low = float(frame["low"].iat[index])
    close = float(frame["close"].iat[index])
    pivot = (high + low + close) / 3.0
    bc = (high + low) / 2.0
    tc = (pivot - bc) + pivot
    if bc > tc:
        tc, bc = bc, tc
    return {
        "pivot": pivot,
        "bc": bc,
        "tc": tc,
        "r1": 2 * pivot - low,
        "s1": 2 * pivot - high,
        "r2": pivot + high - low,
        "s2": pivot - (high - low),
        "r3": high + 2 * (pivot - low),
        "s3": low - 2 * (high - pivot),
    }


def resolve_bar_exit(
    action: str,
    stop_loss: float,
    take_profit: float,
    open_bid: float,
    high_bid: float,
    low_bid: float,
    spread: float,
) -> tuple[float, str] | None:
    """Resolve OHLC stop/target touches, preferring the stop if both were hit."""
    open_price = open_bid if action == "BUY" else open_bid + spread
    high_price = high_bid if action == "BUY" else high_bid + spread
    low_price = low_bid if action == "BUY" else low_bid + spread
    if action == "BUY":
        if stop_loss > 0 and low_price <= stop_loss:
            return min(open_price, stop_loss), "stop_loss"
        if take_profit > 0 and high_price >= take_profit:
            return take_profit, "take_profit"
    else:
        if stop_loss > 0 and high_price >= stop_loss:
            return max(open_price, stop_loss), "stop_loss"
        if take_profit > 0 and low_price <= take_profit:
            return take_profit, "take_profit"
    return None


def _next_volume(
    symbol: str,
    action: str,
    price: float,
    stop_loss: float,
    balance: float,
    risk_percent: float,
    volume_min: float,
    volume_max: float,
    volume_step: float,
) -> tuple[float, float] | None:
    order_type = mt5.ORDER_TYPE_BUY if action == "BUY" else mt5.ORDER_TYPE_SELL
    risk_cap = balance * risk_percent / 100.0
    minimum_loss = mt5.order_calc_profit(
        order_type, symbol, volume_min, price, stop_loss
    )
    minimum_margin = mt5.order_calc_margin(order_type, symbol, volume_min, price)
    if minimum_loss is None or minimum_margin is None:
        raise BacktestError(f"MT5 could not calculate risk/margin for {symbol}: {mt5.last_error()}")
    if abs(float(minimum_loss)) > risk_cap or float(minimum_margin) > balance:
        return None

    step_count = math.floor((volume_max - volume_min) / volume_step + 1e-9)
    low, high = 0, step_count
    while low < high:
        middle = (low + high + 1) // 2
        volume = round(volume_min + middle * volume_step, 8)
        loss = mt5.order_calc_profit(order_type, symbol, volume, price, stop_loss)
        margin = mt5.order_calc_margin(order_type, symbol, volume, price)
        if loss is None or margin is None:
            raise BacktestError(f"MT5 could not calculate risk/margin for {symbol}: {mt5.last_error()}")
        if abs(float(loss)) <= risk_cap and float(margin) <= balance:
            low = middle
        else:
            high = middle - 1
    volume = round(volume_min + low * volume_step, 8)
    loss = mt5.order_calc_profit(order_type, symbol, volume, price, stop_loss)
    if loss is None:
        raise BacktestError(f"MT5 could not calculate risk for {symbol}: {mt5.last_error()}")
    return volume, abs(float(loss))


def _floating_profit(position: Position, price: float) -> float:
    order_type = mt5.ORDER_TYPE_BUY if position.action == "BUY" else mt5.ORDER_TYPE_SELL
    result = mt5.order_calc_profit(
        order_type,
        position.symbol,
        position.volume,
        position.entry_price,
        price,
    )
    if result is None:
        raise BacktestError(f"MT5 could not calculate replay P/L for {position.symbol}: {mt5.last_error()}")
    return float(result)


def _fill_price(action: str, bid: float, spread: float) -> float:
    return bid + spread if action == "BUY" else bid


def _market_exit_price(action: str, bid: float, spread: float) -> float:
    return bid if action == "BUY" else bid + spread


def _updated_stop_loss(
    position: Position,
    market_price: float,
    atr: float,
    trailing_policy: str,
) -> float | None:
    profit_points = (
        market_price - position.entry_price
        if position.action == "BUY"
        else position.entry_price - market_price
    )
    if trailing_policy == "breakeven_then_atr":
        initial_risk_distance = abs(
            position.entry_price - position.initial_stop_loss
        )
        if initial_risk_distance <= 0 or atr <= 0:
            return None
        if profit_points < initial_risk_distance:
            return None

        candidate = position.entry_price
        if profit_points >= initial_risk_distance * 1.5:
            atr_stop = (
                market_price - atr
                if position.action == "BUY"
                else market_price + atr
            )
            candidate = (
                max(candidate, atr_stop)
                if position.action == "BUY"
                else min(candidate, atr_stop)
            )

        improves_stop = (
            candidate > position.stop_loss
            if position.action == "BUY"
            else candidate < position.stop_loss
        )
        return candidate if improves_stop else None
    if trailing_policy != "legacy":
        raise BacktestError(f"Unknown trailing-stop policy: {trailing_policy}")

    if atr <= 0:
        return None
    if profit_points >= atr * 3.0:
        return (
            market_price - atr * 2.0
            if position.action == "BUY"
            else market_price + atr * 2.0
        )
    if profit_points >= atr * 1.5:
        if position.action == "BUY" and position.stop_loss < position.entry_price:
            return position.entry_price + atr * 0.4
        if position.action == "SELL" and (
            position.stop_loss > position.entry_price
            or position.stop_loss == 0.0
        ):
            return position.entry_price - atr * 0.4
    return None


def _close_trade(
    position: Position,
    exit_time: int,
    exit_price: float,
    reason: str,
    exit_spread_points: int,
) -> Trade:
    pnl = _floating_profit(position, exit_price)
    return Trade(
        symbol=position.symbol,
        action=position.action,
        entry_time=datetime.fromtimestamp(position.entry_time, timezone.utc).isoformat(),
        exit_time=datetime.fromtimestamp(exit_time, timezone.utc).isoformat(),
        volume=position.volume,
        entry_price=position.entry_price,
        exit_price=exit_price,
        stop_loss=position.stop_loss,
        take_profit=position.take_profit,
        pnl=pnl,
        risk_amount=position.risk_amount,
        exit_reason=reason,
        entry_spread_points=position.entry_spread_points,
        exit_spread_points=exit_spread_points,
        consensus_score=position.consensus_score,
        initial_stop_loss=position.initial_stop_loss,
        initial_take_profit=position.initial_take_profit,
        planned_rr=position.planned_rr,
        realized_r_multiple=(
            pnl / position.risk_amount if position.risk_amount else 0.0
        ),
        higher_timeframe_confirmation=position.higher_timeframe_confirmation,
    )


def _stats(trades: list[Trade], balance: float, max_drawdown: float) -> dict[str, Any]:
    wins = [trade for trade in trades if trade.pnl > 0]
    losses = [trade for trade in trades if trade.pnl < 0]
    net = sum(trade.pnl for trade in trades)
    gross_profit = sum(trade.pnl for trade in wins)
    gross_loss = abs(sum(trade.pnl for trade in losses))
    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": len(trades) - len(wins) - len(losses),
        "win_rate_percent": (len(wins) / len(trades) * 100.0) if trades else 0.0,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "net_profit": net,
        "ending_balance": balance,
        "return_percent": (net / (balance - net) * 100.0) if balance != net else 0.0,
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "max_drawdown_amount": max_drawdown,
    }


def run_backtest(
    histories: dict[str, dict[str, pd.DataFrame]],
    start_time: int,
    end_time: int,
    starting_balance: float,
    risk_percent: float = 1.0,
    execution_timeframe: str = "5m",
    min_consensus_score: int = 65,
    stop_atr_multiple: float = 1.5,
    target_rr: float = 2.0,
    confirmation_timeframes: tuple[str, ...] = (),
    trailing_policy: str = "legacy",
    include_sub_5m_confluence: bool = True,
) -> dict[str, Any]:
    if execution_timeframe not in {"3m", "5m"}:
        raise BacktestError("Execution timeframe must be 3m or 5m.")
    if (
        min_consensus_score <= 0
        or not math.isfinite(stop_atr_multiple)
        or stop_atr_multiple <= 0
        or not math.isfinite(target_rr)
        or target_rr < 2.0
    ):
        raise BacktestError(
            "Consensus threshold and stop distance must be positive, and planned "
            "reward:risk must be at least 1:2."
        )
    if any(timeframe not in {"15m", "h1"} for timeframe in confirmation_timeframes):
        raise BacktestError(
            "Higher-timeframe confirmation may use only completed 15m and h1 trends."
        )
    if trailing_policy not in {"legacy", "breakeven_then_atr"}:
        raise BacktestError(f"Unknown trailing-stop policy: {trailing_policy}")
    bar_duration = TIMEFRAMES[execution_timeframe][1]
    symbol_info: dict[str, Any] = {}
    prepared: dict[str, dict[str, pd.DataFrame]] = {}
    time_arrays: dict[str, dict[str, Any]] = {}
    point_sizes: dict[str, float] = {}
    for symbol, frames in histories.items():
        info = mt5.symbol_info(symbol)
        if info is None:
            raise BacktestError(f"MT5 symbol settings unavailable for {symbol}: {mt5.last_error()}")
        if info.volume_min <= 0 or info.volume_step <= 0 or info.volume_max < info.volume_min:
            raise BacktestError(f"Invalid MT5 volume settings for {symbol}.")
        symbol_info[symbol] = info
        point_sizes[symbol] = float(info.point)
        prepared[symbol] = {}
        for timeframe, frame in frames.items():
            prepared[symbol][timeframe] = _add_indicators(frame)
        time_arrays[symbol] = {
            timeframe: frame["time"].to_numpy(dtype="int64", copy=False)
            for timeframe, frame in prepared[symbol].items()
        }

    ordered_symbols = list(histories)
    event_indices = {
        symbol: bisect.bisect_left(time_arrays[symbol][execution_timeframe], start_time)
        for symbol in ordered_symbols
    }
    all_times = sorted({
        timestamp
        for symbol in ordered_symbols
        for timestamp in time_arrays[symbol][execution_timeframe][event_indices[symbol]:]
        if timestamp < end_time
    })
    if not all_times:
        raise BacktestError(
            f"No {execution_timeframe} history overlaps the requested backtest period."
        )

    trades: list[Trade] = []
    balance = starting_balance
    peak_equity = balance
    max_drawdown = 0.0
    position: Position | None = None
    daily_date = None
    daily_start_balance = balance
    daily_realized_pnl = 0.0
    blocked_daily_dates: set[str] = set()
    symbol_trade_counts = {symbol: 0 for symbol in ordered_symbols}
    signals_blocked_by_htf_confirmation = 0

    for event_number, bar_start in enumerate(all_times, start=1):
        indices = {
            symbol: event_indices[symbol]
            for symbol in ordered_symbols
            if event_indices[symbol] < len(time_arrays[symbol][execution_timeframe])
            and time_arrays[symbol][execution_timeframe][event_indices[symbol]] == bar_start
        }
        closed_position = False
        if position is not None and position.symbol in indices:
            symbol = position.symbol
            index = indices[symbol]
            frame = prepared[symbol][execution_timeframe]
            spread_points = int(frame["spread"].iat[index])
            spread = max(0, spread_points) * point_sizes[symbol]
            exit_result = resolve_bar_exit(
                position.action,
                position.stop_loss,
                position.take_profit,
                float(frame["open"].iat[index]),
                float(frame["high"].iat[index]),
                float(frame["low"].iat[index]),
                spread,
            )
            close_time = bar_start + bar_duration
            snapshot = _timeframe_snapshot(frame, index, bar_duration)
            atr = snapshot["atr"]
            market_price = _market_exit_price(
                position.action,
                float(frame["close"].iat[index]),
                spread,
            )
            if exit_result is not None:
                exit_price, reason = exit_result
                trade = _close_trade(
                    position, close_time, exit_price, reason, spread_points
                )
                trades.append(trade)
                balance += trade.pnl
                daily_realized_pnl += trade.pnl
                position = None
                closed_position = True
            else:
                updated_stop = _updated_stop_loss(
                    position, market_price, atr, trailing_policy
                )
                stop_updated = updated_stop is not None
                if updated_stop is not None:
                    position.stop_loss = updated_stop

                    age = close_time - position.entry_time
                    pnl_now = _floating_profit(position, market_price)
                    if not stop_updated and age >= 120 and pnl_now < 0 and snapshot["ema20"] > 0:
                        breached = (
                            market_price < snapshot["ema20"] - atr * 0.8
                            if position.action == "BUY"
                            else market_price > snapshot["ema20"] + atr * 0.8
                        )
                        if breached:
                            trade = _close_trade(
                                position, close_time, market_price,
                                "early_structural_exit", spread_points,
                            )
                            trades.append(trade)
                            balance += trade.pnl
                            daily_realized_pnl += trade.pnl
                            position = None
                            closed_position = True

                if position is not None:
                    equity = balance + _floating_profit(position, market_price)
                    peak_equity = max(peak_equity, equity)
                    max_drawdown = max(max_drawdown, peak_equity - equity)
        else:
            equity = balance
            peak_equity = max(peak_equity, equity)
            max_drawdown = max(max_drawdown, peak_equity - equity)

        if not closed_position and position is None:
            date_key = datetime.fromtimestamp(bar_start, timezone.utc).date().isoformat()
            if date_key != daily_date:
                daily_date = date_key
                daily_start_balance = balance
                daily_realized_pnl = 0.0
            day_loss_limit = daily_start_balance * engine.MAX_DAILY_LOSS_PERCENT / 100.0
            if daily_realized_pnl <= -day_loss_limit:
                blocked_daily_dates.add(date_key)
            elif date_key not in blocked_daily_dates:
                choices = []
                decision_time = bar_start + bar_duration
                for symbol, index in indices.items():
                    if index + 1 >= len(time_arrays[symbol][execution_timeframe]):
                        continue
                    timeframe_frames = prepared[symbol]
                    completed_indices = {}
                    for timeframe, (_, duration) in TIMEFRAMES.items():
                        if timeframe not in timeframe_frames:
                            continue
                        tf_times = time_arrays[symbol][timeframe]
                        completed = bisect.bisect_right(tf_times, decision_time - duration) - 1
                        completed_indices[timeframe] = completed
                    execution_index = completed_indices[execution_timeframe]
                    m5_index = completed_indices["5m"]
                    h1_index = completed_indices["h1"]
                    if (
                        execution_index < 199
                        or h1_index < 199
                        or execution_index != index
                    ):
                        continue
                    m15_index = completed_indices["15m"]
                    if m15_index < 0:
                        continue
                    timeframe_states = {
                        "5m": _timeframe_snapshot(
                            timeframe_frames["5m"], m5_index, 300
                        ),
                        "15m": {"trend": _trend_at(timeframe_frames["15m"], m15_index)},
                        "h1": {"trend": _trend_at(timeframe_frames["h1"], h1_index)},
                    }
                    if "3m" in timeframe_frames:
                        m3_index = completed_indices["3m"]
                        timeframe_states["3m"] = {
                            "trend": _trend_at(timeframe_frames["3m"], m3_index)
                        }
                    decision_state = (
                        timeframe_states[execution_timeframe]
                        if execution_timeframe == "5m"
                        else _timeframe_snapshot(
                            timeframe_frames[execution_timeframe],
                            execution_index,
                            bar_duration,
                        )
                    )
                    current_daily_index = completed_indices["d1"]
                    cpr = _cpr_from_daily(
                        timeframe_frames["d1"], current_daily_index
                    )
                    _, audit = engine.evaluate_multi_path_strategy(
                        decision_state,
                        timeframe_states["h1"],
                        float(timeframe_frames[execution_timeframe]["close"].iat[execution_index]),
                        cpr,
                        timeframe_states,
                        include_sub_5m_confluence=include_sub_5m_confluence,
                    )
                    score = int(audit["consensus_score"])
                    signal = (
                        "BUY" if score >= min_consensus_score
                        else "SELL" if score <= -min_consensus_score
                        else None
                    )
                    if signal:
                        if not _confirmation_matches(
                            signal, timeframe_states, confirmation_timeframes
                        ):
                            signals_blocked_by_htf_confirmation += 1
                            continue
                        confirmed_trends = {
                            timeframe: timeframe_states[timeframe]["trend"]
                            for timeframe in confirmation_timeframes
                        }
                        confirmation_label = ";".join(
                            f"{timeframe}={trend}"
                            for timeframe, trend in confirmed_trends.items()
                        )
                        choices.append(
                            (
                                abs(score),
                                symbol,
                                signal,
                                audit,
                                index,
                                confirmation_label,
                            )
                        )

                if choices:
                    _, symbol, action, audit, index, confirmation_label = max(
                        choices, key=lambda item: (item[0], -ordered_symbols.index(item[1]))
                    )
                    frame = prepared[symbol][execution_timeframe]
                    next_index = index + 1
                    info = symbol_info[symbol]
                    spread_points = int(frame["spread"].iat[next_index])
                    spread = max(0, spread_points) * point_sizes[symbol]
                    fill = _fill_price(
                        action, float(frame["open"].iat[next_index]), spread
                    )
                    atr = _value(frame, "atr", index, 0.0)
                    if not math.isfinite(atr) or atr <= 0:
                        continue
                    stop_distance = atr * stop_atr_multiple
                    target_distance = stop_distance * target_rr
                    stop_loss = fill - stop_distance if action == "BUY" else fill + stop_distance
                    take_profit = fill + target_distance if action == "BUY" else fill - target_distance
                    sizing = _next_volume(
                        symbol,
                        action,
                        fill,
                        stop_loss,
                        balance,
                        risk_percent,
                        float(info.volume_min),
                        float(info.volume_max),
                        float(info.volume_step),
                    )
                    if sizing is None:
                        continue
                    volume, risk_amount = sizing
                    position = Position(
                        symbol=symbol,
                        action=action,
                        volume=volume,
                        entry_time=int(frame["time"].iat[next_index]),
                        entry_price=fill,
                        stop_loss=stop_loss,
                        take_profit=take_profit,
                        risk_amount=risk_amount,
                        entry_spread_points=spread_points,
                        consensus_score=int(audit["consensus_score"]),
                        initial_stop_loss=stop_loss,
                        initial_take_profit=take_profit,
                        planned_rr=target_rr,
                        higher_timeframe_confirmation=confirmation_label,
                    )
                    symbol_trade_counts[symbol] += 1

        for symbol in indices:
            event_indices[symbol] += 1
        if event_number % 10000 == 0:
            print(
                f"Replayed {event_number:,}/{len(all_times):,} "
                f"{execution_timeframe} events; closed trades: {len(trades)}"
            )

    if position is not None:
        symbol = position.symbol
        frame = prepared[symbol][execution_timeframe]
        index = min(
            bisect.bisect_right(time_arrays[symbol][execution_timeframe], end_time) - 1,
            len(time_arrays[symbol][execution_timeframe]) - 1,
        )
        if index >= 0:
            spread_points = int(frame["spread"].iat[index])
            spread = max(0, spread_points) * point_sizes[symbol]
            exit_time = min(end_time, int(frame["time"].iat[index]) + bar_duration)
            exit_price = _market_exit_price(
                position.action,
                float(frame["close"].iat[index]),
                spread,
            )
            trade = _close_trade(
                position, exit_time, exit_price, "end_of_test",
                spread_points,
            )
            trades.append(trade)
            final_equity = balance + trade.pnl
            max_drawdown = max(max_drawdown, peak_equity - final_equity)
            balance = final_equity

    summary = _stats(trades, balance, max_drawdown)
    summary["trades_by_symbol"] = symbol_trade_counts
    summary["days_blocked_by_daily_loss"] = len(blocked_daily_dates)
    summary["signals_blocked_by_htf_confirmation"] = (
        signals_blocked_by_htf_confirmation
    )
    trades_by_day: dict[str, int] = {}
    for trade in trades:
        trade_day = trade.entry_time[:10]
        trades_by_day[trade_day] = trades_by_day.get(trade_day, 0) + 1
    calendar_days = (
        datetime.fromtimestamp(end_time, timezone.utc).date()
        - datetime.fromtimestamp(start_time, timezone.utc).date()
    ).days + 1
    summary["calendar_days"] = calendar_days
    summary["days_with_trades"] = len(trades_by_day)
    summary["average_trades_per_calendar_day"] = (
        len(trades) / calendar_days if calendar_days else 0.0
    )
    summary["average_trades_per_active_day"] = (
        len(trades) / len(trades_by_day) if trades_by_day else 0.0
    )
    summary["days_with_3_to_4_trades"] = sum(
        3 <= count <= 4 for count in trades_by_day.values()
    )
    summary["first_trade_date_utc"] = min(trades_by_day) if trades_by_day else None
    summary["last_trade_date_utc"] = max(trades_by_day) if trades_by_day else None
    summary["trades_by_utc_day"] = trades_by_day
    return {
        "period_start_utc": datetime.fromtimestamp(start_time, timezone.utc).isoformat(),
        "period_end_utc": datetime.fromtimestamp(end_time, timezone.utc).isoformat(),
        "starting_balance": starting_balance,
        "risk_per_trade_percent": risk_percent,
        "execution_timeframe": execution_timeframe,
        "minimum_consensus_score": min_consensus_score,
        "stop_atr_multiple": stop_atr_multiple,
        "planned_reward_risk_ratio": target_rr,
        "higher_timeframe_confirmation": list(confirmation_timeframes),
        "sub_5m_confluence_enabled": include_sub_5m_confluence,
        "trailing_policy": trailing_policy,
        "trailing_policy_description": (
            "Move stop to entry after +1R, then trail by 1 ATR after +1.5R; "
            "keep the initial 2R target."
            if trailing_policy == "breakeven_then_atr"
            else "Use the existing replay trailing behavior."
        ),
        "daily_loss_limit_percent": engine.MAX_DAILY_LOSS_PERCENT,
        "daily_loss_reset_timezone": "UTC",
        "execution_model": "MT5 OHLC candles; next-bar-open entry; stop wins when both SL and TP are touched",
        "cost_model": (
            "Historical MT5 spread points applied to bid OHLC for ask-side fills. "
            "Current MT5 contract/profit/margin calculations; historical commission "
            "and slippage unavailable and excluded."
        ),
        "position_management": (
            "Losing EMA20 structural exits are checked at "
            f"{execution_timeframe} closes; stop updates apply from the next candle."
        ),
        "summary": summary,
        "trades": [asdict(trade) for trade in trades],
    }


def _get_histories(
    symbols: list[str],
    start: datetime,
    end: datetime,
    execution_timeframe: str = "5m",
    include_sub_5m_confluence: bool = True,
) -> tuple[dict[str, dict[str, pd.DataFrame]], dict[str, Any], int, int]:
    if execution_timeframe not in {"3m", "5m"}:
        raise BacktestError("Execution timeframe must be 3m or 5m.")
    warmup_start = start - timedelta(days=30)
    requested_start = int(start.timestamp())
    requested_end = int(end.timestamp())
    histories: dict[str, dict[str, pd.DataFrame]] = {}
    coverage: dict[str, Any] = {}
    for symbol in symbols:
        print(f"Loading MT5 history for {symbol}...")
        if not mt5.symbol_select(symbol, True):
            raise BacktestError(f"MT5 cannot select {symbol}: {mt5.last_error()}")
        symbol_frames = {}
        coverage[symbol] = {}
        for timeframe, (mt5_timeframe, duration) in TIMEFRAMES.items():
            if timeframe == "3m" and not include_sub_5m_confluence:
                continue
            rates = mt5.copy_rates_range(symbol, mt5_timeframe, warmup_start, end)
            frame = _rates_frame(rates)
            times = frame["time"]
            frame = frame.loc[
                (times >= int(warmup_start.timestamp()))
                & (times + duration <= requested_end)
            ].reset_index(drop=True)
            if frame.empty:
                raise BacktestError(
                    f"MT5 has no completed {timeframe} candles for {symbol} in "
                    f"the requested period ending {end.isoformat()}."
                )
            symbol_frames[timeframe] = frame
        histories[symbol] = symbol_frames

    latest_execution_close = min(
        int(frames[execution_timeframe]["time"].iat[-1])
        + TIMEFRAMES[execution_timeframe][1]
        for frames in histories.values()
    )
    actual_end = min(requested_end, latest_execution_close)
    earliest_history = max(
        int(frame["time"].iat[0])
        for frames in histories.values()
        for timeframe, frame in frames.items()
    )
    actual_start = max(
        requested_start,
        earliest_history + int(timedelta(days=30).total_seconds()),
    )
    if actual_start >= actual_end:
        raise BacktestError(
            "The available MT5 history is too short after the 30-day indicator "
            "warm-up; no usable backtest period remains."
        )

    for symbol, frames in histories.items():
        coverage[symbol] = {}
        for timeframe, (_, duration) in TIMEFRAMES.items():
            if timeframe not in frames:
                continue
            frame = frames[timeframe]
            frame = frame.loc[
                frame["time"] + duration <= actual_end
            ].reset_index(drop=True)
            if frame.empty:
                raise BacktestError(
                    f"MT5 has no completed {timeframe} candles for {symbol} "
                    "within the common available history window."
                )
            frames[timeframe] = frame
            first = int(frame["time"].iat[0])
            last = int(frame["time"].iat[-1])
            coverage[symbol][timeframe] = {
                "bars": len(frame),
                "first_utc": datetime.fromtimestamp(first, timezone.utc).isoformat(),
                "last_utc": datetime.fromtimestamp(last, timezone.utc).isoformat(),
            }
            print(f"  {symbol} {timeframe}: {len(frame):,} bars")
    return histories, coverage, actual_start, actual_end


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Replay a baseline or experimental scalping profile on MT5 candles."
    )
    parser.add_argument("--profile", choices=("baseline", "scalp"), default="baseline")
    parser.add_argument("--years", type=float, default=2.0)
    parser.add_argument("--start", help="ISO date/time; defaults to --years before end.")
    parser.add_argument("--end", help="ISO date/time; defaults to now (UTC).")
    parser.add_argument("--balance", type=float, default=1000.0)
    parser.add_argument("--risk-percent", type=float, default=1.0)
    parser.add_argument(
        "--min-consensus-score",
        type=int,
        help="Override the profile's minimum absolute consensus score.",
    )
    parser.add_argument("--symbols", nargs="+", choices=list(engine.SUPPORTED_SYMBOLS), default=list(engine.SUPPORTED_SYMBOLS))
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    if not math.isfinite(args.balance) or args.balance <= 0:
        parser.error("--balance must be a positive finite amount.")
    if not math.isfinite(args.risk_percent) or not 0 < args.risk_percent <= 100:
        parser.error("--risk-percent must be greater than 0 and no more than 100.")
    if args.min_consensus_score is not None and args.min_consensus_score <= 0:
        parser.error("--min-consensus-score must be positive.")
    if not math.isfinite(args.years) or args.years <= 0:
        parser.error("--years must be greater than zero.")

    end = _utc_datetime(args.end, datetime.now(timezone.utc))
    start = _utc_datetime(args.start, end - timedelta(days=args.years * 365.25))
    if start >= end:
        parser.error("--start must be earlier than --end.")

    if not mt5.initialize():
        print(f"MT5 initialization failed: {mt5.last_error()}", file=sys.stderr)
        return 2
    try:
        terminal = mt5.terminal_info()
        if terminal is None or not terminal.connected:
            raise BacktestError("MT5 terminal is not connected.")
        account = mt5.account_info()
        if account is None:
            raise BacktestError(f"MT5 account currency is unavailable: {mt5.last_error()}")
        profile = {
            "baseline": {
                "execution_timeframe": "5m",
                "min_consensus_score": 65,
                "stop_atr_multiple": 1.5,
                "confirmation_timeframes": (),
                "trailing_policy": "legacy",
                "include_sub_5m_confluence": True,
            },
            "scalp": {
                "execution_timeframe": "5m",
                "min_consensus_score": 65,
                "stop_atr_multiple": 1.0,
                "confirmation_timeframes": ("15m", "h1"),
                "trailing_policy": "breakeven_then_atr",
                "include_sub_5m_confluence": False,
            },
        }[args.profile]
        if args.min_consensus_score is not None:
            profile["min_consensus_score"] = args.min_consensus_score
        histories, coverage, actual_start, actual_end = _get_histories(
            args.symbols,
            start,
            end,
            profile["execution_timeframe"],
            profile["include_sub_5m_confluence"],
        )
        if actual_start > int(start.timestamp()):
            print(
                "Available history begins after the requested start; running "
                f"from {datetime.fromtimestamp(actual_start, timezone.utc).isoformat()}."
            )
        if actual_end < int(end.timestamp()):
            print(
                "Using the latest common completed "
                f"{profile['execution_timeframe']} candle; ending at "
                f"{datetime.fromtimestamp(actual_end, timezone.utc).isoformat()}."
            )
        report = run_backtest(
            histories,
            actual_start,
            actual_end,
            args.balance,
            args.risk_percent,
            **profile,
            target_rr=2.0,
        )
        report["symbols"] = args.symbols
        report["strategy_profile"] = args.profile
        report["requested_period_start_utc"] = start.isoformat()
        report["requested_period_end_utc"] = end.isoformat()
        report["history_coverage"] = coverage
        report["mt5_terminal"] = {
            "name": terminal.name,
            "build": terminal.build,
            "max_bars": terminal.maxbars,
        }
        report["account_currency"] = account.currency
        default_output = (
            "backtest_scalp_results"
            if args.profile == "scalp"
            else "backtest_results"
        )
        output = Path(args.output or default_output)
        json_path = output.with_suffix(".json")
        csv_path = output.with_suffix(".csv")
        json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        with csv_path.open("w", newline="", encoding="utf-8") as file:
            trades = report["trades"]
            fields = list(trades[0]) if trades else list(Trade.__dataclass_fields__)
            writer = csv.DictWriter(file, fieldnames=fields)
            writer.writeheader()
            writer.writerows(trades)
        print(json.dumps(report["summary"], indent=2))
        print(f"Detailed report: {json_path.resolve()}")
        print(f"Trade list: {csv_path.resolve()}")
        return 0
    except BacktestError as error:
        print(f"Backtest cannot run: {error}", file=sys.stderr)
        return 2
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
