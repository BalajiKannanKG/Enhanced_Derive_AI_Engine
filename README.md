# Enhanced Deriv AI Engine — Institutional Upgraded Edition

Reference documentation for the local trading engine and dashboard: architecture, runtime behavior, HTTP API, trading style profiles, the legacy multi-path strategy, the **Industrial Scanner v3**, risk controls, the compounding challenge, the MT5 bridge, telemetry, setup, backtesting, known issues, and the repository map.

> **Trading Risk Warning:** This is an experimental quantitative trading engine and local web dashboard prototype. It is not financial advice and does not guarantee profitability. Always start in **data-only mode** (`start_dashboard.bat`). Do not enable live order dispatch (`--enable-live-trading`) until your broker account, MT5 terminal configuration, symbol specifications, and risk limits have been independently verified on a demo account.

**Review basis:** this revision was written on 2026-11-14 by reading the uploaded source files line by line: `Enhanced_Derive_AI_Engine.py`, `mt5_bridge.py`, `compounding_challenge.py`, `industrial_scanner.py`, `index.html`, `style.css`, and a copy of `trade_telemetry.sqlite`. Files that are referenced but were **not** part of that review are marked *(not re-verified)* in the text and in the [repository map](#18-complete-repository-map). Statements below describe what the code **does today**, not what it was designed to do.

---

## Table of Contents

0. [Read this first: verified caveats](#0-read-this-first-verified-caveats)
1. [What changed in this revision](#1-what-changed-in-this-revision)
2. [Project scope and architecture](#2-project-scope-and-architecture)
3. [Runtime behavior](#3-runtime-behavior)
4. [Dashboard (`index.html` + `style.css`)](#4-dashboard-indexhtml--stylecss)
5. [HTTP and WebSocket API reference](#5-http-and-websocket-api-reference)
6. [Trading style profiles](#6-trading-style-profiles)
7. [Legacy multi-path strategy evaluator](#7-legacy-multi-path-strategy-evaluator)
8. [Industrial Scanner v3](#8-industrial-scanner-v3)
9. [Trading modes, risk controls and position management](#9-trading-modes-risk-controls-and-position-management)
10. [Compounding Challenge system](#10-compounding-challenge-system)
11. [MT5 bridge reference](#11-mt5-bridge-reference)
12. [Telemetry and database](#12-telemetry-and-database)
13. [Installation and local setup (Windows)](#13-installation-and-local-setup-windows)
14. [Run, verify, and stop](#14-run-verify-and-stop)
15. [Historical MT5 backtest](#15-historical-mt5-backtest)
16. [Configuration reference](#16-configuration-reference)
17. [Known issues and roadmap](#17-known-issues-and-roadmap)
18. [Complete repository map](#18-complete-repository-map)
- [Appendix A: Scanner output reference](#appendix-a-scanner-output-reference)
- [Appendix B: Scanner validation results](#appendix-b-scanner-validation-results)
- [Appendix C: Optional scanner integration edits (not applied)](#appendix-c-optional-scanner-integration-edits-not-applied)

---

## 0. Read this first: verified caveats

These are the findings that most affect safe operation. Each is explained in detail in the section linked.

| # | Finding | Impact | Details |
|---|---|---|---|
| 1 | **Automated entries and position management are not wired in.** `check_and_auto_trigger_trade()` and `manage_active_trades()` are defined but **never called** anywhere in the uploaded code. | `--enable-auto-trading` sets a flag, but no automated entry, scale-out, breakeven, trailing stop or stagnation cut runs. The sample database contains **no** automated trades. | [§9](#9-trading-modes-risk-controls-and-position-management) |
| 2 | **Manual orders are one click, with no confirmation dialog.** `executeManualTrade()` sends the order as soon as BUY/SELL is pressed, using the lot-size box. | Older documentation described a lot prompt and a browser confirmation; neither exists in the current page. | [§4](#4-dashboard-indexhtml--stylecss), [§9](#9-trading-modes-risk-controls-and-position-management) |
| 3 | **Scanner "Execute" buttons ignore the setup's own stop and target.** The scan modal's *Execute* and per-setup *Execute this setup* buttons call `executeAiTrade()` → `executeManualTrade()`, which always sends `1.5 × ATR` / `3.0 × ATR` (selected timeframe) distances. | The SL/TP shown in the modal are advisory; the order placed uses different levels. | [§4](#4-dashboard-indexhtml--stylecss), [§8.12](#812-dashboard-integration) |
| 4 | **`MAX_RISK_PER_TRADE_PERCENT` is not applied to the volume the dashboard sends.** `/api/execute-trade` validates `risk_pct` but uses the UI-supplied `volume` as is. The risk, margin and minimum-stop checks live in `MT5Bridge.place_market_order()`, which the engine no longer calls. | Position size is whatever the lot box says (observed in the sample DB: up to 5.0 lots). Any enforcement now depends on `trading_modules.execute_guarded_order()`, which was not available for review. | [§9](#9-trading-modes-risk-controls-and-position-management), [§11](#11-mt5-bridge-reference) |
| 5 | **`trading_modules.py` is imported but missing from the reviewed set.** It supplies `get_db_connection`, `calculate_atr`, `evaluate_trade_risk_reward`, `monitor_and_cut_stagnant_positions`, `execute_guarded_order`. | The engine and `compounding_challenge.py` cannot start without it. ATR values and order guards come from it. | [§17](#17-known-issues-and-roadmap) |
| 6 | **`--style` does not fully switch the strategy profile.** `evaluate_multi_path_strategy(..., profile=CURRENT_PROFILE)` binds its default at import time; callers never pass a profile. | `--style SWING` changes the style *branch* but the score threshold still comes from the import-time profile (INTRADAY, 55). | [§6](#6-trading-style-profiles) |
| 7 | **Daily loss limit is bypassed** (`check_daily_loss_limit()` always returns allowed) and **`min_adx` is never enforced** (`validate_trade_quality()` is never called). | `MAX_DAILY_LOSS_PERCENT` has no effect. | [§9](#9-trading-modes-risk-controls-and-position-management) |
| 8 | **Scanner results are not evidence of an edge.** Deriv Volatility indices are generated by a random-number generator; on pure random-walk data the scanner's expectancy is about 0R. | Treat signals, grades and "confidence" as an alignment ranking, not a win probability. Validate on your own history with the included backtest helpers. | [§8.13](#813-validation-and-honest-limits), [Appendix B](#appendix-b-scanner-validation-results) |

---

## 1. What changed in this revision

### 1.1 Code changes documented here

| File | Change |
|---|---|
| `industrial_scanner.py` | Replaced the 152-line v1 scanner (fixed point bonuses, ~6 indicators) with the **Industrial Scanner v3** (~1,300 lines): regime detection, 10 weighted factors, 20+ candlestick patterns, regular/hidden divergence, convergence and squeeze logic, Supertrend, Fibonacci, 12 chart patterns (double top/bottom, head & shoulders, wedges, triangles, flags, pennants) with measured-move targets, six named setup types ranked per timeframe, structure-aware SL/TP, veto gates, grades, and two backtest helpers. Same function signature plus one optional argument; all v1 output keys preserved. Pure `numpy`; no `pandas_ta`. See [§8](#8-industrial-scanner-v3). |
| `index.html` | AI scan modal now renders **detected signals** (candle patterns, chart patterns with status and target, divergences) and a **ranked list of trade opportunities** with an *Execute this setup* button each. Helper functions `esc()` and `renderOpportunities()` added. See [§4](#4-dashboard-indexhtml--stylecss). |
| `Enhanced_Derive_AI_Engine.py`, `mt5_bridge.py`, `compounding_challenge.py` | **Unchanged** in this revision. They already call the scanner through `handle_ai_vision_scan()`. |

### 1.2 Corrections to the previous README

The previous README no longer matched the code in the following places. All are corrected below.

| Previous statement | What the code actually does |
|---|---|
| `MAX_OPEN_POSITIONS = 1`; one open position allowed. | `MAX_OPEN_POSITIONS = 5` (engine, dashboard "Lock: 0/5" badge, and a hard-coded 5 in `MT5Bridge.place_market_order()`). |
| Automated entries and position management run each cycle. | The functions exist but are never invoked ([§9](#9-trading-modes-risk-controls-and-position-management)). |
| Auto-entry sizes volume with `MT5Bridge.place_market_order()` (binary-search lot sizing). | Auto-entry (and `/api/execute-trade`) use `trading_modules.execute_guarded_order()`; lot size comes from `CompoundingChallengeTracker.calculate_dynamic_lot_size()` with a **0.50 lot floor**. `place_market_order()` is unused by the engine. |
| Manual orders ask for a lot volume and a browser confirmation. | One click, no confirmation; lot taken from the dashboard input box (default 0.50). |
| The server recalculates stop/target from fresh quote and ATR. | The dashboard always sends `sl_distance`/`tp_distance` (1.5×/3.0× ATR of the **selected dashboard timeframe**); the server anchors them to the fresh quote. Profile ATR multiples are used only if the distances are absent. |
| Challenge day rolls at UTC midnight. | `load_state()`, `log_challenge_trade()` and `get_current_challenge_status()` use **US Eastern time**; `evaluate_day()` compares a **UTC** date. See [§10](#10-compounding-challenge-system). |
| Broadcast evaluates the challenge with live MT5 balance/equity. | `broadcast_state_update()` reads the challenge from the database; the **monitor loop** (once per second) evaluates it with live balance/equity. |
| Challenge fallback balance is `100111.59`. | That value no longer exists. Fallbacks are `200.00`. |
| `compounding_challenge.py` and `mt5_bridge.py` contain duplicate definitions. | Not in the reviewed versions: each function, class and constant is defined once. |
| Dashboard routes: `/`, `/tracker`, `/tradelog`, `/api/tradelog-data`, `/api/control`, `/api/execute-trade`, `/agent_state.json`. | Also `/api/ai-vision-scan`, `/api/modify-position`, `/api/close-position` ([§5](#5-http-and-websocket-api-reference)). |
| Dependencies: `pandas`, `pandas-ta`, `websockets`, `MetaTrader5`. | Also required: `trading_modules` (local), `google-genai` (hard import, otherwise unused), and `tzdata` on Windows (for `zoneinfo`). See [§13](#13-installation-and-local-setup-windows). |
| `TRADING_STYLE` env var sets the initial style; `--style` overrides. | `--style` has a default (`INTRADAY`) that **always** overrides the env var at startup, so the env var only affects the evaluator's import-time default profile ([§6](#6-trading-style-profiles)). |
| Stagnation cut is a single rule. | Two stacked mechanisms: `trading_modules.monitor_and_cut_stagnant_positions()` first, then the local rule (both dormant, see above). |

---

## 2. Project scope and architecture

The engine is a multi-timeframe quantitative trading engine that connects locally to a MetaTrader 5 (MT5) terminal running Deriv synthetic volatility indices: `Volatility 10 Index`, `Volatility 25 Index`, `Volatility 50 Index`, and `Volatility 100 Index`.

Key points:

- **No Expert Advisor required.** The engine talks to MT5 through the official `MetaTrader5` Python package (`mt5_bridge.py`).
- **Market data comes from MT5 only.** Candles are loaded from MT5 history (300 bars per timeframe) and the newest bar is updated from live **bid** quotes.
- **Seven timeframes:** `3m`, `5m`, `15m`, `30m`, `h1`, `h4`, `d1`, with EMA 20/30/50/100/200, RSI 14, ATR 14, ADX 14, Bollinger Bands, Stochastic, CPR levels and candle-label annotations.
- **Three trading style profiles:** `SCALPING`, `INTRADAY` (default), `SWING`.
- **Two decision engines:** the legacy **multi-path evaluator** (drives the dashboard signal and the dormant auto-entry code) and the **Industrial Scanner v3** (drives the dashboard *AI Vision Scan* button).
- **Compounding Challenge tracker:** a 5%-per-step growth tracker with drawdown limit, high-water mark and consecutive-loss circuit breaker, stored in SQLite.
- **Trade telemetry:** manually dispatched trades are logged to SQLite (`trade_telemetry`) and a CSV.
- **Local dashboard:** HTTP server (default `127.0.0.1:8080`) plus a WebSocket server (default `127.0.0.1:8765`) streaming market, account, signal and challenge state.

### Data flow

```
MT5 terminal ──(MetaTrader5 package)──► MT5Bridge ──► poll_mt5_market_data()  (1 Hz)
                                                           │  ticks → forming candle (bid)
                                                           │  history (300 bars × 7 TF × 4 symbols) → symbol_tf_caches
                                                           ▼
                                                   process_dataframe()  → indicators + last 75 candles per TF
                                                           ▼
                            ┌──────────────────────────────┴──────────────────────────────┐
                            ▼                                                             ▼
         evaluate_multi_path_strategy()  (dashboard signal,              perform_industrial_technical_scan()
         decision audit; auto-entry code is dormant)                     (on demand: POST /api/ai-vision-scan)
                            └──────────────────────────────┬──────────────────────────────┘
                                                           ▼
                           broadcast_state_update() → WebSocket push + /agent_state.json
                                                           ▼
                                                 Browser dashboard (index.html)
                                                           │ BUY/SELL, close, drag SL/TP, AI scan
                                                           ▼
                           POST /api/* → execute_guarded_order() / MT5Bridge → MT5 terminal
```

---

## 3. Runtime behavior

`Enhanced_Derive_AI_Engine.py` is a single-process application with background threads.

### 3.1 Threads

1. **MT5 monitor loop** (`mt5_monitor_loop`), once per second:
   1. `refresh_mt5_state()`: account snapshot (balance, equity, positions, per-symbol quote and volume specs).
   2. `check_and_sync_closed_telemetry_trades()`: for telemetry rows with `exit_reason = 'OPEN'`, scans the last 7 days of MT5 deals and writes `close_time` and an exit reason **inferred from P&L sign** (`TAKE_PROFIT_HIT` if profit > 0, else `STOP_LOSS_HIT`), then triggers a challenge sync.
   3. `compounding_challenge.sync_closed_challenge_trades()`, then a `CompoundingChallengeTracker(initial_capital=<live balance>)` evaluated with `simulated_pnl = equity − balance`.
   4. `poll_mt5_market_data()`: applies the live bid to every timeframe cache and finishes with `broadcast_state_update()`. History is loaded once (retried every 60 s until all timeframes of all symbols return data).
2. **WebSocket server** (default `127.0.0.1:8765`): pushes the state JSON to connected browsers. It ignores anything the browser sends.
3. **HTTP dashboard server** (`ThreadingHTTPServer`, default `127.0.0.1:8080`) on the main thread.

The MT5 terminal must be running, logged in, connected to the broker, and have the four exact symbol names available. If history is missing, the terminal may need to download more.

Set `DASHBOARD_PORT` to run a second instance. The WebSocket port is `DASHBOARD_PORT + 1` for any value other than 8080 (e.g. `8081` → HTTP 8081, WebSocket 8082). The `.bat` launchers use 8081 *(not re-verified)*. The browser derives the WebSocket port with the same rule.

The HTML page loads Chart.js from jsDelivr, so an internet connection is needed for charts unless the dependency is bundled locally.

### 3.2 Market data and candle caches

- Separate in-memory caches per symbol and timeframe, capped at **300 candles** (`iloc[-300:]`). They are not a durable store.
- Each tick (bid) updates the current bucket of every timeframe (open/high/low/close), or opens a new bucket when the time boundary passes.
- `process_dataframe()` runs on every broadcast for every symbol × timeframe (28 frames per second) and returns the **last 75 candles** plus aligned indicator histories. This 75-bar window is the data the legacy evaluator and the scanner receive.
- `data_ready` is `true` only when the cache holds at least **200** candles (so EMA200 is meaningful). With fewer than 5 candles, indicators are reported as unavailable (`trend: FLAT`, ATR 0) rather than synthesized.

### 3.3 Indicators calculated per timeframe

EMA 20/30/50/100/200 (`pandas_ta`), RSI 14, ADX 14, Bollinger Bands (length 16, std 1.986), Stochastic (14, 3), **ATR 14 via `trading_modules.calculate_atr`**, previous-day open/close, bullish pressure (percentage of bullish candles in the 75-bar window), and a per-candle label:

| Label | Rule |
|---|---|
| Doji | body / range < 0.12 |
| Hammer / Bullish Pinbar | bullish and lower wick > 2 × body |
| Shooting Star / Bearish Pinbar | bearish and upper wick > 2 × body |
| Impulse Marubozu | body / range > 0.75 |
| Mild Bullish / Mild Bearish | otherwise, by candle direction |

`trend` is `BULLISH` if EMA30 > EMA100, else `BEARISH` (or `FLAT` when data is missing).

**CPR levels** are computed from the previous daily candle: pivot `(H+L+C)/3`, BC `(H+L)/2`, TC `2·pivot − BC` (swapped if inverted), R1/S1, R2/S2, R3/S3, plus an `is_virgin` flag that is always `true`.

### 3.4 State payload

`broadcast_state_update()` builds one JSON object per cycle and sends it to every WebSocket client (also served at `/agent_state.json`):

| Field | Content |
|---|---|
| `timestamp`, `agent_status`, `trading_style` | Wall-clock time; `DATA ONLY` / `RUNNING` / `PAUSED` / `STOPPED`; active style. |
| `default_symbol`, `supported_symbols` | Default symbol and the four supported symbols. |
| `symbols[<symbol>]` | Per-symbol: `current_price`, `signal`, `cpr`, `timeframes{tf: processed data}`, `decision_audit`, `mt5_symbol` (volume specs and quote availability), `market_data_source: "MT5"`. |
| `current_price`, `signal`, `cpr`, `timeframes`, `decision_audit` | Copy of the default symbol's values (legacy top-level fields). |
| `mt5_account`, `mt5_bridge` | Account snapshot and connection status. |
| `live_trading_enabled`, `auto_trading_enabled` | Mode flags. `auto_trading_enabled` is forced `false` when the challenge reports `halt_trading`. |
| `max_risk_per_trade_percent`, `max_open_positions` | Configured risk cap (default 1) and position cap (5). |
| `challenge` | Challenge metrics ([§10](#10-compounding-challenge-system)). |

### 3.5 Security model

The server binds to `127.0.0.1`. WebSocket connections accept only origins `http://localhost:<port>` and `http://127.0.0.1:<port>`. Every POST endpoint rejects requests whose `Host` is not `localhost:<port>`/`127.0.0.1:<port>` or whose `Origin` (if present) is not the same (HTTP 403). These checks are defense-in-depth for a local tool, **not authentication**. Never expose the app through a LAN, reverse proxy, or public tunnel.

GET requests are served only for `/index.html`, `/tracker.html`, `/tradelog.html`, `/style.css` and the JSON routes below; anything else returns 404. The `agent_state.json` file that may exist on disk is a static sample; the HTTP route serves live in-memory state.

---

## 4. Dashboard (`index.html` + `style.css`)

A compact, non-scrolling trading-desk layout. The page streams state over WebSocket and falls back to polling `agent_state.json` every 3 seconds while the socket is down (the socket reconnects every second).

### 4.1 Layout and panels

- **Header:** symbol selector (four indices), status badges (`Lock: n/5` portfolio position lock, `Spread` guard badge, `Sync` state), light/dark toggle (applied through `data-theme`), **✨ AI Vision Scan** button, lot-size controls (− / input / +, presets 0.10, 0.50, 1.00; default **0.50**, step 0.01), and **BUY / SELL** buttons.
- **Macro bar:** per-timeframe trend matrix with candle-close countdown timers (`3m … d1`), daily CPR and vCPR badge, TC / Pivot / BC, an order-mode badge (`ORDERS DISABLED`, `MANUAL LIVE ORDERS`, or `AUTO + MANUAL LIVE`), signal badge, volatility-squeeze state tag, and breakout alert.
- **Left column:** main price chart (candles, EMAs, Bollinger Bands, CPR levels) with RSI and Stochastic sub-charts; timeframe tabs; a **Clear Lines** tool; per-timeframe metrics (trend, EMA stack, RSI/ATR, pressure).
- **Right column:**
  - **Compounding Challenge card** (status badge, day/step, balance, daily P&L, drawdown floor, total trades, auto/manual split, win rate, return %, step-progress bar).
  - **Open Positions card** with a per-ticket **Close** button (`/api/close-position`).
  - **Forecast / S&R panel**: nearest CPR level, distance in points, and a status such as *TESTING R1*, *BREAKOUT ABOVE*, *DEFENDING*; plus a simple three-step projection (Stochastic bias + pull toward the nearest level, each step capped at 0.4 ATR). This is a heuristic visualization, not a model forecast.
  - **Confluence checklist**: six BUY lights and six SELL lights (Bollinger, Stochastic, EMA, support/resistance, squeeze, retest) evaluated client-side by `evaluateConfluences()`.
  - **Decision Audit:** regime, consensus score, active paths and reasoning from the legacy evaluator, and a **Confirm** button that fires the BUY/SELL recommendation.
  - Position/margin helper showing the lot range from MT5 and the default SL/TP prices (`price ± 1.5 / 3.0 × ATR`).

### 4.2 Order buttons and trade controls

- Buttons are disabled unless live trading is enabled, MT5 is connected with trading allowed, the engine status is `RUNNING`, the symbol is available, and fewer than `max_open_positions` (5) positions are open.
- `executeManualTrade()` validates volume against the broker's min/max/step, requires a valid ATR on the selected timeframe, then **posts immediately** to `/api/execute-trade` with `volume` and `sl_distance = 1.5 × ATR`, `tp_distance = 3.0 × ATR`. There is **no confirmation dialog**.
- **Interactive chart:** open positions' TP and SL lines can be dragged on the price chart; releasing asks a `confirm()` and then calls `/api/modify-position`. Clicking the chart draws horizontal price lines (cleared with **Clear Lines**).
- `dispatchTradeInTranches()` (splits an order into tranches of up to 200 lots, rounded to 0.5) is defined but **not called** anywhere.

### 4.3 AI scan modal (Industrial Scanner v3)

The **✨ AI Vision Scan** button posts to `/api/ai-vision-scan` and opens a modal titled *Industrial Multi-TF Quantitative Intelligence*:

- Header badge: BUY / SELL / HOLD.
- Four metrics: confidence, entry price, stop loss, take profit.
- Reasoning box (the scanner's `reasoning` string, ranked by factor contribution).
- **Detected signals** chips: candlestick patterns (strength shown), chart patterns (name, `forming`/`confirmed`, measured target), regular/hidden divergences.
- **Ranked trade opportunities**: each card shows direction, setup type, timeframe, counter-trend flag, grade and score, SL / TP / R:R / regime, up to five confirmations, the trigger, the invalidation rule, and an **Execute this setup** button.
- Footer: *Close* and, when the action is not HOLD, **Execute BUY/SELL Trade**.

> **Important:** every *Execute* button in this modal calls `executeAiTrade(action, entry, tp, sl)`, which closes the modal and calls `executeManualTrade(action, button)`. The `entry`, `tp` and `sl` arguments are **not used**; the order is sent immediately with the lot-size box value and `1.5 × / 3.0 × ATR` distances of the selected timeframe. Treat the displayed setup levels as guidance.
>
> Cosmetic leftovers: the button tooltip still says "via Gemini Vision" and the execute toast says "Gemini AI". The confidence field falls back to `85%` when the value is `0` (e.g. insufficient-data HOLD), which is misleading.

### 4.4 Theming (`style.css`)

CSS variables on `:root` define the dark default (`--bg-color #050508`, cyan/green/red/yellow/purple/pink accents, border and surface tokens); `[data-theme="light"]` overrides them for the light theme. The layout is compact and non-scrolling; the AI modal classes (`.ai-modal-backdrop`, `.ai-modal-card`, `.ai-grid-metrics`, `.ai-reasoning-box`, `.ai-recommendation-badge`) live in the same stylesheet.

### 4.5 Known cosmetic issues

- The RSI label reads `RSI(10)`, but the engine computes **RSI 14**.
- `themeToggleBtn` and `last-update` element IDs appear twice in the header markup.
- Pages `tracker.html` and `tradelog.html` are linked by route but were not part of this review.

---

## 5. HTTP and WebSocket API reference

| Method & path | Purpose | Gates / behavior |
|---|---|---|
| `GET /` | Main dashboard (`index.html`). | Static file. |
| `GET /tracker`, `GET /tradelog` | `tracker.html`, `tradelog.html`. | Static files *(not re-verified)*. |
| `GET /style.css` | Stylesheet. | Static file. |
| `GET /agent_state.json` | Latest in-memory state JSON. | `Cache-Control: no-store`. Returns an empty "waiting for data" skeleton until the first broadcast. |
| `GET /api/tradelog-data` | Deal history for the supported symbols (last 365 days) from MT5. | HTTP 503 if MT5 history is unavailable. Each BUY/SELL deal is one row; `entry_price` and `exit_price` are both the deal price, and `result` is derived from the deal's net P&L. |
| `POST /api/control` | Dashboard control. | Body `{"action": "START" \| "PAUSED" \| "STOP"}` → status `RUNNING` (or `DATA ONLY` if live trading is off), `PAUSED`, `STOPPED`. In-memory only. Note the action string is `PAUSED`, not `PAUSE`. |
| `POST /api/ai-vision-scan` | Runs the Industrial Scanner for **all four symbols** sequentially and returns `{"status":"SUCCESS","analysis":{<symbol>: result}}`. | No live-trading gate (read-only analysis). Request fields `symbol`, `live_price`, `images` are **ignored by the endpoint** (it scans every supported symbol using `current_prices`); the page then picks the selected symbol. A 1.5 s sleep follows each symbol (left over from the former remote-API design). Errors return a per-symbol HOLD. |
| `POST /api/modify-position` | Set SL/TP of an open ticket. | Body `{ticket, sl, tp}` → `MT5Bridge.modify_position()`. |
| `POST /api/close-position` | Close an open ticket. | Body `{ticket}` → `MT5Bridge.close_position()`. Not gated by live-trading mode. |
| `POST /api/execute-trade` | Place a market order. | See below. |

### `POST /api/execute-trade` sequence

1. HTTP 403 if `LIVE_TRADING_ENABLED` is false; HTTP 409 if `agent_status != "RUNNING"`.
2. Parse JSON; validate `risk_pct` (default `MAX_RISK_PER_TRADE_PERCENT / 100`; must be > 0 and not above the configured cap, else 403).
3. Account snapshot (503 if unavailable); `check_daily_loss_limit()` (bypassed).
4. ATR from the active profile's primary timeframe cache (fallback `1.0`); live tick price (ask for BUY, bid for SELL).
5. SL/TP: uses `sl_distance` and `tp_distance` from the request when both are present (the dashboard always sends them); otherwise `profile ATR multiple × ATR`.
6. Volume: the request's `volume` if given, otherwise `calculate_dynamic_lot_size()` (floor 0.50 lots, risk % × balance ÷ (2 × ATR)). The result is clamped and stepped to the symbol's min/max/step.
7. Dispatch through `trading_modules.execute_guarded_order(..., max_allowed_spread_multiplier=1.5)`.
8. A background thread logs the trade to `trade_telemetry` (regime `MANUAL_UI_DISPATCH`, score `100`) and opens a `challenge_trades` row (`OPEN`, reason `Manual UI Dispatch`).

---

## 6. Trading style profiles

Select the style with `--style` (`SCALPING`, `INTRADAY`, `SWING`; default `INTRADAY`).

| Setting | SCALPING | INTRADAY | SWING |
|---|---|---|---|
| Primary timeframe | 3m | 5m | h1 |
| Confluence (higher) timeframe | 5m | h1 | d1 |
| Stop-loss (× ATR) | 1.0 | 1.5 | 2.5 |
| Take-profit (× ATR) | 1.5 | 3.0 | 6.0 |
| Stagnation cutoff | 90 s | 180 s | 3600 s |
| Minimum consensus score | 45 | 55 | 65 |
| Minimum ADX | 22 | 25 | 28 |
| Partial scale-out (× ATR profit) | 1.5 | 2.0 | 3.0 |
| Break-even trigger (R) | 1.0 | 1.0 | 1.2 |

Known behaviors:

- **Precedence:** `--style` always wins over `TRADING_STYLE` because argparse supplies the default `INTRADAY`. The env var only influences the profile object that `evaluate_multi_path_strategy()` binds as its default argument at import time.
- **Profile-binding bug:** `main()` reassigns `ACTIVE_TRADING_STYLE` and `CURRENT_PROFILE` after parsing, but the evaluator's default `profile=CURRENT_PROFILE` was bound earlier and no caller passes a profile. Result: the style *branch* follows `--style`, while `min_consensus_score` follows the import-time profile.
- `min_adx` is stored in every profile, but nothing enforces it in the legacy path ([§9](#9-trading-modes-risk-controls-and-position-management)).
- Active-trade stop/target multiples are widened by 1.2× when volatility expansion is detected (ADX > 30, or ATR more than 1.3× its 20-bar average) in the dormant auto-entry code.
- The Industrial Scanner reads the same keys (`primary_tf`, `confluence_tf`, `sl_atr_mult`, `tp_atr_mult`, `min_consensus_score`, `min_adx`) if a profile is passed, but today the engine does **not** pass one ([§8.11](#811-engine-integration-status)).

---

## 7. Legacy multi-path strategy evaluator

`evaluate_multi_path_strategy()` builds a weighted **consensus score** from signal paths. Positive favors BUY, negative favors SELL. The action fires when `score ≥ +min_consensus_score` (BUY) or `score ≤ −min_consensus_score` (SELL). It runs once per second per symbol for the dashboard signal and decision audit.

**Regime gating:** `is_squeeze = Bollinger width ≤ 4 × ATR`; "trending" when ADX ≥ 22 and not in a squeeze; "ranging" when ADX < 20 or in a squeeze.

| Style / regime | Regime label | Paths (points) |
|---|---|---|
| SCALPING | `SCALPING_MOMENTUM_REVERSAL` | Oversold bounce (Stoch < 20 and RSI < 40: +45) / overbought rejection (Stoch > 80, RSI > 60: −45); bullish/bearish micro-impulse candle (±25). |
| SWING | `SWING_MACRO_TREND_FOLLOWING` | Higher-TF trend alignment with price vs EMA20 (±55, trending only); price vs daily CPR pivot with higher-TF trend (±30). |
| INTRADAY, trending | `STRATEGY_1_MTF_TREND_MOMENTUM` | **A** trend pullback (higher-TF trend, price vs EMA20, RSI band 45–65 / 35–55, Stoch < 30 / > 70: ±40). **E** 3m + 5m + 15m + higher-TF alignment (±35). |
| INTRADAY, ranging | `STRATEGY_3_CPR_REVERSAL_REJECTION` | **B** Bollinger breakout/breakdown with Stoch+RSI confirmation (±45, not in squeeze). **H** inside-bar breakout (±25). **C** CPR support/resistance rejection (S1, S2, pivot / R1, R2, pivot within 0.6 ATR with Stoch < 25 / > 75: ±35). **D** extreme RSI < 15 / > 85 with Stoch (±35). **G** pinbar (wick ≥ 2.5 × body and > 60% of range: ±25). |

**Paths applied across styles**

- **F:** fair-value-gap retest (±20).
- **I:** 15m market-open bias (±30) and opening-range trap fade (±35, extreme Stoch/RSI beyond the opening range).
- **J:** Keltner breakout (EMA20 ± 2 ATR with Stoch and ADX > 25: ±30).
- **K:** EMA-ribbon velocity expansion (stack + price vs EMA20 + ADX > 28, trending only: ±35).
- **L:** Stochastic double-bottom / double-top reversal (K and D < 15 with RSI < 30, or > 85 with RSI > 70: ±30).

**Session weighting:** the final score is multiplied by a UTC-hour curve: 1.20 (07:00–17:00), 1.10 (05:00–07:00 and 17:00–20:00), 0.95 (20:00–23:00 and 02:00–05:00), 0.85 otherwise. Forced to 1.0 under unit tests or when `TEST_MODE=1`.

> **Unused helpers:** `get_telemetry_path_adjustments()` (win-rate multipliers per strategy path from telemetry: ≥ 60% over ≥ 2 trades → ×1.25; ≤ 35% → ×0.75) and `run_walk_forward_validation()` (60/40 split of candle direction ratios; returns a fixed `STABLE` placeholder below 30 candles) are defined but not wired into decisions.
>
> **Encoding:** the decision-audit reasoning string contains a corrupted character (`Threshold: �…`) that should be re-saved as UTF-8 (intended: `±`).

---

## 8. Industrial Scanner v3

`industrial_scanner.py` is a local, regime-adaptive, multi-timeframe confluence engine. It replaces the v1 scanner, which added fixed point bonuses from correlated signals (one idea could cross the BUY/SELL threshold several times), mixed trend-following and mean-reversion without regime logic, used flat 1.5×/3.0× ATR stops, had no veto gates, and could crash the HTTP handler on bad data.

**Properties:** pure `numpy` (no `pandas_ta`), zero network calls, deterministic, never raises (errors return a HOLD with the reason). About 25 ms for a full 7-timeframe scan on 75 candles per timeframe and about 63 ms on 200 candles.

### 8.1 Entry point

```python
perform_industrial_technical_scan(symbol, timeframes_dict, current_price, cpr, profile=None) -> dict
```

| Argument | Meaning |
|---|---|
| `symbol` | Symbol name (informational). |
| `timeframes_dict` | `{tf: process_dataframe() output}` for `3m, 5m, 15m, 30m, h1, h4, d1`. Uses `candles` (open/high/low/close), `trend`, `data_ready`, and the engine's `ema20/30/100/200`, `atr`, `adx`, `rsi`, `stoch_k_history`, `stoch_d_history` when they are valid; everything else is recomputed from the candles. |
| `current_price` | Live price used for entry and distance checks (falls back to the last close). |
| `cpr` | Output of `calculate_cpr()` (pivot, bc, tc, r1–r3, s1–s3). May be empty. |
| `profile` | Optional dict overriding `DEFAULT_PROFILE` (see [§8.10](#810-profile-parameters)). |

The primary timeframe is `profile.primary_tf` (fallbacks `5m`, `3m`, `15m`); the macro/confluence timeframe is `profile.confluence_tf` (fallbacks `h1`, `h4`, `d1`). At least **30** candles are required on the primary timeframe; timeframes with fewer than 10 candles are ignored. Otherwise the scanner returns HOLD with `"Insufficient local candle history…"`.

### 8.2 Pipeline

1. **Build a feature state per timeframe:** Wilder ATR/ADX/DI, RSI 14, Stochastic (14, 3), Bollinger (16, 1.986 and 20, 2.0), MACD (12, 26, 9), Supertrend (10, 3), EMA20/30 series, Keltner squeeze, efficiency ratio, ATR and Bollinger-width percentile ranks, swing highs/lows (k = 3, with equal-price plateaus merged into one swing), Donchian-20 bounds.
2. **Detect the regime** on the primary timeframe.
3. **Score ten independent factors** (−100 … +100 each).
4. **Weight the factors by regime** into a composite score.
5. **Apply veto gates** (counter-trend, exhaustion, dead volatility, too few agreeing factors, poor reward:risk).
6. **Compute structure-aware SL/TP**, confidence and grade.
7. **Scan every timeframe for named trade setups** and rank them.

### 8.3 Regime detection

| Regime | Condition (primary timeframe) |
|---|---|
| `SQUEEZE` | Bollinger width in the bottom 20% of its recent range **and** ADX < 22. |
| `EXPANSION` | ATR at or above the 85th percentile, rising, and Bollinger width ≥ 70th percentile. |
| `TRENDING` | ADX ≥ 22 and efficiency ratio ≥ 0.25. |
| `RANGING` | ADX < 20 or efficiency ratio < 0.18. |
| otherwise | Transitional: `TRENDING` if ADX ≥ 22, else `RANGING`. |

### 8.4 Factors

| Factor | What it scores |
|---|---|
| `mtf` | Per-timeframe direction (trend label ±60, EMA20/30/100 stack ±20, price vs EMA200 ±10, MACD histogram sign ±10), combined with timeframe weights `d1 .20, h4 .18, h1 .18, 30m .14, 15m .12, 5m .10, 3m .08` (renormalized over available timeframes). Also reports the weight fraction that agrees. |
| `trend` | Needs ADX ≥ 18. Direction from DI and EMA stack; strength `30 + (ADX − 18) × 3.5` (cap 100); +15 for a pullback within 0.6 ATR of EMA20; ×0.6 if more than 2.5 ATR extended; ×0.5 if price is on the wrong side of EMA100. |
| `momentum` | RSI deviation from 50 (scaled, capped) blended with MACD histogram sign and slope. |
| `mean_reversion` | Stochastic < 20 with RSI < 38 (+55) or > 80 with RSI > 62 (−55); stochastic cross (+25/−25); band touch (±20). |
| `breakout` | Donchian-20 break with a candle body ≥ 55% of range (±60); ×1.25 if ATR is expanding, ×0.6 if not (fake-out risk); ×1.2 after a squeeze. |
| `structure` | Key levels = CPR levels plus the last four swing highs and lows. Within 0.7 ATR: support/resistance with a rejection candle ±30, otherwise ±15; holding the correct side of the pivot with the macro trend ±15. |
| `candle` | Strongest bullish vs strongest bearish pattern ([§8.5](#85-candlestick-patterns)). |
| `divergence` | Regular and hidden RSI/MACD divergence ([§8.6](#86-divergence-and-convergence)). |
| `confirmation` | Supertrend, Fibonacci zone, squeeze release, MACD and EMA convergence ([§8.6](#86-divergence-and-convergence)). |
| `chart_pattern` | Geometric patterns ([§8.7](#87-chart-patterns)); strongest bullish minus strongest bearish, ×1.2. |

### 8.5 Candlestick patterns

Detected on the last 1–3 candles of the primary timeframe. **Context rules:** a reversal pattern is ignored when the preceding four-bar move already points the same way; damped ×0.6 when there is no prior move; ×1.35 at support/resistance (CPR level, swing point, or Bollinger band within 0.8 ATR) and ×0.85 elsewhere.

| Pattern | Bias | Base strength | Notes |
|---|---|---|---|
| Hammer | Bull | 58 | Lower wick ≥ 2× body, small upper wick, after a down move. |
| Hanging Man | Bear | 45 | Same shape after an up move. |
| Shooting Star | Bear | 58 | Upper wick ≥ 2× body after an up move. |
| Inverted Hammer | Bull | 42 | Same shape after a down move. |
| Dragonfly / Gravestone Doji | Bull / Bear | 45 | Body ≤ 10% of range with a long lower / upper wick. |
| Doji (indecision after move) | Against prior move | 22 | |
| Bullish / Bearish Marubozu | With candle | 40 | Continuation (no reversal context needed). |
| Bullish / Bearish Engulfing | ±| 65 | Body larger than the previous body. |
| Piercing Line / Dark Cloud Cover | Bull / Bear | 55 | |
| Bullish / Bearish Harami | ±| 40 | |
| Tweezer Bottom / Top | ±| 50 | Equal lows/highs within 0.1 ATR. |
| Morning Star / Evening Star | ±| 75 | Three-candle reversal. |
| Three White Soldiers / Three Black Crows | ±| 55 | Continuation. |
| Inside Bar (coiling) | Neutral | 0 | Informational. |

### 8.6 Divergence and convergence

- **Regular divergence** (reversal): price makes a lower low while the indicator makes a higher low (bullish), or a higher high with a lower high in the indicator (bearish).
- **Hidden divergence** (continuation): price higher low with indicator lower low (bullish), or price lower high with indicator higher high (bearish).
- Computed on **RSI** and the **MACD line** over the latest two swing points (4–45 bars apart, the latest within 30 bars). Minimum price difference 0.15 ATR; minimum indicator difference 2 RSI points or 0.03 ATR for MACD. Strength: regular 55, hidden 35, ×1.15 if fresh (≤ 8 bars) else ×0.8; RSI + MACD agreeing on a regular divergence adds +20.
- **Convergence signals** (in `confirmation`): MACD histogram converging toward zero (pre-cross) and signal-line crossovers; **EMA20/30 convergence** (gap below half its 20-bar average = "coil") and a breakout from the coil; **volatility squeeze** (Bollinger inside Keltner) and its release with momentum direction.
- **Other confirmations:** Supertrend direction and recent flips; Fibonacci 38.2–61.8% retracement of the latest swing leg (golden zone 50–61.8% scores higher).

### 8.7 Chart patterns

All 12 patterns from the classic reversal/continuation chart are implemented, plus the symmetrical triangle.

| Group | Bullish | Bearish |
|---|---|---|
| Reversal | Double Bottom, Inverse Head & Shoulders, Falling Wedge | Double Top, Head & Shoulders, Rising Wedge |
| Continuation | Bull Flag, Bull Pennant, Ascending Triangle | Bear Flag, Bear Pennant, Descending Triangle |
| Either way | Symmetrical Triangle (direction set by the break) | |

Each result carries `name`, `bias`, `kind` (reversal/continuation), `status`, `strength`, `level` (breakout boundary), `target` (measured move), `stop_anchor`, and `bars`.

**Status.** `confirmed` means price closed beyond the boundary by at least 0.1 ATR (and not more than 2.5 ATR; 3.0 for triangles/wedges, 3.5 for flags/pennants, so stale breakouts are ignored) **and** one of the last two candles is a decisive candle in the break direction (body ≥ 50% of range). Otherwise the pattern is `forming`. Strength: confirmed 62 (+8 if ATR is expanding), forming 28, symmetrical triangle forming 0.

| Pattern | Detection rules |
|---|---|
| Double Top / Bottom | Two swing extremes within 0.3 ATR, 6–40 bars apart, depth ≥ 2.5 ATR. Neckline = extreme between them. Target = neckline ± depth. |
| Head & Shoulders (and inverse) | Three swings; head exceeds both shoulders by ≥ 1.0 ATR; shoulders within `max(0.6 ATR, 20% of head height)`; head height ≥ 2.5 ATR; sloped neckline through the two troughs. Target = neckline ∓ head height. |
| Triangles and wedges | Trendlines through recent swing highs and lows. The scanner tries swing subsets (4, 3, then 2 per side, at least 5 touches in total) so an impulse before the pattern does not corrupt the fit; fit residual ≤ 0.6 ATR; width ≥ 3 ATR; lines **converging** (end width ≤ 0.8 of start). Slopes in ATR over the pattern span: *Ascending* upper flat (≤ 0.5), lower ≥ 0.9; *Descending* lower flat, upper ≤ −0.9; *Symmetrical* upper ≤ −0.5, lower ≥ 0.5 (needs 6 touches, end width ≤ 0.6 of start); *Rising Wedge* both > 0.8 with the lower steeper; *Falling Wedge* both < −0.8 with the upper steeper. Target = boundary ± start width. |
| Flags and pennants | A **pole** of ≥ 5 ATR within 3–12 bars with efficiency ≥ 0.7, then a 5–20 bar consolidation retracing ≤ 50% and not exceeding the pole extreme by more than 0.3 ATR. *Flag*: channel width ratio ≥ 0.75 drifting against the pole. *Pennant*: converging (ratio 0.1–0.7). Breakout may be up to four bars old. Target = breakout level ± pole height. |

> **False-positive rate:** after tightening, about **12% of windows** of pure random noise contained some confirmed chart pattern (windows overlap, so independent events are fewer). Geometric patterns appear in random data; see [§8.13](#813-validation-and-honest-limits).

### 8.8 Composite score, vetoes and trade levels

**Composite:** the weighted sum of the ten factors using the regime's weights.

| Regime | mtf | trend | mom | mean-rev | breakout | structure | candle | diverg. | confirm | chart |
|---|---|---|---|---|---|---|---|---|---|---|
| TRENDING | .24 | .18 | .11 | .04 | .07 | .08 | .06 | .07 | .06 | .09 |
| RANGING | .10 | .04 | .07 | .22 | .04 | .18 | .10 | .11 | .05 | .09 |
| SQUEEZE | .15 | .07 | .10 | .00 | .24 | .07 | .07 | .05 | .12 | .13 |
| EXPANSION | .20 | .15 | .11 | .00 | .20 | .07 | .05 | .05 | .08 | .09 |

With only one timeframe available (the backtest helpers), the `mtf` weight is removed and the rest renormalized.

**Action:** `BUY` if the composite ≥ the threshold, `SELL` if ≤ −threshold, otherwise `HOLD`. Default threshold **40** (a weighted average, so 40 is already selective).

**Veto gates** (any one turns the signal into HOLD and is listed in `vetoes`):

| Gate | Rule |
|---|---|
| Counter-macro | Direction opposes the macro-timeframe bias: in TRENDING/EXPANSION the threshold rises by 15; vetoed if the score is still below it. |
| RSI exhaustion | Long with RSI > 75 (or short with RSI < 25) unless the breakout factor agrees. |
| Dead volatility | ATR below the 10th percentile. |
| Minimum ADX | If `profile.min_adx` > 0 and ADX is below it. |
| Agreement | Fewer than `min_agreeing_factors` (default 3) factors with |score| ≥ 15 in the trade direction. |
| Reward:risk | Nearest opposing level leaves less than `min_rr` (default 1.5) R. |

**Stop and target** (`_trade_levels`): stop distance = `ATR × sl_atr_mult` (×1.25 when ATR is above its 92nd percentile, which also adds a warning). If a protective swing lies 1.0–2.5 ATR away the stop moves just beyond it (+0.25 ATR), never tighter than 80% of the ATR stop. Target = `max(ATR × tp_atr_mult, stop × min_rr)`, capped just before the nearest opposing level if that still leaves `min_rr`; otherwise the trade is vetoed. When a chart pattern supplies a measured move, that target is used (capped at 6 ATR, floor `min_rr`).

**Confidence** (a conviction rank, **not** a win probability): `30 + 0.45·|score| + 8·MTF agreement + 2·max(0, agreeing − 3) − 6·warnings`, clamped to 20–80; HOLD signals are capped at 50 minus 8 per veto.

**Grade (primary action):** A requires `|score| ≥ threshold + 20`, at least 5 agreeing factors, R:R ≥ 2 and confidence ≥ 65; B requires `|score| ≥ threshold + 8` and at least 4 agreeing factors; otherwise C. HOLD shows `-`.

### 8.9 Opportunity finder

The scanner also checks **every timeframe** for six named setups and returns up to six, ranked. This is independent of the primary BUY/SELL/HOLD decision, so a HOLD can still list opportunities (the reasoning notes the best one).

| Setup | Required conditions |
|---|---|
| **Trend Pullback** | Direction score ≥ 50 on that timeframe, ADX ≥ 20, price within 0.8 ATR of EMA20/30, a trigger (strong candle pattern, stochastic cross, or MACD convergence) and at least four confirmations. |
| **Divergence Reversal** | Regular divergence, a reversal candle pattern or stochastic cross, and price at a support/resistance zone. Counter-trend. |
| **Hidden Divergence Continuation** | Hidden divergence in the direction of the prevailing trend plus a trigger. |
| **Squeeze Breakout** | Squeeze released in the last three bars with momentum in the trade direction, and a Donchian-20 break or strong breakout candle. |
| **Range Fade** | `RANGING` regime, price at a band/level, stochastic < 25 (or > 75) **and** RSI < 40 (or > 60), plus a pattern or stochastic cross. Counter-trend. |
| **Chart Pattern: \<name\>** | A `confirmed` chart pattern. Continuation patterns must agree with the timeframe or multi-timeframe trend; reversal patterns need an independent second confirmation (candle pattern, divergence, stochastic cross or a Supertrend/MACD/squeeze confirmation). |

**Setup score** (an alignment score, not a probability): `15 + 7 × confirmations (max 6)`, `+8` aligned with the timeframe trend (`−12` against), `+8` aligned with the multi-timeframe composite (`−10` against), `+5` if R:R ≥ 2.5, `+5` at a key level, `−8` for counter-trend setups against the macro trend. Setups scoring below 45 are discarded. Grade: **A** score ≥ 75 and R:R ≥ 2; **B** ≥ 60; **C** otherwise. The maximum attainable score is roughly 83, so grade A is rare by design.

Every setup includes entry, stop, target, R:R, the confirmations list, the trigger, an invalidation rule, and a `counter_trend` flag.

### 8.10 Profile parameters

`DEFAULT_PROFILE` (used when `profile=None`; any key can be overridden):

| Key | Default | Meaning |
|---|---|---|
| `primary_tf` / `confluence_tf` | `5m` / `h1` | Execution and macro timeframes. |
| `sl_atr_mult` / `tp_atr_mult` | 1.5 / 3.0 | ATR multiples for stop and target. |
| `min_consensus_score` | 40 | Composite threshold. |
| `min_adx` | 0.0 | Hard ADX gate (off by default). |
| `min_rr` | 1.5 | Minimum reward:risk. |
| `min_agreeing_factors` | 3 | Minimum independent agreeing factors. |

The engine's `TRADING_PROFILES` use the same key names, but their `min_consensus_score` values (45/55/65) are on the legacy evaluator's scale and would be **very strict** for the scanner's composite, and their `min_adx` values (22/25/28) would activate the ADX gate. Calibrate on history before passing them.

### 8.11 Engine integration status

- **Wired:** `handle_ai_vision_scan()` builds `{tf: process_dataframe(df)}` for the symbol and calls `perform_industrial_technical_scan(symbol, timeframes_dict, live_price, cpr)`. The scanner uses `DEFAULT_PROFILE` because no profile is passed.
- **Data window:** the engine hands the scanner the last **75** candles per timeframe (`df.tail(75)` in `process_dataframe`), although the cache holds up to 300. The scanner works with any length; larger patterns (wedges, big triangles) benefit from more history. See [Appendix C](#appendix-c-optional-scanner-integration-edits-not-applied).
- **Leftovers in `/api/ai-vision-scan`:** the 1.5 s inter-symbol sleep and single-worker executor are no longer needed now that the scan is local.

### 8.12 Dashboard integration

Fields consumed by the modal: `action`, `confidence`, `entry_price`, `stop_loss`, `take_profit`, `reasoning`, plus the v3 additions `opportunities`, `candle_patterns`, `chart_patterns`, `divergences`. The *Execute* limitation is described in [§4.3](#43-ai-scan-modal-industrial-scanner-v3).

### 8.13 Validation and honest limits

Two replay helpers are included:

- `backtest_scanner(candles, profile=None, warmup=60, max_hold=40, cooldown=5, cpr=None)`: single-timeframe replay of the primary signal with no look-ahead. Walks forward to stop, target or `max_hold` bars (stop wins ties). Returns trade count, win rate, average confidence, expectancy in R, profit factor, longest losing streak, and breakdowns by regime and grade.
- `backtest_opportunities(candles, profile=None, min_score=60, …)`: replays the top-ranked setup per bar and reports results **per setup type**, so unprofitable setup types can be switched off.

Limits you should keep in mind:

1. **No demonstrated edge.** On random-walk data the scanner's expectancy is about zero ([Appendix B](#appendix-b-scanner-validation-results)). Deriv Volatility indices are generated by a random-number generator; whether technical patterns carry an edge there is an open question that only your own history can answer.
2. **Confidence and scores are rankings**, not probabilities.
3. **Chart patterns fire on noise** (~12% of random windows) and many setup types have too few trades in tests to judge.
4. The backtest helpers use one timeframe and no spreads, slippage or commission.
5. Geometric pattern recognition is inherently approximate; thresholds are documented above and can be tuned.

---

## 9. Trading modes, risk controls and position management

### 9.1 Modes

- **Default / data-only:** `LIVE_TRADING_ENABLED` is false and status is `DATA ONLY`. Order buttons are disabled and `/api/execute-trade` returns HTTP 403. The decision audit and scanner still run for display.
- **Manual live:** start with `--enable-live-trading` (or `start_manual_trading.bat`, type `LIVE` *(not re-verified)*). The engine connects to MT5 at startup and sets status `RUNNING`. Orders are sent **on click** with the lot-size box value; there is no confirmation dialog.
- **Automated entries:** `--enable-live-trading --enable-auto-trading` sets `AUTO_TRADING_ENABLED`; passing `--enable-auto-trading` alone is rejected at startup. **No code invokes the automated entry routine**, so this flag currently has no trading effect (see below). The flag is still cleared when the challenge halts.
- **Paused / stopped:** dashboard controls change in-memory status only and are not persisted. Manual orders require status `RUNNING`.

### 9.2 Dormant automated entry flow (defined, not called)

`check_and_auto_trigger_trade()` is intended to run once per newly completed primary-timeframe candle per symbol and would:

1. Re-run the legacy evaluator on completed-candle values (the first completed candle after startup only arms the engine).
2. Require the profile threshold; widen SL/TP ×1.2 on volatility expansion.
3. Reject if reward:risk (via `evaluate_trade_risk_reward`) is below 1.5; the returned size multiplier scales the risk percent.
4. Check the daily-loss stub, `MAX_OPEN_POSITIONS` (5) among positions with magic `123456`, symbol availability, and a spread guard (`spread > 0.25 × ATR` rejects).
5. Size the lot with `calculate_dynamic_lot_size()` (floor **0.50**), clamp to broker limits, and dispatch through `execute_guarded_order(..., max_allowed_spread_multiplier=1.5)`.
6. Log to `trade_telemetry` and the CSV.

### 9.3 Dormant position management (defined, not called)

`manage_active_trades()` would, for positions with magic `123456` on supported symbols:

- first call `trading_modules.monitor_and_cut_stagnant_positions()` (its in-code comment describes profit bounds of −2.0 to 1.0);
- **partial scale-out:** close 50% when profit reaches the profile's scale-out ATR multiple (needs ≥ 0.02 lots);
- **breakeven:** at ≥ 1× ATR profit move the stop to entry ± 0.2 ATR;
- **Chandelier trail:** at ≥ 2× ATR profit trail 1.5 ATR from the 5-candle high/low, moving only when it improves by more than 0.1 ATR;
- **stagnation cut:** after the profile's stagnation time, close a losing trade if price breached EMA20 by 0.5 ATR or RSI is below 42 (BUY) / above 58 (SELL).

> To restore these features, something must call both functions from the monitor loop. That is a code change and is intentionally **not** part of this documentation revision.

### 9.4 Risk configuration

| Setting | Behavior |
|---|---|
| `MAX_RISK_PER_TRADE_PERCENT` | Default `1`. Validated against the requested `risk_pct`, and used to size volume only when no `volume` is supplied. **Not applied to the UI-supplied volume** in `/api/execute-trade`. |
| `MAX_DAILY_LOSS_PERCENT` | Parsed and validated (default `2`), but **not enforced**: `check_daily_loss_limit()` always allows trading. |
| `MAX_OPEN_POSITIONS` | Constant `5`. Enforced by the dashboard (buttons disabled and an alert) and by `MT5Bridge.place_market_order()`. The server-side `/api/execute-trade` path does not show an explicit count check in the engine code. |

Both percentage settings accept values above 0 and up to 100; invalid values stop the engine from starting.

> **Limitations:** Orders are reported successful only after MT5 accepts them; this does not guarantee a fill or remove slippage, gap, or liquidity risk. Daily boundaries use the computer's local time, UTC, or US Eastern time depending on the component and should be reconciled with the broker's day before relying on them.

---

## 10. Compounding Challenge system

Implemented in `compounding_challenge.py` and stored in `trade_telemetry.sqlite`. The module imports `trading_modules` and `MetaTrader5`.

| Parameter | Value |
|---|---|
| Initial capital | $200.00 |
| Step growth target | +5% per step |
| Max daily drawdown | 3% |
| Max consecutive losses | 3 |

**Tables**

- `challenge_daily_status`: one row per day (unique `date_str`): day number, start/current balance, high-water mark, step, target balance, daily profit, drawdown limit, progress %, status, consecutive losses, halt flag.
- `challenge_trades`: one row per challenge trade: date/time, ticket, symbol, challenge day, step tier, action, lot size, entry/exit price, P&L, status, exit reason.

**Behavior**

- **Day boundary / time zones:** `load_state()`, `log_challenge_trade()` and `get_current_challenge_status()` use **US Eastern time** (`America/New_York` via `zoneinfo`). `evaluate_day()` compares a **UTC** date to the stored Eastern date, so the two can disagree for several hours each evening. On a new Eastern day the tracker rolls over: the previous ending balance becomes the start balance, the step resets to 1, the target becomes `start × 1.05`, and the drawdown floor `start × 0.97`.
- **Steps:** while the current balance (or equity) is at or above the target and the status is active, the step increments and the target compounds by 5%.
- **Halts:** `HALTED_MAX_DRAWDOWN` when equity ≤ the drawdown floor; `HALTED_CIRCUIT_BREAKER` after 3 consecutive losing closed trades. `evaluate_day()` **auto-resumes** (back to `ACTIVE`, losses reset) when equity recovers above the floor. `get_current_challenge_status()` reports `halt_trading` for statuses `DRAWDOWN_BREACH`, `CIRCUIT_BREAKER_PAUSE`, `HALTED_MAX_DRAWDOWN`, `HALTED_CIRCUIT_BREAKER`.
- **Closed-trade bookkeeping:** `log_challenge_trade()` deduplicates by ticket; closing statuses (`CLOSED`, `CLOSED_PROFIT`, `CLOSED_LOSS`, `CLOSED_STAGNATION`) adjust the in-memory balance, high-water mark and loss streak and upsert the day row. `sync_closed_challenge_trades()` scans the last 7 days of MT5 deals and closes matching `OPEN` rows with exit price, net P&L (profit + swap + commission), status `CLOSED_PROFIT`/`CLOSED_LOSS` and reason `TP_HIT`/`SL_HIT` (inferred from the sign of P&L, not from the actual closing mechanism).
- **Metrics:** `get_current_challenge_status()` returns day number, balances, high-water mark, daily P&L, target, step progress %, drawdown floor, status, loss streak, `halt_trading`, total/daily trades, manual vs auto counts, win rate, daily win rate, total return % (vs $200) and daily return %. The manual/auto split is guessed from keywords in `exit_reason` (`MANUAL`, `UI`, `DISPATCH`, `USER`); because sync overwrites the reason with `TP_HIT`/`SL_HIT`, closed manual trades are typically counted as **auto**.
- `calculate_dynamic_lot_size(balance, atr, min_lot=0.50, volume_step=0.01, risk_pct)` = `balance × risk% ÷ (2 × ATR)`, floored at `min_lot`, stepped to `volume_step`. It treats price points as currency per lot (no tick-value or contract-size conversion), and the 0.50 floor can exceed the intended risk on small accounts.
- `export_challenge_report()` writes `challenge_trades` to CSV.
- In the monitor loop a tracker is created every second with the **live balance as initial capital** and evaluated with floating P&L. In `broadcast_state_update()` the challenge block is read from the database and, if `halt_trading` is true, `AUTO_TRADING_ENABLED` is set to `False` in memory.
- Fallbacks when the database has no data: balance `$200.00`, target `$210.00`, drawdown floor `$194.00`.

**Seeding** *(not re-verified)*: `python seed_challenge.py` creates the tables and inserts today's Day 1 / Step 1 record (uses `INSERT OR REPLACE` on the date).

---

## 11. MT5 bridge reference

`mt5_bridge.py` (`MT5Bridge`) wraps the `MetaTrader5` package. All methods hold an `RLock`.

| Method | Behavior |
|---|---|
| `connect()` / `disconnect()` | Initializes the terminal (optional `MT5_TERMINAL_PATH`), verifies terminal and account, re-initializes if the connection dropped. Status strings include `PYTHON PACKAGE MISSING`, `NOT CONNECTED`, `CONNECTED`, `CONNECTED - TRADING DISABLED`, `CONNECTION ERROR`, `ACCOUNT DATA ERROR`. |
| `account_snapshot()` | Balance, equity, floating profit, positions (ticket, magic, type, price, volume, profit, swap, SL/TP, time), per-symbol availability, bid/ask/time, volume min/max/step, digits, realized P&L since local midnight, daily start balance and daily P&L, currency, server. `trade_allowed` requires account permission, terminal permission and the API not disabled. |
| `market_data_snapshot(timeframes, history_count=300, include_history)` | Per symbol: latest quote, and optionally `copy_rates_from_pos` history per timeframe (the engine requests 300 bars). |
| `place_market_order(payload, max_risk_percent)` | Validates symbol, direction, volume limits and step, stop/target side, broker minimum stop distance, risk cap (via `order_calc_profit`) or risk-sized volume (binary search over lot steps), free margin (`order_calc_margin`), runs `order_check`, then `order_send` with magic `123456`, deviation 50, comment `Enhanced Derive AI Engine`, and a fill mode chosen from the symbol's allowed modes. Hard-codes a 5-position cap. **Not called by the engine in the reviewed version.** |
| `modify_position(ticket, sl, tp)` | `TRADE_ACTION_SLTP` for an existing position. |
| `close_position(ticket, volume=None)` | Full or partial close with an opposite market deal (deviation 50, original magic). |
| `get_position_by_ticket(ticket)` | Position dict or `None`. |
| `trade_history(days=365)` | BUY/SELL deals for supported symbols as dicts (trade/deal IDs, time, type, symbol, lot size, price, net P&L including commission/swap/fee, result label, magic, comment). |

Order results are treated as successful for `DONE`, `DONE_PARTIAL` or `PLACED` retcodes only.

---

## 12. Telemetry and database

### 12.1 `trade_telemetry.sqlite`

| Table | Columns |
|---|---|
| `trade_telemetry` | `id, date_str, time_opened, close_time, ticket, symbol, trading_style, timeframe, action, regime, consensus_score, strategies_utilized, evolving_modifications, entry_price, margin, volume, atr, sl_price, tp_price, partial_closures, exit_reason` (missing columns are auto-migrated at startup). |
| `challenge_daily_status` | See [§10](#10-compounding-challenge-system). |
| `challenge_trades` | See [§10](#10-compounding-challenge-system). |

Telemetry `exit_reason` doubles as a status field: `OPEN` at entry, then `TAKE_PROFIT_HIT` / `STOP_LOSS_HIT` (inferred from P&L sign by the sync), and, in the dormant management code, `PARTIAL_CLOSED` and `CLOSED_STAGNATION`. Manual dashboard orders are stored with regime `MANUAL_UI_DISPATCH`, score `100` and strategy `Manual UI Execution`.

`multi_timeframe_trade_log.csv` receives the same rows (header created by the engine if missing).

### 12.2 Observations from the supplied sample database (2026-11-14)

- 101 `trade_telemetry` rows and 101 `challenge_trades` rows, all dated 2026-11-14.
- Of the telemetry rows, 61 are labelled `INTRADAY` / `MANUAL_UI_DISPATCH`; 40 have no style, regime or exit reason (their origin cannot be determined from the supplied code).
- **No row came from the automated path**, consistent with §9.
- Two symbols appear (Volatility 25 and 100); volumes range from 0.5 to 5.0 lots; several orders share the same second, consistent with rapid manual clicking.
- `challenge_daily_status` holds two days; the second shows a step counter of 22, reflecting the 5%-compounding loop on a large intraday balance change.

Use this as an illustration of how the tables fill, not as performance evidence.

---

## 13. Installation and local setup (Windows)

### Prerequisites

- Windows 10/11 and PowerShell.
- Python 3.12 recommended (3.12.10 was used to validate the original workspace; other versions are not certified).
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

### Packages

| Package | Purpose | Notes |
|---|---|---|
| `pandas` | OHLC time series, CSV/SQL loading | `>=2.0` (declared). |
| `pandas-ta` | EMA, RSI, ADX, BB, Stochastic in the engine | Unpinned (declared). |
| `websockets` | Browser-facing WebSocket server | Unpinned (declared). |
| `MetaTrader5` | Local terminal API | `>=5.0.45` (declared). |
| `numpy` | Industrial Scanner v3 | Installed as a dependency of `pandas`. |
| `tzdata` | `zoneinfo` time-zone database on Windows (challenge module uses `America/New_York`) | **Not in the declared list** per the previous README; install if `ZoneInfo` raises `ZoneInfoNotFoundError`. |
| `google-genai` | `from google import genai` and `from google.genai import types` at the top of the engine | **Hard import**, otherwise unused now that the scanner is local; the engine will not start without it. |
| `trading_modules` (local file) | `get_db_connection`, `calculate_atr`, `evaluate_trade_risk_reward`, `monitor_and_cut_stagnant_positions`, `execute_guarded_order` | Must sit next to the engine. **Not included in the reviewed set.** |

Versions observed in the original workspace (not pinned requirements): pandas 3.1.1, pandas-ta 0.4.71b0, websockets 17.4, NumPy 2.2.6. There is no lock file yet. Please verify `pyproject.toml` *(not re-verified)* lists everything the imports need.

---

## 14. Run, verify, and stop

### Safe local run

```powershell
.\.venv\Scripts\python.exe .\Enhanced_Derive_AI_Engine.py
```

Open <http://localhost:8080>. This mode streams market data and does not enable order dispatch. Stop with **Ctrl+C**.

### Launchers *(not re-verified)*

| File | Mode | Dashboard |
|---|---|---|
| `start_dashboard.bat` | Data-only | <http://localhost:8081> |
| `start_manual_trading.bat` (type `LIVE`) | Manual orders; auto entries off | <http://localhost:8081> |
| `start_automated_trading.bat` (type `AUTO LIVE`) | Sets the auto flag (see §9: no automated entries currently run) | <http://localhost:8081> |

### Command-line options

```powershell
.\.venv\Scripts\python.exe .\Enhanced_Derive_AI_Engine.py --style SCALPING --enable-live-trading --enable-auto-trading
```

| Flag | Meaning |
|---|---|
| `--style {SCALPING,INTRADAY,SWING}` | Trading profile (default `INTRADAY`). See §6 for the profile-binding caveat. |
| `--enable-live-trading` | Connects to MT5 at startup, allows order dispatch, sets status `RUNNING`. |
| `--enable-auto-trading` | Sets the automated-entry flag (requires live trading; see §9). |

### Enable live order dispatch (advanced)

```powershell
$env:MT5_TERMINAL_PATH = 'C:\Program Files\MetaTrader 5\terminal64.exe'
$env:MT5_SYMBOL = 'Volatility 25 Index'
$env:MAX_RISK_PER_TRADE_PERCENT = '1'
$env:MAX_DAILY_LOSS_PERCENT = '2'
.\.venv\Scripts\python.exe .\Enhanced_Derive_AI_Engine.py --enable-live-trading
```

Remember the caveats in §0: orders are one-click, volume comes from the lot box, and the risk cap is not applied to that volume.

### Tests and basic checks *(not re-verified)*

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q .\Enhanced_Derive_AI_Engine.py .\mt5_bridge.py .\backtest.py .\compounding_challenge.py .\industrial_scanner.py .\tests
```

Per the previous documentation, tests cover default-mode trade suppression, local POST origins, risk caps, MT5 bridge behavior with mocks, and backtest logic. They do not simulate Deriv, a real MT5 terminal, browser rendering, or broker execution, and **no tests cover the Industrial Scanner**.

To confirm listeners while the app runs:

```powershell
Get-NetTCPConnection -State Listen -LocalPort 8080,8765
```

Both should bind to `127.0.0.1`.

---

## 15. Historical MT5 backtest

`backtest.py` *(not re-verified in this revision; description carried over)* replays the **legacy** strategy (it imports the engine's evaluator) across the four indices using MT5 3m, 5m, 15m, 1h and 1d candles. It does not use the Industrial Scanner; for that, see `backtest_scanner()` and `backtest_opportunities()` in [§8.13](#813-validation-and-honest-limits).

- Default starting balance $1,000, 1% risk per trade, one open portfolio position.
- Signals use completed bars; entries use the next 5m bar open.
- If a candle touches both stop and target, the **stop is assumed first**.
- Historical spread is applied to ask-side prices; MT5 contract calculations estimate P/L and margin. Commission and slippage are **not** included.
- ATR trailing/breakeven and early exits are checked only at 5m closes, effective from the next bar.
- It uses whatever history is available, reports requested vs actual dates, starts at the latest common history date plus a 30-day indicator warm-up, and refuses to run if no usable period remains.
- The daily-loss reset is evaluated at UTC midnight and may differ from the broker's day.

To extend history: raise **MT5 > Tools > Options > Charts > Max bars in chart**, then open each symbol/timeframe chart and press **Home** repeatedly.

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

### Backtesting the scanner on your own history (example)

```python
from industrial_scanner import backtest_scanner, backtest_opportunities
# candles: list of {"open","high","low","close"} dicts, oldest first (one timeframe)
print(backtest_scanner(candles))
print(backtest_opportunities(candles, min_score=60))   # results per setup type
```

---

## 16. Configuration reference

| Setting / file | Purpose |
|---|---|
| `MT5_TERMINAL_PATH` | Optional path to `terminal64.exe`; auto-discovery if unset. |
| `MT5_SYMBOL` | Default dashboard symbol (one of the four supported); default `Volatility 25 Index`. |
| `TRADING_STYLE` | Env var for the initial/default profile; effectively overridden by `--style` (see §6). |
| `DASHBOARD_PORT` | HTTP port (default 8080); WebSocket uses `DASHBOARD_PORT + 1` unless 8080 (then 8765). |
| `MAX_RISK_PER_TRADE_PERCENT` | Per-trade risk cap; default `1` (see §9.4 for where it applies). |
| `MAX_DAILY_LOSS_PERCENT` | Validated but not enforced; default `2`. |
| `TEST_MODE=1` | Forces the session multiplier to 1.0. |
| `MAX_OPEN_POSITIONS` (constant) | `5`. |
| `MT5_MAGIC_NUMBER` (constant) | `123456`; marks engine-managed positions. |
| `trade_telemetry.sqlite` | Tables `trade_telemetry`, `challenge_daily_status`, `challenge_trades`. |
| `multi_timeframe_trade_log.csv` | Header created by the engine if missing; rows appended alongside SQLite telemetry. |
| `agent_state.json` | Static sample snapshot; the HTTP route serves live in-memory state. |
| `credentials.json`, `token.pickle` | Legacy files, ignored by Git, not needed for MT5 data. |

`.gitignore` excludes the virtual environment, credentials, runtime account/order/history files, logs, and Python caches. Never commit secrets or private trading data.

---

## 17. Known issues and roadmap

### 17.1 Known issues (ordered by impact)

1. **Automated entry and position management are not invoked** (§9). `check_and_auto_trigger_trade()` and `manage_active_trades()` need a caller.
2. **One-click manual orders with no confirmation**, default 0.50 lots, no server-side application of the risk cap to the UI volume (§4.2, §5, §9.4).
3. **Scanner Execute buttons ignore the setup's SL/TP** and place a standard `1.5×/3.0× ATR` order (§4.3).
4. **`trading_modules.py` is required but missing from the reviewed files**; behavior of `execute_guarded_order`, `calculate_atr`, `evaluate_trade_risk_reward` and `monitor_and_cut_stagnant_positions` could not be verified.
5. **`--style` / profile binding:** the evaluator's default profile is bound at import (§6).
6. **Daily loss limit is bypassed**; `MAX_DAILY_LOSS_PERCENT` does nothing.
7. **`min_adx` filter is not applied** (`validate_trade_quality()` unused).
8. **Mixed time zones** across components: UTC (`evaluate_day`, session curve), US Eastern (challenge day), local time (MT5 daily P&L, telemetry sync date range) (§10).
9. **Exit reasons are inferred from P&L sign** (`TP_HIT`/`SL_HIT`, `TAKE_PROFIT_HIT`/`STOP_LOSS_HIT`), so trailing, scale-out and stagnation exits are mislabeled; the challenge's manual/auto split is unreliable as a result.
10. **Challenge tracker re-created every second** with the live balance as initial capital and evaluated with floating P&L; confirm this matches the intended compounding behavior.
11. **`calculate_dynamic_lot_size()`** ignores tick value/contract size and floors at 0.50 lots.
12. **Hard-coded `google-genai` import** and leftover "Gemini" labels, though no remote AI is used.
13. **Scanner endpoint leftovers:** 1.5 s sleep per symbol and unused request fields (§5).
14. **Window size:** the scanner sees only 75 candles per timeframe although 300 are cached (§8.11).
15. **Duplicate code in `check_and_auto_trigger_trade`** (repeated balance/tracker lines and a duplicated comment) and an `if succeeded` block that can reference variables from a failed guarded order path.
16. **Encoding:** a corrupted character in the decision-audit reasoning string (`Threshold: �`).
17. **Dashboard:** `RSI(10)` label vs RSI 14; duplicate element IDs; `ai.confidence || '85'` shows 85% for a confidence of 0; `dispatchTradeInTranches()` unused; scanner reasoning is inserted as HTML (it is generated locally, but still treat as trusted-only).
18. **Per-second recomputation:** all 28 symbol/timeframe frames are re-processed every second, which is CPU-heavy.
19. **Prototype pages:** `tracker.html` and `tradelog.html` *(not re-verified)* showed static example KPIs in earlier reviews.
20. **Unused helpers:** `get_telemetry_path_adjustments()` and `run_walk_forward_validation()`.
21. **No automated tests for the Industrial Scanner.**

### 17.2 Roadmap

**Phase 1: Paper trading and backtesting.** Explicit paper mode that cannot write to MT5; tick-level replay and validation against MT5 tester results; replay/live parity tests; clear labeling of simulated data; multi-timeframe scanner backtest on exported MT5 history; per-setup-type enable/disable driven by those results.

**Phase 2: Risk and order safeguards.** Restore and test the daily loss limit; agree on the daily reset timezone and P&L definition; apply the per-trade risk cap and open-position cap server-side to every order path; add a manual-order confirmation; order idempotency, lifecycle states, expiry and audit logging; enforce the ADX/quality filter; reconnect the automated entry and trade-management routines behind explicit tests.

**Phase 3: Connection recovery.** Stale-feed detection, bounded reconnect/backoff, graceful shutdown, port-conflict reporting; avoid placeholder values in decisions.

**Phase 4: Dashboard reliability.** Verify responsive behavior; make data-only/live/stale states unambiguous; remove static metrics; bundle Chart.js for offline use; make scanner Execute buttons honor the setup's SL/TP (or label them clearly).

**Phase 5: Trade history and analytics.** One canonical closed-trade record sourced from confirmed MT5 fills; rebuild daily summaries, win rate, drawdown and growth curves from it with test fixtures; record real exit mechanisms.

**Open decisions:** supported Deriv account/environment; trading-day definition; approved volume/margin/max-open-risk/kill-switch limits; authoritative MT5 bridge schema; offline operation; supported browsers and displays; whether strategy parameters live in the UI, environment, or versioned config; whether the scanner's thresholds should replace or complement the legacy evaluator.

---

## 18. Complete repository map

Status key: **R** = read in this revision; **N** = referenced but not available for this revision.

| Path | Status | Responsibility |
|---|---|---|
| `Enhanced_Derive_AI_Engine.py` | R | **Core engine:** candle caches, MT5 tick processing, indicators, legacy multi-path evaluator, scanner endpoint, order/close/modify endpoints, SQLite/CSV telemetry, challenge integration, HTTP and WebSocket servers. Contains dormant auto-entry and trade-management code. |
| `industrial_scanner.py` | R | **Industrial Scanner v3** (regime-adaptive multi-timeframe confluence engine, pattern recognition, opportunity finder, backtest helpers). Pure `numpy`. |
| `trading_modules.py` | N | Imported by the engine and `compounding_challenge.py`: `get_db_connection`, `calculate_atr`, `evaluate_trade_risk_reward`, `monitor_and_cut_stagnant_positions`, `execute_guarded_order`. |
| `mt5_bridge.py` | R | **MT5 bridge:** connection lifecycle, account snapshots, market data, risk-sized market orders (unused by the engine), position modify/close, deal history. |
| `compounding_challenge.py` | R | **Challenge tracker:** step targets, drawdown and circuit breaker, `evaluate_day`, trade logging, MT5 closed-deal sync, status metrics, CSV export. |
| `Enhanced_Derive_AI_Engine_New.py` | N | Experimental alternative implementation (not reviewed). |
| `seed_challenge.py` | N | Creates challenge tables and seeds a Day 1 record. |
| `backtest.py` | N | Historical replay backtester (legacy strategy) producing JSON and CSV reports. |
| `index.html` | R | Main live dashboard, including the AI scan modal and opportunity list. |
| `index3.html` | N | Alternative dashboard layout. |
| `tracker.html` | N | Challenge / day tracker page. |
| `tradelog.html` | N | Trade log and performance page. |
| `style.css` | R | Dashboard theme (dark default, light via `data-theme`), compact non-scrolling layout, AI modal styles. |
| `start_dashboard.bat` | N | Launcher: data-only mode. |
| `start_manual_trading.bat` | N | Launcher: manual live mode. |
| `start_automated_trading.bat` | N | Launcher: sets the auto-trading flag. |
| `pyproject.toml` | N | Project metadata, dependencies, and the `run-engine` entry point. |
| `trade_telemetry.sqlite` | R (copy) | Runtime database: trade telemetry and challenge tables. |
| `multi_timeframe_trade_log.csv` | N | Runtime CSV trade log. |
| `agent_state.json` | N | Sample state snapshot (live state is served from memory). |
| `tests/test_startup_safety.py` | N | Startup safety, origin checks, risk validation tests. |
| `tests/test_mt5_bridge.py` | N | MT5 bridge tests with mocks. |
| `tests/test_backtest.py` | N | Backtest logic tests. |
| `README.md` | R | This document. |

---

## Appendix A: Scanner output reference

`perform_industrial_technical_scan()` returns a dictionary. **v1-compatible fields** are unchanged in name and meaning.

| Field | Type | Meaning |
|---|---|---|
| `action` | `"BUY"` / `"SELL"` / `"HOLD"` | Primary decision after vetoes. |
| `confidence` | int 20–80 (0 for insufficient data) | Conviction rank, not a probability. |
| `consensus_score` | int −100…100 | Regime-weighted composite. |
| `entry_price`, `stop_loss`, `take_profit` | float | Entry (live price) and structure-aware levels; `0.0` for HOLD. |
| `reasoning` | string | `|`-separated: regime, top contributing factors with notes, grade and R:R or HOLD reason, warnings. |
| `breakdown` | dict | `adx, rsi, stoch_k, atr, atr_percentile, bb_width_percentile, efficiency_ratio, macro_trend, primary_tf, macro_tf, mtf_agreement`. |

**New in v3:**

| Field | Meaning |
|---|---|
| `grade` | `A`/`B`/`C` for BUY/SELL, `-` for HOLD. |
| `regime` | `TRENDING`, `RANGING`, `SQUEEZE`, `EXPANSION` (or `UNKNOWN` on insufficient data). |
| `risk_reward` | R:R of the primary trade (0 for HOLD). |
| `factor_scores` | The ten factor scores (`mtf, trend, momentum, mean_reversion, breakout, structure, candle, divergence, confirmation, chart_pattern`). |
| `timeframe_bias` | Per-timeframe directional score (−100…100). |
| `vetoes`, `warnings` | Gate reasons that forced HOLD; cautions (e.g. extreme volatility, TP capped at a level). |
| `candle_patterns` | `[{name, bias, strength, at_level}]` for the primary timeframe. |
| `chart_patterns` | `[{name, bias, kind, status, strength, level, target, stop_anchor, bars}]`. |
| `divergences` | `[{kind, indicator, bias, strength, bars_ago}]`. |
| `confirmations` | `[{name, bias, strength}]`. |
| `opportunities` | Up to six ranked setups: `{type, timeframe, action, setup_score, grade, entry_price, stop_loss, take_profit, risk_reward, confirmations[], trigger, invalidation, counter_trend, regime}`. |

Insufficient data returns a HOLD with `confidence 0`, `regime "UNKNOWN"` and the reason in `reasoning` and `vetoes`.

---

## Appendix B: Scanner validation results

Measured on **synthetic** data (random walks and hand-constructed charts), not on your market data. Reproduce with the helpers in §8.13.

| Test | Result |
|---|---|
| Pattern detection on constructed charts | All 12 chart patterns (both directions) detected with sensible measured targets; candlestick patterns behaved as designed, including context damping (e.g. the same hammer scored 78 after a downtrend and 46 mid-range); bullish and bearish divergence detected on constructed paths. |
| Pattern false positives on pure noise | About 12% of overlapping windows contained a confirmed chart pattern after threshold tightening (about 35% before). |
| Scanner expectancy on random walks | About 0R (pooled +0.008R over 268 trades across 8 random walks, before chart patterns were added). A single run once showed +0.28R, which disappeared when many seeds were pooled. |
| Setup finder on random walks (final version) | Pooled expectancy about +0.07R over 114 trades, which is within statistical noise (roughly ±0.13R standard error). Per-setup-type samples were tiny (1–30 trades) and not meaningful. |
| Speed | About 25 ms per full 7-timeframe scan on 75 candles; about 63 ms on 200 candles. |
| Calibration | An early confidence formula reported ~70 against a 36% win rate on random data; it was recalibrated and capped at 80. |

Conclusion: the scanner does not invent an edge in noise, and nothing here demonstrates profitability. Validate on your own stored history before relying on any signal.

---

## Appendix C: Optional scanner integration edits (not applied)

These are **documentation only**. No code was changed in this revision.

1. **Larger candle window for the scanner.** `process_dataframe()` could additionally return `"scan_candles"` built from `df.tail(200)` (open/high/low/close/time), and `_build_state()` in the scanner could prefer `tf.get("scan_candles")` over `tf.get("candles")`. The dashboard's `candles` list (75 bars) would stay as is. Expect scan time to rise from ~25 ms to ~63 ms.
2. **Pass the active profile.** `handle_ai_vision_scan()` could call `perform_industrial_technical_scan(symbol, timeframes_dict, live_price, cpr, profile=CURRENT_PROFILE)`. Recalibrate thresholds first (§8.10); the engine's 45/55/65 are on a different scale.
3. **Remove scan leftovers.** The per-symbol `time.sleep(1.5)` and the single-worker executor in `/api/ai-vision-scan` exist only because the scan used to call a remote API.
4. **Honor setup levels on execution.** `executeManualTrade()` could accept optional absolute SL/TP from the setup and convert them to `sl_distance`/`tp_distance` relative to the fresh quote, rejecting the order if price has already moved beyond either level.
