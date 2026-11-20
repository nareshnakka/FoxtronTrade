@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title FoxtronTrade — Auto Update

set "ROOT=%~dp0"
set "REPO=https://github.com/nareshnakka/FoxtronTrade.git"
set "ZIPURL=https://github.com/nareshnakka/FoxtronTrade/archive/refs/heads/master.zip"
set "ZIPURL2=https://codeload.github.com/nareshnakka/FoxtronTrade/zip/refs/heads/master"
set "BRANCH=master"
set "PY=%ROOT%.venv\Scripts\python.exe"
set "LOGDIR=%ROOT%data"
set "LOG=%LOGDIR%\auto_update.log"
set "TMP=%TEMP%\FoxtronTrade_update_%RANDOM%"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
echo ==== %DATE% %TIME% update start ====>> "%LOG%"

echo.
echo ============================================
echo   FoxtronTrade — Auto Update from GitHub
echo ============================================
echo   Repo: %REPO%
echo.

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

REM Snapshot requirements hash to detect dep changes
set "REQ_BEFORE="
if exist "%ROOT%requirements.txt" (
  for /f %%H in ('certutil -hashfile "%ROOT%requirements.txt" SHA256 ^| findstr /v ":" ^| findstr /v "CertUtil"') do set "REQ_BEFORE=%%H"
)

set "USED_ZIP=0"

REM ---- Try git first; fall back to ZIP download ----
call :try_git_update
if errorlevel 1 (
  echo.
  echo Git update failed — trying direct ZIP download from GitHub...
  call :zip_update
  if errorlevel 1 (
    echo.
    echo ERROR: could not reach GitHub via git or ZIP.
    echo Check: internet, firewall, DNS, corporate proxy.
    echo See: %LOG%
    echo.
    echo Manual fallback:
    echo   1^) Open %ZIPURL%
    echo   2^) Extract over this folder
    echo   3^) Keep your .env and access_token.json
    echo.
    pause
    exit /b 1
  )
  set "USED_ZIP=1"
)

:after_pull
if "%USED_ZIP%"=="1" (
  echo Updated via ZIP download.
) else (
  for /f "delims=" %%C in ('git -C "%ROOT%" rev-parse --short HEAD 2^>nul') do set "NOW=%%C"
  echo Current: !NOW!
  git -C "%ROOT%" log -1 --format="  %%s (%%ci)" 2>nul
)
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

REM ---- Ensure options DB exists (extract seed zip if needed) ----
if exist "%PY%" (
  if not exist "%ROOT%data\option_chain_live\chain.db" (
    echo Options DB missing — restoring from seed zip...
    "%PY%" "%ROOT%tools_migrate_restore.py"
  ) else if not exist "%ROOT%data\.migrate_restore_ok" (
    echo Verifying options DB...
    "%PY%" "%ROOT%tools_migrate_restore.py"
  )
)

echo Update complete.
echo   Next: FoxtronTrade.bat
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


:try_git_update
where git >nul 2>&1
if errorlevel 1 (
  echo Git not installed — will use ZIP method.
  echo git_missing>> "%LOG%"
  exit /b 1
)

call :ensure_repo
if errorlevel 1 exit /b 1

echo Fetching latest via git...
git -C "%ROOT%" fetch --prune origin %BRANCH% >> "%LOG%" 2>&1
if errorlevel 1 (
  echo   git fetch failed.
  type "%LOG%" | more +0
  powershell -NoProfile -Command "Get-Content -LiteralPath '%LOG%' -Tail 20 -EA SilentlyContinue"
  exit /b 1
)

for /f "delims=" %%C in ('git -C "%ROOT%" rev-parse HEAD 2^>nul') do set "OLD=%%C"
for /f "delims=" %%C in ('git -C "%ROOT%" rev-parse origin/%BRANCH% 2^>nul') do set "NEW=%%C"

if /I "%OLD%"=="%NEW%" (
  echo Already up to date.
  echo   Commit: %OLD%
  exit /b 0
)

echo Updating %OLD:~0,7% -^> %NEW:~0,7% ...
echo   (.env / access_token.json stay local — not in git)

