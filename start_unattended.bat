@echo off
setlocal
cd /d "%~dp0"

echo === FoxtronTrade UNATTENDED 24x7 ===
echo Keeps engine alive, auto-restarts on freeze, morning TOTP login.
echo Dashboard: http://127.0.0.1:5050
echo Leave this window open. Ctrl+C stops supervisor.
echo.

REM Reduce sleep while plugged in (best-effort; may need admin)
powercfg /change standby-timeout-ac 0 >nul 2>&1
powercfg /change monitor-timeout-ac 0 >nul 2>&1

set "PYTHONPATH=%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"

if not exist "%PY%" (
  echo ERROR: venv not found at "%PY%"
  pause
  exit /b 1
)

echo Stopping any old process on ports 5050 / 5000...
powershell -NoProfile -Command ^
  "foreach ($p in 5050,5000) { Get-NetTCPConnection -LocalPort $p -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue } }"
timeout /t 2 /nobreak >nul

"%PY%" run_unattended.py

endlocal
pause
