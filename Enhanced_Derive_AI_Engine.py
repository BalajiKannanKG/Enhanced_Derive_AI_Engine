# Enhanced_Derive_AI_Engine.py - Full 760+ Line Codebase + Real-Time WebSocket Push
import argparse
import functools
import json
import math
import re
import threading
import time
import os
from datetime import datetime
from datetime import timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from typing import Any, cast
import pandas as pd
import pandas_ta as ta
from mt5_bridge import MT5Bridge
from websockets.sync.server import serve

ta_indicators: Any = cast(Any, ta)

SUPPORTED_SYMBOLS = (
    "Volatility 10 Index",
    "Volatility 25 Index",
    "Volatility 50 Index",
    "Volatility 100 Index",
)
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "8080"))
WEBSOCKET_PORT = 8765 if DASHBOARD_PORT == 8080 else DASHBOARD_PORT + 1
MT5_SYMBOL = os.environ.get("MT5_SYMBOL", "Volatility 25 Index")
if MT5_SYMBOL not in SUPPORTED_SYMBOLS:
    raise ValueError(f"MT5_SYMBOL must be one of: {', '.join(SUPPORTED_SYMBOLS)}")
DEFAULT_SYMBOL = MT5_SYMBOL
MAX_OPEN_POSITIONS = 1
MT5_MAGIC_NUMBER = 123456

def load_percentage_setting(name, default):
    try:
        configured_value = os.environ.get(name)
        value = float(default if configured_value is None else configured_value)
    except ValueError as error:
        raise ValueError(f"{name} must be a number between 0 and 100.") from error
    if not math.isfinite(value) or value <= 0 or value > 100:
        raise ValueError(f"{name} must be greater than 0 and no more than 100.")
    return value

MAX_RISK_PER_TRADE_PERCENT = load_percentage_setting("MAX_RISK_PER_TRADE_PERCENT", 1.0)
MAX_DAILY_LOSS_PERCENT = load_percentage_setting("MAX_DAILY_LOSS_PERCENT", 2.0)
LIVE_TRADING_ENABLED = False
AUTO_TRADING_ENABLED = False
agent_status = "DATA ONLY"
current_signal = "HOLD / SCANNING"
current_price = 0.0
current_prices = {symbol: 0.0 for symbol in SUPPORTED_SYMBOLS}
state_lock = threading.Lock()
connected_ui_clients = set()
latest_state_payload = None
mt5_bridge = MT5Bridge(symbol=DEFAULT_SYMBOL, symbols=list(SUPPORTED_SYMBOLS))
mt5_state_lock = threading.Lock()
latest_mt5_account = {}
latest_mt5_status = mt5_bridge.status()
last_auto_evaluated_candle_times: dict[str, str] = {}
auto_trade_lock = threading.Lock()

def check_daily_loss_limit(account_state):
    if not isinstance(account_state, dict):
        return False, "MT5 account state is unavailable."

    try:
        starting_balance = float(account_state["daily_start_balance"])
        daily_pnl = float(account_state["daily_pnl"])
    except (KeyError, TypeError, ValueError):
        return False, "MT5 account state must include daily_start_balance and daily_pnl."

    if not math.isfinite(starting_balance) or starting_balance <= 0 or not math.isfinite(daily_pnl):
        return False, "MT5 daily risk metrics are invalid."

    daily_loss_percent = max(0.0, -daily_pnl / starting_balance * 100.0)
    if daily_loss_percent >= MAX_DAILY_LOSS_PERCENT:
        return False, (
            f"Daily loss limit reached ({daily_loss_percent:.2f}% of "
            f"{starting_balance:.2f}; limit {MAX_DAILY_LOSS_PERCENT:.2f}%)."
        )
    return True, ""

def validate_requested_risk_percent(payload):
    try:
        risk_fraction = float(payload.get("risk_pct", MAX_RISK_PER_TRADE_PERCENT / 100.0))
    except (AttributeError, TypeError, ValueError):
        return None, "Trade request risk_pct must be a number."

    if not math.isfinite(risk_fraction) or risk_fraction <= 0:
        return None, "Trade request risk_pct must be greater than zero."
    risk_percent = risk_fraction * 100.0
    if risk_percent > MAX_RISK_PER_TRADE_PERCENT:
        return None, (
            f"Trade risk {risk_percent:.2f}% exceeds the configured "
            f"{MAX_RISK_PER_TRADE_PERCENT:.2f}% per-trade limit."
        )
    return risk_fraction, ""

TF_GRANULARITY = {
    '3m': 180,
    '5m': 300,
    '15m': 900,
    '30m': 1800,
    'h1': 3600,
    'h4': 14400,
    'd1': 86400
}
MT5_TIMEFRAMES = {
    '3m': 'TIMEFRAME_M3',
    '5m': 'TIMEFRAME_M5',
    '15m': 'TIMEFRAME_M15',
    '30m': 'TIMEFRAME_M30',
    'h1': 'TIMEFRAME_H1',
    'h4': 'TIMEFRAME_H4',
    'd1': 'TIMEFRAME_D1',
}

def create_timeframe_cache() -> dict[str, pd.DataFrame | None]:
    return {timeframe: None for timeframe in TF_GRANULARITY}


symbol_tf_caches = {
    symbol: create_timeframe_cache() for symbol in SUPPORTED_SYMBOLS
}
tf_caches = symbol_tf_caches[DEFAULT_SYMBOL]


