@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title FoxtronTrade

set "ROOT=%~dp0"
set "PY=%ROOT%.venv\Scripts\python.exe"
set "REQ=%ROOT%requirements.txt"
set "SETUP_OK=%ROOT%.venv\.setup_ok"
set "LOGDIR=%ROOT%data"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

echo.
echo ============================================
echo   FoxtronTrade — Start / Stop (24x7)
echo ============================================
echo.

REM ---- If already running: STOP and exit ----
call :is_running
if !ERRORLEVEL! EQU 1 (
  echo Status: RUNNING — stopping now...
  call :stop_all
  echo.
  echo Stopped. Run this bat again to start.
  echo.
  pause
  exit /b 0
)

echo Status: STOPPED — starting 24x7...
echo.

REM ---- First-run / missing venv: setup ----
call :ensure_setup
if errorlevel 1 (
  echo.
  echo SETUP FAILED. Install Python 3.11+ from https://www.python.org/downloads/
  echo Tick "Add python.exe to PATH", then run this bat again.
  echo.
  pause
  exit /b 1
)

REM ---- Always ensure option DB is verified after extract (auto-restore) ----
if not exist "%ROOT%data\.migrate_restore_ok" (
  echo Verifying captured options DB...
  "%PY%" "%ROOT%tools_migrate_restore.py"
  if errorlevel 1 (
    echo ERROR: option-chain DB missing/corrupt — re-pack from old laptop.
    pause
    exit /b 1
  )
)

REM ---- Keep laptop awake while plugged in (best-effort) ----
powercfg /change standby-timeout-ac 0 >nul 2>&1
powercfg /change monitor-timeout-ac 0 >nul 2>&1
powercfg /change hibernate-timeout-ac 0 >nul 2>&1

REM Clear stale listeners then launch supervisor (window stays open)
call :stop_ports
timeout /t 2 /nobreak >nul

set "PYTHONPATH=%ROOT%"
echo Dashboard: http://127.0.0.1:5050
echo Leave this window open. Run FoxtronTrade.bat again to STOP.
echo.
"%PY%" "%ROOT%run_unattended.py"
set "EC=!ERRORLEVEL!"
echo.
echo Supervisor exited code !EC!
pause
exit /b !EC!


:is_running
REM Returns 1 if unattended/app is running, else 0
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


:stop_all
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
exit /b 0


:stop_ports
powershell -NoProfile -Command ^
  "foreach ($p in 5050,5000) { Get-NetTCPConnection -LocalPort $p -EA SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -EA SilentlyContinue } }"
exit /b 0


:ensure_setup
if exist "%PY%" if exist "%SETUP_OK%" (
  echo Setup: OK ^(.venv ready^)
  exit /b 0
)

echo Setup: preparing Python virtual environment...
call :find_python
if errorlevel 1 exit /b 1

if not exist "%PY%" (
  echo   Creating .venv ...
  "%SYSPY%" -m venv "%ROOT%.venv"
  if errorlevel 1 (
    echo ERROR: could not create .venv
    exit /b 1
  )
)

echo   Upgrading pip ...
"%PY%" -m pip install --upgrade pip > "%LOGDIR%\setup_pip.log" 2>&1
echo   Installing requirements ...
"%PY%" -m pip install -r "%REQ%" > "%LOGDIR%\setup_reqs.log" 2>&1
if errorlevel 1 (
  echo ERROR: pip install failed — see data\setup_reqs.log
  exit /b 1
)

REM Quick import check
"%PY%" -c "import flask,kiteconnect,dotenv,pyotp,pandas,requests; print('imports_ok')" >nul 2>&1
if errorlevel 1 (
  echo ERROR: packages installed but imports failed
  exit /b 1
)

echo   Verifying / restoring option-chain DB ...
"%PY%" "%ROOT%tools_migrate_restore.py"
if errorlevel 1 (
  echo ERROR: option DB restore/verify failed — captured history missing or corrupt
  exit /b 1
)

echo ok> "%SETUP_OK%"
echo Setup: DONE
exit /b 0


:find_python
set "SYSPY="
where py >nul 2>&1
if not errorlevel 1 (
  for /f "delims=" %%I in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do set "SYSPY=%%I"
)
if not defined SYSPY (
  where python >nul 2>&1
  if not errorlevel 1 (
    for /f "delims=" %%I in ('python -c "import sys; print(sys.executable)" 2^>nul') do set "SYSPY=%%I"
  )
)
if not defined SYSPY (
  echo Python not found on PATH. Trying winget install Python 3.12 ...
  where winget >nul 2>&1
  if errorlevel 1 (
    echo ERROR: neither Python nor winget found.
    exit /b 1
  )
  winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
  if errorlevel 1 (
    echo ERROR: winget Python install failed.
    exit /b 1
  )
  REM Refresh PATH for this session
  set "PATH=%LocalAppData%\Programs\Python\Python312;%LocalAppData%\Programs\Python\Python312\Scripts;%PATH%"
  where py >nul 2>&1
  if not errorlevel 1 (
    for /f "delims=" %%I in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do set "SYSPY=%%I"
  )
  if not defined SYSPY (
    where python >nul 2>&1
    if not errorlevel 1 (
      for /f "delims=" %%I in ('python -c "import sys; print(sys.executable)" 2^>nul') do set "SYSPY=%%I"
    )
  )
)
if not defined SYSPY (
  echo ERROR: Python still not found after install attempt.
  exit /b 1
)
echo   Using system Python: %SYSPY%
exit /b 0
