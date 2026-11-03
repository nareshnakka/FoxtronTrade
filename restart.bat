@echo off
setlocal
cd /d "%~dp0"

echo Stopping any process on ports 5050 and 5000...
powershell -NoProfile -Command ^
  "foreach ($p in 5050,5000) { Get-NetTCPConnection -LocalPort $p -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue } }"
timeout /t 2 /nobreak >nul

set "PYTHONPATH=%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"

if not exist "%PY%" (
  echo ERROR: venv not found at "%PY%"
  echo Create it with: py -3.12 -m venv .venv ^& .venv\Scripts\pip install -r requirements.txt
  pause
  exit /b 1
)

if not exist "%~dp0access_token.json" (
  echo No access_token.json — run login first:
  echo   "%PY%" login.py
  pause
)

echo Starting FoxtronTrade dashboard...
echo Dashboard: http://127.0.0.1:5050
echo Press Ctrl+C to stop.
echo.
"%PY%" app.py

endlocal