def calculate_cpr(df):
    if df is None or len(df) < 2:
        return {}
    
    prev_day = df.iloc[-2]
    high, low, close = float(prev_day['high']), float(prev_day['low']), float(prev_day['close'])
    pivot = (high + low + close) / 3.0
    bc = (high + low) / 2.0
    tc = (pivot - bc) + pivot
    if bc > tc: tc, bc = bc, tc
        
    return {
        "pivot": round(pivot, 3), "bc": round(bc, 3), "tc": round(tc, 3),
        "r1": round((2 * pivot) - low, 3), "s1": round((2 * pivot) - high, 3),
        "r2": round(pivot + (high - low), 3), "s2": round(pivot - (high - low), 3),
        "r3": round(high + 2 * (pivot - low), 3), "s3": round(low - 2 * (high - pivot), 3),
        "is_virgin": True
    }

def get_true_previous_day_ohlc(symbol: str = DEFAULT_SYMBOL):
    daily_df = symbol_tf_caches[symbol].get('d1')
    if daily_df is not None and len(daily_df) >= 2:
        prev_day = daily_df.iloc[-2]
        return float(prev_day['open']), float(prev_day['close'])
    elif daily_df is not None and len(daily_df) == 1:
        return float(daily_df.iloc[-1]['open']), float(daily_df.iloc[-1]['close'])
    symbol_price = current_prices.get(symbol, 0.0)
    return symbol_price, symbol_price

def process_dataframe(df, symbol: str = DEFAULT_SYMBOL):
    if df is None or len(df) < 5:
        p_open, p_close = get_true_previous_day_ohlc(symbol)
        p = current_prices.get(symbol, 0.0)
        candles = []
        if df is not None and not df.empty:
            for idx, row in df.iterrows():
                candle_time = row["time"] if "time" in row else idx
                candles.append({
                    "time": candle_time,
                    "open": round(float(row["open"]), 3),
                    "high": round(float(row["high"]), 3),
                    "low": round(float(row["low"]), 3),
                    "close": round(float(row["close"]), 3),
                    "pattern": "Normal",
                })
            if p <= 0:
                p = float(df.iloc[-1]["close"])
        length = len(candles)
        return {
            "data_ready": False,
            "trend": "FLAT", "ema20": p, "ema30": p, "ema100": p, "ema200": p,
            "rsi": 50.0, "atr": 0.0, "adx": 0.0, "bullish_pressure": 50, "candles": candles,
            "bb_upper": [p] * length, "bb_middle": [p] * length, "bb_lower": [p] * length,
            "ema20_history": [p] * length, "ema30_history": [p] * length, "ema100_history": [p] * length,
            "atr_history": [0.0] * length, "adx_history": [0.0] * length,
            "rsi_history": [50.0] * length, "stoch_k_history": [50.0] * length,
            "stoch_d_history": [50.0] * length, "prev_day_open": p_open, "prev_day_close": p_close
        }
    
    df = df.copy()
    
    df = df[~df.index.duplicated(keep='last')]
    df = df.sort_index()
    
    df['ema20'] = ta_indicators.ema(df['close'], length=20)
    df['ema30'] = ta_indicators.ema(df['close'], length=30)
    df['ema100'] = ta_indicators.ema(df['close'], length=100)
    df['ema200'] = ta_indicators.ema(df['close'], length=200)
    df['rsi'] = ta_indicators.rsi(df['close'], length=14)
    df['atr'] = ta_indicators.atr(df['high'], df['low'], df['close'], length=14)
    
    adx_df = ta_indicators.adx(df['high'], df['low'], df['close'], length=14)
    df['adx'] = adx_df['ADX_14'] if adx_df is not None and not adx_df.empty else 20.0

    bb = ta_indicators.bbands(df['close'], length=16, std=1.986)
    if bb is not None:
        df['bb_lower'] = bb.iloc[:, 0]
        df['bb_middle'] = bb.iloc[:, 1]
        df['bb_upper'] = bb.iloc[:, 2]
    else:
        df['bb_lower'] = df['close']
        df['bb_middle'] = df['close']
        df['bb_upper'] = df['close']

    stoch = ta_indicators.stoch(df['high'], df['low'], df['close'], k=14, d=3)
    if stoch is not None:
        df['stoch_k'] = stoch.iloc[:, 0]
        df['stoch_d'] = stoch.iloc[:, 1]
    else:
        df['stoch_k'] = 50
        df['stoch_d'] = 50

    latest = df.iloc[-1]
    close_val = float(latest['close'])
    ema20_val = round(float(latest['ema20'] if pd.notna(latest['ema20']) else close_val), 3)
    ema30_val = round(float(latest['ema30'] if pd.notna(latest['ema30']) else close_val), 3)
    ema100_val = round(float(latest['ema100'] if pd.notna(latest['ema100']) else close_val), 3)
    ema200_val = round(float(latest['ema200'] if pd.notna(latest['ema200']) else close_val), 3)
    rsi_val = round(float(latest['rsi'] if pd.notna(latest['rsi']) else 50), 1)
    atr_val = round(float(latest['atr'] if pd.notna(latest['atr']) else 0.0), 3)
    adx_val = round(float(latest['adx'] if pd.notna(latest['adx']) else 20.0), 1)
    
    trend = "BULLISH" if ema30_val > ema100_val else "BEARISH"
    prev_open, prev_close = get_true_previous_day_ohlc(symbol)

    candles_list = []
    tail_df = df.tail(75)
    for idx, row in tail_df.iterrows():
        o, h, l, c = float(row['open']), float(row['high']), float(row['low']), float(row['close'])
        body = abs(c - o)
        range_len = h - l if h != l else 0.001
        
        pattern = "Normal"
        if body / range_len < 0.12: pattern = "Doji"
        elif (c > o) and ((o - l) > 2 * body): pattern = "Hammer / Bullish Pinbar"
        elif (c < o) and ((h - o) > 2 * body): pattern = "Shooting Star / Bearish Pinbar"
        elif body / range_len > 0.75: pattern = "Impulse Marubozu"
        elif c > o: pattern = "Mild Bullish"
        else: pattern = "Mild Bearish"

        time_val = row['time'] if 'time' in row else idx

        candles_list.append({
            "time": time_val,
            "open": round(o, 3), "high": round(h, 3), "low": round(l, 3), "close": round(c, 3), "pattern": pattern
        })

    bullish_count = sum(1 for c in candles_list if c['close'] >= c['open'])
    pressure = int((bullish_count / len(candles_list)) * 100) if len(candles_list) > 0 else 50

    return {
        "data_ready": len(df) >= 200,
        "trend": trend, "ema20": ema20_val, "ema30": ema30_val, "ema100": ema100_val, "ema200": ema200_val,
        "rsi": rsi_val, "atr": atr_val, "adx": adx_val, "bullish_pressure": pressure, "candles": candles_list,
        "bb_upper": [round(x, 3) for x in tail_df['bb_upper'].fillna(close_val).tolist()],
        "bb_middle": [round(x, 3) for x in tail_df['bb_middle'].fillna(close_val).tolist()],
        "bb_lower": [round(x, 3) for x in tail_df['bb_lower'].fillna(close_val).tolist()],
        "ema20_history": [round(x, 3) for x in tail_df['ema20'].fillna(close_val).tolist()],
        "ema30_history": [round(x, 3) for x in tail_df['ema30'].fillna(close_val).tolist()],
        "ema100_history": [round(x, 3) for x in tail_df['ema100'].fillna(close_val).tolist()],
        "atr_history": [round(x, 3) for x in tail_df['atr'].fillna(1.0).tolist()],
        "adx_history": [round(x, 1) for x in tail_df['adx'].fillna(20.0).tolist()],
        "rsi_history": [round(x, 1) for x in tail_df['rsi'].fillna(50).tolist()],
        "stoch_k_history": [round(x, 1) for x in tail_df['stoch_k'].fillna(50).tolist()],
        "stoch_d_history": [round(x, 1) for x in tail_df['stoch_d'].fillna(50).tolist()],
        "prev_day_open": round(prev_open, 3),
        "prev_day_close": round(prev_close, 3)
    }

