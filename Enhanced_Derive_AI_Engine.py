# Enhanced_Derive_AI_Engine.py - Full 760+ Line Codebase + Real-Time WebSocket Push
import argparse
import functools
import json
import math
import threading
import time
import os
import traceback
from datetime import datetime, timedelta
from datetime import timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
import pandas as pd
import pandas_ta as ta
import websocket
from mt5_bridge import MT5Bridge

try:
    from websockets.sync.server import serve
    import websockets
except ImportError:
    pass

DERIV_WS_URL = "wss://api.derivws.com/trading/v1/options/ws/public?app_id=1089"
SYMBOL = "R_25"
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "8080"))
WEBSOCKET_PORT = 8765 if DASHBOARD_PORT == 8080 else DASHBOARD_PORT + 1
MT5_SYMBOL = os.environ.get("MT5_SYMBOL", "Volatility 25 Index")
MT5_MAGIC_NUMBER = 123456

def load_percentage_setting(name, default):
    try:
        value = float(os.environ.get(name, default))
    except ValueError as error:
        raise ValueError(f"{name} must be a number between 0 and 100.") from error
    if not math.isfinite(value) or value <= 0 or value > 100:
        raise ValueError(f"{name} must be greater than 0 and no more than 100.")
    return value

MAX_RISK_PER_TRADE_PERCENT = load_percentage_setting("MAX_RISK_PER_TRADE_PERCENT", 1.0)
MAX_DAILY_LOSS_PERCENT = load_percentage_setting("MAX_DAILY_LOSS_PERCENT", 2.0)
LIVE_TRADING_ENABLED = False
agent_status = "DATA ONLY"
current_signal = "HOLD / SCANNING"
current_price = 2785.007
state_lock = threading.Lock()
connected_ui_clients = set()
latest_state_payload = None
mt5_bridge = MT5Bridge(symbol=MT5_SYMBOL)
mt5_state_lock = threading.Lock()
latest_mt5_account = {}
latest_mt5_status = mt5_bridge.status()

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

tf_caches = {
    '3m': None, '5m': None, '15m': None, 
    '30m': None, 'h1': None, 'h4': None, 'd1': None
}

TF_GRANULARITY = {
    '3m': 180,
    '5m': 300,
    '15m': 900,
    '30m': 1800,
    'h1': 3600,
    'h4': 14400,
    'd1': 86400
}

def calculate_cpr(df):
    if df is None or len(df) < 2:
        return {"pivot": 2780.5, "bc": 2778.2, "tc": 2782.8, "r1": 2792.1, "s1": 2770.3, "r2": 2801.4, "s2": 2761.0, "r3": 2815.0, "s3": 2750.0, "is_virgin": True}
    
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

def get_true_previous_day_ohlc():
    daily_df = tf_caches.get('d1')
    if daily_df is not None and len(daily_df) >= 2:
        prev_day = daily_df.iloc[-2]
        return float(prev_day['open']), float(prev_day['close'])
    elif daily_df is not None and len(daily_df) == 1:
        return float(daily_df.iloc[-1]['open']), float(daily_df.iloc[-1]['close'])
    return current_price, current_price

