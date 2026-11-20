@echo off
setlocal EnableExtensions
cd /d "%~dp0"

call "%~dp0FoxtronTrade_Setup.bat"
if errorlevel 1 (
  echo.
  echo SETUP FAILED — install Python 3.12 then retry:
  echo   winget install -e --id Python.Python.3.12
  pause
  exit /b 1
)

echo Stopping any process on ports 5050 and 5000...
powershell -NoProfile -Command ^
  "foreach ($p in 5050,5000) { Get-NetTCPConnection -LocalPort $p -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue } }"
timeout /t 2 /nobreak >nul

set "PYTHONPATH=%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"

if not exist "%~dp0access_token.json" (
  echo No access_token.json — login from dashboard after start, or run auto-login.
)

echo Starting FoxtronTrade dashboard...
echo Dashboard: http://127.0.0.1:5050
echo Press Ctrl+C to stop.
echo.
"%PY%" app.py
set "EC=%ERRORLEVEL%"
pause
exit /b %EC%
