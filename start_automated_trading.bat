@echo off
setlocal
cd /d "%~dp0"

set "PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
    echo Project virtual environment not found: "%PYTHON%"
    echo Install the project dependencies first. See README.md section 5.
    pause
    exit /b 1
)

echo WARNING: Automated strategy signals can submit REAL MT5 market orders.
echo Entries use closed 5-minute candles, risk-based volume, and the configured loss limits.
echo No order is sent until the next eligible 5-minute candle after startup.
set "CONFIRM_AUTO_LIVE="
set /p "CONFIRM_AUTO_LIVE=Type AUTO LIVE to enable automated live trading: "
if /I not "%CONFIRM_AUTO_LIVE%"=="AUTO LIVE" (
    echo Automated live trading was not started.
    pause
    exit /b 0
)

set "DASHBOARD_PORT=8081"
echo Starting automated live trading on http://localhost:8081
echo Minimize MT5 if you do not want its window in the foreground.
echo Keep this window open; press Ctrl+C here to stop the engine.
"%PYTHON%" "%~dp0Enhanced_Derive_AI_Engine.py" --enable-live-trading --enable-auto-trading
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo Dashboard exited with code %EXIT_CODE%.
    pause
)
exit /b %EXIT_CODE%
