@echo off
REM FoxtronTrade — one-time / repair setup (Python + .venv + options DB)
REM Prefer system Python 3.11-3.13; otherwise install project-local runtime\Python312.
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title FoxtronTrade — Setup

set "ROOT=%~dp0"
set "PY=%ROOT%.venv\Scripts\python.exe"
set "REQ=%ROOT%requirements.txt"
set "SETUP_OK=%ROOT%.venv\.setup_ok"
set "LOGDIR=%ROOT%data"
set "RUNTIME=%ROOT%runtime"
set "LOCALPY=%RUNTIME%\Python312\python.exe"
set "PY_VER=3.12.10"
set "PY_URL=https://www.python.org/ftp/python/%PY_VER%/python-%PY_VER%-amd64.exe"
set "PY_INSTALLER=%RUNTIME%\python-%PY_VER%-amd64.exe"
set "NOPAUSE=%~1"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
if not exist "%RUNTIME%" mkdir "%RUNTIME%"

echo.
echo ============================================
echo   FoxtronTrade — Setup
echo ============================================
echo.

if exist "%PY%" if exist "%SETUP_OK%" (
  echo Setup: OK ^(.venv ready^)
  if /I not "%NOPAUSE%"=="nopause" pause
  endlocal & exit /b 0
)

echo Preparing Python virtual environment...
call :refresh_path
call :find_python
if errorlevel 1 (
  echo No suitable system Python — installing project-local Python %PY_VER% ...
  call :install_local_python
  if errorlevel 1 goto :fail
  call :try_py_exe "%LOCALPY%"
  if not defined SYSPY (
    echo ERROR: local Python install finished but runtime\Python312\python.exe missing.
    goto :fail
  )
)

echo   Base interpreter: %SYSPY_VER%  %SYSPY%

if exist "%ROOT%.venv" if not exist "%SETUP_OK%" (
  echo   Removing incomplete .venv ...
  rmdir /s /q "%ROOT%.venv" >nul 2>&1
)

if not exist "%PY%" (
  echo   Creating .venv ...
  "%SYSPY%" -m venv "%ROOT%.venv" > "%LOGDIR%\setup_venv.log" 2>&1
  if errorlevel 1 (
    echo ERROR: could not create .venv — see data\setup_venv.log
    type "%LOGDIR%\setup_venv.log"
    goto :fail
  )
)
if not exist "%PY%" (
  echo ERROR: .venv created but Scripts\python.exe missing.
  goto :fail
)

echo   Upgrading pip ...
"%PY%" -m pip install --upgrade pip > "%LOGDIR%\setup_pip.log" 2>&1
echo   Installing requirements ...
"%PY%" -m pip install -r "%REQ%" > "%LOGDIR%\setup_reqs.log" 2>&1
if errorlevel 1 (
  echo ERROR: pip install failed — see data\setup_reqs.log
  echo.
  echo --- last lines of data\setup_reqs.log ---
  powershell -NoProfile -Command "Get-Content -LiteralPath '%LOGDIR%\setup_reqs.log' -Tail 40 -EA SilentlyContinue"
  goto :fail
)

"%PY%" -c "import flask,kiteconnect,dotenv,pyotp,pandas,requests; print('imports_ok')" >nul 2>&1
if errorlevel 1 (
  echo ERROR: packages installed but imports failed
  "%PY%" -c "import flask,kiteconnect,dotenv,pyotp,pandas,requests; print('imports_ok')"
  goto :fail
)

echo   Verifying / restoring option-chain DB ...
"%PY%" "%ROOT%tools_migrate_restore.py"
if errorlevel 1 (
  echo ERROR: option DB restore/verify failed — run Update.bat then Setup.bat again
  goto :fail
)

echo ok> "%SETUP_OK%"
echo.
echo Setup: DONE
echo Next: double-click Start.bat
echo.
if /I not "%NOPAUSE%"=="nopause" pause
endlocal & exit /b 0

:fail
echo.
echo SETUP FAILED.
echo Tip: delete .venv and runtime folders, check internet, run Setup.bat again.
echo.
if /I not "%NOPAUSE%"=="nopause" pause
endlocal & exit /b 1