def process_dataframe(df):
    if df is None or len(df) < 5:
        p_open, p_close = get_true_previous_day_ohlc()
        base_time = int(time.time()) - (50 * 180)
        fallback_candles = []
        p = current_price
        for i in range(50):
            fallback_candles.append({
                "time": base_time + (i * 180),
                "open": round(p - 0.5, 3),
                "high": round(p + 1.0, 3),
                "low": round(p - 1.0, 3),
                "close": round(p, 3),
                "pattern": "Normal"
            })
        return {
            "trend": "FLAT", "ema20": current_price, "ema30": current_price, "ema100": current_price, "ema200": current_price,
            "rsi": 50.0, "atr": 1.0, "adx": 20.0, "bullish_pressure": 50, "candles": fallback_candles,
            "bb_upper": [current_price + 2] * 50, "bb_middle": [current_price] * 50, "bb_lower": [current_price - 2] * 50,
            "ema20_history": [current_price] * 50, "rsi_history": [50.0] * 50, "stoch_k_history": [50.0] * 50,
            "prev_day_open": p_open, "prev_day_close": p_close
        }
    
    df = df.copy()
    
    df = df[~df.index.duplicated(keep='last')]
    df = df.sort_index()
    
    df['ema20'] = ta.ema(df['close'], length=20)
    df['ema30'] = ta.ema(df['close'], length=30)
    df['ema100'] = ta.ema(df['close'], length=100)
    df['ema200'] = ta.ema(df['close'], length=200)
    df['rsi'] = ta.rsi(df['close'], length=14)
    df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=14)
    
    adx_df = ta.adx(df['high'], df['low'], df['close'], length=14)
    df['adx'] = adx_df['ADX_14'] if adx_df is not None and not adx_df.empty else 20.0

    bb = ta.bbands(df['close'], length=16, std=1.986)
    if bb is not None:
        df['bb_lower'] = bb.iloc[:, 0]
        df['bb_middle'] = bb.iloc[:, 1]
        df['bb_upper'] = bb.iloc[:, 2]
    else:
        df['bb_lower'] = df['close']
        df['bb_middle'] = df['close']
        df['bb_upper'] = df['close']

    stoch = ta.stoch(df['high'], df['low'], df['close'], k=14, d=3)
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
    atr_val = round(float(latest['atr'] if pd.notna(latest['atr']) else 1.0), 3)
    adx_val = round(float(latest['adx'] if pd.notna(latest['adx']) else 20.0), 1)
    
    trend = "BULLISH" if ema30_val > ema100_val else "BEARISH"
    prev_open, prev_close = get_true_previous_day_ohlc()

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
        "trend": trend, "ema20": ema20_val, "ema30": ema30_val, "ema100": ema100_val, "ema200": ema200_val,
        "rsi": rsi_val, "atr": atr_val, "adx": adx_val, "bullish_pressure": pressure, "candles": candles_list,
        "bb_upper": [round(x, 3) for x in tail_df['bb_upper'].fillna(close_val).tolist()],
        "bb_middle": [round(x, 3) for x in tail_df['bb_middle'].fillna(close_val).tolist()],
        "bb_lower": [round(x, 3) for x in tail_df['bb_lower'].fillna(close_val).tolist()],
        "ema20_history": [round(x, 3) for x in tail_df['ema20'].fillna(close_val).tolist()],
        "rsi_history": [round(x, 1) for x in tail_df['rsi'].fillna(50).tolist()],
        "stoch_k_history": [round(x, 1) for x in tail_df['stoch_k'].fillna(50).tolist()],
        "stoch_d_history": [round(x, 1) for x in tail_df['stoch_d'].fillna(50).tolist()],
        "prev_day_open": round(prev_open, 3),
        "prev_day_close": round(prev_close, 3)
    }

def evaluate_multi_path_strategy(tf_data, h1_data, current_price, cpr, all_timeframes):
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
        if t3m == "BULLISH" and t5m == "BULLISH" and t15m == "BULLISH" and h1_trend == "BULLISH":
            total_score += 30
            active_paths.append("Path E (MTF Bullish Alignment)")
        elif t3m == "BEARISH" and t5m == "BEARISH" and t15m == "BEARISH" and h1_trend == "BEARISH":
            total_score -= 30
            active_paths.append("Path E (MTF Bearish Alignment)")

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

def check_and_auto_trigger_trade(agent_state):
    timeframes = agent_state.get("timeframes", {})
    tf_data = timeframes.get("5m", {})
    h1_data = timeframes.get("h1", {})
    current_price = agent_state.get("current_price", 0.0)

    if current_price <= 0 or not tf_data or not h1_data:
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
        current_price,
        agent_state.get("cpr", {}),
        timeframes,
    )
    agent_state["decision_audit"] = audit

    if not LIVE_TRADING_ENABLED or agent_status != "RUNNING":
        return

    mt5_account = agent_state.get("mt5_account", {})
    risk_allowed, risk_message = check_daily_loss_limit(mt5_account)
    if not risk_allowed:
        agent_state["decision_audit"] = {
            "regime": "RISK BLOCK",
            "consensus_score": 0,
            "active_paths": [],
            "reasoning": risk_message
        }
        return

    if not latest_mt5_status.get("trade_allowed"):
        return

    if mt5_account.get("open_positions", 0) > 0 or len(mt5_account.get("positions", [])) > 0:
        return

    if action:
        stop_buffer = 15.0  
        take_profit_buffer = 30.0

        if action == "BUY":
            sl = current_price - stop_buffer
            tp = current_price + take_profit_buffer
        else: 
            sl = current_price + stop_buffer
            tp = current_price - take_profit_buffer

        payload = {
            "symbol": MT5_SYMBOL,
            "action": action,
            "volume": 0.50,
            "entry": round(current_price, 4),
            "tp": round(tp, 4),
            "sl": round(sl, 4),
            "strategy_path": f"Confluence Consensus Score: {audit['consensus_score']}"
        }

        succeeded, message, result = mt5_bridge.place_market_order(
            payload, MAX_RISK_PER_TRADE_PERCENT
        )
        agent_state["decision_audit"]["reasoning"] += (
            f" | MT5 {message}" if succeeded else f" | Order rejected: {message}"
        )
        if succeeded:
            print(
                f"[AUTONOMOUS CONSENSUS] {action} {payload['volume']} lots "
                f"on {MT5_SYMBOL}; order={result.get('order')}"
            )
        else:
            print(f"ERROR: MT5 rejected autonomous {action}: {message}")