git -C "%ROOT%" merge --ff-only "origin/%BRANCH%" >> "%LOG%" 2>&1
if errorlevel 1 (
  echo Local changes blocked fast-forward — syncing to GitHub tip...
  git -C "%ROOT%" stash push -u -m "FoxtronTrade_Update auto-stash" >> "%LOG%" 2>&1
  git -C "%ROOT%" reset --hard "origin/%BRANCH%" >> "%LOG%" 2>&1
  if errorlevel 1 (
    echo   git reset failed.
    exit /b 1
  )
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

echo No .git folder — initializing for git updates...
git -C "%ROOT%" init >> "%LOG%" 2>&1
git -C "%ROOT%" remote add origin "%REPO%" >> "%LOG%" 2>&1
git -C "%ROOT%" fetch origin %BRANCH% >> "%LOG%" 2>&1
if errorlevel 1 (
  echo   first git fetch failed ^(ZIP fallback will try next^).
  exit /b 1
)
git -C "%ROOT%" checkout -B %BRANCH% "origin/%BRANCH%" >> "%LOG%" 2>&1
if errorlevel 1 (
  git -C "%ROOT%" reset --hard "origin/%BRANCH%" >> "%LOG%" 2>&1
  if errorlevel 1 exit /b 1
)
echo Linked to GitHub.
exit /b 0


:zip_update
REM Download master.zip and overlay code while keeping secrets / local runtime
if exist "%TMP%" rmdir /s /q "%TMP%" >nul 2>&1
mkdir "%TMP%" >nul 2>&1
set "ZIPFILE=%TMP%\master.zip"

echo   Downloading master.zip ...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ProgressPreference='SilentlyContinue'; " ^
  "$urls=@('%ZIPURL%','%ZIPURL2%'); " ^
  "$out='%ZIPFILE%'; $ok=$false; " ^
  "foreach($u in $urls){ " ^
  "  try { " ^
  "    Write-Host ('  GET ' + $u); " ^
  "    Invoke-WebRequest -Uri $u -OutFile $out -UseBasicParsing -TimeoutSec 120; " ^
  "    if((Test-Path $out) -and ((Get-Item $out).Length -gt 100KB)){ $ok=$true; break } " ^
  "  } catch { Write-Host ('  fail: ' + $_.Exception.Message); Add-Content -Path '%LOG%' -Value $_.Exception.Message } " ^
  "}; " ^
  "if(-not $ok){ exit 1 }; " ^
  "Write-Host ('  Downloaded {0:N1} MB' -f ((Get-Item $out).Length/1MB)); exit 0"
if errorlevel 1 (
  echo   ZIP download failed.
  exit /b 1
)

echo   Extracting...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Expand-Archive -LiteralPath '%ZIPFILE%' -DestinationPath '%TMP%\extract' -Force"
if errorlevel 1 (
  echo   Expand-Archive failed.
  exit /b 1
)

REM GitHub zip extracts to FoxtronTrade-master\
set "SRC="
for /d %%D in ("%TMP%\extract\*") do set "SRC=%%~fD"
if not defined SRC (
  echo   Could not find extracted folder.
  exit /b 1
)
echo   Source: %SRC%

echo   Backing up local secrets / live DB...
set "BAK=%TMP%\preserve"
mkdir "%BAK%" >nul 2>&1
if exist "%ROOT%.env" copy /y "%ROOT%.env" "%BAK%\.env" >nul
if exist "%ROOT%access_token.json" copy /y "%ROOT%access_token.json" "%BAK%\access_token.json" >nul
if exist "%ROOT%data\option_chain_live\chain.db" (
  mkdir "%BAK%\option_chain_live" >nul 2>&1
  copy /y "%ROOT%data\option_chain_live\chain.db" "%BAK%\option_chain_live\chain.db" >nul
  if exist "%ROOT%data\option_chain_live\chain.db-wal" copy /y "%ROOT%data\option_chain_live\chain.db-wal" "%BAK%\option_chain_live\" >nul
  if exist "%ROOT%data\option_chain_live\chain.db-shm" copy /y "%ROOT%data\option_chain_live\chain.db-shm" "%BAK%\option_chain_live\" >nul
)
if exist "%ROOT%data\trades.json" copy /y "%ROOT%data\trades.json" "%BAK%\trades.json" >nul

echo   Applying update ^(keeps .venv / runtime / secrets^)...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$src='%SRC%'; $dst='%ROOT%'.TrimEnd('\'); " ^
  "$skipDir=@('.git','.venv','venv','runtime','__pycache__','.localmode'); " ^
  "$skipName=@('.env','access_token.json'); " ^
  "function Copy-Tree($from,$to){ " ^
  "  Get-ChildItem -LiteralPath $from -Force | ForEach-Object { " ^
  "    if($_.PSIsContainer){ " ^
  "      if($skipDir -contains $_.Name){ return } " ^
  "      $target=Join-Path $to $_.Name; " ^
  "      if(-not (Test-Path $target)){ New-Item -ItemType Directory -Path $target | Out-Null } " ^
  "      Copy-Tree $_.FullName $target " ^
  "    } else { " ^
  "      if($skipName -contains $_.Name){ return } " ^
  "      Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $to $_.Name) -Force " ^
  "    } " ^
  "  } " ^
  "}; " ^
  "Copy-Tree $src $dst; " ^
  "Write-Host '  Files applied.'"
if errorlevel 1 (
  echo   File copy failed.
  exit /b 1
)

echo   Restoring local secrets / live DB...
if exist "%BAK%\.env" copy /y "%BAK%\.env" "%ROOT%.env" >nul
if exist "%BAK%\access_token.json" copy /y "%BAK%\access_token.json" "%ROOT%access_token.json" >nul
if exist "%BAK%\option_chain_live\chain.db" (
  if not exist "%ROOT%data\option_chain_live" mkdir "%ROOT%data\option_chain_live"
  copy /y "%BAK%\option_chain_live\*" "%ROOT%data\option_chain_live\" >nul
)
if exist "%BAK%\trades.json" copy /y "%BAK%\trades.json" "%ROOT%data\trades.json" >nul

rmdir /s /q "%TMP%" >nul 2>&1
echo   ZIP update OK.
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
