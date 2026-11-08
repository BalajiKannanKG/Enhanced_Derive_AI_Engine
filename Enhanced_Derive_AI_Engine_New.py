# Enhanced_Derive_AI_Engine.py - Complete Unabridged Autonomous Quantitative Trading Engine (Institutional Upgraded Edition)
import argparse
import functools
import json
import math
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast
from urllib.parse import urlsplit

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
DB_PATH = os.path.join(PROJECT_DIR, "trade_telemetry.sqlite")
CSV_LOG_PATH = os.path.join(PROJECT_DIR, "multi_timeframe_trade_log.csv")

# --- PROFESSIONAL TRADING STYLE PROFILES ---
TRADING_PROFILES = {
    "SCALPING": {
        "primary_tf": "3m",
        "confluence_tf": "5m",
        "sl_atr_mult": 1.0,
        "tp_atr_mult": 1.5,
        "stagnation_seconds": 90,
        "min_consensus_score": 45,
        "min_adx": 22.0,
        "partial_scale_out_atr": 1.5,
        "break_even_trigger_r": 1.0
    },
    "INTRADAY": {
        "primary_tf": "5m",
        "confluence_tf": "h1",
        "sl_atr_mult": 1.5,
        "tp_atr_mult": 3.0,
        "stagnation_seconds": 180,
        "min_consensus_score": 55,
        "min_adx": 25.0,
        "partial_scale_out_atr": 2.0,
        "break_even_trigger_r": 1.0
    },
    "SWING": {
        "primary_tf": "h1",
        "confluence_tf": "d1",
        "sl_atr_mult": 2.5,
        "tp_atr_mult": 6.0,
        "stagnation_seconds": 3600,
        "min_consensus_score": 65,
        "min_adx": 28.0,
        "partial_scale_out_atr": 3.0,
        "break_even_trigger_r": 1.2
    }
}

ACTIVE_TRADING_STYLE = os.environ.get("TRADING_STYLE", "INTRADAY").upper()
if ACTIVE_TRADING_STYLE not in TRADING_PROFILES:
    ACTIVE_TRADING_STYLE = "INTRADAY"
CURRENT_PROFILE = TRADING_PROFILES[ACTIVE_TRADING_STYLE]

def init_telemetry_sink():
    """Initializes comprehensive SQLite telemetry table including trading style metrics."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS trade_telemetry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date_str TEXT,
            time_opened TEXT,
            close_time TEXT,
            ticket INTEGER,
            symbol TEXT,
            trading_style TEXT,
            timeframe TEXT,
            action TEXT,
            regime TEXT,
            consensus_score INTEGER,
            strategies_utilized TEXT,
            evolving_modifications TEXT,
            entry_price REAL,
            margin REAL,
            volume REAL,
            atr REAL,
            sl_price REAL,
            tp_price REAL,
            partial_closures TEXT,
            exit_reason TEXT
        )
    """)
    conn.commit()
    conn.close()

    if not os.path.exists(CSV_LOG_PATH):
        df = pd.DataFrame(columns=[
            "Date", "TimeOpened", "CloseTime", "Ticket", "Symbol", "TradingStyle", "Timeframe", 
            "Action", "Regime", "ConsensusScore", "StrategiesUtilized", 
            "EvolvingModifications", "EntryPrice", "Margin", "Volume", 
            "ATR", "SL", "TP", "PartialClosures", "Status"
        ])
        df.to_csv(CSV_LOG_PATH, index=False)

init_telemetry_sink()

def log_trade_event(
    ticket, symbol, trading_style, timeframe, action, regime, score, strategies, 
    modifications, entry_price, margin, volume, atr, sl, tp, 
    partial_closures=None, close_time="", status="OPEN"
):
    """Records comprehensive multi-style trade telemetry snapshot to SQLite and CSV sink."""
    now_utc = datetime.now(timezone.utc)
    date_str = now_utc.strftime("%Y-%m-%d")
    time_opened = now_utc.strftime("%H:%M:%S")
    strategies_str = ", ".join(strategies) if isinstance(strategies, list) else str(strategies)
    mods_str = ", ".join(modifications) if isinstance(modifications, list) else str(modifications)
    partials_str = json.dumps(partial_closures) if partial_closures else "None"
    
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO trade_telemetry 
            (date_str, time_opened, close_time, ticket, symbol, trading_style, timeframe, action, regime, 
             consensus_score, strategies_utilized, evolving_modifications, entry_price, 
             margin, volume, atr, sl_price, tp_price, partial_closures, exit_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            date_str, time_opened, close_time, ticket, symbol, trading_style, timeframe, action, regime, 
            score, strategies_str, mods_str, entry_price, margin, volume, atr, sl, tp, 
            partials_str, status
        ))
        conn.commit()
        conn.close()

        csv_row = [
            date_str, time_opened, close_time, ticket, symbol, trading_style, timeframe, action, regime, 
            score, strategies_str, mods_str, entry_price, margin, volume, atr, sl, tp, 
            partials_str, status
        ]
        pd.DataFrame([csv_row]).to_csv(CSV_LOG_PATH, mode='a', header=False, index=False)
    except Exception as error:
        print(f"ERROR logging trade telemetry: {error}")

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
scaled_out_positions = set()
active_trade_evolution_logs: dict[int, list] = {}

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
        if not math.isfinite(risk_fraction) or risk_fraction <= 0 or risk_fraction > 100:
            return None, "risk_pct must be between 0 and 100."
        if risk_fraction > MAX_RISK_PER_TRADE_PERCENT / 100.0:
            return None, f"Requested risk percent exceeds configured limit of {MAX_RISK_PER_TRADE_PERCENT:.2f}%."
        return risk_fraction, ""
    except (ValueError, TypeError):
        return None, "risk_pct must be a number."

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