:refresh_path
for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command "$m=[Environment]::GetEnvironmentVariable('Path','Machine'); $u=[Environment]::GetEnvironmentVariable('Path','User'); if($m -and $u){$m+';'+$u}elseif($m){$m}else{$u}"`) do set "PATH=%%P"
set "PATH=%LocalAppData%\Programs\Python\Python312;%LocalAppData%\Programs\Python\Python312\Scripts;%LocalAppData%\Programs\Python\Python311;%LocalAppData%\Programs\Python\Python311\Scripts;%LocalAppData%\Programs\Python\Python313;%LocalAppData%\Programs\Python\Python313\Scripts;%ProgramFiles%\Python312;%ProgramFiles%\Python312\Scripts;%PATH%"
exit /b 0


:find_python
set "SYSPY="
set "SYSPY_VER="
call :try_py_exe "%LOCALPY%"
if defined SYSPY exit /b 0
call :try_py_exe "%LocalAppData%\Programs\Python\Python312\python.exe"
if defined SYSPY exit /b 0
call :try_py_exe "%LocalAppData%\Programs\Python\Python311\python.exe"
if defined SYSPY exit /b 0
call :try_py_exe "%LocalAppData%\Programs\Python\Python313\python.exe"
if defined SYSPY exit /b 0
call :try_py_exe "%ProgramFiles%\Python312\python.exe"
if defined SYSPY exit /b 0
call :try_py_exe "%ProgramFiles%\Python311\python.exe"
if defined SYSPY exit /b 0
call :try_py_exe "%ProgramFiles%\Python313\python.exe"
if defined SYSPY exit /b 0
call :try_py_cmd py -3.12
if defined SYSPY exit /b 0
call :try_py_cmd py -3.11
if defined SYSPY exit /b 0
call :try_py_cmd py -3.13
if defined SYSPY exit /b 0
call :try_py_cmd py -3
if defined SYSPY exit /b 0
call :try_py_cmd python
if defined SYSPY exit /b 0
exit /b 1


:try_py_exe
set "SYSPY="
set "SYSPY_VER="
set "_CAND=%~1"
if "%_CAND%"=="" exit /b 1
if not exist "%_CAND%" exit /b 1
echo %_CAND% | findstr /I "WindowsApps" >nul && exit /b 1
for /f "delims=" %%V in ('"%_CAND%" -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')" 2^>nul') do set "SYSPY_VER=%%V"
if not defined SYSPY_VER exit /b 1
if not "!SYSPY_VER!"=="3.11" if not "!SYSPY_VER!"=="3.12" if not "!SYSPY_VER!"=="3.13" (
  echo   Skipping Python !SYSPY_VER! at %_CAND% ^(need 3.11-3.13^)
  set "SYSPY_VER="
  exit /b 1
)
set "SYSPY=%_CAND%"
exit /b 0


:try_py_cmd
set "SYSPY="
set "SYSPY_VER="
set "_C1=%~1"
set "_C2=%~2"
set "_CAND="
if /I "%_C1%"=="py" (
  where py >nul 2>&1 || exit /b 1
  for /f "delims=" %%I in ('py %_C2% -c "import sys; print(sys.executable)" 2^>nul') do set "_CAND=%%I"
) else (
  where %_C1% >nul 2>&1 || exit /b 1
  for /f "delims=" %%I in ('%_C1% -c "import sys; print(sys.executable)" 2^>nul') do set "_CAND=%%I"
)
if not defined _CAND exit /b 1
call :try_py_exe "!_CAND!"
exit /b %ERRORLEVEL%


:install_local_python
if exist "%LOCALPY%" (
  echo   Local runtime already present: %LOCALPY%
  exit /b 0
)
echo   Downloading Python %PY_VER% ^(official installer^)...
echo   URL: %PY_URL%
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ProgressPreference='SilentlyContinue'; " ^
  "try { " ^
  "  Invoke-WebRequest -Uri '%PY_URL%' -OutFile '%PY_INSTALLER%' -UseBasicParsing; " ^
  "  if (-not (Test-Path '%PY_INSTALLER%')) { throw 'download missing' }; " ^
  "  $len=(Get-Item '%PY_INSTALLER%').Length; " ^
  "  if ($len -lt 5MB) { throw \"download too small: $len bytes\" }; " ^
  "  Write-Host ('  Downloaded {0:N1} MB' -f ($len/1MB)); " ^
  "} catch { " ^
  "  Write-Host ('ERROR: download failed: ' + $_.Exception.Message); " ^
  "  exit 1 " ^
  "}"
if errorlevel 1 (
  echo ERROR: could not download Python installer. Check internet / firewall.
  echo Manual: open %PY_URL%  OR place python.exe at: %LOCALPY%
  exit /b 1
)
echo   Installing into: %RUNTIME%\Python312
"%PY_INSTALLER%" /quiet InstallAllUsers=0 PrependPath=0 Include_test=0 Include_launcher=0 Include_doc=0 Shortcuts=0 SimpleInstall=1 TargetDir="%RUNTIME%\Python312"
set "EC=!ERRORLEVEL!"
if not exist "%LOCALPY%" (
  echo ERROR: installer finished but python.exe not found at %LOCALPY%
  echo Installer exit code: !EC!
  echo Try: "%PY_INSTALLER%"
  exit /b 1
)
"%LOCALPY%" -c "import sys; print('local_python', sys.version)" || (
  echo ERROR: local python.exe does not run.
  exit /b 1
)
echo   Local Python ready.
del /f /q "%PY_INSTALLER%" >nul 2>&1
exit /b 0