def evaluate_multi_path_strategy(
    tf_data,
    h1_data,
    current_price,
    cpr,
    all_timeframes,
    include_sub_5m_confluence=True,
):
    atr = tf_data.get("atr", 1.0)
    adx = tf_data.get("adx", 20.0)
    rsi = tf_data.get("rsi", 50.0)
    stoch_k = tf_data.get("stoch_k_history", [50])[-1]
    ema20 = tf_data.get("ema20", current_price)
    
    bb_upper = tf_data.get("bb_upper", [current_price + 10])[-1]
    bb_lower = tf_data.get("bb_lower", [current_price - 10])[-1]
    bb_width = bb_upper - bb_lower
    is_squeeze = bb_width <= (atr * 4.0)
    h1_trend = h1_data.get("trend", "FLAT")

    total_score = 0
    active_paths = []

    # PATH A: Trend Pullback & EMA Retest
    if adx > 22.0:
        if h1_trend == "BULLISH" and current_price > ema20 and 45 < rsi < 65 and stoch_k < 30:
            total_score += 30
            active_paths.append("Path A (Bull Trend Pullback)")
        elif h1_trend == "BEARISH" and current_price < ema20 and 35 < rsi < 55 and stoch_k > 70:
            total_score -= 30
            active_paths.append("Path A (Bear Trend Pullback)")

    # PATH B: Volatility Squeeze & Breakout
    if not is_squeeze:
        if current_price > bb_upper and stoch_k > 60 and rsi > 55:
            total_score += 25
            active_paths.append("Path B (Bull Band Breakout)")
        elif current_price < bb_lower and stoch_k < 40 and rsi < 45:
            total_score -= 25
            active_paths.append("Path B (Bear Band Breakdown)")

    # PATH C: CPR Support / Resistance Rejection
    if cpr:
        pivot = cpr.get("pivot", current_price)
        for s in [cpr.get("s1", 0), cpr.get("s2", 0), pivot]:
            if s > 0 and abs(current_price - s) <= (atr * 0.6) and current_price >= s and stoch_k < 25:
                total_score += 25
                active_paths.append(f"Path C (CPR Support Rejection at {s})")
                break
        for r in [cpr.get("r1", 0), cpr.get("r2", 0), pivot]:
            if r > 0 and abs(current_price - r) <= (atr * 0.6) and current_price <= r and stoch_k > 75:
                total_score -= 25
                active_paths.append(f"Path C (CPR Resistance Rejection at {r})")
                break

    # PATH D: RSI Extreme Reversal
    if rsi < 15.0 and stoch_k < 15:
        total_score += 35
        active_paths.append("Path D (Extreme Oversold Reversal)")
    elif rsi > 85.0 and stoch_k > 85:
        total_score -= 35
        active_paths.append("Path D (Extreme Overbought Reversal)")

    # PATH E: Multi-Timeframe Golden Confluence
    if all_timeframes:
        t3m = all_timeframes.get('3m', {}).get('trend', 'FLAT')
        t5m = all_timeframes.get('5m', {}).get('trend', 'FLAT')
        t15m = all_timeframes.get('15m', {}).get('trend', 'FLAT')
        if include_sub_5m_confluence and (
            t3m == "BULLISH"
            and t5m == "BULLISH"
            and t15m == "BULLISH"
            and h1_trend == "BULLISH"
        ):
            total_score += 30
            active_paths.append("Path E (MTF Bullish Alignment)")
        elif include_sub_5m_confluence and (
            t3m == "BEARISH"
            and t5m == "BEARISH"
            and t15m == "BEARISH"
            and h1_trend == "BEARISH"
        ):
            total_score -= 30
            active_paths.append("Path E (MTF Bearish Alignment)")
        elif not include_sub_5m_confluence and (
            t5m == "BULLISH"
            and t15m == "BULLISH"
            and h1_trend == "BULLISH"
        ):
            total_score += 30
            active_paths.append("Path E (5m/15m/1h Bullish Alignment)")
        elif not include_sub_5m_confluence and (
            t5m == "BEARISH"
            and t15m == "BEARISH"
            and h1_trend == "BEARISH"
        ):
            total_score -= 30
            active_paths.append("Path E (5m/15m/1h Bearish Alignment)")

    # PATH F: Fair Value Gap (FVG) Retest
    if len(tf_data.get("candles", [])) >= 3:
        c_prev2 = tf_data["candles"][-3]
        c_prev1 = tf_data["candles"][-2]
        c_curr = tf_data["candles"][-1]
        
        if c_prev2['high'] < c_curr['low'] and c_prev1['close'] > c_prev1['open']:
            fvg_mid = (c_prev2['high'] + c_curr['low']) / 2.0
            if abs(current_price - fvg_mid) <= (atr * 0.4) and current_price >= fvg_mid:
                total_score += 25
                active_paths.append("Path F (Bullish FVG Retest)")
        elif c_prev2['low'] > c_curr['high'] and c_prev1['close'] < c_prev1['open']:
            fvg_mid = (c_prev2['low'] + c_curr['high']) / 2.0
            if abs(current_price - fvg_mid) <= (atr * 0.4) and current_price <= fvg_mid:
                total_score -= 25
                active_paths.append("Path F (Bearish FVG Retest)")

    # PATH G: Wick Rejection / Pinbar
    if len(tf_data.get("candles", [])) >= 1:
        latest_c = tf_data["candles"][-1]
        o, h, l, c = latest_c['open'], latest_c['high'], latest_c['low'], latest_c['close']
        body = abs(c - o)
        rng = h - l if h != l else 0.001
        lower_wick = min(o, c) - l
        upper_wick = h - max(o, c)
        
        if lower_wick >= (body * 2.5) and (lower_wick / rng) > 0.6:
            total_score += 20
            active_paths.append("Path G (Bullish Pinbar)")
        elif upper_wick >= (body * 2.5) and (upper_wick / rng) > 0.6:
            total_score -= 20
            active_paths.append("Path G (Bearish Pinbar)")

    # PATH H: Inside Bar Breakout
    if len(tf_data.get("candles", [])) >= 2:
        mother = tf_data["candles"][-2]
        inside = tf_data["candles"][-1]
        if inside['high'] <= mother['high'] and inside['low'] >= mother['low']:
            if current_price > mother['high'] and stoch_k > 50:
                total_score += 20
                active_paths.append("Path H (Inside Bar Bull Breakout)")
            elif current_price < mother['low'] and stoch_k < 50:
                total_score -= 20
                active_paths.append("Path H (Inside Bar Bear Breakdown)")

    audit = {
        "regime": "SQUEEZE / CONSOLIDATION" if is_squeeze else ("TRENDING" if adx > 22 else "RANGING"),
        "consensus_score": total_score,
        "active_paths": active_paths,
        "reasoning": f"Weighted Score: {total_score} | Active Signals: {', '.join(active_paths) if active_paths else 'None'}"
    }

    action = None
    if total_score >= 65:
        action = "BUY"
    elif total_score <= -65:
        action = "SELL"

    return action, audit