def validate_trade_quality(adx_val: float, consensus_score: int) -> tuple[bool, str]:
    """Validates trade quality via ADX trend strength and minimum consensus score."""
    min_adx = CURRENT_PROFILE.get("min_adx", 25.0)
    min_score = CURRENT_PROFILE.get("min_consensus_score", 55)

    if adx_val < min_adx:
        return False, f"Trade Rejected: ADX ({adx_val:.1f}) is below the required trend strength threshold ({min_adx})."
    if abs(consensus_score) < min_score:
        return False, f"Trade Rejected: Consensus score ({consensus_score}) is below required profile threshold ({min_score})."
    return True, "Quality filters passed."

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
                o = float(row["open"]) if float(row["open"]) > 0 else p
                c = float(row["close"]) if float(row["close"]) > 0 else p
                h = max(float(row["high"]), o, c) if float(row["high"]) > 0 else max(o, c)
                l = min(float(row["low"]), o, c) if float(row["low"]) > 0 else min(o, c)
                candles.append({
                    "time": candle_time,
                    "open": round(o, 3),
                    "high": round(h, 3),
                    "low": round(l, 3),
                    "close": round(c, 3),
                    "pattern": "Normal",
                })
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
    
    df['open'] = df['open'].where(df['open'] > 0, df['close'])
    df['close'] = df['close'].where(df['close'] > 0, df['open'])
    df['high'] = df[['high', 'open', 'close']].max(axis=1).where(lambda x: x > 0, df['close'])
    df['low'] = df[['low', 'open', 'close']].min(axis=1).where(lambda x: x > 0, df['close'])

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

def get_continuous_session_multiplier() -> float:
    """Computes a continuous session volatility weighting curve based on UTC hour for synthetic indices."""
    utc_now = datetime.now(timezone.utc)
    hour = utc_now.hour + utc_now.minute / 60.0
    if 7.0 <= hour <= 17.0:
        return 1.20
    elif 5.0 <= hour < 7.0 or 17.0 < hour <= 20.0:
        return 1.10
    elif 20.0 < hour <= 23.0 or 2.0 <= hour < 5.0:
        return 0.95
    else:
        return 0.85

