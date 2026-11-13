import sqlite3
import numpy as np
import MetaTrader5 as mt5
import time
from typing import Any, Tuple, Dict, List, Optional

# MODULE 1: SQLite WAL Mode & Concurrency Hardening
def get_db_connection(db_path: str, timeout: float = 30.0) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=timeout)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn

# MODULE 2: Volatility & Technical Indicator Analysis
def calculate_atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> np.ndarray:
    highs = np.asarray(highs, dtype=float)
    lows = np.asarray(lows, dtype=float)
    closes = np.asarray(closes, dtype=float)
    
    n = len(closes)
    tr = np.zeros(n)
    if n > 0:
        tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        tr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )
    
    atr = np.zeros(n)
    if n < period:
        return atr
    
    atr[period - 1] = np.mean(tr[:period])
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr

def calculate_rsi(closes: np.ndarray, period: int = 14) -> np.ndarray:
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    rsi = np.zeros(n)
    if n <= period:
        return rsi

    deltas = np.diff(closes)
    seed = deltas[:period]
    up = seed[seed >= 0].sum() / period
    down = -seed[seed < 0].sum() / period

    if down == 0:
        rsi[period] = 100.0
    else:
        rs = up / down
        rsi[period] = 100.0 - (100.0 / (1.0 + rs))

    for i in range(period + 1, n):
        delta = deltas[i - 1]
        if delta > 0:
            up = (up * (period - 1) + delta) / period
            down = (down * (period - 1)) / period
        else:
            up = (up * (period - 1)) / period
            down = (down * (period - 1) - delta) / period

        if down == 0:
            rsi[i] = 100.0
        else:
            rs = up / down
            rsi[i] = 100.0 - (100.0 / (1.0 + rs))
    return rsi

def calculate_macd(closes: np.ndarray, fast_period: int = 12, slow_period: int = 26, signal_period: int = 9) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    closes = np.asarray(closes, dtype=float)
    
    def ema(data, span):
        alpha = 2.0 / (span + 1)
        res = np.empty_like(data)
        if len(data) == 0:
            return res
        res[0] = data[0]
        for i in range(1, len(data)):
            res[i] = alpha * data[i] + (1 - alpha) * res[i - 1]
        return res

    fast_ema = ema(closes, fast_period)
    slow_ema = ema(closes, slow_period)
    macd_line = fast_ema - slow_ema
    signal_line = ema(macd_line, signal_period)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram

# MODULE 3: Risk-Reward Ratio (RR) & Profile-Based Scale-Out
def evaluate_trade_risk_reward(entry_price: float, stop_loss: float, target_price: float, min_rr: float = 1.5) -> Tuple[bool, float, float]:
    risk = abs(entry_price - stop_loss)
    reward = abs(target_price - entry_price)
    
    if risk == 0:
        return False, 0.0, 0.0
    
    rr_ratio = reward / risk
    valid = rr_ratio >= min_rr
    
    if rr_ratio >= 3.0:
        multiplier = 1.5
    elif rr_ratio >= 2.0:
        multiplier = 1.2
    elif rr_ratio >= min_rr:
        multiplier = 1.0
    else:
        multiplier = 0.0
        
    return valid, rr_ratio, multiplier

# MODULE 4: Smart Stagnation Cut & Position Maintenance
def monitor_and_cut_stagnant_positions(mt5_connection: Any, active_positions: List[Dict[str, Any]], max_stagnation_seconds: float) -> List[int]:
    closed_tickets = []
    current_time = time.time()
    
    for pos in active_positions:
        ticket = pos.get("ticket")
        time_opened = pos.get("time", current_time)
        profit = pos.get("profit", 0.0)
        symbol = pos.get("symbol")
        
        elapsed = current_time - time_opened
        if elapsed >= max_stagnation_seconds:
            if -2.0 <= profit <= 1.0:
                is_buy = pos.get("type") == "BUY" or pos.get("type") == mt5.POSITION_TYPE_BUY
                tick = mt5.symbol_info_tick(symbol)
                if tick:
                    price = tick.bid if is_buy else tick.ask
                    info = mt5.symbol_info(symbol)
                    filling_mode = getattr(info, "filling_mode", 1) if info else 1
                    
                    request = {
                        "action": mt5.TRADE_ACTION_DEAL,
                        "symbol": symbol,
                        "volume": float(lot_size),
                        "type": order_type,
                        "price": float(price),
                        "sl": float(sl),
                        "tp": float(tp),
                        "deviation": 20,
                        "magic": MT5_MAGIC_NUMBER,
                        "comment": "Guarded AI Engine Order",
                        "type_time": mt5.ORDER_TIME_GTC,
                        # Explicitly set type_filling to match broker requirements (FOK or IOC)
                        "type_filling": mt5.ORDER_FILLING_FOK, 
                    }
                    result = mt5.order_send(request)
                    if result is not None and result.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_DONE_PARTIAL):
                        closed_tickets.append(ticket)
    return closed_tickets

# MODULE 5: Local Automated Execution Guardrails via MT5 API
def execute_guarded_order(symbol, order_type, lot_size, price, sl, tp, max_allowed_spread_multiplier=1.5):
    MT5_MAGIC_NUMBER = 123456
    
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(lot_size),
        "type": order_type,
        "price": float(price),
        "sl": float(sl),
        "tp": float(tp),
        "deviation": 20,
        "magic": MT5_MAGIC_NUMBER,
        "comment": "Guarded AI Engine Order",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_FOK,
    }
    
    result = mt5.order_send(request)
    if result is None:
        return False, "MT5 order_send returned None.", None
        
    if result.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_DONE_PARTIAL):
        return True, "Order executed successfully.", result
    else:
        return False, f"Order check rejected: {result.comment} (retcode: {result.retcode})", result