def check_and_auto_trigger_trade(agent_state, symbol: str = DEFAULT_SYMBOL):
    timeframes = agent_state.get("timeframes", {})
    tf_data = timeframes.get("5m", {})
    h1_data = timeframes.get("h1", {})
    market_price = agent_state.get("current_price", 0.0)

    if market_price <= 0 or not tf_data or not h1_data:
        agent_state["signal"] = "HOLD / SCANNING"
        agent_state["decision_audit"] = {
            "regime": "SCANNING",
            "consensus_score": 0,
            "active_paths": [],
            "reasoning": "Waiting for complete 5m and 1h market data."
        }
        return

    action, audit = evaluate_multi_path_strategy(
        tf_data,
        h1_data,
        market_price,
        agent_state.get("cpr", {}),
        timeframes,
    )
    agent_state["signal"] = action or "HOLD / SCANNING"
    agent_state["decision_audit"] = audit

    if not LIVE_TRADING_ENABLED or not AUTO_TRADING_ENABLED or agent_status != "RUNNING":
        return

    if not tf_data.get("data_ready") or not h1_data.get("data_ready"):
        agent_state["decision_audit"]["reasoning"] += (
            " | Auto trading waits for sufficient 5m and 1h history."
        )
        return

    mt5_account = agent_state.get("mt5_account", {})
    candles = tf_data.get("candles", [])
    if len(candles) < 2:
        agent_state["decision_audit"]["reasoning"] += (
            " | Auto trading waits for a completed 5m candle."
        )
        return

    closed_timeframe = dict(tf_data)
    closed_timeframe["candles"] = candles[:-1]
    history_fields = (
        "bb_upper", "bb_middle", "bb_lower", "ema20_history", "ema30_history",
        "ema100_history", "atr_history", "adx_history", "rsi_history",
        "stoch_k_history", "stoch_d_history",
    )
    for field in history_fields:
        values = tf_data.get(field)
        if not isinstance(values, list) or len(values) != len(candles):
            agent_state["decision_audit"]["reasoning"] += (
                " | Auto trading waits for complete closed-candle indicator history."
            )
            return
        closed_timeframe[field] = values[:-1]

    if not closed_timeframe["candles"]:
        agent_state["decision_audit"]["reasoning"] += (
            " | Auto trading waits for a completed 5m candle."
        )
        return

    closed_indicators = {
        "ema20": "ema20_history",
        "atr": "atr_history",
        "adx": "adx_history",
        "rsi": "rsi_history",
    }
    for field, history in closed_indicators.items():
        closed_timeframe[field] = closed_timeframe[history][-1]
    closed_timeframe["ema30"] = closed_timeframe["ema30_history"][-1]
    closed_timeframe["ema100"] = closed_timeframe["ema100_history"][-1]
    closed_timeframe["trend"] = (
        "BULLISH"
        if closed_timeframe["ema30"] > closed_timeframe["ema100"]
        else "BEARISH"
    )
    closed_price = float(closed_timeframe["candles"][-1]["close"])
    closed_candle_time = closed_timeframe["candles"][-1].get("time")
    if closed_candle_time is None:
        agent_state["decision_audit"]["reasoning"] += (
            " | Auto trading waits for the closed 5m candle timestamp."
        )
        return

    closed_candle_time = str(closed_candle_time)
    previous_candle_time = last_auto_evaluated_candle_times.get(symbol)
    if previous_candle_time is None:
        last_auto_evaluated_candle_times[symbol] = closed_candle_time
        agent_state["decision_audit"]["reasoning"] += (
            " | Auto trading armed; waiting for the next completed 5m candle."
        )
        return
    if closed_candle_time == previous_candle_time:
        return
    last_auto_evaluated_candle_times[symbol] = closed_candle_time

    decision_timeframes = dict(timeframes)
    decision_timeframes["5m"] = closed_timeframe
    action, audit = evaluate_multi_path_strategy(
        closed_timeframe,
        h1_data,
        closed_price,
        agent_state.get("cpr", {}),
        decision_timeframes,
    )
    agent_state["decision_audit"] = audit
    if not action:
        return

    atr = float(closed_timeframe["atr"])
    if not math.isfinite(atr) or atr <= 0:
        agent_state["decision_audit"]["reasoning"] += (
            " | Auto trade skipped: closed-candle ATR is unavailable."
        )
        return

    payload = {
        "symbol": symbol,
        "action": action,
        "risk_sized": True,
        "sl_distance": atr * 1.5,
        "tp_distance": atr * 3.0,
        "strategy_path": f"Confluence Consensus Score: {audit['consensus_score']}"
    }

    with auto_trade_lock:
        account_state, bridge_status = mt5_bridge.account_snapshot()
        if account_state is None:
            message = bridge_status.get("detail", "MT5 account state is unavailable.")
            agent_state["decision_audit"]["reasoning"] += f" | Auto trade blocked: {message}"
            return
        with mt5_state_lock:
            latest_mt5_account.update(account_state)
            latest_mt5_status.update(bridge_status)

        risk_allowed, risk_message = check_daily_loss_limit(account_state)
        if not risk_allowed:
            agent_state["decision_audit"]["reasoning"] += (
                f" | Auto trade blocked: {risk_message}"
            )
            return
        positions = account_state.get("positions", [])
        if len(positions) >= MAX_OPEN_POSITIONS:
            agent_state["decision_audit"]["reasoning"] += (
                f" | Auto trade blocked: portfolio limit is {MAX_OPEN_POSITIONS} open position."
            )
            return
        if not bridge_status.get("trade_allowed"):
            agent_state["decision_audit"]["reasoning"] += (
                " | Auto trade blocked: MT5 trading permission is unavailable."
            )
            return
        symbol_market = account_state.get("symbols", {}).get(symbol, {})
        if not symbol_market.get("available"):
            message = symbol_market.get("detail", f"MT5 symbol {symbol} is unavailable.")
            agent_state["decision_audit"]["reasoning"] += f" | Auto trade blocked: {message}"
            return

        succeeded, message, result = mt5_bridge.place_market_order(
            payload, MAX_RISK_PER_TRADE_PERCENT
        )
    agent_state["decision_audit"]["reasoning"] += (
        f" | MT5 {message}" if succeeded else f" | Order rejected: {message}"
    )
    if succeeded:
        print(
            f"[AUTONOMOUS CONSENSUS] {action} {result.get('volume')} lots "
            f"on {symbol}; order={result.get('order')}"
        )
    else:
        print(f"ERROR: MT5 rejected autonomous {action} on {symbol}: {message}")