def evaluate_multi_path_strategy(
    tf_data,
    higher_tf_data,
    current_price,
    cpr,
    all_timeframes,
    profile=CURRENT_PROFILE,
    include_sub_5m_confluence=True,
):
    """Institutional-Grade Multi-Path Strategy Evaluator incorporating Regime Gating, Continuous Session Volatility Curves, and Momentum Velocity Skew."""
    atr = tf_data.get("atr", 1.0)
    adx = tf_data.get("adx", 20.0)
    rsi = tf_data.get("rsi", 50.0)
    stoch_k = tf_data.get("stoch_k_history", [50])[-1]
    ema20 = tf_data.get("ema20", current_price)
    
    bb_upper = tf_data.get("bb_upper", [current_price + 10])[-1]
    bb_lower = tf_data.get("bb_lower", [current_price - 10])[-1]
    bb_width = bb_upper - bb_lower
    is_squeeze = bb_width <= (atr * 4.0)
    higher_trend = higher_tf_data.get("trend", "FLAT")

    total_score = 0
    active_paths = []

    session_weight = get_continuous_session_multiplier()

    is_trending_regime = adx >= 22.0 and not is_squeeze
    is_ranging_regime = adx < 20.0 or is_squeeze

    if ACTIVE_TRADING_STYLE == "SCALPING":
        regime = "SCALPING_MOMENTUM_REVERSAL"
        if stoch_k < 20 and rsi < 40:
            total_score += 45
            active_paths.append("Scalp Oversold Bounce")
        elif stoch_k > 80 and rsi > 60:
            total_score -= 45
            active_paths.append("Scalp Overbought Rejection")
        if len(tf_data.get("candles", [])) >= 1:
            latest_c = tf_data["candles"][-1]
            if latest_c['close'] > latest_c['open'] and rsi < 55:
                total_score += 25
                active_paths.append("Scalp Bullish Micro-Impulse")
            elif latest_c['close'] < latest_c['open'] and rsi > 45:
                total_score -= 25
                active_paths.append("Scalp Bearish Micro-Impulse")

    elif ACTIVE_TRADING_STYLE == "SWING":
        regime = "SWING_MACRO_TREND_FOLLOWING"
        if is_trending_regime:
            if higher_trend == "BULLISH" and current_price > ema20:
                total_score += 55
                active_paths.append("Swing Higher-TF Bullish Trend Alignment")
            elif higher_trend == "BEARISH" and current_price < ema20:
                total_score -= 55
                active_paths.append("Swing Higher-TF Bearish Trend Alignment")
        if cpr:
            pivot = cpr.get("pivot", current_price)
            if current_price > pivot and higher_trend == "BULLISH":
                total_score += 30
                active_paths.append("Swing Above Daily CPR Pivot")
            elif current_price < pivot and higher_trend == "BEARISH":
                total_score -= 30
                active_paths.append("Swing Below Daily CPR Pivot")

    else:
        if is_trending_regime:
            regime = "STRATEGY_1_MTF_TREND_MOMENTUM"
            if higher_trend == "BULLISH" and current_price > ema20 and 45 < rsi < 65 and stoch_k < 30:
                total_score += 40
                active_paths.append("Path A (Bull Trend Pullback)")
            elif higher_trend == "BEARISH" and current_price < ema20 and 35 < rsi < 55 and stoch_k > 70:
                total_score -= 40
                active_paths.append("Path A (Bear Trend Pullback)")

            if all_timeframes:
                t3m = all_timeframes.get('3m', {}).get('trend', 'FLAT')
                t5m = all_timeframes.get('5m', {}).get('trend', 'FLAT')
                t15m = all_timeframes.get('15m', {}).get('trend', 'FLAT')
                if include_sub_5m_confluence and (t3m == "BULLISH" and t5m == "BULLISH" and t15m == "BULLISH" and higher_trend == "BULLISH"):
                    total_score += 35
                    active_paths.append("Path E (MTF Bullish Alignment)")
                elif include_sub_5m_confluence and (t3m == "BEARISH" and t5m == "BEARISH" and t15m == "BEARISH" and higher_trend == "BEARISH"):
                    total_score -= 35
                    active_paths.append("Path E (MTF Bearish Alignment)")

        elif not is_squeeze:
            regime = "STRATEGY_2_QUALITY_BREAKOUT"
            if current_price > bb_upper and stoch_k > 60 and rsi > 55:
                total_score += 45
                active_paths.append("Path B (Quality Bull Band Breakout)")
            elif current_price < bb_lower and stoch_k < 40 and rsi < 45:
                total_score -= 45
                active_paths.append("Path B (Quality Bear Band Breakdown)")

            if len(tf_data.get("candles", [])) >= 2:
                mother = tf_data["candles"][-2]
                inside = tf_data["candles"][-1]
                if inside['high'] <= mother['high'] and inside['low'] >= mother['low']:
                    if current_price > mother['high'] and stoch_k > 50:
                        total_score += 25
                        active_paths.append("Path H (Inside Bar Bull Breakout)")
                    elif current_price < mother['low'] and stoch_k < 50:
                        total_score -= 25
                        active_paths.append("Path H (Inside Bar Bear Breakdown)")
        else:
            regime = "STRATEGY_3_CPR_REVERSAL_REJECTION"
            if cpr:
                pivot = cpr.get("pivot", current_price)
                for s in [cpr.get("s1", 0), cpr.get("s2", 0), pivot]:
                    if s > 0 and abs(current_price - s) <= (atr * 0.6) and current_price >= s and stoch_k < 25:
                        total_score += 35
                        active_paths.append(f"Path C (CPR Support Rejection at {s})")
                        break
                for r in [cpr.get("r1", 0), cpr.get("r2", 0), pivot]:
                    if r > 0 and abs(current_price - r) <= (atr * 0.6) and current_price <= r and stoch_k > 75:
                        total_score -= 35
                        active_paths.append(f"Path C (CPR Resistance Rejection at {r})")
                        break

            if rsi < 15.0 and stoch_k < 15:
                total_score += 35
                active_paths.append("Path D (Extreme Oversold Reversal)")
            elif rsi > 85.0 and stoch_k > 85:
                total_score -= 35
                active_paths.append("Path D (Extreme Overbought Reversal)")

            if len(tf_data.get("candles", [])) >= 1:
                latest_c = tf_data["candles"][-1]
                o, h, l, c = latest_c['open'], latest_c['high'], latest_c['low'], latest_c['close']
                body = abs(c - o)
                rng = h - l if h != l else 0.001
                lower_wick = min(o, c) - l
                upper_wick = h - max(o, c)
                if lower_wick >= (body * 2.5) and (lower_wick / rng) > 0.6:
                    total_score += 25
                    active_paths.append("Path G (Bullish Pinbar)")
                elif upper_wick >= (body * 2.5) and (upper_wick / rng) > 0.6:
                    total_score -= 25
                    active_paths.append("Path G (Bearish Pinbar)")

    if len(tf_data.get("candles", [])) >= 3:
        c_prev2 = tf_data["candles"][-3]
        c_prev1 = tf_data["candles"][-2]
        c_curr = tf_data["candles"][-1]
        if c_prev2['high'] < c_curr['low'] and c_prev1['close'] > c_prev1['open']:
            fvg_mid = (c_prev2['high'] + c_curr['low']) / 2.0
            if abs(current_price - fvg_mid) <= (atr * 0.4) and current_price >= fvg_mid:
                total_score += 20
                active_paths.append("Path F (Bullish FVG Retest)")
        elif c_prev2['low'] > c_curr['high'] and c_prev1['close'] < c_prev1['open']:
            fvg_mid = (c_prev2['low'] + c_curr['high']) / 2.0
            if abs(current_price - fvg_mid) <= (atr * 0.4) and current_price <= fvg_mid:
                total_score -= 20
                active_paths.append("Path F (Bearish FVG Retest)")

    if all_timeframes and '15m' in all_timeframes:
        tf_15m = all_timeframes['15m']
        candles_15m = tf_15m.get("candles", [])
        if len(candles_15m) >= 2:
            session_open_candle = candles_15m[0]
            latest_15m = candles_15m[-1]
            open_price = session_open_candle['open']
            open_high = session_open_candle['high']
            open_low = session_open_candle['low']
            close_15m = latest_15m['close']
            
            if close_15m > open_price and current_price >= open_price:
                total_score += 30
                active_paths.append("Path I (Market Open 15m Bullish Bias)")
            elif close_15m < open_price and current_price <= open_price:
                total_score -= 30
                active_paths.append("Path I (Market Open 15m Bearish Bias)")

            if current_price > open_high and stoch_k > 85 and rsi > 75:
                total_score -= 35
                active_paths.append("Path I (Opening Range Bullish Trap Fade)")
            elif current_price < open_low and stoch_k < 15 and rsi < 25:
                total_score += 35
                active_paths.append("Path I (Opening Range Bearish Trap Fade)")

    keltner_upper = ema20 + (atr * 2.0)
    keltner_lower = ema20 - (atr * 2.0)
    if current_price > keltner_upper and stoch_k > 65 and adx > 25:
        total_score += 30
        active_paths.append("Path J (Keltner Upper Channel Breakout)")
    elif current_price < keltner_lower and stoch_k < 35 and adx > 25:
        total_score -= 30
        active_paths.append("Path J (Keltner Lower Channel Breakdown)")

    ema30 = tf_data.get("ema30", current_price)
    ema100 = tf_data.get("ema100", current_price)
    if is_trending_regime:
        if ema20 > ema30 > ema100 and current_price > ema20 and adx > 28:
            total_score += 35
            active_paths.append("Path K (Bullish EMA Ribbon Velocity Expansion)")
        elif ema20 < ema30 < ema100 and current_price < ema20 and adx > 28:
            total_score -= 35
            active_paths.append("Path K (Bearish EMA Ribbon Velocity Expansion)")

    stoch_d = tf_data.get("stoch_d_history", [50])[-1]
    if stoch_k < 15 and stoch_d < 15 and rsi < 30:
        total_score += 30
        active_paths.append("Path L (Stochastics Double-Bottom Oversold Reversal)")
    elif stoch_k > 85 and stoch_d > 85 and rsi > 70:
        total_score -= 30
        active_paths.append("Path L (Stochastics Double-Top Overbought Rejection)")

    total_score = int(round(total_score * session_weight))

    min_score = profile["min_consensus_score"]
    audit = {
        "regime": regime,
        "consensus_score": total_score,
        "active_paths": active_paths,
        "reasoning": f"Style: {ACTIVE_TRADING_STYLE} | Regime: {regime} | SessionWt: {session_weight:.2f} | Score: {total_score} (Threshold: ±{min_score})"
    }

    action = None
    if total_score >= min_score:
        action = "BUY"
    elif total_score <= -min_score:
        action = "SELL"

    return action, audit

