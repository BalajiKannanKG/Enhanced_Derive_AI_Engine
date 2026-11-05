# Enhanced Deriv AI Engine

This README is the project’s working reference for local setup, architecture,
business requirements captured to date, interfaces, operational safety, and
future enhancements. It distinguishes implemented behavior from requirements
that still need an MT5 bridge or further design.

> **Trading risk:** This is an experimental trading dashboard and automation
> prototype, not financial advice or a guarantee of results. Start in
> data-only mode. Do not enable live order dispatch until the broker account,
> symbol specifications, MT5 Expert Advisor, and end-to-end risk handling have
> been independently verified.

## 1. Project scope and business requirements

### Purpose

The application connects to Deriv market data for the `R_25` synthetic index,
calculates multi-timeframe indicators and strategy signals, and serves a local
browser dashboard. An optional file-based integration passes order requests to
an MT5 Expert Advisor.

### Requirements captured so far

| Area | Requirement / current decision |
|---|---|
| Market data | Deriv public WebSocket market data for `R_25`; historical candles followed by live ticks. |
| Timeframes | 3m, 5m, 15m, 30m, 1h, 4h, and 1d. Request up to 300 historical candles per timeframe; keep recent candles in memory. |
| Dashboard | Local live chart and indicators, market/strategy status, account/position display, manual order controls, day tracker, and trade log pages. |
| Order routing | Optional JSON-file handoff to an MT5 Expert Advisor. The engine is not itself an MT5 client and does not confirm broker execution. |
| Local safety | Default to data-only; require an explicit live-mode command-line flag and MT5 Common Files path before enabling order dispatch. Bind HTTP and browser WebSocket services to loopback. |
| Risk controls | Configurable per-trade risk metadata (default 1%) and daily-loss stop (default 2%). In live mode, missing/invalid daily MT5 metrics block new orders. |
| Future history source | Begin with Deriv API candle history for any future backtest feature. Plan for MT5 history as a later source; neither backtesting nor an MT5-history adapter is currently implemented. |
| Deployment | Run on the user's Windows computer for now. Network/public hosting, authentication, and multi-user operation are out of scope until separately designed. |

These are the decisions captured in the current project conversation; confirm
them before implementing a feature that changes trading behavior.

## 2. Current implementation

### Runtime architecture

`Enhanced_Derive_AI_Engine.py` is a single-process Python application with
background threads for:

1. A Deriv WebSocket client. On connection it requests candle history for each
   configured granularity, then subscribes to live `R_25` ticks. It retries
   after a disconnect with an increasing delay.
2. A heartbeat loop for the Deriv connection.
3. A browser-facing WebSocket server on `127.0.0.1:8765`, which broadcasts
   market and account state.
4. An HTTP dashboard server on `127.0.0.1:8080`.

The HTML pages use Chart.js from jsDelivr, so an internet connection is also
needed for those charts unless that dependency is later bundled locally.

### Dashboard pages and routes

| URL | Page / purpose |
|---|---|
| `/` | Main live dashboard (`index.html`). |
| `/tracker` | Day tracker (`tracker.html`). |
| `/tradelog` | Trade log and performance page (`tradelog.html`). |
| `/api/tradelog-data` | Reads trade history JSON from the configured MT5 directory when present; otherwise reads local `trade_history.json`, then the local CSV as a fallback. |
| `/api/control` (`POST`) | Dashboard Start, Pause, and Stop control. |
| `/api/execute-trade` (`POST`) | Checks live-mode and risk prerequisites, then writes an order request for the MT5 Expert Advisor. |
| `/agent_state.json` | Static-file route retained for the pages' polling fallback; the current engine does **not** write this file. |

The HTML client streams state over `ws://localhost:8765`. The server allows
origins `http://localhost:8080` and `http://127.0.0.1:8080`. HTTP POST endpoints
also check the local Host and Origin. These checks are defense-in-depth for a
local tool, not user authentication. Do not expose the app through a LAN,
reverse proxy, or public tunnel.

### Market data and indicators

