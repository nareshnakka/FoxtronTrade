@echo off
REM FoxtronTrade - one-time / repair setup (Python + .venv + options DB)
REM Prefer system Python 3.11-3.13; otherwise extract embeddable Python into runtime\Python312.
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title FoxtronTrade - Setup

set "ROOT=%~dp0"
set "PY=%ROOT%.venv\Scripts\python.exe"
set "REQ=%ROOT%requirements.txt"
set "SETUP_OK=%ROOT%.venv\.setup_ok"
set "LOGDIR=%ROOT%data"
set "RUNTIME=%ROOT%runtime"
set "LOCALPY=%RUNTIME%\Python312\python.exe"
set "NOPAUSE=%~1"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
if not exist "%RUNTIME%" mkdir "%RUNTIME%"

echo.
echo ============================================
echo   FoxtronTrade - Setup
echo ============================================
echo.

if exist "%SETUP_OK%" (
  if exist "%ROOT%.venv\Scripts\python.exe" (
    echo Setup: OK ^(.venv ready^)
    if /I not "%NOPAUSE%"=="nopause" pause
    endlocal & exit /b 0
  )
  if exist "%ROOT%.venv\python_path.txt" (
    for /f "usebackq delims=" %%P in ("%ROOT%.venv\python_path.txt") do set "_OKPY=%%P"
    if defined _OKPY if exist "!_OKPY!" (
      echo Setup: OK ^(portable Python ready^)
      if /I not "%NOPAUSE%"=="nopause" pause
      endlocal & exit /b 0
    )
  )
  if exist "%LOCALPY%" (
    echo Setup: OK ^(portable Python ready^)
    if /I not "%NOPAUSE%"=="nopause" pause
    endlocal & exit /b 0
  )
)

echo Preparing Python virtual environment...
call :refresh_path
call :find_python
if errorlevel 1 (
  echo No suitable system Python - bootstrapping portable Python 3.12 ...
  powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%tools_bootstrap_python.ps1"
  if errorlevel 1 goto :fail
  call :try_py_exe "%LOCALPY%"
  if not defined SYSPY (
    echo ERROR: portable Python missing or unusable after bootstrap: %LOCALPY%
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
    echo   -m venv failed ^(common with embeddable^) - trying virtualenv...
    "%SYSPY%" -m pip install --upgrade virtualenv >> "%LOGDIR%\setup_venv.log" 2>&1
    "%SYSPY%" -m virtualenv "%ROOT%.venv" >> "%LOGDIR%\setup_venv.log" 2>&1
  )
  if not exist "%PY%" (
    echo   virtualenv also failed - installing packages into portable Python directly...
    set "PY=%LOCALPY%"
    if not exist "!PY!" set "PY=%SYSPY%"
    goto :install_pkgs
  )
)

:install_pkgs
if not defined PY set "PY=%ROOT%.venv\Scripts\python.exe"
if not exist "%PY%" (
  if exist "%LOCALPY%" (set "PY=%LOCALPY%") else (set "PY=%SYSPY%")
)
echo   Using app Python: %PY%

echo   Upgrading pip ...
"%PY%" -m pip install --upgrade pip > "%LOGDIR%\setup_pip.log" 2>&1
echo   Installing requirements ...
"%PY%" -m pip install -r "%REQ%" > "%LOGDIR%\setup_reqs.log" 2>&1
if errorlevel 1 (
  echo ERROR: pip install failed - see data\setup_reqs.log
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
  echo ERROR: option DB restore/verify failed - run Update.bat then Setup.bat again
  goto :fail
)

REM Remember which python Start.bat should use
if not exist "%ROOT%.venv" mkdir "%ROOT%.venv" >nul 2>&1
echo %PY%> "%SETUP_OK%"
echo %PY%> "%ROOT%.venv\python_path.txt"

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
set "PATH=%LocalAppData%\Programs\Python\Python312;%LocalAppData%\Programs\Python\Python312\Scripts;%LocalAppData%\Programs\Python\Python311;%LocalAppData%\Programs\Python\Python311\Scripts;%ProgramFiles%\Python312;%ProgramFiles%\Python312\Scripts;%PATH%"
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
REM for /f + quoted exe path drops stdout; probe via temp file instead.
set "_VERFILE=%TEMP%\foxtrade_pyver_%RANDOM%.txt"
"%_CAND%" -c "import sys; print(str(sys.version_info[0])+chr(46)+str(sys.version_info[1]))" > "%_VERFILE%" 2>nul
if exist "%_VERFILE%" (
  set /p SYSPY_VER=<"%_VERFILE%"
  del /q "%_VERFILE%" >nul 2>&1
)
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