def check_and_auto_trigger_trade(agent_state, symbol: str = DEFAULT_SYMBOL):
    profile = CURRENT_PROFILE
    primary_tf = profile["primary_tf"]
    confluence_tf = profile["confluence_tf"]

    timeframes = agent_state.get("timeframes", {})
    tf_data = timeframes.get(primary_tf, {})
    higher_tf_data = timeframes.get(confluence_tf, {})
    market_price = agent_state.get("current_price", 0.0)

    if market_price <= 0 or not tf_data or not higher_tf_data:
        agent_state["signal"] = "HOLD / SCANNING"
        agent_state["decision_audit"] = {
            "regime": "SCANNING",
            "consensus_score": 0,
            "active_paths": [],
            "reasoning": f"Waiting for {primary_tf} and {confluence_tf} market data."
        }
        return

    action, audit = evaluate_multi_path_strategy(
        tf_data,
        higher_tf_data,
        market_price,
        agent_state.get("cpr", {}),
        timeframes,
    )
    agent_state["signal"] = action or "HOLD / SCANNING"
    agent_state["decision_audit"] = audit

    if not LIVE_TRADING_ENABLED or not AUTO_TRADING_ENABLED or agent_status != "RUNNING":
        return

    if not tf_data.get("data_ready") or not higher_tf_data.get("data_ready"):
        return

    candles = tf_data.get("candles", [])
    if len(candles) < 2:
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
            return
        closed_timeframe[field] = values[:-1]

    closed_indicators = {
        "ema20": "ema20_history",
        "atr": "atr_history",
        "adx": "adx_history",
        "rsi": "rsi_history",
    }
    for field, history in closed_indicators.items():
        closed_timeframe[field] = closed_timeframe[history][-1]
    closed_price = float(closed_timeframe["candles"][-1]["close"])
    closed_candle_time = closed_timeframe["candles"][-1].get("time")
    if closed_candle_time is None:
        return

    closed_candle_time = str(closed_candle_time)
    previous_candle_time = last_auto_evaluated_candle_times.get(symbol)
    if previous_candle_time is None:
        last_auto_evaluated_candle_times[symbol] = closed_candle_time
        return
    if closed_candle_time == previous_candle_time:
        return
    last_auto_evaluated_candle_times[symbol] = closed_candle_time

    decision_timeframes = dict(timeframes)
    decision_timeframes[primary_tf] = closed_timeframe
    action, audit = evaluate_multi_path_strategy(
        closed_timeframe,
        higher_tf_data,
        closed_price,
        agent_state.get("cpr", {}),
        decision_timeframes,
    )
    agent_state["decision_audit"] = audit
    if not action:
        return

    atr = float(closed_timeframe["atr"])
    adx = float(closed_timeframe["adx"])
    if not math.isfinite(atr) or atr <= 0:
        return

    base_sl_mult = profile["sl_atr_mult"]
    base_tp_mult = profile["tp_atr_mult"]
    atr_history = closed_timeframe.get("atr_history", [atr])
    avg_atr = sum(atr_history[-20:]) / len(atr_history[-20:]) if atr_history else atr
    is_volatility_expansion = (adx > 30.0) or (avg_atr > 0 and (atr / avg_atr) > 1.3)

    sl_mult = base_sl_mult * 1.2 if is_volatility_expansion else base_sl_mult
    tp_mult = base_tp_mult * 1.2 if is_volatility_expansion else base_tp_mult

    payload = {
        "symbol": symbol,
        "action": action,
        "risk_sized": True,
        "sl_distance": atr * sl_mult,
        "tp_distance": atr * tp_mult,
        "strategy_path": f"Style: {ACTIVE_TRADING_STYLE} | Regime: {audit['regime']} | Score: {audit['consensus_score']} | VolatilityExp: {is_volatility_expansion}"
    }

    with auto_trade_lock:
        account_state, bridge_status = mt5_bridge.account_snapshot()
        if account_state is None:
            return
        with mt5_state_lock:
            latest_mt5_account.update(account_state)
            latest_mt5_status.update(bridge_status)

        risk_allowed, _ = check_daily_loss_limit(account_state)
        if not risk_allowed:
            return
            
        all_positions = account_state.get("positions", [])
        open_managed_positions = [p for p in all_positions if int(p.get("magic", 0)) == MT5_MAGIC_NUMBER]
        if len(open_managed_positions) >= MAX_OPEN_POSITIONS or not bridge_status.get("trade_allowed"):
            return

        symbol_market = account_state.get("symbols", {}).get(symbol, {})
        if not symbol_market.get("available"):
            return

        import MetaTrader5 as mt5
        symbol_info = mt5.symbol_info(symbol)
        if symbol_info is not None:
            current_spread = symbol_info.spread * symbol_info.point
            max_allowed_spread = atr * 0.25
            if current_spread > max_allowed_spread:
                print(f"[SPREAD GUARD REJECT] {symbol} spread ({current_spread:.3f}) exceeds max allowed ({max_allowed_spread:.3f}).")
                return

        succeeded, message, result = mt5_bridge.place_market_order(
            payload, MAX_RISK_PER_TRADE_PERCENT
        )
    
    if succeeded:
        ticket = result.get('order')
        volume = result.get('volume', 0.0)
        entry = float(result.get('price', closed_price))
        margin = float(result.get('margin', 0.0))
        active_trade_evolution_logs[ticket] = []
        log_trade_event(
            ticket=ticket,
            symbol=symbol,
            trading_style=ACTIVE_TRADING_STYLE,
            timeframe=primary_tf,
            action=action,
            regime=audit['regime'],
            score=audit['consensus_score'],
            strategies=audit['active_paths'],
            modifications=[f"Initial Style-Profiled Order Placed (Dynamic ATR Mult: {sl_mult:.2f})"],
            entry_price=entry,
            margin=margin,
            volume=volume,
            atr=atr,
            sl=entry - (atr * sl_mult) if action == "BUY" else entry + (atr * sl_mult),
            tp=entry + (tp_mult * atr) if action == "BUY" else entry - (tp_mult * atr),
            status="OPEN"
        )
        print(f"[{ACTIVE_TRADING_STYLE} TRADE LOGGED] Ticket {ticket} | {action} {volume} lots on {symbol}")