def manage_active_trades(agent_state):
    if not LIVE_TRADING_ENABLED or not latest_mt5_status.get("trade_allowed"):
        return

    mt5_account = agent_state.get("mt5_account", {})
    positions = mt5_account.get("positions", [])
    if not positions:
        return

    import MetaTrader5 as mt5

    for pos in positions:
        symbol = pos.get("symbol")
        if symbol not in SUPPORTED_SYMBOLS or int(pos.get("magic", 0)) != MT5_MAGIC_NUMBER:
            continue

        symbol_state = agent_state.get("symbols", {}).get(symbol, {})
        tf_data = symbol_state.get("timeframes", {}).get("5m", {})
        atr = float(tf_data.get("atr", 0.0))
        ema20 = float(tf_data.get("ema20", 0.0))
        if not math.isfinite(atr) or atr <= 0:
            continue

        ticket = int(pos["ticket"])
        pos_type = pos["type"]
        entry_price = float(pos["price"])
        current_sl = float(pos["sl"])
        current_tp = float(pos["tp"])
        symbol_info_tick = getattr(mt5, "symbol_info_tick")
        tick = symbol_info_tick(symbol)
        if tick is None:
            continue

        market_price = float(tick.bid if pos_type == "BUY" else tick.ask)
        profit_points = (
            market_price - entry_price
            if pos_type == "BUY"
            else entry_price - market_price
        )
        new_sl = None
        new_tp = current_tp

        if profit_points >= atr * 3.0:
            new_sl = (
                float(tick.bid) - atr * 2.0
                if pos_type == "BUY"
                else float(tick.ask) + atr * 2.0
            )
            new_tp = 0.0

        if profit_points >= atr * 1.5 and new_sl is None:
            if pos_type == "BUY" and current_sl < entry_price:
                new_sl = entry_price + atr * 0.4
            elif pos_type == "SELL" and (
                current_sl > entry_price or current_sl == 0.0
            ):
                new_sl = entry_price - atr * 0.4
        elif profit_points >= atr * 2.0 and new_sl is None:
            if pos_type == "BUY":
                candidate_sl = float(tick.bid) - atr * 2.0
                if candidate_sl > current_sl + atr * 0.2:
                    new_sl = candidate_sl
            else:
                candidate_sl = float(tick.ask) + atr * 2.0
                if current_sl == 0.0 or candidate_sl < current_sl - atr * 0.2:
                    new_sl = candidate_sl

        if new_sl is not None:
            succeeded, message, _ = mt5_bridge.modify_position(
                ticket, new_sl, new_tp
            )
            if not succeeded:
                print(f"ERROR: MT5 could not modify position {ticket}: {message}")
            continue

        opened_at = datetime.fromtimestamp(
            int(pos["time"]), tz=datetime.now().astimezone().tzinfo
        )
        age_seconds = (datetime.now().astimezone() - opened_at).total_seconds()
        if age_seconds < 120 or float(pos["profit"]) >= 0 or ema20 <= 0:
            continue

        current_tick = float(tick.bid if pos_type == "BUY" else tick.ask)
        breached_structure = (
            current_tick < ema20 - atr * 0.8
            if pos_type == "BUY"
            else current_tick > ema20 + atr * 0.8
        )
        if breached_structure:
            succeeded, message, _ = mt5_bridge.close_position(ticket)
            if not succeeded:
                print(f"ERROR: MT5 could not close position {ticket}: {message}")
            else:
                print(f"[EARLY EXIT] Closed losing MT5 position {ticket}.")