The engine builds OHLC candles from Deriv history and live ticks. It calculates
EMA 20/30/100/200, RSI 14, ATR 14, ADX 14, Bollinger Bands (length 16, standard
deviation 1.986), Stochastic (14,3), candle-pattern labels, previous-day open
and close, and CPR pivot/support/resistance levels.

The strategy evaluator contains eight signal paths:

1. Trend pullback / EMA retest.
2. Bollinger-band breakout or breakdown.
3. CPR support/resistance rejection.
4. Extreme RSI/Stochastic reversal.
5. Multi-timeframe trend alignment.
6. Fair-value-gap retest.
7. Wick rejection / pinbar.
8. Inside-bar breakout.

Signals are accumulated into a weighted consensus score; the evaluator returns
BUY at `+65` or above, SELL at `-65` or below, and otherwise no action.
Automated entry is currently based on 5m data with 1h trend context. Indicator
parameters, thresholds, stop distances, and trade sizing are code-level
settings, not yet a user-configurable strategy system.

If fewer than five candles are available for a timeframe, the dashboard data
builder supplies synthetic fallback candles and default indicator values. Those
are display placeholders, not real market history; confirm live feed and
history have arrived before relying on chart or signal output. Candle caches
are in memory and are not a durable market-data store.

### Trading modes and controls

- **Default/data-only:** `LIVE_TRADING_ENABLED` is false and the displayed
  status starts at `DATA ONLY`. The Start control does not enable trading.
  Automated trade management is disabled, and `/api/execute-trade` returns
  HTTP 403.
- **Live-capable run:** Start the process with
  `--enable-live-trading` and set `MT5_COMMON_FILES_PATH`. The dashboard must
  then be set to Start before manual order dispatch or automated trading can
  proceed.
- **Paused/stopped:** Dashboard controls update the in-memory engine status.
  These states are not persisted across restarts.

Automated trade management currently allows at most one open position and
avoids writing a new request while `trade_request.json` already exists. It
uses a fixed minimum volume of 0.50 lots and fixed entry stop/target distances
of 15/30 price units. Position management can request a break-even stop after
15 favorable price units and an ATR-based trailing stop after 25. These values
are legacy implementation constants, not validated account- or
contract-specific risk sizing.

### Important behavior and known limitations

- A live launch requires `MT5_COMMON_FILES_PATH`, but the path is not verified
  to be a valid MT5 Common Files directory.
- The engine reads `account_state.json` for account/position data. Missing
  account data is displayed as empty; live order creation fails closed when
  required risk or position metrics are absent.
- `/api/execute-trade` applies the configured risk cap to the request's
  `risk_pct` field. `risk_pct` is a fraction (`0.01` means 1%). This is not a
  calculation of monetary loss at the stop price. The MT5 Expert Advisor must
  independently calculate actual exposure using the account balance, volume,
  stop distance, and current symbol contract specifications before executing.
- The engine writes a JSON request file but does not wait for, parse, or verify
  an MT5 acknowledgement or broker fill. An order API success means the
  request was written, not that a trade was executed. The bridge should consume
  and clear the pending request; while the file remains, automated orders are
  blocked.
- Local `trade_history.json` records dispatched request payloads, not confirmed
  closed trades. The tracker expects closed-trade fields such as entry time,
  exit, P&L, result, and balance. The trade-log page contains example/static
  initial KPI values (including a max-drawdown value that is not calculated).
  Some KPIs are recalculated when API data is available, but empty data leaves
  the initial examples visible. Treat these pages as prototypes until a real
  closed-trade schema and source are connected. The CSV is currently read as a
  fallback; the Python engine does not create it.
- The dashboard's `agent_state.json` polling fallback is not kept current by
  this engine. The intended live state path is the browser WebSocket.
- In the current flow, the multi-path strategy evaluator is invoked for
  automated decisions only when live trading is enabled and the engine is
  running. Data-only mode does not guarantee that the dashboard's strategy
  audit reflects a continuously evaluated signal.
