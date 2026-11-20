@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title FoxtronTrade — Auto Update

set "ROOT=%~dp0"
set "REPO=https://github.com/nareshnakka/FoxtronTrade.git"
set "BRANCH=master"
set "PY=%ROOT%.venv\Scripts\python.exe"
set "LOGDIR=%ROOT%data"
set "LOG=%LOGDIR%\auto_update.log"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

echo.
echo ============================================
echo   FoxtronTrade — Auto Update from GitHub
echo ============================================
echo   Repo: %REPO%
echo   Branch: %BRANCH%
echo.

REM ---- Ensure git ----
call :ensure_git
if errorlevel 1 (
  echo.
  pause
  exit /b 1
)

REM ---- Stop trading while updating ----
call :is_running
if !ERRORLEVEL! EQU 1 (
  echo Engine is RUNNING — stopping before update...
  call :stop_all
  echo Stopped.
  echo.
  set "WAS_RUNNING=1"
) else (
  set "WAS_RUNNING=0"
)

REM ---- Point at GitHub (init if migrate-zip had no .git) ----
call :ensure_repo
if errorlevel 1 (
  echo.
  pause
  exit /b 1
)

REM Snapshot requirements hash to detect dep changes
set "REQ_BEFORE="
if exist "%ROOT%requirements.txt" (
  for /f %%H in ('certutil -hashfile "%ROOT%requirements.txt" SHA256 ^| findstr /v ":" ^| findstr /v "CertUtil"') do set "REQ_BEFORE=%%H"
)

echo Fetching latest from GitHub...
git -C "%ROOT%" fetch origin %BRANCH% >> "%LOG%" 2>&1
if errorlevel 1 (
  echo ERROR: git fetch failed. Check internet / GitHub access.
  echo See: %LOG%
  pause
  exit /b 1
)

for /f "delims=" %%C in ('git -C "%ROOT%" rev-parse HEAD 2^>nul') do set "OLD=%%C"
for /f "delims=" %%C in ('git -C "%ROOT%" rev-parse origin/%BRANCH% 2^>nul') do set "NEW=%%C"

if /I "%OLD%"=="%NEW%" (
  echo Already up to date.
  echo   Commit: %OLD%
  goto :after_pull
)

echo Updating %OLD:~0,7% -^> %NEW:~0,7% ...
echo   (.env / access_token.json stay local — they are not in git)

REM Prefer clean fast-forward; if local edits block it, stash then reset to remote
git -C "%ROOT%" merge --ff-only "origin/%BRANCH%" >> "%LOG%" 2>&1
if errorlevel 1 (
  echo Local changes blocked fast-forward — syncing to GitHub tip...
  git -C "%ROOT%" stash push -u -m "FoxtronTrade_Update auto-stash" >> "%LOG%" 2>&1
  git -C "%ROOT%" reset --hard "origin/%BRANCH%" >> "%LOG%" 2>&1
  if errorlevel 1 (
    echo ERROR: could not reset to origin/%BRANCH%
    echo See: %LOG%
    pause
    exit /b 1
  )
  echo Synced to origin/%BRANCH%.
  echo   Note: any local code edits were overwritten; stash kept in git stash list.
)

:after_pull
for /f "delims=" %%C in ('git -C "%ROOT%" rev-parse --short HEAD 2^>nul') do set "NOW=%%C"
echo Current: %NOW%
git -C "%ROOT%" log -1 --format="  %%s (%%ci)" 2>nul
echo.

REM ---- Refresh Python deps if requirements changed ----
set "REQ_AFTER="
if exist "%ROOT%requirements.txt" (
  for /f %%H in ('certutil -hashfile "%ROOT%requirements.txt" SHA256 ^| findstr /v ":" ^| findstr /v "CertUtil"') do set "REQ_AFTER=%%H"
)
if exist "%PY%" (
  if /I not "%REQ_BEFORE%"=="%REQ_AFTER%" (
    echo requirements.txt changed — updating .venv packages...
    "%PY%" -m pip install -r "%ROOT%requirements.txt" >> "%LOG%" 2>&1
    if errorlevel 1 (
      echo WARN: pip install had errors — see %LOG%
    ) else (
      echo Packages updated.
    )
    echo.
  )
)

REM ---- Ensure options DB exists (extract seed zip from git if needed) ----
if exist "%PY%" (
  if not exist "%ROOT%data\option_chain_live\chain.db" (
    echo Options DB missing — restoring from GitHub seed zip...
    "%PY%" "%ROOT%tools_migrate_restore.py"
  ) else if not exist "%ROOT%data\.migrate_restore_ok" (
    echo Verifying options DB...
    "%PY%" "%ROOT%tools_migrate_restore.py"
  )
)

echo Update complete.
echo   Dashboard start: FoxtronTrade.bat
echo.

if "%WAS_RUNNING%"=="1" (
  echo Engine was running before update.
  choice /C YN /M "Start FoxtronTrade now"
  if errorlevel 2 goto :done
  if errorlevel 1 (
    echo Starting...
    start "FoxtronTrade" cmd /c ""%ROOT%FoxtronTrade.bat""
  )
)

:done
echo.
pause
exit /b 0


:ensure_git
where git >nul 2>&1
if not errorlevel 1 exit /b 0
echo Git not found. Installing via winget...
where winget >nul 2>&1
if errorlevel 1 (
  echo ERROR: Git missing and winget not available.
  echo Install Git from https://git-scm.com/download/win then re-run.
  exit /b 1
)
winget install -e --id Git.Git --accept-package-agreements --accept-source-agreements
if errorlevel 1 (
  echo ERROR: winget Git install failed.
  exit /b 1
)
set "PATH=%ProgramFiles%\Git\cmd;%ProgramFiles%\Git\bin;%PATH%"
where git >nul 2>&1
if errorlevel 1 (
  echo ERROR: Git still not on PATH. Close this window, reopen, run again.
  exit /b 1
)
exit /b 0


:ensure_repo
if exist "%ROOT%.git" (
  git -C "%ROOT%" remote get-url origin >nul 2>&1
  if errorlevel 1 (
    git -C "%ROOT%" remote add origin "%REPO%" >> "%LOG%" 2>&1
  ) else (
    git -C "%ROOT%" remote set-url origin "%REPO%" >> "%LOG%" 2>&1
  )
  exit /b 0
)

echo No .git folder (migrate zip). Linking this folder to GitHub...
git -C "%ROOT%" init >> "%LOG%" 2>&1
git -C "%ROOT%" remote add origin "%REPO%" >> "%LOG%" 2>&1
git -C "%ROOT%" fetch origin %BRANCH% >> "%LOG%" 2>&1
if errorlevel 1 (
  echo ERROR: first fetch failed. Check internet / repo access.
  echo See: %LOG%
  exit /b 1
)

REM Track remote without wiping ignored local secrets / live DB
git -C "%ROOT%" checkout -B %BRANCH% "origin/%BRANCH%" >> "%LOG%" 2>&1
if errorlevel 1 (
  echo Local files conflict with first sync — hard-syncing tracked files...
  git -C "%ROOT%" reset --hard "origin/%BRANCH%" >> "%LOG%" 2>&1
  if errorlevel 1 (
    echo ERROR: could not attach to origin/%BRANCH%
    exit /b 1
  )
)
echo Linked to GitHub. Local .env / access_token.json / ignored data kept.
exit /b 0


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
