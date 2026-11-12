# Enhanced Deriv AI Engine — Institutional Upgraded Edition

Reference documentation for local setup, architecture, trading style profiles, strategy logic, risk controls, the compounding challenge, the backtesting engine, operational safety, and the repository map.

> **Trading Risk Warning:** This is an experimental quantitative trading engine and local web dashboard prototype. It is not financial advice and does not guarantee profitability. Always start in **data-only mode** (`start_dashboard.bat`). Do not enable live order dispatch (`--enable-live-trading`) until your broker account, MT5 terminal configuration, symbol specifications, and risk limits have been independently verified on a demo account.

---

## 1. Project Scope & Architecture Overview

The engine is a multi-timeframe quantitative trading engine that connects locally to a MetaTrader 5 (MT5) terminal running Deriv synthetic volatility indices: `Volatility 10 Index`, `Volatility 25 Index`, `Volatility 50 Index`, and `Volatility 100 Index`.

Key points:

- **No Expert Advisor required.** The engine talks to MT5 directly through the official `MetaTrader5` Python package (`mt5_bridge.py`).
- **Market data comes from MT5 only.** Candles are loaded from MT5 history and updated from live bid quotes.
- **Seven timeframes:** `3m`, `5m`, `15m`, `30m`, `h1`, `h4`, `d1`, with EMA, RSI, ATR, ADX, Bollinger Bands, Stochastic, CPR and candle-pattern labels.
- **Three trading style profiles:** `SCALPING`, `INTRADAY` (default), `SWING`.
- **Compounding Challenge tracker:** a 5%-per-step growth tracker with drawdown limit, high-water mark, and consecutive-loss circuit breaker, stored in SQLite.
- **Trade telemetry:** every automated trade is logged to SQLite (`trade_telemetry`) and a CSV, including the strategy paths that triggered it.
- **Local dashboard:** HTTP server (default `127.0.0.1:8080`) plus a WebSocket server (default `127.0.0.1:8765`) streaming market, account, signal and challenge state.

---

## 2. Current Implementation

### Runtime architecture

`Enhanced_Derive_AI_Engine.py` is a single-process application with background threads for:

1. **MT5 monitor:** loads historical candles per timeframe and polls each supported symbol's live bid once per second. Each cycle also refreshes account state, runs trade management, and syncs closed trades from MT5 history.
2. **WebSocket server** on `127.0.0.1:8765` (default) that broadcasts state to the browser.
3. **HTTP dashboard server** on `127.0.0.1:8080` (default).

The MT5 terminal must be running, logged in, connected to the broker, and have the four exact symbol names enabled. If history is missing, the terminal may need to download more.

Set `DASHBOARD_PORT` to run a second instance. The WebSocket port is `DASHBOARD_PORT + 1` for any value other than 8080 (e.g. `DASHBOARD_PORT=8081` → HTTP 8081, WebSocket 8082). The `.bat` launchers use 8081.

The HTML pages load Chart.js from jsDelivr, so an internet connection is needed for charts unless the dependency is bundled locally.

### Dashboard pages and routes

| URL | Purpose |
|---|---|
| `/` | Main live dashboard (`index.html`). |
| `/tracker` | Day tracker (`tracker.html`). |
| `/tradelog` | Trade log and performance page (`tradelog.html`). |
| `/api/tradelog-data` | Recent real deal history from the connected MT5 terminal. |
| `/api/control` (`POST`) | Dashboard Start / Pause / Stop control. |
| `/api/execute-trade` (`POST`) | Validates account state, symbol, volume, stop/target, margin and monetary risk, then sends a market order to MT5. |
| `/agent_state.json` | No-cache JSON of the latest **in-memory** engine state; also used by the dashboard's polling fallback. |

The browser streams state over WebSocket and falls back to polling `agent_state.json`. The server only accepts origins `http://localhost:<port>` and `http://127.0.0.1:<port>`, and POST endpoints also check Host and Origin. These checks are defense-in-depth for a local tool, **not authentication**. Never expose the app through a LAN, reverse proxy, or public tunnel.

The `agent_state.json` file that may exist on disk is only a static sample snapshot; the HTTP route serves live in-memory state, not the file.

### Dashboard UI

`index.html` + `style.css` form a compact, non-scrolling trading-desk layout:

