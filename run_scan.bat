@echo off
REM Manual local launcher for the QuantScout scan/decide/trade engine on Windows.
REM This runs the exact same engine.py / scripts\run_scan.py as the scheduled
REM GitHub Actions job — use this to test with your real credentials before
REM trusting them in GitHub Actions, or to run a one-off pass by hand.
setlocal

cd /d "%~dp0"

if not exist secrets.local.bat (
    echo.
    echo [!] secrets.local.bat not found.
    echo     Copy secrets.local.bat.example to secrets.local.bat and fill in
    echo     your real keys first. secrets.local.bat is gitignored so it will
    echo     never be committed.
    echo.
    pause
    exit /b 1
)

call secrets.local.bat

echo Installing dependencies (requirements-engine.txt)...
python -m pip install --quiet -r requirements-engine.txt
if errorlevel 1 (
    echo [!] pip install failed. Is Python installed and on PATH?
    pause
    exit /b 1
)

echo.
echo Running scan...
echo.
python scripts\run_scan.py

echo.
echo Done.
pause
