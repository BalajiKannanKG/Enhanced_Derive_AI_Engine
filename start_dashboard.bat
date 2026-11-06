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

set "DASHBOARD_PORT=8081"
echo Starting the dashboard in data-only mode.
echo Open http://localhost:8081 after the server starts.
echo Keep this window open; press Ctrl+C here to stop the engine.
"%PYTHON%" "%~dp0Enhanced_Derive_AI_Engine.py"
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo Dashboard exited with code %EXIT_CODE%.
    pause
)
exit /b %EXIT_CODE%
