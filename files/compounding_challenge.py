# compounding_challenge.py - Complete Institutional Compounding Challenge Tracker & Risk Manager
import os
import pandas as pd
from datetime import datetime, timezone, timedelta
import MetaTrader5 as mt5
from trading_modules import get_db_connection

# Define Eastern Time zone (handling EST / EDT offset safely)
# Alternatively, use zoneinfo.ZoneInfo("America/New_York") if on Python 3.9+
def get_eastern_time():
    utc_now = datetime.now(timezone.utc)
    # Eastern time is UTC-5, or UTC-4 during daylight saving. 
    # Using zoneinfo is recommended for daylight saving accuracy:
    from zoneinfo import ZoneInfo
    return utc_now.astimezone(ZoneInfo("America/New_York"))

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(PROJECT_DIR, "trade_telemetry.sqlite")

INITIAL_CAPITAL = 200.00
STEP_PROFIT_TARGET_PCT = 5.0
MAX_DAILY_DRAWDOWN_PCT = 3.0
MAX_CONSECUTIVE_LOSSES = 3


def init_challenge_db():
    """Initializes dedicated challenge tables in the SQLite telemetry database and auto-migrates columns."""
    conn = get_db_connection(DB_PATH)
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
    
    # Auto-migrate missing columns if table already existed with an older schema
    cursor.execute("PRAGMA table_info(challenge_daily_status)")
    existing_cols = {row[1] for row in cursor.fetchall()}
    required_cols = {
        "current_balance": "REAL",
        "high_water_mark": "REAL",
        "current_step": "INTEGER",
        "target_balance": "REAL",
        "daily_profit": "REAL",
        "max_drawdown_limit": "REAL",
        "progress_pct": "REAL",
        "status": "TEXT",
        "consecutive_losses": "INTEGER",
        "halt_triggered": "INTEGER"
    }
    for col, col_type in required_cols.items():
        if col not in existing_cols:
            try:
                cursor.execute(f"ALTER TABLE challenge_daily_status ADD COLUMN {col} {col_type}")
            except Exception:
                pass

    conn.commit()
    conn.close()