- Header with symbol selector, status badges and a light/dark theme toggle (the theme is applied via `data-theme` on the page).
- A macro bar with per-timeframe trend status, an account metrics card, and market alerts with Buy / Sell buttons.
- A left column with the main price chart (EMAs, Bollinger Bands, CPR levels) plus RSI and Stochastic oscillator charts.
- A right column with the **Compounding Challenge card**, a forecast panel, the **AI Decision Audit** (regime, score, active paths, reasoning) and the confluence checklist.

Buy/Sell buttons are disabled unless the engine runs with live trading enabled.

### Market data and indicators

The engine keeps separate OHLC caches for each supported symbol and timeframe. It calculates EMA 20/30/100/200, RSI 14, ATR 14, ADX 14, Bollinger Bands (length 16, std dev 1.986), Stochastic (14, 3), candle-pattern labels, previous-day open/close, and CPR pivot/support/resistance levels. If fewer than five candles exist for a timeframe, indicators are reported as unavailable rather than synthesized. Candle caches are in memory and are not a durable market-data store.

---

## 3. Trading Style Profiles

Select the style with the `--style` flag (`SCALPING`, `INTRADAY`, `SWING`). The `TRADING_STYLE` environment variable sets the initial value, but the `--style` flag (default `INTRADAY`) overrides it at startup.

| Setting | SCALPING | INTRADAY | SWING |
|---|---|---|---|
| Primary timeframe | 3m | 5m | h1 |
| Confluence (higher) timeframe | 5m | h1 | d1 |
| Stop-loss (× ATR) | 1.0 | 1.5 | 2.5 |
| Take-profit (× ATR) | 1.5 | 3.0 | 6.0 |
| Stagnation cutoff | 90 s | 180 s | 3600 s |
| Minimum consensus score | 45 | 55 | 65 |
| Minimum ADX (see note) | 22 | 25 | 28 |
| Partial scale-out (× ATR profit) | 1.5 | 2.0 | 3.0 |

> **Note:** `min_adx` is defined in each profile and `validate_trade_quality()` exists, but that function is **not currently called** by the entry path. Only the minimum consensus score is enforced.

Active-trade stop/target multiples are widened by 1.2× when volatility expansion is detected (ADX > 30, or current ATR more than 1.3× its 20-bar average).

---

## 4. Strategy Evaluator

`evaluate_multi_path_strategy()` builds a weighted **consensus score** from signal paths. Positive scores favor BUY, negative favor SELL. The action fires when `score >= +min_consensus_score` (BUY) or `score <= -min_consensus_score` (SELL) for the active profile.

**Regime gating:** a market is "trending" when ADX ≥ 22 and Bollinger width is not in a squeeze (`width <= 4 × ATR`), and "ranging" when ADX < 20 or in a squeeze.

**Style-specific paths**

- **SCALPING:** oversold bounce / overbought rejection (Stochastic + RSI extremes) and bullish/bearish micro-impulse candles.
- **SWING:** higher-timeframe trend alignment with price vs EMA20, plus price vs daily CPR pivot.
- **INTRADAY** (default), trending regime:
  - Path A: trend pullback (higher-TF trend, price vs EMA20, RSI band, Stochastic).
  - Path E: multi-timeframe trend alignment (3m/5m/15m + higher TF; 3m is omitted when sub-5m confluence is disabled, as in backtests).
- **INTRADAY**, ranging regime:
  - Path B: Bollinger breakout/breakdown (not in a squeeze).
  - Path H: inside-bar breakout.
  - Path C: CPR support/resistance rejection.
  - Path D: extreme RSI/Stochastic reversal.
  - Path G: wick rejection / pinbar.

**Paths applied across styles**

- Path F: fair-value-gap retest.
- Path I: 15m market-open bias and opening-range trap fade.
- Path J: Keltner channel breakout (EMA20 ± 2 ATR with Stochastic and ADX filters).
- Path K: EMA ribbon velocity expansion (trending regime only).
- Path L: Stochastic double-bottom / double-top reversal.

**Session weighting:** the final score is multiplied by a UTC-hour volatility curve: 1.20 (07:00–17:00), 1.10 (05:00–07:00 and 17:00–20:00), 0.95 (20:00–23:00 and 02:00–05:00), 0.85 otherwise. The multiplier is forced to 1.0 under unit tests or when `TEST_MODE=1`.

