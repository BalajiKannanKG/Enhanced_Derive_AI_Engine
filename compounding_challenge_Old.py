# compounding_challenge.py - Complete Institutional Compounding Challenge Tracker & Risk Manager
import os
import sqlite3
import pandas as pd
from datetime import datetime, timedelta, timezone
import MetaTrader5 as mt5

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(PROJECT_DIR, "trade_telemetry.sqlite")

INITIAL_CAPITAL = 200.00
STEP_PROFIT_TARGET_PCT = 5.0
MAX_DAILY_DRAWDOWN_PCT = 3.0
MAX_CONSECUTIVE_LOSSES = 3

def init_challenge_db():
    """Initializes dedicated challenge tables in the SQLite telemetry database and auto-migrates columns."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS challenge_daily_status (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date_str TEXT UNIQUE,
            day_number INTEGER,
            starting_balance REAL,
            current_balance REAL,
            high_water_mark REAL,
            current_step INTEGER,
            target_balance REAL,
            daily_profit REAL,
            max_drawdown_limit REAL,
            progress_pct REAL,
            status TEXT,
            consecutive_losses INTEGER,
            halt_triggered INTEGER
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS challenge_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date_str TEXT,
            time_opened TEXT,
            ticket INTEGER,
            symbol TEXT,
            challenge_day INTEGER,
            step_tier INTEGER,
            action TEXT,
            lot_size REAL,
            entry_price REAL,
            exit_price REAL,
            pnl REAL,
            status TEXT,
            exit_reason TEXT
        )
    """)
    conn.commit()
    conn.close()

def get_current_challenge_status():
    """Retrieves current challenge status metrics and trade statistics from SQLite with exact column mapping."""
    init_challenge_db()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM challenge_daily_status ORDER BY id DESC LIMIT 1")
    row = cursor.fetchone()
    
    cursor.execute("SELECT exit_reason, pnl FROM challenge_trades WHERE status != 'OPEN'")
    trade_rows = cursor.fetchall()
    conn.close()
    
    total_trades = len(trade_rows)
    manual_trades = 0
    auto_trades = 0
    wins = 0
    
    for reason, pnl in trade_rows:
        reason_str = str(reason).upper()
        if "MANUAL" in reason_str:
            manual_trades += 1
        else:
            auto_trades += 1
        if pnl and pnl > 0:
            wins += 1
            
    win_rate = (wins / total_trades * 100.0) if total_trades > 0 else 0.0

    if not row:
        return {
            "day_number": 1,
            "starting_balance": INITIAL_CAPITAL,
            "current_balance": INITIAL_CAPITAL,
            "high_water_mark": INITIAL_CAPITAL,
            "daily_pnl": 0.0,
            "daily_target": round(INITIAL_CAPITAL * (1.0 + STEP_PROFIT_TARGET_PCT / 100.0), 2),
            "step_progress": 0.0,
            "max_drawdown_limit": round(INITIAL_CAPITAL * (1.0 - MAX_DAILY_DRAWDOWN_PCT / 100.0), 2),
            "status": "ACTIVE",
            "consecutive_losses": 0,
            "halt_trading": False,
            "total_trades": 0,
            "manual_trades": 0,
            "auto_trades": 0,
            "win_rate": 0.0
        }
    
    # Table Column Mapping:
    # 0: id, 1: date_str, 2: day_number, 3: starting_balance, 4: current_balance,
    # 5: high_water_mark, 6: current_step, 7: target_balance, 8: daily_profit,
    # 9: max_drawdown_limit, 10: progress_pct, 11: status, 12: consecutive_losses, 13: halt_triggered
    
    day_number = int(row[2])
    starting_balance = float(row[3])
    current_balance = float(row[4])
    high_water_mark = float(row[5])
    current_step = int(row[6])
    target_balance = float(row[7])
    daily_pnl = float(row[8])
    max_dd = float(row[9])
    progress_pct = float(row[10])
    status = row[11]
    consecutive_losses = int(row[12])
    
    halt_trading = status in ("DRAWDOWN_BREACH", "CIRCUIT_BREAKER_PAUSE", "HALTED_MAX_DRAWDOWN", "HALTED_CIRCUIT_BREAKER")
    
    return {
        "day_number": day_number,
        "starting_balance": starting_balance,
        "current_balance": current_balance,
        "high_water_mark": high_water_mark,
        "daily_pnl": daily_pnl,
        "daily_target": target_balance,
        "step_progress": progress_pct,
        "max_drawdown_limit": max_dd,
        "status": status,
        "consecutive_losses": consecutive_losses,
        "halt_trading": halt_trading,
        "total_trades": total_trades,
        "manual_trades": manual_trades,
        "auto_trades": auto_trades,
        "win_rate": round(win_rate, 1)
    }

