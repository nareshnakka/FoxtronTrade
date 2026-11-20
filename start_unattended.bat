@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo === FoxtronTrade UNATTENDED 24x7 ===
echo Keeps engine alive, auto-restarts on freeze, morning TOTP login.
echo Dashboard: http://127.0.0.1:5050
echo Leave this window open. Ctrl+C stops supervisor.
echo.

call "%~dp0FoxtronTrade_Setup.bat"
if errorlevel 1 (
  echo.
  echo SETUP FAILED — install Python 3.12 then retry:
  echo   winget install -e --id Python.Python.3.12
  echo Or use FoxtronTrade.bat
  pause
  exit /b 1
)

powercfg /change standby-timeout-ac 0 >nul 2>&1
powercfg /change monitor-timeout-ac 0 >nul 2>&1

set "PYTHONPATH=%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"

echo Stopping any old process on ports 5050 / 5000...
powershell -NoProfile -Command ^
  "foreach ($p in 5050,5000) { Get-NetTCPConnection -LocalPort $p -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue } }"
timeout /t 2 /nobreak >nul

"%PY%" run_unattended.py
set "EC=%ERRORLEVEL%"
echo.
echo Supervisor exited code %EC%
pause
exit /b %EC%