> **Unused helpers:** `get_telemetry_path_adjustments()` (per-path win-rate multipliers from telemetry) and `run_walk_forward_validation()` (a simple in-/out-of-sample ratio check) are defined but not wired into the decision path. `run_walk_forward_validation` returns a fixed placeholder when fewer than 30 candles are available.

---

## 5. Trading Modes, Risk Controls & Position Management

### Modes

- **Default / data-only:** `LIVE_TRADING_ENABLED` is false and status is `DATA ONLY`. Manual buttons are disabled and `/api/execute-trade` returns HTTP 403. The decision audit is still computed for display.
- **Manual live:** start with `--enable-live-trading` (or `start_manual_trading.bat`, type `LIVE`). Each click asks for a lot volume and a browser confirmation. The server recalculates stop/target from the fresh MT5 quote and ATR, then rechecks open positions, margin, volume rules, minimum stop distance, and risk.
- **Automated entries:** require `--enable-live-trading` **and** `--enable-auto-trading` (or `start_automated_trading.bat`, type `AUTO LIVE`). Passing `--enable-auto-trading` alone is rejected at startup.
- **Paused / stopped:** dashboard controls change in-memory status only and are not persisted across restarts.

### Automated entry flow

1. A decision is evaluated **once per newly completed primary-timeframe candle** per symbol, using completed-candle indicator values. The first completed candle after startup only arms the engine.
2. A signal must meet the profile's consensus threshold.
3. **Spread guard:** the order is skipped if the current spread exceeds `0.25 × ATR`.
4. Only one open position is allowed (`MAX_OPEN_POSITIONS = 1`) among positions with magic number `123456`.
5. `MT5Bridge.place_market_order()` sizes volume as the largest broker-valid lot (binary search over lot steps) whose calculated stop-loss loss and required margin fit `MAX_RISK_PER_TRADE_PERCENT` and free margin. If even the minimum lot exceeds the cap or margin, the order is rejected.
6. The trade is logged to `trade_telemetry` and the CSV.

### Position management (live runs, magic `123456`, supported symbols)

- **Partial scale-out:** closes 50% of the volume when profit reaches the profile's scale-out ATR multiple (requires at least 0.02 lots).
- **Breakeven:** once profit reaches 1× ATR, the stop moves to entry plus a 0.2 × ATR buffer.
- **Chandelier trail:** at 2× ATR profit, the stop trails 1.5 × ATR from the highest high (BUY) or lowest low (SELL) of the last five candles, moving only when it improves by more than 0.1 × ATR.
- **Stagnation cut:** after the profile's stagnation time, a losing trade is closed if price has breached EMA20 by 0.5 × ATR, or RSI is below 42 (BUY) / above 58 (SELL).

### Risk configuration

| Setting | Behavior |
|---|---|
| `MAX_RISK_PER_TRADE_PERCENT` | Maximum calculated stop-loss loss as % of balance. Default `1`. Enforced on manual and automated orders. |
| `MAX_DAILY_LOSS_PERCENT` | Parsed and validated (default `2`), but **currently not enforced**: `check_daily_loss_limit()` is bypassed and always allows trading. |

Both settings accept values above 0 and up to 100. Invalid values stop the engine from starting.

> **Limitations:** Orders are reported successful only after MT5 accepts them; this does not guarantee a fill or remove slippage, gap, or liquidity risk. Daily boundaries use the computer's local time or UTC depending on the component and should be reconciled with the broker's day before relying on them.

---

## 6. Compounding Challenge System

Implemented in `compounding_challenge.py` and stored in `trade_telemetry.sqlite`.

| Parameter | Value |
|---|---|
| Initial capital | $200.00 |
| Step growth target | +5% per step |
| Max daily drawdown | 3% |
| Max consecutive losses | 3 |

**Tables**

- `challenge_daily_status`: one row per UTC day (day number, start/current balance, high-water mark, step, target, drawdown limit, progress %, status, consecutive losses, halt flag).
- `challenge_trades`: one row per challenge trade (ticket, symbol, action, lots, entry/exit, PnL, status, exit reason).

**Behavior**