def sync_closed_challenge_trades():
    """Scans MT5 history for closed challenge tickets and updates their final PnL, exit price, and status."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT ticket FROM challenge_trades WHERE status = 'OPEN'")
        open_tickets = [row[0] for row in cursor.fetchall()]
        
        if not open_tickets:
            conn.close()
            return

        utc_from = datetime.now(timezone.utc) - timedelta(days=7)
        deals = mt5.history_deals_get(utc_from, datetime.now(timezone.utc))
        
        if not deals:
            conn.close()
            return

        tracker = CompoundingChallengeTracker()
        
        for deal in deals:
            ticket = deal.position_id
            if ticket in open_tickets and deal.entry == mt5.DEAL_ENTRY_OUT:
                exit_price = float(deal.price)
                pnl = float(deal.profit + deal.swap + deal.commission)
                exit_reason = "TP_HIT" if pnl > 0 else "SL_HIT"
                status = "CLOSED_PROFIT" if pnl > 0 else "CLOSED_LOSS"
                
                cursor.execute("""
                    UPDATE challenge_trades 
                    SET exit_price = ?, pnl = ?, status = ?, exit_reason = ?
                    WHERE ticket = ?
                """, (exit_price, pnl, status, exit_reason, ticket))
                conn.commit()
                
                tracker.log_challenge_trade(
                    ticket=ticket,
                    symbol=deal.symbol,
                    action="BUY" if deal.type == mt5.DEAL_TYPE_BUY else "SELL",
                    lot_size=deal.volume,
                    entry_price=0.0,
                    exit_price=exit_price,
                    pnl=pnl,
                    status=status,
                    exit_reason=exit_reason
                )
        conn.close()
    except Exception as e:
        print(f"Error syncing closed challenge trades: {e}")

class CompoundingChallengeTracker:
    def __init__(self, initial_capital=INITIAL_CAPITAL, step_growth_rate=STEP_PROFIT_TARGET_PCT, max_daily_loss_pct=MAX_DAILY_DRAWDOWN_PCT):
        self.initial_capital = initial_capital
        self.step_growth_rate = step_growth_rate / 100.0
        self.max_daily_loss_pct = max_daily_loss_pct / 100.0
        init_challenge_db()
        self.state = self.load_state()

    def load_state(self):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, date_str, day_number, starting_balance, current_balance, high_water_mark, 
                   current_step, target_balance, daily_profit, max_drawdown_limit, progress_pct, status, consecutive_losses 
            FROM challenge_daily_status ORDER BY id DESC LIMIT 1
        """)
        row = cursor.fetchone()
        conn.close()

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if row:
            db_date = row[1]
            prev_day_num = int(row[2])
            prev_ending_balance = float(row[4]) # current_balance from previous session/day
            
            if db_date != now_str:
                start_bal = prev_ending_balance
                hwm = start_bal
                target = round(start_bal * (1.0 + self.step_growth_rate), 2)
                dd_limit = round(start_bal * (1.0 - self.max_daily_loss_pct), 2)
                return {
                    "date_str": now_str,
                    "day_number": prev_day_num + 1,
                    "starting_balance": start_bal,
                    "current_balance": start_bal,
                    "high_water_mark": hwm,
                    "current_step": 1,
                    "current_target_balance": target,
                    "max_drawdown_limit": dd_limit,
                    "status": "ACTIVE",
                    "consecutive_losses": 0
                }
            else:
                return {
                    "date_str": row[1],
                    "day_number": int(row[2]),
                    "starting_balance": float(row[3]),
                    "current_balance": prev_ending_balance,
                    "high_water_mark": float(row[5]),
                    "current_step": int(row[6]),
                    "current_target_balance": float(row[7]),
                    "max_drawdown_limit": round(prev_ending_balance * (1.0 - self.max_daily_loss_pct), 2),
                    "status": row[11],
                    "consecutive_losses": int(row[12])
                }
        else:
            target = round(self.initial_capital * (1.0 + self.step_growth_rate), 2)
            dd_limit = round(self.initial_capital * (1.0 - self.max_daily_loss_pct), 2)
            return {
                "date_str": now_str,
                "day_number": 1,
                "starting_balance": self.initial_capital,
                "current_balance": self.initial_capital,
                "high_water_mark": self.initial_capital,
                "current_step": 1,
                "current_target_balance": target,
                "max_drawdown_limit": dd_limit,
                "status": "ACTIVE",
                "consecutive_losses": 0
            }

    def calculate_dynamic_lot_size(self, account_balance: float, atr_val: float, min_lot: float = 0.50, volume_step: float = 0.01, risk_pct: float = 1.0) -> float:
        if atr_val <= 0 or account_balance <= 0:
            return min_lot
            
        risk_amount = account_balance * (risk_pct / 100.0)
        stop_distance_points = atr_val * 2.0
        if stop_distance_points <= 0:
            return min_lot
            
        raw_lot = risk_amount / stop_distance_points
        calculated_lot = max(min_lot, raw_lot)
        
        if volume_step > 0:
            steps = round((calculated_lot - min_lot) / volume_step)
            calculated_lot = min_lot + (steps * volume_step)
            
        return round(calculated_lot, 2)

    def log_challenge_trade(self, ticket: int, symbol: str, action: str, lot_size: float, entry_price: float, exit_price: float = 0.0, pnl: float = 0.0, status: str = "OPEN", exit_reason: str = ""):
        now_utc = datetime.now(timezone.utc)
        date_str = now_utc.strftime("%Y-%m-%d")
        time_opened = now_utc.strftime("%H:%M:%S")

        if status in ["CLOSED", "CLOSED_PROFIT", "CLOSED_LOSS", "CLOSED_STAGNATION"]:
            self.state["current_balance"] = round(self.state["current_balance"] + pnl, 2)
            self.state["high_water_mark"] = max(self.state["high_water_mark"], self.state["current_balance"])
            
            if pnl < 0:
                self.state["consecutive_losses"] += 1
            else:
                self.state["consecutive_losses"] = 0

            if self.state["current_balance"] <= self.state["max_drawdown_limit"]:
                self.state["status"] = "HALTED_MAX_DRAWDOWN"
            elif self.state["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
                self.state["status"] = "HALTED_CIRCUIT_BREAKER"

        start_bal = self.state["starting_balance"]
        current_bal = self.state["current_balance"]
        daily_profit = current_bal - start_bal
        current_target = self.state["current_target_balance"]
        
        while current_bal >= current_target and self.state["status"] == "ACTIVE":
            self.state["current_step"] += 1
            current_target = round(current_target * (1.0 + self.step_growth_rate), 2)
            self.state["current_target_balance"] = current_target

        target_profit = current_target - start_bal
        progress_pct = (daily_profit / target_profit) * 100 if target_profit > 0 else 0.0

        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            
            cursor.execute("""
                INSERT INTO challenge_trades 
                (date_str, time_opened, ticket, symbol, challenge_day, step_tier, action, lot_size, entry_price, exit_price, pnl, status, exit_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                date_str, time_opened, ticket, symbol, 
                self.state["day_number"], self.state["current_step"], 
                action, lot_size, entry_price, exit_price, pnl, status, exit_reason
            ))

            cursor.execute("""
                INSERT OR REPLACE INTO challenge_daily_status 
                (date_str, day_number, starting_balance, current_balance, high_water_mark, current_step, target_balance, daily_profit, max_drawdown_limit, progress_pct, status, consecutive_losses, halt_triggered)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                date_str, self.state["day_number"], start_bal, current_bal,
                self.state["high_water_mark"], self.state["current_step"], current_target,
                round(daily_profit, 2), self.state["max_drawdown_limit"], round(progress_pct, 1),
                self.state["status"], self.state["consecutive_losses"], 
                1 if self.state["status"] != "ACTIVE" else 0
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"Error logging challenge trade and telemetry: {e}")

    def evaluate_day(self, simulated_pnl: float = 0.0) -> dict:
        start_bal = self.state["starting_balance"]
        current_equity = start_bal + simulated_pnl
        daily_profit = simulated_pnl
        
        self.state["high_water_mark"] = max(self.state["high_water_mark"], current_equity)
        halt_triggered = current_equity <= self.state["max_drawdown_limit"] or self.state["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES

        if halt_triggered and self.state["status"] == "ACTIVE":
            self.state["status"] = "HALTED_MAX_DRAWDOWN" if current_equity <= self.state["max_drawdown_limit"] else "HALTED_CIRCUIT_BREAKER"

        current_target = self.state["current_target_balance"]
        while current_equity >= current_target and not halt_triggered:
            self.state["current_step"] += 1
            current_target = round(current_target * (1.0 + self.step_growth_rate), 2)
            self.state["current_target_balance"] = current_target

        target_profit = current_target - start_bal
        progress_pct = (daily_profit / target_profit) * 100 if target_profit > 0 else 0.0

        return {
            "day": self.state["day_number"],
            "starting_balance": start_bal,
            "current_balance": round(current_equity, 2),
            "high_water_mark": self.state["high_water_mark"],
            "current_step": self.state["current_step"],
            "target_balance": current_target,
            "daily_profit": round(daily_profit, 2),
            "max_drawdown_limit": self.state["max_drawdown_limit"],
            "progress_pct": round(progress_pct, 1),
            "halt_trading": halt_triggered,
            "status": self.state["status"],
            "consecutive_losses": self.state["consecutive_losses"]
        }

    def export_challenge_report(self, filepath: str = "challenge_export_report.csv"):
        try:
            conn = sqlite3.connect(DB_PATH)
            df_trades = pd.read_sql_query("SELECT * FROM challenge_trades", conn)
            df_trades.to_csv(filepath, index=False)
            conn.close()
            return True, filepath
        except Exception as e:
            return False, str(e)