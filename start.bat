@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title FoxtronTrade — Start

set "ROOT=%~dp0"
set "PY=%ROOT%.venv\Scripts\python.exe"
set "LOGDIR=%ROOT%data"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

echo.
echo ============================================
echo   FoxtronTrade — Start ^(24x7^)
echo ============================================
echo.

call :is_running
if !ERRORLEVEL! EQU 1 (
  echo Already RUNNING. Use Stop.bat first if you want a clean restart.
  pause
  exit /b 0
)

echo Running Setup if needed...
call "%ROOT%Setup.bat" nopause
if errorlevel 1 (
  echo.
  echo Start aborted — Setup failed.
  pause
  exit /b 1
)

if not exist "%ROOT%data\.migrate_restore_ok" (
  echo Verifying options DB...
  "%PY%" "%ROOT%tools_migrate_restore.py"
  if errorlevel 1 (
    echo ERROR: options DB missing/corrupt — run Update.bat then Setup.bat
    pause
    exit /b 1
  )
)

powercfg /change standby-timeout-ac 0 >nul 2>&1
powercfg /change monitor-timeout-ac 0 >nul 2>&1
powercfg /change hibernate-timeout-ac 0 >nul 2>&1

call :stop_ports
timeout /t 2 /nobreak >nul

set "PYTHONPATH=%ROOT%"
echo Dashboard: http://127.0.0.1:5050
echo Leave this window open. Use Stop.bat to stop.
echo.
"%PY%" "%ROOT%run_unattended.py"
set "EC=!ERRORLEVEL!"
echo.
echo Supervisor exited code !EC!
pause
exit /b !EC!


:is_running
powershell -NoProfile -Command ^
  "$hit=$false; " ^
  "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" -EA SilentlyContinue | ForEach-Object { " ^
  "  if ($_.CommandLine -and ($_.CommandLine -like '*run_unattended.py*' -or $_.CommandLine -like '*app.py*')) { $hit=$true } " ^
  "}; " ^
  "if (-not $hit) { " ^
  "  foreach ($p in 5050,5000) { if (Get-NetTCPConnection -LocalPort $p -EA SilentlyContinue) { $hit=$true } } " ^
  "}; " ^
  "if ($hit) { exit 1 } else { exit 0 }"
exit /b %ERRORLEVEL%


:stop_ports
powershell -NoProfile -Command ^
  "foreach ($p in 5050,5000) { Get-NetTCPConnection -LocalPort $p -EA SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -EA SilentlyContinue } }"
exit /b 0