- `CompoundingChallengeTracker.evaluate_day()` computes current equity, high-water mark, step progression, progress %, and halts. Status becomes `HALTED_MAX_DRAWDOWN` or `HALTED_CIRCUIT_BREAKER`.
- On a new UTC day, the tracker rolls over: the previous ending balance becomes the new start, and the step resets to 1.
- `calculate_dynamic_lot_size()` offers ATR-based sizing (2× ATR stop, broker minimum lot respected).
- `sync_closed_challenge_trades()` scans the last 7 days of MT5 deals and closes matching open challenge tickets with final PnL.
- `export_challenge_report()` writes challenge trades to CSV.
- On every state broadcast, the engine evaluates the challenge using the live MT5 balance/equity. **If it reports `halt_trading`, the engine sets `AUTO_TRADING_ENABLED = False` in memory**, stopping further automated entries for that run.

**Seeding:** run `python seed_challenge.py` to create the tables and insert today's Day 1 / Step 1 record ($200 start, $210 target, $194 drawdown floor). It uses `INSERT OR REPLACE` on the date, so re-running on the same day overwrites that day's row.

> The dashboard fallback balance used when MT5 returns nothing is `100111.59` (the in-code comment says `200.0`). Check that the challenge card is showing live MT5 values before trusting it.

---

## 7. Python Installation and Local Setup (Windows)

### Prerequisites

- Windows 10/11 and PowerShell.
- Python 3.12 recommended (3.12.10 was used to validate this workspace; other versions are not certified).
- Internet access to install packages and load Chart.js from jsDelivr.
- A logged-in MT5 terminal (the `MetaTrader5` package is Windows-only).

```powershell
winget install --id Python.Python.3.12 --exact --scope user --accept-package-agreements --accept-source-agreements
python --version
```

### Virtual environment and dependencies

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .
```

Always use `.venv\Scripts\python.exe` for this project.

### Packages (declared in `pyproject.toml`)

| Package | Purpose | Constraint |
|---|---|---|
| `pandas` | OHLC time series, CSV/SQL loading | `>=2.0` |
| `pandas-ta` | EMA, RSI, ATR, ADX, BB, Stochastic | Unpinned |
| `websockets` | Browser-facing WebSocket server | Unpinned |
| `MetaTrader5` | Local terminal API | `>=5.0.45` |

Versions observed in this workspace (not pinned requirements): pandas 3.1.1, pandas-ta 0.4.71b0, websockets 17.4, NumPy 2.2.6. There is no lock file yet.

---

## 8. Run, Verify, and Stop

### Safe local run

```powershell
.\.venv\Scripts\python.exe .\Enhanced_Derive_AI_Engine.py
```

Open <http://localhost:8080>. This mode streams market data and does not enable order dispatch. Stop with **Ctrl+C**.

### Launchers

| File | Mode | Dashboard |
|---|---|---|
| `start_dashboard.bat` | Data-only | <http://localhost:8081> |
| `start_manual_trading.bat` (type `LIVE`) | Manual confirmed orders; auto entries off | <http://localhost:8081> |
| `start_automated_trading.bat` (type `AUTO LIVE`) | Automated strategy entries | <http://localhost:8081> |

### Command-line options

```powershell
.\.venv\Scripts\python.exe .\Enhanced_Derive_AI_Engine.py --style SCALPING --enable-live-trading --enable-auto-trading
```

| Flag | Meaning |
|---|---|
| `--style {SCALPING,INTRADAY,SWING}` | Trading profile (default `INTRADAY`). |
| `--enable-live-trading` | Allows order dispatch and position management. |
| `--enable-auto-trading` | Allows strategy-generated entries (requires live trading). |

### Enable live order dispatch (advanced)

```powershell
$env:MT5_TERMINAL_PATH = 'C:\Program Files\MetaTrader 5\terminal64.exe'
$env:MT5_SYMBOL = 'Volatility 25 Index'
$env:MAX_RISK_PER_TRADE_PERCENT = '1'
$env:MAX_DAILY_LOSS_PERCENT = '2'
.\.venv\Scripts\python.exe .\Enhanced_Derive_AI_Engine.py --enable-live-trading
```

Each manual order requires a lot volume validated against the symbol's broker minimum, maximum, and step, plus a browser confirmation. The stop/target are anchored to the fresh MT5 bid/ask when the request is processed.

### Tests and basic checks

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q .\Enhanced_Derive_AI_Engine.py .\mt5_bridge.py .\backtest.py .\compounding_challenge.py .\tests
```

Current tests cover default-mode trade suppression, local POST origins, risk caps, MT5 bridge behavior with mocks, and backtest logic. They do not simulate Deriv, a real MT5 terminal, browser rendering, or broker execution.

To confirm listeners while the app runs:

