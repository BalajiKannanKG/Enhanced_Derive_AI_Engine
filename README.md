# Enhanced Deriv AI Engine

This README is the project’s working reference for local setup, architecture,
business requirements captured to date, interfaces, operational safety, and
future enhancements. It distinguishes implemented behavior from requirements
that still need design or independent validation.

> **Trading risk:** This is an experimental trading dashboard and automation
> prototype, not financial advice or a guarantee of results. Start in
> data-only mode. Do not enable live order dispatch until the broker account,
> symbol specifications, MT5 terminal configuration, and end-to-end risk handling have
> been independently verified.

## 1. Project scope and business requirements

### Purpose

The application connects to Deriv market data for the `R_25` synthetic index,
calculates multi-timeframe indicators and strategy signals, and serves a local
browser dashboard. An optional direct connection sends requests to a locally
running MetaTrader 5 terminal through the official Python package. The
application does not require or install an Expert Advisor.

### Requirements captured so far

| Area | Requirement / current decision |
|---|---|
| Market data | Deriv public WebSocket market data for `R_25`; historical candles followed by live ticks. |
| Timeframes | 3m, 5m, 15m, 30m, 1h, 4h, and 1d. Request up to 300 historical candles per timeframe; keep recent candles in memory. |
| Dashboard | Local live chart and indicators, market/strategy status, account/position display, manual order controls, day tracker, and trade log pages. |
| Order routing | Optional direct order submission through the official MetaTrader5 Python API to an already installed, logged-in terminal. |
| Local safety | Default to read-only account data; require `--enable-live-trading` and dashboard Start before order submission. Bind HTTP and browser WebSocket services to loopback. |
| Risk controls | Per-trade monetary stop-loss risk cap (default 1%) and daily realized-plus-floating loss stop (default 2%). Missing or invalid live account data blocks new orders. |
| Trade management | Python ports the provided bridge's magic-number-filtered ATR break-even/trailing stops, early structural exits, and live deal-history export. |
| Future history source | Begin with Deriv API candle history for any future backtest feature. MT5 deal history is available through the direct terminal API; backtesting remains unimplemented. |
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
3. A browser-facing WebSocket server on `127.0.0.1:8765` by default, which broadcasts
   market and account state.
4. An HTTP dashboard server on `127.0.0.1:8080` by default.

Set `DASHBOARD_PORT` to run a second local instance without taking over the
default ports. Its browser WebSocket uses the next port (for example,
`DASHBOARD_PORT=8081` serves HTTP on 8081 and WebSocket on 8082).

The HTML pages use Chart.js from jsDelivr, so an internet connection is also
needed for those charts unless that dependency is later bundled locally.

### Dashboard pages and routes

| URL | Page / purpose |
|---|---|
| `/` | Main live dashboard (`index.html`). |
| `/tracker` | Day tracker (`tracker.html`). |
| `/tradelog` | Trade log and performance page (`tradelog.html`). |
| `/api/tradelog-data` | Queries recent real deal history from the connected MT5 terminal. |
| `/api/control` (`POST`) | Dashboard Start, Pause, and Stop control. |
| `/api/execute-trade` (`POST`) | Validates account state, symbol, volume, stop/target, margin, and monetary risk before sending a market order directly to MT5. |
| `/agent_state.json` | No-cache JSON snapshot of the latest in-memory engine state, also used by the dashboard polling fallback. |

The HTML client streams state over the matching local WebSocket port. The
default server allows origins `http://localhost:8080` and
`http://127.0.0.1:8080`. HTTP POST endpoints
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
  The AI decision audit still calculates and displays strategy scores from
  market data; automated trade management is disabled, and `/api/execute-trade`
  returns HTTP 403.
- **Live-capable run:** Start the process with `--enable-live-trading`. The
  already installed and logged-in MT5 terminal must permit Python/API trading.
  The dashboard must also be set to Start before manual order dispatch or
  automated trading can proceed.
- **Paused/stopped:** Dashboard controls update the in-memory engine status.
  These states are not persisted across restarts.

The engine only opens a new position when the account has no open positions.
Automated entries use 0.50 lots and fixed stop/target distances of 15/30 price
units, but are rejected if broker symbol constraints, margin, or calculated
loss at the stop exceed configured limits. Management and early-exit rules are
translated from `DashboardBridge.mq5`; they only manage positions on the
configured symbol with magic number `123456`.

### Important behavior and known limitations

- Python reads the terminal's existing logged-in session; it does not store or
  request account passwords. `MT5_TERMINAL_PATH` can point to `terminal64.exe`
  if automatic discovery does not select the desired instance. `MT5_SYMBOL`
  selects the exact broker symbol, defaulting to `Volatility 25 Index`.
- The package reports account, position, and deal history directly from MT5.
  Connection and Algo Trading permission are reported independently. Account
  history day boundaries use the computer's local timezone and should be
  reconciled with broker-day conventions before using the daily stop for
  production.
- `/api/execute-trade` checks calculated monetary loss at the requested stop
  using MT5's contract-aware `order_calc_profit`, then validates broker lot
  step/range, minimum stop distance, free margin, and MT5's `order_check`
  response. An order is only reported successful after MT5 accepts it; this
  does not guarantee a later fill.
- Live trading stays disabled unless the process is explicitly started with
  `--enable-live-trading` and the dashboard Start control is selected. Enabling
  Algo Trading in MT5 alone does not enable this application's order path.