def refresh_mt5_state():
    global latest_mt5_account, latest_mt5_status
    account, status = mt5_bridge.account_snapshot()
    with mt5_state_lock:
        latest_mt5_account = account or {}
        latest_mt5_status = status

def mt5_monitor_loop():
    history_loaded = False
    last_history_attempt = 0.0
    while True:
        refresh_mt5_state()
        now = time.monotonic()
        if not history_loaded and now - last_history_attempt >= 60:
            history_loaded = poll_mt5_market_data(include_history=True)
            last_history_attempt = now
        else:
            poll_mt5_market_data()
        time.sleep(1)


def apply_mt5_tick(symbol: str, price: float, tick_epoch: int):
    global current_price
    if symbol not in symbol_tf_caches or price <= 0:
        return

    with state_lock:
        current_prices[symbol] = price
        if symbol == DEFAULT_SYMBOL:
            current_price = price
        caches = symbol_tf_caches[symbol]
        for timeframe, granularity in TF_GRANULARITY.items():
            df = caches.get(timeframe)
            if df is None or df.empty:
                continue

            bucket_epoch = (tick_epoch // granularity) * granularity
            bucket_time = datetime.fromtimestamp(bucket_epoch, timezone.utc)
            last_time = df.index[-1]
            if bucket_time > last_time:
                new_row = pd.DataFrame(
                    [{
                        "time": bucket_epoch,
                        "open": price,
                        "high": price,
                        "low": price,
                        "close": price,
                    }],
                    index=pd.DatetimeIndex([bucket_time]),
                )
                caches[timeframe] = pd.concat([df, new_row]).iloc[-300:]
            elif bucket_time == last_time:
                df.at[last_time, "close"] = price
                df.at[last_time, "high"] = max(float(df.at[last_time, "high"]), price)
                df.at[last_time, "low"] = min(float(df.at[last_time, "low"]), price)


def poll_mt5_market_data(include_history: bool = False) -> bool:
    snapshot, status = mt5_bridge.market_data_snapshot(
        MT5_TIMEFRAMES,
        history_count=300,
        include_history=include_history,
    )
    if snapshot is None:
        print(f"MT5 market data unavailable: {status.get('detail', 'Connection failed.')}")
        return False

    history_loaded = True
    for symbol in SUPPORTED_SYMBOLS:
        symbol_data = snapshot.get(symbol, {})
        quote = symbol_data.get("quote")
        if quote is not None:
            apply_mt5_tick(symbol, float(quote["bid"]), int(quote["time"]))

        if include_history:
            history = symbol_data.get("history", {})
            for timeframe in MT5_TIMEFRAMES:
                rates = history.get(timeframe)
                if rates is None or len(rates) == 0:
                    history_loaded = False
                    print(
                        f"MT5 history unavailable for {symbol} {timeframe}; "
                        "check the terminal's symbol selection and chart history."
                    )
                    continue
                frame = pd.DataFrame(rates)
                frame["time"] = frame["time"].astype("int64")
                frame.index = pd.to_datetime(frame["time"], unit="s", utc=True)
                frame = frame[["time", "open", "high", "low", "close"]]
                frame = frame[~frame.index.duplicated(keep="last")].sort_index()
                with state_lock:
                    symbol_tf_caches[symbol][timeframe] = frame.iloc[-300:]

    broadcast_state_update()
    return history_loaded


def broadcast_state_update():
    """Build the latest engine snapshot and publish it to connected clients."""
    global latest_state_payload
    with state_lock:
        with mt5_state_lock:
            mt5_account_data = dict(latest_mt5_account)
            mt5_status = dict(latest_mt5_status)

        symbol_states = {}
        for symbol, caches in symbol_tf_caches.items():
            timeframes_dict = {}
            for timeframe, df in caches.items():
                timeframes_dict[timeframe] = process_dataframe(df, symbol)
            daily_df = caches.get("d1")
            symbol_states[symbol] = {
                "symbol": symbol,
                "market_data_symbol": symbol,
                "market_data_source": "MT5",
                "current_price": current_prices[symbol],
                "signal": "HOLD / SCANNING",
                "cpr": calculate_cpr(daily_df),
                "timeframes": timeframes_dict,
                "decision_audit": {
                    "regime": "SCANNING",
                    "consensus_score": 0,
                    "active_paths": [],
                    "reasoning": "Waiting for market data."
                },
                "mt5_symbol": mt5_account_data.get("symbols", {}).get(symbol, {}),
            }

        state_data = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "agent_status": agent_status,
            "default_symbol": DEFAULT_SYMBOL,
            "supported_symbols": list(SUPPORTED_SYMBOLS),
            "market_data_source": "MT5",
            "symbols": symbol_states,
            "mt5_account": mt5_account_data,
            "mt5_bridge": mt5_status,
            "live_trading_enabled": LIVE_TRADING_ENABLED,
            "auto_trading_enabled": AUTO_TRADING_ENABLED,
            "max_risk_per_trade_percent": MAX_RISK_PER_TRADE_PERCENT,
            "max_open_positions": MAX_OPEN_POSITIONS,
        }

        manage_active_trades(state_data)
        for symbol, symbol_state in symbol_states.items():
            symbol_state["mt5_account"] = mt5_account_data
            check_and_auto_trigger_trade(symbol_state, symbol)

        default_state = symbol_states[DEFAULT_SYMBOL]
        for field in ("current_price", "signal", "cpr", "timeframes", "decision_audit"):
            state_data[field] = default_state[field]

        payload_str = json.dumps(state_data)
        latest_state_payload = payload_str
        
        # Thread-safe client snapshot copy
        clients_snapshot = list(connected_ui_clients)

    disconnected = set()
    for client in clients_snapshot:
        try:
            client.send(payload_str)
        except Exception:
            disconnected.add(client)
            
    if disconnected:
        with state_lock:
            connected_ui_clients.difference_update(disconnected)