```powershell
Get-NetTCPConnection -State Listen -LocalPort 8080,8765
```

Both should bind to `127.0.0.1`.

---

## 9. Historical MT5 Backtest

`backtest.py` replays the strategy (it imports the engine's evaluator) across the four indices using MT5 3m, 5m, 15m, 1h and 1d candles.

- Default starting balance $1,000, 1% risk per trade, one open portfolio position.
- Signals use completed bars; entries use the next 5m bar open.
- If a candle touches both stop and target, the **stop is assumed first**.
- Historical spread is applied to ask-side prices; MT5 contract calculations estimate P/L and margin. Commission and slippage are **not** included.
- ATR trailing/breakeven and early exits are checked only at 5m closes, effective from the next bar.
- It uses whatever history is available, reports requested vs actual dates, starts at the latest common history date plus a 30-day indicator warm-up, and refuses to run if no usable period remains.
- The daily-loss reset is evaluated at UTC midnight and may differ from the broker's day.

To extend history: raise **MT5 > Tools > Options > Charts > Max bars in chart**, then open each symbol/timeframe chart and press **Home** repeatedly.

### Options

| Option | Purpose |
|---|---|
| `--profile` | Strategy profile (e.g. `scalp`). |
| `--years` | Years of history to request. |
| `--start`, `--end` | Explicit UTC date range. |
| `--balance` | Starting balance (account currency). |
| `--risk-percent` | Risk cap per trade. |
| `--min-consensus-score` | Override the score threshold for experiments. |
| `--symbols` | Limit to specific supported symbols. |
| `--output` | Output directory for the JSON summary and CSV trade list. |

```powershell
.\.venv\Scripts\python.exe .\backtest.py --years 2 --balance 1000 --risk-percent 1 --output .\backtest_results
```

The backtest is read-only with respect to trading.

### Experimental scalping profile

The `scalp` profile is backtest-only and does not change live settings. It enters on completed 5m candles, requires 15m and 1h EMA trends to agree with the signal, and uses a 1-ATR stop with a 2R target. It moves the stop to entry at +1R and starts a 1-ATR trail at +1.5R. Reports include blocked signals, planned reward:risk, realized R-multiple, and trade frequency. The 3–4 trades/day figure is a target to evaluate, not a guaranteed rate. Compare on separate out-of-sample history before changing any live setting.

```powershell
.\.venv\Scripts\python.exe .\backtest.py --profile scalp --years 2 --balance 1000 --risk-percent 1 --output .\backtest_scalp_results
```

---

## 10. Telemetry, Configuration and Local Files

| Setting / file | Purpose |
|---|---|
| `MT5_TERMINAL_PATH` | Optional path to `terminal64.exe`; auto-discovery if unset. |
| `MT5_SYMBOL` | Default dashboard symbol (one of the four supported); default `Volatility 25 Index`. |
| `TRADING_STYLE` | Initial style from the environment (overridden by `--style`). |
| `DASHBOARD_PORT` | HTTP port; WebSocket uses `DASHBOARD_PORT + 1` unless 8080. |
| `MAX_RISK_PER_TRADE_PERCENT` | Per-trade risk cap; default `1`. |
| `MAX_DAILY_LOSS_PERCENT` | Validated but not currently enforced; default `2`. |
| `trade_telemetry.sqlite` | Tables `trade_telemetry` (per-trade style, regime, score, strategies, modifications, SL/TP, exit reason), `challenge_daily_status`, `challenge_trades`. Columns are auto-migrated on startup. |
| `multi_timeframe_trade_log.csv` | Header file created by the engine if missing; trade rows are written alongside SQLite telemetry. |
| `agent_state.json` | Static sample snapshot; the HTTP route serves live in-memory state. |
| `credentials.json`, `token.pickle` | Legacy files, ignored by Git, not needed for MT5 data. |

`.gitignore` excludes the virtual environment, credentials, runtime account/order/history files, logs, and Python caches. Never commit secrets or private trading data.

---

## 11. Known Issues & Cleanup Items

These reflect the current code and are good candidates for future work:

1. **Daily loss limit is bypassed.** `check_daily_loss_limit()` always returns allowed. `MAX_DAILY_LOSS_PERCENT` has no effect until this is restored.
2. **`min_adx` filter is not applied.** `validate_trade_quality()` is defined but unused.
3. **Unused helpers.** `get_telemetry_path_adjustments()` and `run_walk_forward_validation()` are not wired into decisions.
4. **Duplicate definitions in `compounding_challenge.py`.** The constants, `init_challenge_db`, `sync_closed_challenge_trades` and `CompoundingChallengeTracker` are each defined twice (the later definition wins). `mt5_bridge.py` also defines `close_position` twice.
5. **Challenge fallback balance** in the broadcast code (`100111.59`) does not match its comment (`200.0`).
6. **Challenge tracker re-initialization.** The tracker is rebuilt on every broadcast with the live balance as its initial capital; verify that this matches the intended compounding behavior.
7. **Encoding.** A non-UTF-8 character in the decision-audit reasoning string (`Threshold: ±{min_score}`) should be re-saved as UTF-8.
8. **Prototype pages.** `tracker.html` and `tradelog.html` show static example KPIs until real closed-trade data is connected.
9. **Time zones.** Daily boundaries mix local time and UTC across components; reconcile with the broker's server day.

### Roadmap

**Phase 1 — Paper trading and backtesting:** explicit paper mode that cannot write to MT5; tick-level replay and validation against MT5 tester results; replay/live parity tests; clear labeling of simulated data.

**Phase 2 — Risk and order safeguards:** restore and test the daily loss limit; agree on daily reset timezone and P&L definition; order idempotency, lifecycle states, expiry and audit logging; enforce the ADX/quality filter.

**Phase 3 — Connection recovery:** stale-feed detection, bounded reconnect/backoff, graceful shutdown, port-conflict reporting; avoid placeholder values in decisions.

**Phase 4 — Dashboard reliability:** verify responsive behavior; make data-only/live/stale states unambiguous; remove static metrics; bundle Chart.js for offline use.

**Phase 5 — Trade history and analytics:** one canonical closed-trade record sourced from confirmed MT5 fills; rebuild daily summaries, win rate, drawdown and growth curves from it with test fixtures.

**Open decisions:** supported Deriv account/environment; trading-day definition; approved volume/margin/max-open-risk/kill-switch limits; authoritative MT5 bridge schema; offline operation; supported browsers and displays; whether strategy parameters live in the UI, environment, or versioned config.

---

## 12. Complete Repository Map

| Path | Responsibility |
|---|---|
| `Enhanced_Derive_AI_Engine.py` | **Core engine:** candle caches, MT5 tick processing, indicators, multi-path strategy evaluator, auto-entry, trade management, SQLite/CSV telemetry, challenge integration, HTTP and WebSocket servers. |
| `Enhanced_Derive_AI_Engine_New.py` | Experimental alternative implementation (not included in the latest review). |
| `mt5_bridge.py` | **MT5 bridge:** connection lifecycle, account snapshots, market data, risk-sized market orders, position modify/close, deal history. |
| `compounding_challenge.py` | **Challenge tracker:** step targets, drawdown and circuit breaker, `evaluate_day`, trade logging, MT5 closed-deal sync, CSV export. |
| `seed_challenge.py` | Creates challenge tables and seeds today's Day 1 record in `trade_telemetry.sqlite`. |
| `backtest.py` | Historical replay backtester producing JSON and CSV reports. |
| `index.html` | Main live dashboard. |
| `index3.html` | Alternative dashboard layout. |
| `tracker.html` | Challenge / day tracker page. |
| `tradelog.html` | Trade log and performance page. |
| `style.css` | Dashboard theme (dark default, light via `data-theme`), compact non-scrolling layout. |
| `start_dashboard.bat` | Launcher: data-only mode. |
| `start_manual_trading.bat` | Launcher: manual live mode (`--enable-live-trading`). |
| `start_automated_trading.bat` | Launcher: automated live mode (`--enable-live-trading --enable-auto-trading`). |
| `pyproject.toml` | Project metadata, dependencies, and the `run-engine` entry point. |
| `trade_telemetry.sqlite` | Runtime database: trade telemetry and challenge tables. |
| `multi_timeframe_trade_log.csv` | Runtime CSV trade log. |
| `agent_state.json` | Sample state snapshot (live state is served from memory). |
| `tests/test_startup_safety.py` | Startup safety, origin checks, risk validation tests. |
| `tests/test_mt5_bridge.py` | MT5 bridge tests with mocks. |
| `tests/test_backtest.py` | Backtest logic tests. |
| `README.md` | This document. |