def manage_active_trades(agent_state):
    if not LIVE_TRADING_ENABLED or not latest_mt5_status.get("trade_allowed"):
        return

    mt5_account = agent_state.get("mt5_account", {})
    positions = mt5_account.get("positions", [])
    tf_data = agent_state.get("timeframes", {}).get("5m", {})
    atr = float(tf_data.get("atr", 0.0))
    ema20 = float(tf_data.get("ema20", 0.0))
    if not positions or not math.isfinite(atr) or atr <= 0:
        return

    import MetaTrader5 as mt5

    for pos in positions:
        if (
            pos.get("symbol") != MT5_SYMBOL
            or int(pos.get("magic", 0)) != MT5_MAGIC_NUMBER
        ):
            continue

        ticket = int(pos["ticket"])
        pos_type = pos["type"]
        entry_price = float(pos["price"])
        current_sl = float(pos["sl"])
        current_tp = float(pos["tp"])
        tick = mt5.symbol_info_tick(MT5_SYMBOL)
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
    while True:
        refresh_mt5_state()
        time.sleep(1)

def broadcast_state_update():
    """Build the latest engine snapshot and publish it to connected clients."""
    global latest_state_payload
    with state_lock:
        timeframes_dict = {}
        for tf, df in tf_caches.items():
            if df is not None and not df.empty:
                timeframes_dict[tf] = process_dataframe(df)
            else:
                timeframes_dict[tf] = process_dataframe(None)

        daily_df = tf_caches.get('d1')
        cpr_dict = calculate_cpr(daily_df)

        with mt5_state_lock:
            mt5_account_data = dict(latest_mt5_account)
            mt5_status = dict(latest_mt5_status)

        state_data = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "agent_status": agent_status,
            "current_price": current_price,
            "signal": current_signal,
            "cpr": cpr_dict,
            "timeframes": timeframes_dict,
            "mt5_account": mt5_account_data,
            "mt5_bridge": mt5_status,
            "vix25_specs": {
                "min_lot": 0.50,
                "max_lot": 200.00,
                "margin_per_min_lot": 0.35
            },
            "decision_audit": {
                "regime": "SCANNING",
                "consensus_score": 0,
                "active_paths": [],
                "reasoning": "Initializing decision matrix..."
            }
        }

        manage_active_trades(state_data)
        check_and_auto_trigger_trade(state_data)

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

