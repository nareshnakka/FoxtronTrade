@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title FoxtronTrade - Start

set "ROOT=%~dp0"
set "LOGDIR=%ROOT%data"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

echo.
echo ============================================
echo   FoxtronTrade - Start (24x7)
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
  echo Start aborted - Setup failed.
  pause
  exit /b 1
)

call :resolve_py
if not defined PY (
  echo ERROR: no Python found after Setup.
  pause
  exit /b 1
)
echo   Python: %PY%

if not exist "%ROOT%.env" (
  echo.
  echo ERROR: .env missing in %ROOT%
  echo   Copy .env from your other PC into this folder ^(next to app.py^).
  echo   .env is never in git — Update.bat will not create it.
  echo.
  pause
  exit /b 1
)

REM Stuck Logout flag blocks Login until cleared — Start always allows a fresh session.
if exist "%ROOT%data\manual_logout.flag" (
  echo Clearing stuck Logout flag so Login can run...
  del /f /q "%ROOT%data\manual_logout.flag" >nul 2>&1
)

echo Checking Kite auto-login /.env ...
"%PY%" "%ROOT%tools_auth_fix.py"
if errorlevel 1 (
  echo.
  echo WARN: auto-login not ready — Login button may open the browser instead.
  echo   Fix .env keys: KITE_API_KEY, KITE_API_SECRET, KITE_USER_ID, KITE_PASSWORD, KITE_TOTP_SECRET
  echo.

)

if not exist "%ROOT%data\.migrate_restore_ok" (
  echo Verifying options DB...
  "%PY%" "%ROOT%tools_migrate_restore.py"
  if errorlevel 1 (
    echo ERROR: options DB missing/corrupt - run Update.bat then Setup.bat
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


:resolve_py
set "PY="
if exist "%ROOT%.venv\Scripts\python.exe" (
  set "PY=%ROOT%.venv\Scripts\python.exe"
  exit /b 0
)
if exist "%ROOT%.venv\python_path.txt" (
  for /f "usebackq delims=" %%P in ("%ROOT%.venv\python_path.txt") do set "PY=%%P"
  if defined PY if exist "!PY!" exit /b 0
)
if exist "%ROOT%runtime\Python312\python.exe" (
  set "PY=%ROOT%runtime\Python312\python.exe"
  exit /b 0
)
exit /b 1


:is_running
REM Prefer Python status (clears stale-lock false positives); exit 1 = running
if exist "%ROOT%.venv\Scripts\python.exe" (
  "%ROOT%.venv\Scripts\python.exe" "%ROOT%tools_stop_start.py" status >nul 2>&1
  exit /b %ERRORLEVEL%
)
powershell -NoProfile -Command ^
  "$hit=$false; " ^
  "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" -EA SilentlyContinue | ForEach-Object { " ^
  "  if ($_.CommandLine -and ($_.CommandLine -like '*run_unattended.py*' -or $_.CommandLine -like '*\\app.py*' -or $_.CommandLine -like '* app.py*')) { $hit=$true } " ^
  "}; " ^
  "if (-not $hit) { " ^
  "  foreach ($p in 5050,5000) { " ^
  "    if (Get-NetTCPConnection -LocalPort $p -State Listen -EA SilentlyContinue) { $hit=$true } " ^
  "  } " ^
  "}; " ^
  "if ($hit) { exit 1 } else { exit 0 }"
exit /b %ERRORLEVEL%


:stop_ports
if exist "%ROOT%.venv\Scripts\python.exe" (
  "%ROOT%.venv\Scripts\python.exe" "%ROOT%tools_stop_start.py" stop
) else (
  powershell -NoProfile -Command ^
    "Get-NetTCPConnection -LocalPort 5050,5000 -State Listen -EA SilentlyContinue | ForEach-Object { taskkill /F /T /PID $_.OwningProcess 2>$null }; " ^
    "if (Test-Path '%ROOT%data\unattended.lock') { Remove-Item -Force '%ROOT%data\unattended.lock' }"
)
exit /b 0