def manage_active_trades(agent_state):
    """Advanced active trade manager utilizing profile scale-outs, stagnation cuts, and Chandelier Trailing Stops."""
    if not LIVE_TRADING_ENABLED or not latest_mt5_status.get("trade_allowed"):
        return

    positions = agent_state.get("mt5_account", {}).get("positions", [])
    if not positions:
        return

    import MetaTrader5 as mt5
    profile = CURRENT_PROFILE
    primary_tf = profile["primary_tf"]
    scale_threshold = profile["partial_scale_out_atr"]
    stagnation_limit = profile["stagnation_seconds"]

    for pos in positions:
        symbol = pos.get("symbol")
        if symbol not in SUPPORTED_SYMBOLS or int(pos.get("magic", 0)) != MT5_MAGIC_NUMBER:
            continue

        symbol_state = agent_state.get("symbols", {}).get(symbol, {})
        tf_data = symbol_state.get("timeframes", {}).get(primary_tf, {})
        candles = tf_data.get("candles", [])
        atr = float(tf_data.get("atr", 0.0))
        atr_history = tf_data.get("atr_history", [atr])
        avg_atr = sum(atr_history[-20:]) / len(atr_history[-20:]) if atr_history else atr
        
        ema20 = float(tf_data.get("ema20", 0.0))
        rsi = float(tf_data.get("rsi", 50.0))
        if not math.isfinite(atr) or atr <= 0:
            continue

        ticket = int(pos["ticket"])
        if ticket not in active_trade_evolution_logs:
            active_trade_evolution_logs[ticket] = []

        pos_type = pos["type"]
        entry_price = float(pos["price"])
        current_sl = float(pos["sl"])
        current_tp = float(pos["tp"])
        current_volume = float(pos["volume"])
        margin = float(pos.get("margin", 0.0))
        
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
        evolution_action = None

        if profit_points >= atr * scale_threshold and ticket not in scaled_out_positions and current_volume >= 0.02:
            scale_out_volume = round(current_volume * 0.5, 2)
            succeeded_so, _, _ = mt5_bridge.close_position(ticket, volume=scale_out_volume)
            if succeeded_so:
                scaled_out_positions.add(ticket)
                mod_desc = f"Partial scale-out of {scale_out_volume} lots at {scale_threshold}x ATR profit."
                active_trade_evolution_logs[ticket].append(mod_desc)
                log_trade_event(
                    ticket=ticket, symbol=symbol, trading_style=ACTIVE_TRADING_STYLE, timeframe=primary_tf, action=pos_type,
                    regime="SCALE_OUT", score=0, strategies=[f"{ACTIVE_TRADING_STYLE} Scale-Out Rule"],
                    modifications=[mod_desc], entry_price=entry_price, margin=margin,
                    volume=scale_out_volume, atr=atr, sl=current_sl, tp=current_tp,
                    partial_closures=[{"volume": scale_out_volume, "price": market_price}],
                    status="PARTIAL_CLOSED"
                )

        if profit_points >= atr * 1.0 and (current_sl == 0.0 or (pos_type == "BUY" and current_sl < entry_price) or (pos_type == "SELL" and (current_sl > entry_price or current_sl == 0.0))):
            new_sl = entry_price + (atr * 0.2 if pos_type == "BUY" else -atr * 0.2)
            evolution_action = "Moved SL to Breakeven + 0.2 ATR buffer"
        elif profit_points >= atr * 2.0 and len(candles) >= 5:
            recent_highs = [c['high'] for c in candles[-5:]]
            recent_lows = [c['low'] for c in candles[-5:]]
            chandelier_mult = 1.5
            
            if pos_type == "BUY":
                highest_high = max(recent_highs)
                candidate_sl = highest_high - (atr * chandelier_mult)
                if current_sl == 0.0 or candidate_sl > current_sl + (atr * 0.1):
                    new_sl = candidate_sl
                    evolution_action = f"Chandelier Trailing SL set to 1.5x ATR below 5-candle high ({highest_high:.3f})"
            else:
                lowest_low = min(recent_lows)
                candidate_sl = lowest_low + (atr * chandelier_mult)
                if current_sl == 0.0 or candidate_sl < current_sl - (atr * 0.1):
                    new_sl = candidate_sl
                    evolution_action = f"Chandelier Trailing SL set to 1.5x ATR above 5-candle low ({lowest_low:.3f})"

        if new_sl is not None:
            succeeded, _, _ = mt5_bridge.modify_position(ticket, new_sl, new_tp)
            if succeeded and evolution_action:
                active_trade_evolution_logs[ticket].append(evolution_action)
            continue

        opened_at = datetime.fromtimestamp(
            int(pos["time"]), tz=datetime.now().astimezone().tzinfo
        )
        age_seconds = (datetime.now().astimezone() - opened_at).total_seconds()
        
        if age_seconds >= stagnation_limit and float(pos["profit"]) < 0 and ema20 > 0:
            breached_structure = (
                market_price < ema20 - atr * 0.5
                if pos_type == "BUY"
                else market_price > ema20 + atr * 0.5
            )
            if breached_structure or (pos_type == "BUY" and rsi < 42) or (pos_type == "SELL" and rsi > 58):
                succeeded, _, _ = mt5_bridge.close_position(ticket)
                if succeeded:
                    scaled_out_positions.discard(ticket)
                    close_time_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                    mod_list = active_trade_evolution_logs.pop(ticket, ["Stagnation cut executed"])
                    log_trade_event(
                        ticket=ticket, symbol=symbol, trading_style=ACTIVE_TRADING_STYLE, timeframe=primary_tf, action=pos_type,
                        regime="STAGNATION_CUT", score=0, strategies=[f"{ACTIVE_TRADING_STYLE} Stagnation Rule"],
                        modifications=mod_list, entry_price=entry_price, margin=margin,
                        volume=current_volume, atr=atr, sl=current_sl, tp=current_tp,
                        close_time=close_time_str, status="CLOSED_STAGNATION"
                    )

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
                    continue
                frame = pd.DataFrame(rates)
                frame["time"] = frame["time"].astype("int64")
                frame.index = pd.to_datetime(frame["time"], unit="s", utc=True)
                frame = frame[["time", "open", "high", "low", "close"]]
                frame = frame[~frame.index.duplicated(keep="last")].sort_index()
                
                frame['open'] = frame['open'].where(frame['open'] > 0, frame['close'])
                frame['close'] = frame['close'].where(frame['close'] > 0, frame['open'])
                frame['high'] = frame[['high', 'open', 'close']].max(axis=1).where(lambda x: x > 0, frame['close'])
                frame['low'] = frame[['low', 'open', 'close']].min(axis=1).where(lambda x: x > 0, frame['close'])

                with state_lock:
                    symbol_tf_caches[symbol][timeframe] = frame.iloc[-300:]

    broadcast_state_update()
    return history_loaded