class CustomHTTPRequestHandler(SimpleHTTPRequestHandler):
    def _send_json(self, status_code, response):
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(response).encode('utf-8'))

    def _is_local_dashboard_request(self):
        allowed_hosts = {
            f'localhost:{DASHBOARD_PORT}',
            f'127.0.0.1:{DASHBOARD_PORT}'
        }
        host = self.headers.get('Host', '').lower()
        origin = self.headers.get('Origin')
        return (
            host in allowed_hosts
            and (origin is None or urlsplit(origin).netloc.lower() in allowed_hosts)
        )

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/':
            self.path = '/index.html'
        elif path == '/tracker':
            self.path = '/tracker.html'
        elif path == '/tradelog':
            self.path = '/tradelog.html'
        elif path == '/agent_state.json':
            payload = latest_state_payload
            if payload is None:
                empty_audit = {
                    "regime": "SCANNING",
                    "consensus_score": 0,
                    "active_paths": [],
                    "reasoning": "Waiting for market data."
                }
                empty_symbols = {
                    symbol: {
                        "symbol": symbol,
                        "market_data_symbol": symbol,
                        "market_data_source": "MT5",
                        "current_price": 0.0,
                        "signal": "HOLD / SCANNING",
                        "cpr": {},
                        "timeframes": {},
                        "decision_audit": empty_audit,
                        "mt5_symbol": {},
                    }
                    for symbol in SUPPORTED_SYMBOLS
                }
                payload = json.dumps({
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "agent_status": agent_status,
                    "default_symbol": DEFAULT_SYMBOL,
                    "supported_symbols": list(SUPPORTED_SYMBOLS),
                    "market_data_source": "MT5",
                    "symbols": empty_symbols,
                    "current_price": 0.0,
                    "signal": current_signal,
                    "mt5_account": {},
                    "mt5_bridge": latest_mt5_status,
                    "timeframes": {},
                    "cpr": {},
                    "decision_audit": empty_audit,
                    "live_trading_enabled": LIVE_TRADING_ENABLED,
                    "auto_trading_enabled": AUTO_TRADING_ENABLED,
                    "max_risk_per_trade_percent": MAX_RISK_PER_TRADE_PERCENT,
                    "max_open_positions": MAX_OPEN_POSITIONS,
                })
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(payload.encode('utf-8'))
            return
        elif path == '/api/tradelog-data':
            trades = mt5_bridge.trade_history()
            if trades is None:
                status = mt5_bridge.status()
                self._send_json(503, {
                    "status": "UNAVAILABLE",
                    "message": status["detail"]
                })
                return
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(json.dumps(trades).encode('utf-8'))
            return
        elif path not in ('/index.html', '/tracker.html', '/tradelog.html'):
            self.send_error(404)
            return
        return SimpleHTTPRequestHandler.do_GET(self)

    def do_POST(self):
        global agent_status, latest_mt5_account, latest_mt5_status
        if not self._is_local_dashboard_request():
            self.send_error(403, "Requests must originate from the local dashboard.")
            return

        if self.path == '/api/control':
            content_length = int(self.headers['Content-Length'])
            data = json.loads(self.rfile.read(content_length).decode('utf-8'))
            action = data.get('action')
            if action == 'START':
                agent_status = "RUNNING" if LIVE_TRADING_ENABLED else "DATA ONLY"
            elif action == 'PAUSED': agent_status = "PAUSED"
            elif action == 'STOP': agent_status = "STOPPED"
            
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "agent_status": agent_status}).encode('utf-8'))

        elif self.path == '/api/execute-trade':
            if not LIVE_TRADING_ENABLED:
                self._send_json(403, {
                    "status": "REJECTED",
                    "message": "Live trading is disabled. Restart with --enable-live-trading to enable order dispatch."
                })
                return

            if agent_status != "RUNNING":
                self._send_json(409, {
                    "status": "REJECTED",
                    "message": "Manual order dispatch is not armed. Restart using the manual live-trading launcher."
                })
                return

            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            try:
                payload = json.loads(post_data.decode('utf-8'))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(400, {
                    "status": "REJECTED",
                    "message": "Trade request body must be valid JSON."
                })
                return

            if not isinstance(payload, dict):
                self._send_json(400, {
                    "status": "REJECTED",
                    "message": "Trade request body must be a JSON object."
                })
                return

            risk_percent, risk_error = validate_requested_risk_percent(payload)
            if risk_error or risk_percent is None:
                self._send_json(403, {"status": "REJECTED", "message": risk_error})
                return

            account_state, bridge_status = mt5_bridge.account_snapshot()
            with mt5_state_lock:
                latest_mt5_account = account_state or {}
                latest_mt5_status = bridge_status
            if account_state is None:
                self._send_json(503, {
                    "status": "REJECTED",
                    "message": bridge_status.get("detail", "MT5 account state is unavailable.")
                })
                return
            risk_allowed, risk_message = check_daily_loss_limit(account_state)
            if not risk_allowed:
                self._send_json(503, {
                    "status": "REJECTED",
                    "message": f"Live order blocked: {risk_message}"
                })
                return

            try:
                open_positions = int(account_state["open_positions"])
                positions = account_state["positions"]
            except (KeyError, TypeError, ValueError):
                self._send_json(503, {
                    "status": "REJECTED",
                    "message": "MT5 account state must include valid open_positions and positions fields."
                })
                return

            if open_positions < 0 or not isinstance(positions, list):
                self._send_json(503, {
                    "status": "REJECTED",
                    "message": "MT5 position metrics are invalid."
                })
                return

            if open_positions > 0 or positions:
                self._send_json(409, {
                    "status": "REJECTED",
                    "message": "An MT5 position is already open."
                })
                return

            if not bridge_status.get("trade_allowed"):
                self._send_json(503, {
                    "status": "REJECTED",
                    "message": bridge_status.get("detail", "MT5 trading is unavailable.")
                })
                return

            payload["symbol"] = payload.get("symbol") or MT5_SYMBOL
            payload["risk_pct"] = risk_percent
            succeeded, message, result = mt5_bridge.place_market_order(
                payload, risk_percent * 100.0
            )
            if not succeeded:
                self._send_json(400, {
                    "status": "REJECTED",
                    "message": message,
                    "mt5_result": result
                })
                return

            self._send_json(200, {
                "status": "SUCCESS",
                "message": message,
                "mt5_result": result
            })
        else:
            self.send_error(404)