- Deriv's public WebSocket URL and application ID are constants in the Python
  module. They are not credentials, but API endpoint/app-ID changes require a
  code update.

## 3. Requirements for the MT5 bridge

The Python engine and an MT5 Expert Advisor must agree on JSON file formats and
the meaning of each field. The bridge is a separate component and is not
included in this repository.

### MT5 Common Files directory

Set `MT5_COMMON_FILES_PATH` to the exact Common Files directory shared with
the Expert Advisor. The engine reads `account_state.json` and may read
`trade_history.json` there. It writes `trade_request.json` there.

### Account-state input

At minimum, live order dispatch requires the following fields:

```json
{
  "balance": 200.0,
  "equity": 200.0,
  "profit": 0.0,
  "open_positions": 0,
  "positions": [],
  "daily_start_balance": 200.0,
  "daily_pnl": 0.0
}
```

`daily_start_balance` must be a positive number. `daily_pnl` must be the signed
realized **plus floating** P&L since the broker's daily reset, in account
currency. The timezone/reset convention and how pending commissions/swaps are
counted must be agreed with the bridge. `positions` must be an array; position
objects used by the dashboard/manager should include `type`, `price`, `sl`,
`tp`, and `ticket` where applicable. `open_positions` must be a non-negative
integer and should agree with the array length.

The bridge should write complete, valid JSON atomically (write a temporary
file, then replace the previous snapshot) so the engine does not read a partial
file. That atomic-write approach is an integration requirement/recommendation;
it is not implemented here.

### Trade-request output

An entry request is a JSON object with fields similar to:

```json
{
  "timestamp": 1793900000,
  "symbol": "Volatility 25 Index",
  "action": "BUY",
  "volume": 0.5,
  "entry": 2785.0,
  "tp": 2815.0,
  "sl": 2770.0,
  "risk_pct": 0.01,
  "strategy_path": "Confluence Consensus Score: 65"
}
```

`risk_pct` is a decimal fraction, not a percent integer. This example is
illustrative, not a recommended order. Before execution, the MT5 Expert Advisor
must validate the symbol, action, lot min/max/step, available margin,
market/current price, SL/TP validity, duplicate requests, and worst-case loss.
It should publish an acknowledgement/result contract so the dashboard can
distinguish queued, accepted, rejected, filled, and failed orders. That
acknowledgement contract is not yet implemented.

## 4. Python installation and local setup (Windows)

### Prerequisites

- Windows 10/11 and PowerShell.
- Python 3.12 recommended. Python 3.12.10 was installed and used to validate
  this workspace; other Python versions are not certified here.
- Internet access to install packages, connect to Deriv, and load Chart.js
  from jsDelivr.
- Git is optional for running the app; this project folder has a local Git
  repository initialized for future change tracking.

Install Python 3.12 with Windows Package Manager:

```powershell
winget install --id Python.Python.3.12 --exact --scope user --accept-package-agreements --accept-source-agreements
```