def broadcast_state_update():
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
                "current_price": current_prices.get(symbol, 0.0),
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
            "trading_style": ACTIVE_TRADING_STYLE,
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

        symbol_candidates = []
        profile = CURRENT_PROFILE
        primary_tf = profile["primary_tf"]
        confluence_tf = profile["confluence_tf"]

        for symbol, symbol_state in symbol_states.items():
            symbol_state["mt5_account"] = mt5_account_data
            timeframes = symbol_state.get("timeframes", {})
            tf_data = timeframes.get(primary_tf, {})
            higher_tf_data = timeframes.get(confluence_tf, {})
            market_price = symbol_state.get("current_price", 0.0)

            if market_price > 0 and tf_data and higher_tf_data:
                action, audit = evaluate_multi_path_strategy(
                    tf_data, higher_tf_data, market_price, symbol_state.get("cpr", {}), timeframes
                )
                symbol_state["signal"] = action or "HOLD / SCANNING"
                symbol_state["decision_audit"] = audit

                if tf_data.get("data_ready") and higher_tf_data.get("data_ready"):
                    candles = tf_data.get("candles", [])
                    if len(candles) >= 2:
                        closed_timeframe = dict(tf_data)
                        closed_timeframe["candles"] = candles[:-1]
                        history_fields = (
                            "bb_upper", "bb_middle", "bb_lower", "ema20_history", "ema30_history",
                            "ema100_history", "atr_history", "adx_history", "rsi_history",
                            "stoch_k_history", "stoch_d_history",
                        )
                        valid_history = True
                        for field in history_fields:
                            values = tf_data.get(field)
                            if not isinstance(values, list) or len(values) != len(candles):
                                valid_history = False
                                break
                            closed_timeframe[field] = values[:-1]

                        if valid_history:
                            closed_indicators = {
                                "ema20": "ema20_history",
                                "atr": "atr_history",
                                "adx": "adx_history",
                                "rsi": "rsi_history",
                            }
                            for field, history in closed_indicators.items():
                                closed_timeframe[field] = closed_timeframe[history][-1]
                            closed_price = float(closed_timeframe["candles"][-1]["close"])
                            closed_candle_time = closed_timeframe["candles"][-1].get("time")

                            if closed_candle_time is not None:
                                closed_candle_time_str = str(closed_candle_time)
                                previous_candle_time = last_auto_evaluated_candle_times.get(symbol)
                                if previous_candle_time != closed_candle_time_str:
                                    decision_timeframes = dict(timeframes)
                                    decision_timeframes[primary_tf] = closed_timeframe
                                    c_action, c_audit = evaluate_multi_path_strategy(
                                        closed_timeframe, higher_tf_data, closed_price, symbol_state.get("cpr", {}), decision_timeframes
                                    )
                                    if c_action:
                                        atr = float(closed_timeframe["atr"])
                                        adx = float(closed_timeframe["adx"])
                                        if math.isfinite(atr) and atr > 0:
                                            t3m = timeframes.get('3m', {}).get('trend', 'FLAT')
                                            t5m = timeframes.get('5m', {}).get('trend', 'FLAT')
                                            t15m = timeframes.get('15m', {}).get('trend', 'FLAT')
                                            th1 = timeframes.get('h1', {}).get('trend', 'FLAT')
                                            alignment_bonus = 0
                                            if c_action == "BUY" and t3m == "BULLISH" and t5m == "BULLISH" and t15m == "BULLISH" and th1 == "BULLISH":
                                                alignment_bonus = 20
                                            elif c_action == "SELL" and t3m == "BEARISH" and t5m == "BEARISH" and t15m == "BEARISH" and th1 == "BEARISH":
                                                alignment_bonus = 20

                                            adjusted_score = abs(c_audit["consensus_score"]) + alignment_bonus

                                            symbol_candidates.append({
                                                "symbol": symbol,
                                                "action": c_action,
                                                "audit": c_audit,
                                                "adjusted_score": adjusted_score,
                                                "closed_timeframe": closed_timeframe,
                                                "closed_price": closed_price,
                                                "candle_time": closed_candle_time_str,
                                                "atr": atr,
                                                "adx": adx
                                            })
            else:
                symbol_state["signal"] = "HOLD / SCANNING"
                symbol_state["decision_audit"] = {
                    "regime": "SCANNING", "consensus_score": 0, "active_paths": [],
                    "reasoning": f"Waiting for {primary_tf} and {confluence_tf} market data."
                }

        if LIVE_TRADING_ENABLED and AUTO_TRADING_ENABLED and agent_status == "RUNNING" and symbol_candidates:
            with auto_trade_lock:
                account_state, bridge_status = mt5_bridge.account_snapshot()
                if account_state is not None:
                    with mt5_state_lock:
                        latest_mt5_account.update(account_state)
                        latest_mt5_status.update(bridge_status)

                    risk_allowed, _ = check_daily_loss_limit(account_state)
                    
                    all_positions = account_state.get("positions", [])
                    open_managed_positions = [p for p in all_positions if int(p.get("magic", 0)) == MT5_MAGIC_NUMBER]
                    
                    if risk_allowed and len(open_managed_positions) < MAX_OPEN_POSITIONS and bridge_status.get("trade_allowed"):
                        symbol_candidates.sort(key=lambda x: x["adjusted_score"], reverse=True)
                        best_candidate = symbol_candidates[0]

                        symbol = best_candidate["symbol"]
                        symbol_market = account_state.get("symbols", {}).get(symbol, {})
                        if symbol_market.get("available"):
                            last_auto_evaluated_candle_times[symbol] = best_candidate["candle_time"]
                            
                            atr = best_candidate["atr"]
                            adx = best_candidate["adx"]
                            action = best_candidate["action"]
                            closed_price = best_candidate["closed_price"]
                            audit = best_candidate["audit"]

                            base_sl_mult = profile["sl_atr_mult"]
                            base_tp_mult = profile["tp_atr_mult"]
                            atr_history = best_candidate["closed_timeframe"].get("atr_history", [atr])
                            avg_atr = sum(atr_history[-20:]) / len(atr_history[-20:]) if atr_history else atr
                            is_volatility_expansion = (adx > 30.0) or (avg_atr > 0 and (atr / avg_atr) > 1.3)

                            sl_mult = base_sl_mult * 1.2 if is_volatility_expansion else base_sl_mult
                            tp_mult = base_tp_mult * 1.2 if is_volatility_expansion else base_tp_mult

                            import MetaTrader5 as mt5
                            symbol_info = mt5.symbol_info(symbol)
                            if symbol_info is not None:
                                current_spread = symbol_info.spread * symbol_info.point
                                max_allowed_spread = atr * 0.25
                                if current_spread > max_allowed_spread:
                                    print(f"[SPREAD GUARD REJECT] Best candidate {symbol} spread ({current_spread:.3f}) exceeds max allowed ({max_allowed_spread:.3f}). Skipping.")
                                    return

                            payload = {
                                "symbol": symbol,
                                "action": action,
                                "risk_sized": True,
                                "sl_distance": atr * sl_mult,
                                "tp_distance": atr * tp_mult,
                                "strategy_path": f"Style: {ACTIVE_TRADING_STYLE} | Regime: {audit['regime']} | Score: {audit['consensus_score']} | VolatilityExp: {is_volatility_expansion}"
                            }

                            succeeded, message, result = mt5_bridge.place_market_order(
                                payload, MAX_RISK_PER_TRADE_PERCENT
                            )
                            if succeeded:
                                ticket = result.get('order')
                                volume = result.get('volume', 0.0)
                                entry = float(result.get('price', closed_price))
                                margin = float(result.get('margin', 0.0))
                                active_trade_evolution_logs[ticket] = []
                                log_trade_event(
                                    ticket=ticket,
                                    symbol=symbol,
                                    trading_style=ACTIVE_TRADING_STYLE,
                                    timeframe=primary_tf,
                                    action=action,
                                    regime=audit['regime'],
                                    score=audit['consensus_score'],
                                    strategies=audit['active_paths'],
                                    modifications=["Initial Order Placed (Global Portfolio & Spread Guard Armed)"],
                                    entry_price=entry,
                                    margin=margin,
                                    volume=volume,
                                    atr=atr,
                                    sl=entry - (atr * sl_mult) if action == "BUY" else entry + (atr * sl_mult),
                                    tp=entry + (tp_mult * atr) if action == "BUY" else entry - (tp_mult * atr),
                                    status="OPEN"
                                )
                                print(f"[{ACTIVE_TRADING_STYLE} BEST TRADE LOGGED] Ticket {ticket} | {action} {volume} lots on {symbol} (Score: {audit['consensus_score']})")

        default_state = symbol_states[DEFAULT_SYMBOL]
        for field in ("current_price", "signal", "cpr", "timeframes", "decision_audit"):
            state_data[field] = default_state[field]

        payload_str = json.dumps(state_data)
        latest_state_payload = payload_str
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
                    "trading_style": ACTIVE_TRADING_STYLE,
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
        elif path not in ('/index.html', '/tracker.html', '/tradelog.html', '/style.css'):
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
                    "message": "Live trading is disabled. Restart with --enable-live-trading."
                })
                return

            if agent_status != "RUNNING":
                self._send_json(409, {
                    "status": "REJECTED",
                    "message": "Manual order dispatch is not armed."
                })
                return

            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            try:
                payload = json.loads(post_data.decode('utf-8'))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(400, {"status": "REJECTED", "message": "Invalid JSON payload."})
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
                self._send_json(503, {"status": "REJECTED", "message": "MT5 account state unavailable."})
                return

            risk_allowed, risk_message = check_daily_loss_limit(account_state)
            if not risk_allowed:
                self._send_json(503, {"status": "REJECTED", "message": risk_message})
                return

            payload["symbol"] = payload.get("symbol") or MT5_SYMBOL
            payload["risk_pct"] = risk_percent
            succeeded, message, result = mt5_bridge.place_market_order(payload, risk_percent * 100.0)
            if not succeeded:
                self._send_json(400, {"status": "REJECTED", "message": message, "mt5_result": result})
                return

            ticket = result.get('order')
            if ticket:
                volume = result.get('volume', 0.0)
                entry = float(result.get('price', 0.0))
                margin = float(result.get('margin', 0.0))
                action = payload.get("action", "BUY").upper()
                symbol = payload["symbol"]
                
                symbol_states = symbol_tf_caches.get(symbol, {})
                tf_df = symbol_states.get(CURRENT_PROFILE["primary_tf"])
                atr = 1.0
                if tf_df is not None and not tf_df.empty and 'atr' in tf_df.columns:
                    latest_atr = tf_df['atr'].iloc[-1]
                    if math.isfinite(float(latest_atr)):
                        atr = float(latest_atr)

                sl = float(result.get('sl', entry - (atr * CURRENT_PROFILE["sl_atr_mult"]) if action == "BUY" else entry + (atr * CURRENT_PROFILE["sl_atr_mult"])))
                tp = float(result.get('tp', entry + (atr * CURRENT_PROFILE["tp_atr_mult"]) if action == "BUY" else entry - (atr * CURRENT_PROFILE["tp_atr_mult"])))

                active_trade_evolution_logs[ticket] = []
                log_trade_event(
                    ticket=ticket,
                    symbol=symbol,
                    trading_style=ACTIVE_TRADING_STYLE,
                    timeframe=CURRENT_PROFILE["primary_tf"],
                    action=action,
                    regime="MANUAL_UI_DISPATCH",
                    score=100,
                    strategies=["Manual UI Execution"],
                    modifications=["Manual UI Order Placed & Enrolled in Active Python Post-Trade Management"],
                    entry_price=entry,
                    margin=margin,
                    volume=volume,
                    atr=atr,
                    sl=sl,
                    tp=tp,
                    status="OPEN"
                )
                print(f"[{ACTIVE_TRADING_STYLE} MANUAL UI TRADE LOGGED & ENROLLED] Ticket {ticket} | {action} {volume} lots on {symbol}")

            self._send_json(200, {"status": "SUCCESS", "message": message, "mt5_result": result})
        else:
            self.send_error(404)

