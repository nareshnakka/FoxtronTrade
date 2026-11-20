@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title FoxtronTrade — Stop

set "ROOT=%~dp0"

echo.
echo ============================================
echo   FoxtronTrade — Stop
echo ============================================
echo.

echo Stopping engine / dashboard...
powershell -NoProfile -Command ^
  "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" -EA SilentlyContinue | ForEach-Object { " ^
  "  if ($_.CommandLine -and ($_.CommandLine -like '*run_unattended.py*' -or $_.CommandLine -like '*\\app.py*' -or $_.CommandLine -like '* app.py*')) { " ^
  "    Stop-Process -Id $_.ProcessId -Force -EA SilentlyContinue " ^
  "  } " ^
  "}; " ^
  "foreach ($p in 5050,5000) { " ^
  "  Get-NetTCPConnection -LocalPort $p -EA SilentlyContinue | ForEach-Object { " ^
  "    Stop-Process -Id $_.OwningProcess -Force -EA SilentlyContinue " ^
  "  } " ^
  "}"

if exist "%ROOT%data\unattended.lock" del /f /q "%ROOT%data\unattended.lock" >nul 2>&1
timeout /t 1 /nobreak >nul

echo Stopped.
echo.
pause
exit /b 0