- The tracker expects closed-trade fields such as entry time,
  exit, P&L, result, and balance. The trade-log page contains example/static
  initial KPI values (including a max-drawdown value that is not calculated).
  Some KPIs are recalculated when API data is available, but empty data leaves
  the initial examples visible. Treat these pages as prototypes until a real
  closed-trade schema and source are connected. The CSV is currently read as a
  fallback; the Python engine does not create it.
- The engine publishes its latest in-memory state both to the browser WebSocket
  and through the no-cache `agent_state.json` endpoint. The dashboard can still
  display stale information if connected to an older process serving port 8080.
- This Python integration replaces the EA's account telemetry, order handling,
  position management, and deal-history export. It does not install, compile,
  or require any EA.
- In the current flow, the multi-path strategy evaluator is invoked for
  automated decisions only when live trading is enabled and the engine is
  running. Data-only mode does not guarantee that the dashboard's strategy
  audit reflects a continuously evaluated signal.
- Deriv's public WebSocket URL and application ID are constants in the Python
  module. They are not credentials, but API endpoint/app-ID changes require a
  code update.

## 3. Direct Python MT5 connection

`mt5_bridge.py` uses the official `MetaTrader5` Python package to communicate
with a locally installed terminal. It attaches to the logged-in terminal
session; it does not use Common Files, account-state JSON, a socket service, or
an Expert Advisor. Set `MT5_TERMINAL_PATH` when multiple terminals are installed
or automatic selection chooses the wrong one. Never put an account password in
the project or command line.

The MT5 terminal must be open and logged in. Its global Algo Trading setting
and account-level trading permission must allow Python-originated requests for
orders to be sent. The dashboard reflects the terminal connection and trading
permission separately.

### Converted `DashboardBridge.mq5` behavior

- Account balance, equity, floating P/L, open positions, and daily P/L are
  polled directly from the terminal.
- Market orders are sent by Python with the requested SL and TP attached. MT5
  symbol volume limits/steps, stop distance, margin, order-check result, and
  monetary stop-loss exposure are validated first.
- The EA's 1.5-ATR break-even, 2-ATR trailing, 3-ATR uncapped winner runner,
  and 120-second ATR/EMA20 early-exit rules are managed from Python for symbol
  `MT5_SYMBOL` and magic number `123456`.
- Real deal history is queried directly from the terminal. The engine does not
  invent historical RSI, trend, exit prices, or trade outcomes.
- Live order placement and position management remain gated by the explicit
  `--enable-live-trading` process option and dashboard Start control.

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
| `MetaTrader5` | Direct local terminal API for account data, orders, position management, and deals | `>=5.0.45` |

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

To start the engine independently of VS Code, double-click `start_dashboard.bat`
in File Explorer. It uses this project's `.venv`, starts in data-only mode, and
keeps its own console open for logs; open the dashboard at
[http://localhost:8081](http://localhost:8081). Keep the console running and
press **Ctrl+C** there to stop the engine. This launcher does not enable live
trading.

The MT5 Python integration still requires the installed terminal process.
`mt5.initialize()` may open it when the engine starts; minimize the terminal
window if you do not want it in the foreground. Data-only mode prevents order
dispatch but does not disable read-only MT5 account telemetry.

The installed console entry point can also be used:

```powershell
.\.venv\Scripts\run-engine.exe --help
```

### Enable live order dispatch (advanced)

Only proceed after verifying the logged-in terminal and broker symbol settings:

```powershell
$env:MT5_TERMINAL_PATH = 'C:\Program Files\MetaTrader 5\terminal64.exe'
$env:MT5_SYMBOL = 'Volatility 25 Index'
$env:MAX_RISK_PER_TRADE_PERCENT = '1'
$env:MAX_DAILY_LOSS_PERCENT = '2'
.\.venv\Scripts\python.exe .\Enhanced_Derive_AI_Engine.py --enable-live-trading
```

Live mode is opt-in for each process launch. Both risk settings accept numbers
greater than 0 and up to 100; values are percentages. Defaults are 1.0% per
trade and 2.0% daily loss. Invalid settings prevent the module from starting.
The daily-loss calculation is `max(0, -daily_pnl / daily_start_balance * 100)`;
new orders are blocked when that value reaches the configured cap. Missing
terminal/account data blocks orders.

The dashboard must also be set to Start. The application calculates risk from
the terminal's symbol contract and rejects any order whose stop-loss risk
exceeds the configured percentage. This does not guarantee execution or remove
market-gap/slippage risk.

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
| `MT5_TERMINAL_PATH` | Optional path to `terminal64.exe`; uses package auto-discovery if unset. |
| `MT5_SYMBOL` | Exact MT5 broker symbol; defaults to `Volatility 25 Index`. |
| `MAX_RISK_PER_TRADE_PERCENT` | Maximum calculated stop-loss risk as a percentage of balance; default `1`. |
| `MAX_DAILY_LOSS_PERCENT` | Daily realized-plus-floating loss threshold expressed as a percentage; default `2`. |
| `credentials.json`, `token.pickle` | Local credential/token files; ignored by Git and not required for the current public market-data connection. |
| `mt5_bridge.py` | Direct MT5 account, order, position-management, and history API integration. |
| `agent_state.json` | Ignored legacy file; the HTTP route returns current in-memory state instead. |

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
- Validate all order and position-management requests in Python against actual
  symbol contract specifications and terminal/account trade permissions.
- Add idempotency, explicit order lifecycle states, expiry, and audit logging
  around direct MT5 requests.
- Confirm broker-server daily reset semantics and reconcile the configured
  loss limit's day boundary with the account's timezone.

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
- Verify the live-state polling fallback and WebSocket recovery on reconnect.
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