def run_websocket_server():
    """Runs the standalone WS server for real-time UI streaming."""
    def handler(websocket_conn):
        connected_ui_clients.add(websocket_conn)
        try:
            for message in websocket_conn:
                pass
        except:
            pass
        finally:
            connected_ui_clients.remove(websocket_conn)

    try:
        with serve(
            handler,
            "127.0.0.1",
            WEBSOCKET_PORT,
            origins=[
                re.compile(
                    rf"^http://(?:localhost|127\.0\.0\.1):{DASHBOARD_PORT}$"
                )
            ]
        ) as server:
            server.serve_forever()
    except Exception as e:
        print(f"WS Server note: {e}")

def main():
    global LIVE_TRADING_ENABLED, AUTO_TRADING_ENABLED, agent_status
    parser = argparse.ArgumentParser(description="Run the local MT5 dashboard and market-data engine.")
    parser.add_argument(
        "--enable-live-trading",
        action="store_true",
        help="Allow manually confirmed orders from the dashboard to be sent to MT5."
    )
    parser.add_argument(
        "--enable-auto-trading",
        action="store_true",
        help="Allow strategy-generated orders; requires --enable-live-trading."
    )
    args = parser.parse_args()
    LIVE_TRADING_ENABLED = args.enable_live_trading
    AUTO_TRADING_ENABLED = args.enable_auto_trading

    if AUTO_TRADING_ENABLED and not LIVE_TRADING_ENABLED:
        parser.error("--enable-auto-trading requires --enable-live-trading.")

    if LIVE_TRADING_ENABLED:
        if not mt5_bridge.connect():
            parser.error(f"Cannot enable live trading: {mt5_bridge.status()['detail']}")
        agent_status = "RUNNING"
        print(
            "MANUALLY CONFIRMED LIVE TRADING ENABLED for this run. Dashboard "
            "Buy/Sell orders require a volume entry and browser confirmation."
        )
        if AUTO_TRADING_ENABLED:
            print("AUTOMATED STRATEGY ORDERS ARE ALSO ENABLED.")
    else:
        agent_status = "DATA ONLY"
        print(
            "Live order execution is disabled. MT5 market data and account "
            "information are read-only."
        )

    print("Initializing initial agent state cache...")
    
    refresh_mt5_state()
    threading.Thread(target=mt5_monitor_loop, daemon=True).start()
    threading.Thread(target=run_websocket_server, daemon=True).start()
    
    server_address = ('127.0.0.1', DASHBOARD_PORT)
    print(
        f"Serving HTTP Dashboard on http://localhost:{DASHBOARD_PORT} "
        f"with WebSocket push streaming on port {WEBSOCKET_PORT}..."
    )
    request_handler = functools.partial(CustomHTTPRequestHandler, directory=PROJECT_DIR)
    try:
        ThreadingHTTPServer(server_address, request_handler).serve_forever()
    finally:
        mt5_bridge.disconnect()

if __name__ == '__main__':
    main()