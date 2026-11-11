import sqlite3
import os
from datetime import datetime, timezone

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(PROJECT_DIR, "trade_telemetry.sqlite")

def seed_fresh_challenge(starting_balance=200.00, step_growth_pct=5.0, max_drawdown_pct=3.0):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # 1. Ensure tables exist
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

    # 2. Compute Level 1 metrics based on $200.00 starting capital
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    target_balance = round(starting_balance * (1.0 + (step_growth_pct / 100.0)), 2) # $210.00
    max_dd_limit = round(starting_balance * (1.0 - (max_drawdown_pct / 100.0)), 2)     # $194.00

    # 3. Insert initial Day 1 / Step 1 record
    cursor.execute("""
        INSERT OR REPLACE INTO challenge_daily_status 
        (date_str, day_number, starting_balance, current_balance, high_water_mark, current_step, target_balance, daily_profit, max_drawdown_limit, progress_pct, status, consecutive_losses, halt_triggered)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        today_str,
        1,                  # Day 1
        starting_balance,   # $200.00 starting
        starting_balance,   # $200.00 current
        starting_balance,   # $200.00 high water mark
        1,                  # Step Tier 1
        target_balance,     # $210.00 target
        0.0,                # Daily PnL
        max_dd_limit,       # $194.00 max drawdown limit
        0.0,                # Progress %
        "ACTIVE",           # Status
        0,                  # Consecutive losses
        0                   # Halt flag
    ))

    conn.commit()
    conn.close()

    print(f"✅ Successfully seeded challenge DB!")
    print(f"   Date: {today_str} | Day: 1 | Step: 1 | Balance: ${starting_balance:.2f} | Target: ${target_balance:.2f} | Max DD: ${max_dd_limit:.2f}")

if __name__ == "__main__":
    seed_fresh_challenge()