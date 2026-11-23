@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title FoxtronTrade - Stop

echo.
echo ============================================
echo   FoxtronTrade - Stop
echo ============================================
echo.

if exist "%~dp0.venv\Scripts\python.exe" (
  "%~dp0.venv\Scripts\python.exe" "%~dp0tools_stop_start.py" stop
  echo.
  echo Status after stop:
  "%~dp0.venv\Scripts\python.exe" "%~dp0tools_stop_start.py" status
) else (
  echo Stopping via taskkill fallback...
  powershell -NoProfile -Command ^
    "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" -EA SilentlyContinue | ForEach-Object { " ^
    "  if ($_.CommandLine -and ($_.CommandLine -like '*run_unattended.py*' -or $_.CommandLine -like '*app.py*')) { " ^
    "    taskkill /F /T /PID $_.ProcessId 2>$null " ^
    "  } " ^
    "}; " ^
    "Get-NetTCPConnection -LocalPort 5050,5000 -ErrorAction SilentlyContinue | ForEach-Object { " ^
    "  taskkill /F /T /PID $_.OwningProcess 2>$null " ^
    "}; " ^
    "if (Test-Path '%~dp0data\unattended.lock') { Remove-Item -Force '%~dp0data\unattended.lock' }"
)

echo.
echo Stopped. You can close this window.
echo.
pause
exit /b 0
