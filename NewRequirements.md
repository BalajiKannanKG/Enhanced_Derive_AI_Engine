You are an expert Python systems engineer. Write complete, production-ready Python code implementing the following 5 modules for an algorithmic trading engine running locally on a single machine, connecting via MetaTrader 5 (MT5) to Deriv synthetic indices (Volatility 10, 25, 50, 100). 

Output ONLY clean, runnable Python code using standard libraries, sqlite3, numpy, and MetaTrader5. Do not include conversational intros, explanations, or follow-up questions.

---
MODULE 1: SQLite WAL Mode & Concurrency Hardening
- Write a helper function `get_db_connection(db_path, timeout=30.0)` that connects to SQLite, enables Write-Ahead Logging (`PRAGMA journal_mode=WAL;`, `PRAGMA synchronous=NORMAL;`), and sets a 30-second busy timeout to prevent locking errors during concurrent background loops and UI threads.

MODULE 2: Volatility & Technical Indicator Analysis
- Implement `calculate_atr(highs, lows, closes, period=14)`
- Implement `calculate_rsi(closes, period=14)`
- Implement `calculate_macd(closes, fast_period=12, slow_period=26, signal_period=9)` returning macd, signal, and histogram values using numpy/pandas.

MODULE 3: Risk-Reward Ratio (RR) & Profile-Based Scale-Out
- Implement `evaluate_trade_risk_reward(entry_price, stop_loss, target_price, min_rr=1.5)` that computes the R:R ratio, validates against `min_rr`, and calculates a position size multiplier.

MODULE 4: Smart Stagnation Cut & Position Maintenance
- Implement `monitor_and_cut_stagnant_positions(mt5_connection, active_positions, max_stagnation_seconds)` that checks elapsed time vs profitability (-2.0 to 1.0) and automatically dispatches position closures if trades stall.

MODULE 5: Local Automated Execution Guardrails via MT5 API
- Implement `execute_guarded_order(symbol, order_type, lot_size, price, sl, tp, max_allowed_spread_multiplier=1.5)` that checks MT5 symbol info, validates rolling tick spread against spikes, and executes orders via `mt5.order_send()`.
---