Alternatively, install Python 3.12 from [python.org](https://www.python.org/downloads/windows/).
After installation, reopen PowerShell/VS Code and confirm:

```powershell
python --version
```

If `python` is not on PATH, use the full interpreter path. For the Python
installation used in this workspace:

```powershell
& "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" --version
```

### Create the virtual environment and install dependencies

From the project directory:

```powershell
# Use the Python 3.12 launcher if installed:
py -3.12 -m venv .venv

# If the Python launcher is unavailable, use python or its full path instead:
# python -m venv .venv
# & "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" -m venv .venv

.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .
```

The virtual environment keeps this project's packages separate from other
Python applications. Always use `.venv\Scripts\python.exe` for this project;
activation is optional.

### Packages

Direct runtime dependencies declared in `pyproject.toml`:

| Package | Purpose | Declared constraint |
|---|---|---|
| `pandas` | OHLC time-series data and CSV loading | `>=2.0` |
| `pandas-ta` | EMA, RSI, ATR, ADX, Bollinger Bands, and Stochastic indicators | Unpinned |
| `websocket-client` | Deriv WebSocket client | Unpinned |
| `websockets` | Browser-facing WebSocket server | Unpinned |

The package build also uses `setuptools>=61.0`. Installing the project pulls
the compatible transitive dependencies. The versions installed and tested in
this workspace included pandas 3.1.1, pandas-ta 0.4.71b0, websocket-client
1.9.2, websockets 17.4, and NumPy 2.2.6. These are an observed environment
snapshot, **not pinned requirements**. There is no lock file yet; repeatable
production installs need a deliberately maintained lock/constraints file.

## 5. Run, verify, and stop

### Safe local run

```powershell
.\.venv\Scripts\python.exe .\Enhanced_Derive_AI_Engine.py
```

Then open [http://localhost:8080](http://localhost:8080). The default mode
allows market-data streaming and dashboard access but does not enable order
dispatch. Keep the terminal open to see connection and engine logs. Stop with
**Ctrl+C** in that terminal.

The installed console entry point can also be used:

```powershell
.\.venv\Scripts\run-engine.exe --help
```

### Enable live order dispatch (advanced)

Only proceed after configuring and testing the MT5 bridge contract above:

```powershell
$env:MT5_COMMON_FILES_PATH = 'C:\path\to\MetaTrader\Terminal\Common\Files'
$env:MAX_RISK_PER_TRADE_PERCENT = '1'
$env:MAX_DAILY_LOSS_PERCENT = '2'
.\.venv\Scripts\python.exe .\Enhanced_Derive_AI_Engine.py --enable-live-trading
```

Live mode is opt-in for each process launch. Both risk settings accept numbers
greater than 0 and up to 100; values are percentages. Defaults are 1.0% per
trade and 2.0% daily loss. Invalid settings prevent the module from starting.
The daily-loss calculation is `max(0, -daily_pnl / daily_start_balance * 100)`;
new orders are blocked when that value reaches the configured cap. Missing,
invalid, or unreadable account-state data blocks orders.

The dashboard must also be set to Start. Manual requests that omit `risk_pct`
receive the configured per-trade cap as a fraction. The engine checks that
requested fraction does not exceed the cap, but only the EA can calculate and
enforce actual monetary risk. Do not treat this setting as complete position
sizing until the EA implements the corresponding calculation and reports its
result.

### Tests and basic checks

Run the unit tests and syntax check:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q .\Enhanced_Derive_AI_Engine.py .\tests
```

The current tests cover default-mode trade suppression, local POST origins,
missing daily-risk data, the daily-loss threshold, and the per-trade risk cap.
They do not simulate Deriv, MT5, browser rendering, or broker execution.

To confirm local listeners while the app runs:

```powershell
Get-NetTCPConnection -State Listen -LocalPort 8080,8765
```

Both should bind to `127.0.0.1`. A successful dashboard HTTP response does not
prove Deriv is connected; check the terminal for the Deriv connection message
and confirm live quotes/candles in the dashboard.

## 6. Configuration and local files

| Setting/file | Purpose |
|---|---|
| `MT5_COMMON_FILES_PATH` | MT5 Common Files directory; defaults to `runtime\mt5_files` inside the project, which is not an MT5 installation. Required for live mode. |
| `MAX_RISK_PER_TRADE_PERCENT` | Maximum request risk fraction expressed as a percentage; default `1`. |
| `MAX_DAILY_LOSS_PERCENT` | Daily realized-plus-floating loss threshold expressed as a percentage; default `2`. |
| `credentials.json`, `token.pickle` | Local credential/token files; ignored by Git and not required for the current public market-data connection. |
| `account_state.json` | MT5 account/position/daily-risk snapshot. |
| `trade_request.json` | Pending MT5 request file. Remove/acknowledge only according to the agreed EA protocol. |
| `trade_history.json` | Request log locally or trade-history source from MT5; current source schemas are not yet unified. |
| `multi_timeframe_trade_log.csv` | Legacy analytics fallback; read by the HTTP route but not written by this engine. |
| `agent_state.json` | Ignored legacy/dashboard fallback path; not written by the current engine. |

`.gitignore` excludes the virtual environment, credentials, token, runtime
account/order/history files, logs, and Python caches. Do not force-add secrets
or private trading data to Git. The repository currently has no initial commit.

## 7. Business and technical enhancement roadmap

These items are future work, not claims about existing features. Preserve the
default data-only mode and add tests with each trading-related change.

### Phase 1 — Paper trading and backtesting

- Add an explicit paper mode that cannot write to MT5.
- Define a historical candle provider interface: Deriv API first, MT5 history
  later, with a normalized OHLC/timezone/schema contract.
- Implement deterministic replay, transaction-cost assumptions, order
  lifecycle simulation, and performance metrics.
- Prevent look-ahead bias; test the same strategy on replay and live ticks.
- Label simulated results and fallback candles so they cannot be confused with
  executed or live data.

### Phase 2 — End-to-end risk and order safeguards

- Agree on daily reset timezone, realized/floating P&L definition, account
  currency, and stale-data behavior with the MT5 bridge.
- Make the EA independently enforce per-order monetary loss, max volume,
  margin, daily stop, duplicate protection, and kill switch from actual symbol
  contract specifications.
- Replace file-exists-as-acknowledgement with request IDs, atomic writes,
  explicit acknowledgement/result states, expiry, retry/idempotency behavior,
  and audit logging.
- Add validation for symbols, order type, entry, stop, target, volume, lot
  steps, and allowed trading mode at both Python and EA boundaries.

### Phase 3 — Connection recovery and diagnostics

- Expose Deriv connection status, last tick/history timestamps, retry count,
  and actionable errors in the dashboard.
- Add bounded reconnect/backoff, stale-feed detection, graceful thread/server
  shutdown, and startup port-conflict reporting.
- Avoid silent fallback to placeholder market values for decisions.

### Phase 4 — Dashboard usability and reliability

- Verify responsive behavior on target screen sizes and browser versions.
- Make data-only/live mode and stale/disconnected states unambiguous.
- Remove stale static metrics and make all displayed account/trade data come
  from validated live or explicitly labeled sample data.
- Replace or properly implement the `agent_state.json` polling fallback.
- Bundle/pin frontend chart dependencies if offline operation is required.

### Phase 5 — Trade history and analytics

- Define one canonical closed-trade record, including stable trade ID, order
  and fill times, symbol, direction, volume, entry/exit, stop/target, realized
  P&L, fees, outcome, balance, and strategy/version.
- Have the MT5 bridge publish confirmed fills and closed trades rather than
  treating order requests as completed trades.
- Rebuild daily summaries, win rate, drawdown, growth curves, and CSV export
  from that canonical source; test calculations against known fixtures.

### Decisions still required before larger changes

- Which Deriv account/environment and symbol definitions are supported?
- What should count as a trading day and daily reset in the selected account's
  timezone?
- What are approved volume, margin, max-open-risk, and kill-switch limits?
- What is the authoritative MT5 bridge schema and acknowledgement protocol?
- Are charts and local operation required to work without internet access?
- Which browser, display sizes, retention period, and backup requirements
  should be supported?
- Should strategy parameters be configurable in UI, environment, or versioned
  configuration files?

## 8. Repository map

| Path | Responsibility |
|---|---|
| `Enhanced_Derive_AI_Engine.py` | Deriv feed, indicator/strategy calculations, HTTP/WebSocket servers, risk checks, and MT5 file handoff. |
| `index.html` | Live trading dashboard and manual controls. |
| `tracker.html` | Day-level tracker and summary charts. |
| `tradelog.html` | Trade-log/performance page. |
| `pyproject.toml` | Python project metadata, dependencies, and `run-engine` entry point. |
| `.gitignore` | Excludes local environments, credentials, and runtime trading data. |
| `tests/test_startup_safety.py` | Focused tests for local startup and risk safeguards. |
| `README.md` | Setup guide and living requirements/architecture baseline. |