def on_message(ws, message):
    global current_price, tf_caches
    try:
        data = json.loads(message)
        msg_type = data.get("msg_type")
        
        if msg_type == "tick":
            tick = data.get("tick")
            if tick and "quote" in tick and "epoch" in tick:
                current_price = float(tick["quote"])
                tick_epoch = int(tick["epoch"])
                
                with state_lock:
                    for tf, gran in TF_GRANULARITY.items():
                        df = tf_caches.get(tf)
                        if df is not None and not df.empty:
                            # Calculate the official time bucket start for this tick
                            bucket_epoch = (tick_epoch // gran) * gran
                            bucket_dt = datetime.fromtimestamp(bucket_epoch, timezone.utc)
                            
                            last_idx = df.index[-1]
                            
                            if bucket_dt > last_idx:
                                # 🚀 Countdown finished: A new candle period has started! Append a new row.
                                new_row = pd.DataFrame([{
                                    "time": bucket_epoch,
                                    "open": current_price,
                                    "high": current_price,
                                    "low": current_price,
                                    "close": current_price
                                }], index=pd.DatetimeIndex([bucket_dt]))
                                
                                # Append and keep the dataframe trimmed to the last 300 rows
                                df = pd.concat([df, new_row])
                                tf_caches[tf] = df.iloc[-300:]
                            else:
                                # 🔄 Still within the current candle period: update High, Low, and Close
                                high_val = float(df.loc[last_idx, 'high'])
                                low_val = float(df.loc[last_idx, 'low'])
                                
                                df.loc[last_idx, 'close'] = current_price
                                if current_price > high_val:
                                    df.loc[last_idx, 'high'] = current_price
                                if current_price < low_val:
                                    df.loc[last_idx, 'low'] = current_price
                                    
                broadcast_state_update()
                
        elif msg_type in ("candles", "history"):
            candles = data.get("candles", [])
            echo = data.get("echo_req", {})
            granularity = echo.get("granularity")
            
            target_tf = next((tf for tf, gran in TF_GRANULARITY.items() if gran == granularity), None)
                    
            if candles and target_tf:
                rows = []
                timestamps = []
                for c in candles:
                    epoch_time = c.get('epoch')
                    dt_index = datetime.fromtimestamp(epoch_time, timezone.utc)
                    timestamps.append(dt_index)
                    rows.append({
                        "time": epoch_time,
                        "open": float(c['open']),
                        "high": float(c['high']),
                        "low": float(c['low']),
                        "close": float(c['close'])
                    })
                with state_lock:
                    temp_df = pd.DataFrame(rows, index=pd.DatetimeIndex(timestamps))
                    temp_df = temp_df[~temp_df.index.duplicated(keep='last')].sort_index()
                    tf_caches[target_tf] = temp_df
                broadcast_state_update()
    except Exception as e:
        print(f"ERROR: on_message: {e}")
        traceback.print_exc()
        
def on_error(ws, error):
    print(f"Deriv Options WS Error: {error}")

active_deriv_ws = None

def on_open(ws):
    global active_deriv_ws
    active_deriv_ws = ws
    print("Connected successfully to Deriv Options Public WebSocket API!")
    
    for tf, gran in TF_GRANULARITY.items():
        history_req = {
            "ticks_history": SYMBOL,
            "adjust_start_time": 1,
            "count": 300,
            "end": "latest",
            "granularity": gran,
            "style": "candles"
        }
        ws.send(json.dumps(history_req))
        time.sleep(0.1)
        
    tick_req = {"ticks": SYMBOL, "subscribe": 1}
    ws.send(json.dumps(tick_req))

def on_close(ws, close_status_code, close_msg):
    global active_deriv_ws
    active_deriv_ws = None
    print(f"Deriv Options WS closed (code: {close_status_code}). Reconnecting in 3 seconds...")

def start_deriv_ws():
    global active_deriv_ws
    retry_delay = 3
    while True:
        try:
            custom_headers = {
                "Origin": "https://app.deriv.com",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
            ws = websocket.WebSocketApp(
                DERIV_WS_URL,
                header=custom_headers,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close
            )
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as e:
            print(f"ERROR: Deriv WS exception: {e}")
        
        print(f"Retrying Deriv connection in {retry_delay} seconds...")
        time.sleep(retry_delay)
        retry_delay = min(retry_delay * 1.5, 30)

def deriv_heartbeat_loop():
    while True:
        time.sleep(25)
        try:
            if active_deriv_ws and active_deriv_ws.sock and active_deriv_ws.sock.connected:
                active_deriv_ws.send(json.dumps({"ping": 1}))
        except Exception:
            pass

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
                payload = json.dumps({
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "agent_status": agent_status,
                    "current_price": None,
                    "signal": current_signal,
                    "mt5_account": {},
                    "mt5_bridge": latest_mt5_status,
                    "timeframes": {},
                    "cpr": {},
                    "decision_audit": {
                        "regime": "SCANNING",
                        "consensus_score": 0,
                        "active_paths": [],
                        "reasoning": "Waiting for market data."
                    }
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
                    "message": "Start the engine from the dashboard before submitting an order."
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
            if risk_error:
                self._send_json(403, {"status": "REJECTED", "message": risk_error})
                return

            account_state, bridge_status = mt5_bridge.account_snapshot()
            with mt5_state_lock:
                latest_mt5_account = account_state or {}
                latest_mt5_status = bridge_status
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
                f"http://localhost:{DASHBOARD_PORT}",
                f"http://127.0.0.1:{DASHBOARD_PORT}"
            ]
        ) as server:
            server.serve_forever()
    except Exception as e:
        print(f"WS Server note: {e}")

def main():
    global LIVE_TRADING_ENABLED
    parser = argparse.ArgumentParser(description="Run the local Deriv dashboard and market-data engine.")
    parser.add_argument(
        "--enable-live-trading",
        action="store_true",
        help="Allow the engine and dashboard to write trade requests to the configured MT5 files directory."
    )
    args = parser.parse_args()
    LIVE_TRADING_ENABLED = args.enable_live_trading

    if LIVE_TRADING_ENABLED:
        if not mt5_bridge.connect():
            parser.error(f"Cannot enable live trading: {mt5_bridge.status()['detail']}")
        print("LIVE TRADING ENABLED for this run. Use the dashboard Start control to activate trading.")
    else:
        print("Live order execution is disabled. MT5 account data remains read-only.")

    print("Initializing initial agent state cache...")
    
    refresh_mt5_state()
    threading.Thread(target=start_deriv_ws, daemon=True).start()
    threading.Thread(target=deriv_heartbeat_loop, daemon=True).start()
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