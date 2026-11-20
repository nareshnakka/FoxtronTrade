@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Pack FoxtronTrade migrate zip

echo ============================================
echo   Pack migration zip ^(code + data + .env^)
echo ============================================
echo.
echo Stopping FoxtronTrade if running...
powershell -NoProfile -Command ^
  "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" -EA SilentlyContinue | ForEach-Object { " ^
  "  if ($_.CommandLine -and ($_.CommandLine -like '*run_unattended.py*' -or $_.CommandLine -like '*app.py*')) { " ^
  "    Stop-Process -Id $_.ProcessId -Force -EA SilentlyContinue } }; " ^
  "foreach ($p in 5050,5000) { Get-NetTCPConnection -LocalPort $p -EA SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -EA SilentlyContinue } }"
timeout /t 2 /nobreak >nul

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0pack_migrate.ps1"
set "EC=%ERRORLEVEL%"
echo.
if %EC% NEQ 0 (
  echo PACK FAILED code %EC%
) else (
  echo PACK DONE
)
pause
exit /b %EC%