class CompoundingChallengeTracker:
    def __init__(self, initial_capital=INITIAL_CAPITAL, step_growth_rate=STEP_PROFIT_TARGET_PCT, max_daily_loss_pct=MAX_DAILY_DRAWDOWN_PCT):
        self.initial_capital = initial_capital
        self.step_growth_rate = step_growth_rate / 100.0
        self.max_daily_loss_pct = max_daily_loss_pct / 100.0
        init_challenge_db()
        self.state = self.load_state()

    def init_db(self):
        """Initializes dedicated challenge tables in the SQLite telemetry database."""
        init_challenge_db()

    def load_state(self):
        """Loads existing state from database or calculates fresh parameters for today in Eastern Time."""
        conn = get_db_connection(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT date_str, day_number, starting_balance, current_balance, high_water_mark, 
                   current_step, target_balance, status, consecutive_losses 
            FROM challenge_daily_status ORDER BY id DESC LIMIT 1
        """)
        row = cursor.fetchone()
        conn.close()

        now_str = get_eastern_time().strftime("%Y-%m-%d")
        if row:
            db_date = row[0]
            prev_day_num = int(row[1])
            prev_starting_balance = float(row[2])
            prev_ending_balance = float(row[3])
            prev_hwm = float(row[4])
            prev_step = int(row[5])
            prev_target = float(row[6])
            prev_status = row[7]
            prev_losses = int(row[8])
            
            if db_date != now_str:
                # Rollover to a brand new trading day in EST
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
                dd_limit = round(prev_starting_balance * (1.0 - self.max_daily_loss_pct), 2)
                return {
                    "date_str": db_date,
                    "day_number": prev_day_num,
                    "starting_balance": prev_starting_balance,
                    "current_balance": prev_ending_balance,
                    "high_water_mark": prev_hwm,
                    "current_step": prev_step,
                    "current_target_balance": prev_target,
                    "max_drawdown_limit": dd_limit,
                    "status": prev_status,
                    "consecutive_losses": prev_losses
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
        """
        Calculates risk-adjusted lot size with a 2.0x ATR buffer
        while strictly respecting broker min_lot & volume_step constraints.
        """
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
        """Logs individual challenge trades with strict ticket-level deduplication."""
        eastern_now = get_eastern_time()
        date_str = eastern_now.strftime("%Y-%m-%d")
        time_opened = eastern_now.strftime("%H:%M:%S")

        try:
            conn = get_db_connection(DB_PATH)
            cursor = conn.cursor()
            
            # Strict uniqueness check on ticket
            cursor.execute("SELECT id, status FROM challenge_trades WHERE ticket = ?", (ticket,))
            existing = cursor.fetchone()

            if existing:
                cursor.execute("""
                    UPDATE challenge_trades 
                    SET exit_price = ?, pnl = ?, status = ?, exit_reason = ?
                    WHERE ticket = ?
                """, (exit_price, pnl, status, exit_reason, ticket))
            else:
                cursor.execute("""
                    INSERT INTO challenge_trades 
                    (date_str, time_opened, ticket, symbol, challenge_day, step_tier, action, lot_size, entry_price, exit_price, pnl, status, exit_reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    date_str, time_opened, ticket, symbol, 
                    self.state["day_number"], self.state["current_step"], 
                    action, lot_size, entry_price, exit_price, pnl, status, exit_reason
                ))

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
            daily_profit = self.state["current_balance"] - start_bal
            current_target = self.state["current_target_balance"]
            
            while self.state["current_balance"] >= current_target and self.state["status"] == "ACTIVE":
                self.state["current_step"] += 1
                current_target = round(current_target * (1.0 + self.step_growth_rate), 2)
                self.state["current_target_balance"] = current_target

            target_profit = current_target - start_bal
            progress_pct = (daily_profit / target_profit) * 100 if target_profit > 0 else 0.0

            cursor.execute("""
                INSERT OR REPLACE INTO challenge_daily_status 
                (date_str, day_number, starting_balance, current_balance, high_water_mark, current_step, target_balance, daily_profit, max_drawdown_limit, progress_pct, status, consecutive_losses, halt_triggered)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                date_str, self.state["day_number"], start_bal, self.state["current_balance"],
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
        """Evaluates daily progress, drawdown lockout, and step milestones."""
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        
        if today_str != self.state["date_str"]:
            start_bal = self.state["current_balance"]
            self.state["day_number"] += 1
            self.state["starting_balance"] = start_bal
            self.state["high_water_mark"] = start_bal
            self.state["current_step"] = 1
            self.state["current_target_balance"] = round(start_bal * (1.0 + self.step_growth_rate), 2)
            self.state["max_drawdown_limit"] = round(start_bal * (1.0 - self.max_daily_loss_pct), 2)
            self.state["status"] = "ACTIVE"
            self.state["date_str"] = today_str
            self.state["consecutive_losses"] = 0

        start_bal = self.state["starting_balance"]
        
        current_equity = self.state["current_balance"] + simulated_pnl
        daily_profit = current_equity - start_bal
        
        self.state["high_water_mark"] = max(self.state["high_water_mark"], current_equity)
        halt_triggered = current_equity <= self.state["max_drawdown_limit"] or self.state["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES

        if halt_triggered:
            if self.state["status"] == "ACTIVE":
                self.state["status"] = "HALTED_MAX_DRAWDOWN" if current_equity <= self.state["max_drawdown_limit"] else "HALTED_CIRCUIT_BREAKER"
        else:
            # Auto-resume: Clear halt when equity recovers above drawdown limit
            if self.state["status"].startswith("HALTED"):
                self.state["status"] = "ACTIVE"
                self.state["consecutive_losses"] = 0

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
        """Exports all challenge trades and daily statuses to CSV for offline analysis."""
        try:
            conn = get_db_connection(DB_PATH)
            df_trades = pd.read_sql_query("SELECT * FROM challenge_trades", conn)
            df_trades.to_csv(filepath, index=False)
            conn.close()
            return True, filepath
        except Exception as e:
            return False, str(e)


def get_current_challenge_status():
    """Retrieves current challenge status with robust manual/auto classification and Eastern Time date matching."""
    init_challenge_db()
    conn = get_db_connection(DB_PATH)
    cursor = conn.cursor()

    today_str = get_eastern_time().strftime("%Y-%m-%d")
    
    # Get today's status row, falling back to latest if today isn't initialized yet
    cursor.execute("SELECT * FROM challenge_daily_status WHERE date_str = ?", (today_str,))
    row = cursor.fetchone()
    if not row:
        cursor.execute("SELECT * FROM challenge_daily_status ORDER BY id DESC LIMIT 1")
        row = cursor.fetchone()

    # Fetch closed challenge trades
    cursor.execute("""
        SELECT ticket, date_str, exit_reason, pnl, status 
        FROM challenge_trades 
        WHERE status != 'OPEN' AND ticket > 0
    """)
    trade_rows = cursor.fetchall()
    conn.close()

    total_wins = 0
    daily_trades = 0
    daily_wins = 0
    manual_trades = 0
    auto_trades = 0

    processed_tickets = set()
    unique_trade_count = 0

    for ticket, t_date, reason, pnl, status in trade_rows:
        unique_key = f"{ticket}_{t_date}_{pnl}"
        if unique_key in processed_tickets:
            continue
        processed_tickets.add(unique_key)
        unique_trade_count += 1

        reason_str = str(reason or "").upper()
        
        # Robust check for manual vs automated trades based on exit reason / dispatch source
        if "MANUAL" in reason_str or "UI" in reason_str or "DISPATCH" in reason_str or "USER" in reason_str:
            manual_trades += 1
        else:
            auto_trades += 1

        if pnl and pnl > 0:
            total_wins += 1
            if t_date == today_str:
                daily_wins += 1

        if t_date == today_str:
            daily_trades += 1

    total_win_rate = (total_wins / unique_trade_count * 100.0) if unique_trade_count > 0 else 0.0
    daily_win_rate = (daily_wins / daily_trades * 100.0) if daily_trades > 0 else 0.0

    if not row:
        return {
            "day_number": 1,
            "starting_balance": 200.00,
            "current_balance": 200.00,
            "high_water_mark": 200.00,
            "daily_pnl": 0.0,
            "daily_target": 210.00,
            "step_progress": 0.0,
            "max_drawdown_limit": 194.00,
            "status": "ACTIVE",
            "consecutive_losses": 0,
            "halt_trading": False,
            "total_trades": unique_trade_count,
            "daily_trades": daily_trades,
            "manual_trades": manual_trades,
            "auto_trades": auto_trades,
            "win_rate": round(total_win_rate, 1),
            "daily_win_rate": round(daily_win_rate, 1),
            "total_return_pct": 0.0,
            "daily_return_pct": 0.0
        }

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

    total_return_pct = ((current_balance - INITIAL_CAPITAL) / INITIAL_CAPITAL) * 100.0
    daily_return_pct = (daily_pnl / starting_balance) * 100.0 if starting_balance > 0 else 0.0

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
        "total_trades": unique_trade_count,
        "daily_trades": daily_trades,
        "manual_trades": manual_trades,
        "auto_trades": auto_trades,
        "win_rate": round(total_win_rate, 1),
        "daily_win_rate": round(daily_win_rate, 1),
        "total_return_pct": round(total_return_pct, 2),
        "daily_return_pct": round(daily_return_pct, 2)
    }
def sync_external_mt5_mobile_trades():
    """Scans MT5 history for closed mobile/external trades, skips duplicates, maps them to challenge dates, and records them."""
    try:
        conn = get_db_connection(DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute("SELECT ticket FROM challenge_trades")
        recorded_tickets = {row[0] for row in cursor.fetchall()}
        
        utc_from = datetime.now(timezone.utc) - timedelta(days=7)
        deals = mt5.history_deals_get(utc_from, datetime.now(timezone.utc))

        if not deals:
            conn.close()
            return

        new_trades_found = False
        affected_dates = set()

        for deal in deals:
            if deal.entry == mt5.DEAL_ENTRY_OUT:
                ticket = deal.position_id
                if ticket in recorded_tickets:
                    continue

                exit_price = float(deal.price)
                pnl = float(deal.profit + deal.swap + deal.commission)
                exit_reason = "MT5_MOBILE_TP" if pnl > 0 else "MT5_MOBILE_SL"
                status = "CLOSED_PROFIT" if pnl > 0 else "CLOSED_LOSS"
                action = "BUY" if deal.type == mt5.DEAL_TYPE_BUY else "SELL"
                
                deal_dt_utc = datetime.fromtimestamp(deal.time, timezone.utc)
                from zoneinfo import ZoneInfo
                deal_dt_et = deal_dt_utc.astimezone(ZoneInfo("America/New_York"))
                date_str = deal_dt_et.strftime("%Y-%m-%d")
                time_opened = deal_dt_et.strftime("%H:%M:%S")

                cursor.execute("SELECT day_number, current_step FROM challenge_daily_status WHERE date_str = ?", (date_str,))
                row = cursor.fetchone()
                
                if row:
                    challenge_day = row[0]
                    step_tier = row[1]
                else:
                    cursor.execute("SELECT day_number, current_step FROM challenge_daily_status ORDER BY id DESC LIMIT 1")
                    latest = cursor.fetchone()
                    challenge_day = latest[0] if latest else 1
                    step_tier = latest[1] if latest else 1

                entry_price = float(deal.price)

                cursor.execute("""
                    INSERT INTO challenge_trades 
                    (date_str, time_opened, ticket, symbol, challenge_day, step_tier, action, lot_size, entry_price, exit_price, pnl, status, exit_reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    date_str, time_opened, ticket, deal.symbol, challenge_day, step_tier, 
                    action, deal.volume, entry_price, exit_price, pnl, status, exit_reason
                ))
                
                recorded_tickets.add(ticket)
                affected_dates.add(date_str)
                new_trades_found = True

        conn.commit()
        conn.close()

        # EXECUTED ONLY WHEN NEW TRADES ARE SYNCED
        if new_trades_found:
            print(f"Successfully synchronized {len(affected_dates)} new batch(es) of MT5 mobile trades.")
            for d_str in affected_dates:
                update_daily_status_from_live_mt5(d_str)

    except Exception as e:
        print(f"Error syncing external MT5 mobile trades: {e}")