def run_websocket_server():
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
            handler, "127.0.0.1", WEBSOCKET_PORT,
            origins=[re.compile(rf"^http://(?:localhost|127\.0\.0\.1):{DASHBOARD_PORT}$")]
        ) as server:
            server.serve_forever()
    except Exception as e:
        print(f"WS Server note: {e}")

def main():
    global LIVE_TRADING_ENABLED, AUTO_TRADING_ENABLED, agent_status
    parser = argparse.ArgumentParser(description="Run the local MT5 dashboard and market-data engine.")
    parser.add_argument("--enable-live-trading", action="store_true")
    parser.add_argument("--enable-auto-trading", action="store_true")
    parser.add_argument(
        "--style",
        choices=["SCALPING", "INTRADAY", "SWING"],
        default="INTRADAY",
        help="Select active trading style profile."
    )
    args = parser.parse_args()
    
    global ACTIVE_TRADING_STYLE, CURRENT_PROFILE
    ACTIVE_TRADING_STYLE = args.style.upper()
    CURRENT_PROFILE = TRADING_PROFILES[ACTIVE_TRADING_STYLE]

    LIVE_TRADING_ENABLED = args.enable_live_trading
    AUTO_TRADING_ENABLED = args.enable_auto_trading

    if AUTO_TRADING_ENABLED and not LIVE_TRADING_ENABLED:
        parser.error("--enable-auto-trading requires --enable-live-trading.")

    if LIVE_TRADING_ENABLED:
        if not mt5_bridge.connect():
            parser.error(f"Cannot enable live trading: {mt5_bridge.status()['detail']}")
        agent_status = "RUNNING"
        print(f"MANUALLY CONFIRMED LIVE TRADING ENABLED [{ACTIVE_TRADING_STYLE} PROFILE].")
        if AUTO_TRADING_ENABLED:
            print(f"AUTOMATED {ACTIVE_TRADING_STYLE} STRATEGY ORDERS ARE ACTIVE.")
    else:
        agent_status = "DATA ONLY"
        print(f"Live order execution disabled [{ACTIVE_TRADING_STYLE} Mode - Read-Only].")

    refresh_mt5_state()
    threading.Thread(target=mt5_monitor_loop, daemon=True).start()
    threading.Thread(target=run_websocket_server, daemon=True).start()
    
    server_address = ('127.0.0.1', DASHBOARD_PORT)
    print(f"Serving HTTP Dashboard on http://localhost:{DASHBOARD_PORT} with WebSocket port {WEBSOCKET_PORT}...")
    request_handler = functools.partial(CustomHTTPRequestHandler, directory=PROJECT_DIR)
    try:
        ThreadingHTTPServer(server_address, request_handler).serve_forever()
    finally:
        mt5_bridge.disconnect()

if __name__ == '__main__':
    main()
