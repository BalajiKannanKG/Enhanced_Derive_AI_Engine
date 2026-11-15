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

echo ========================================================
echo         MT5 AUTOMATED TRADING CHALLENGE LAUNCHER
echo ========================================================
echo.
echo Select Trading Style Profile:
echo   [1] Scalping (Live + Auto)
echo   [2] Intraday (Live + Auto)
echo   [3] Swing (Live + Auto)
echo   [4] Cancel / Exit
echo.
set /p "STYLE_CHOICE=Enter your choice (1-4): "

if "%STYLE_CHOICE%"=="1" (
    set "STYLE=SCALPING"
) else if "%STYLE_CHOICE%"=="2" (
    set "STYLE=INTRADAY"
) else if "%STYLE_CHOICE%"=="3" (
    set "STYLE=SWING"
) else (
    echo Launch cancelled.
    pause
    exit /b 0
)

echo.
echo WARNING: Automated strategy signals can submit REAL MT5 market orders.
echo Profile Selected: %STYLE% (Live Trading + Auto Trading Enabled)
echo.
set "CONFIRM_AUTO_LIVE="
set /p "CONFIRM_AUTO_LIVE=Type AUTO LIVE to confirm and start: "
if /I not "%CONFIRM_AUTO_LIVE%"=="AUTO LIVE" (
    echo Automated live trading was not started.
    pause
    exit /b 0
)

set "DASHBOARD_PORT=8081"
echo.
echo Starting %STYLE% automated live trading engine on http://localhost:8081...
echo Hosting index.html dashboard and websocket telemetry server...
echo Minimize MT5 if you do not want its window in the foreground.
echo Keep this window open; press Ctrl+C here to stop the engine.
echo.

"%PYTHON%" "%~dp0Enhanced_Derive_AI_Engine.py" --style SCALPING --enable-live-trading --enable-auto-trading
set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo Dashboard exited with code %EXIT_CODE%.
pause
exit /b %EXIT_CODE%