def update_daily_status_from_live_mt5(date_str):
    """Syncs challenge_daily_status directly against live MT5 account balance."""
    if not mt5.initialize():
        print("MT5 initialization failed for live balance sync.")
        return

    acct_info = mt5.account_info()
    if acct_info is None:
        print("Could not retrieve MT5 account info.")
        return

    live_balance = float(acct_info.balance)
    live_equity = float(acct_info.equity)

    conn = get_db_connection(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("SELECT starting_balance FROM challenge_daily_status WHERE date_str = ?", (date_str,))
    row = cursor.fetchone()
    if row:
        starting_bal = float(row[0])
        daily_profit = live_balance - starting_bal
        prog_pct = (daily_profit / starting_bal) * 100.0 if starting_bal > 0 else 0.0

        cursor.execute("""
            UPDATE challenge_daily_status 
            SET current_balance = ?, 
                high_water_mark = MAX(high_water_mark, ?), 
                daily_profit = ?, 
                progress_pct = ?
            WHERE date_str = ?
        """, (live_balance, max(live_balance, live_equity), daily_profit, prog_pct, date_str))
        conn.commit()
        print(f"Updated daily status for {date_str}: Live MT5 Balance = ${live_balance:.2f}, Daily Profit = ${daily_profit:.2f}")
    
    conn.close()
def sync_closed_challenge_trades():
    """Scans MT5 history for closed challenge tickets and updates their final PnL, exit price, and status."""
    try:
        conn = get_db_connection(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT ticket FROM challenge_trades WHERE status = 'OPEN'")
        open_tickets = [row[0] for row in cursor.fetchall()]

        if not open_tickets:
            conn.close()
            return

        utc_from = datetime.now(timezone.utc) - timedelta(hours=1)
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