@echo off
REM Shared .venv bootstrap for FoxtronTrade entry points.
REM Exit 0 = ready; non-zero = failed (see messages / data\setup_*.log)
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "ROOT=%~dp0"
set "PY=%ROOT%.venv\Scripts\python.exe"
set "REQ=%ROOT%requirements.txt"
set "SETUP_OK=%ROOT%.venv\.setup_ok"
set "LOGDIR=%ROOT%data"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

if exist "%PY%" if exist "%SETUP_OK%" (
  echo Setup: OK ^(.venv ready^)
  endlocal & exit /b 0
)

echo Setup: preparing Python virtual environment...
call :refresh_path
call :find_python
if errorlevel 1 (
  echo No suitable Python found. Installing Python 3.12 via winget...
  call :install_python312
  if errorlevel 1 (
    endlocal & exit /b 1
  )
  call :refresh_path
  timeout /t 2 /nobreak >nul
  call :find_python
  if errorlevel 1 (
    echo.
    echo ERROR: Python 3.12 installed but not visible in this window yet.
    echo Close this window, open a NEW one, then run FoxtronTrade.bat again.
    echo Or run:  "%LocalAppData%\Programs\Python\Python312\python.exe" -m venv .venv
    endlocal & exit /b 1
  )
)

if exist "%ROOT%.venv" if not exist "%SETUP_OK%" (
  echo   Removing incomplete .venv ...
  rmdir /s /q "%ROOT%.venv" >nul 2>&1
)

if not exist "%PY%" (
  echo   Creating .venv with: %SYSPY%
  "%SYSPY%" -m venv "%ROOT%.venv"
  if errorlevel 1 (
    echo ERROR: could not create .venv with that Python.
    echo Try: winget install -e --id Python.Python.3.12
    endlocal & exit /b 1
  )
)
if not exist "%PY%" (
  echo ERROR: .venv created but Scripts\python.exe missing.
  endlocal & exit /b 1
)

echo   Upgrading pip ...
"%PY%" -m pip install --upgrade pip > "%LOGDIR%\setup_pip.log" 2>&1
echo   Installing requirements ...
"%PY%" -m pip install -r "%REQ%" > "%LOGDIR%\setup_reqs.log" 2>&1
if errorlevel 1 (
  echo ERROR: pip install failed — see data\setup_reqs.log
  echo ^(Common on Python 3.14+/3.15: no wheels yet — use Python 3.12^)
  endlocal & exit /b 1
)

"%PY%" -c "import flask,kiteconnect,dotenv,pyotp,pandas,requests; print('imports_ok')" >nul 2>&1
if errorlevel 1 (
  echo ERROR: packages installed but imports failed
  "%PY%" -c "import flask,kiteconnect,dotenv,pyotp,pandas,requests; print('imports_ok')"
  endlocal & exit /b 1
)

echo   Verifying / restoring option-chain DB ...
"%PY%" "%ROOT%tools_migrate_restore.py"
if errorlevel 1 (
  echo ERROR: option DB restore/verify failed — run AutoUpdate.bat or re-pack seed zip
  endlocal & exit /b 1
)

echo ok> "%SETUP_OK%"
echo Setup: DONE
endlocal & exit /b 0


:refresh_path
REM Pull latest Machine+User PATH into this CMD (winget install does not update current session)
for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command "$m=[Environment]::GetEnvironmentVariable('Path','Machine'); $u=[Environment]::GetEnvironmentVariable('Path','User'); Write-Output ($m + ';' + $u)"`) do set "PATH=%%P"
REM Always prepend common Python 3.12/3.11 locations (even if PATH registry lag)
set "PATH=%LocalAppData%\Programs\Python\Python312;%LocalAppData%\Programs\Python\Python312\Scripts;%LocalAppData%\Programs\Python\Python311;%LocalAppData%\Programs\Python\Python311\Scripts;%LocalAppData%\Programs\Python\Python313;%LocalAppData%\Programs\Python\Python313\Scripts;%ProgramFiles%\Python312;%ProgramFiles%\Python312\Scripts;%ProgramFiles%\Python311;%ProgramFiles%\Python311\Scripts;%ProgramFiles%\Python313;%ProgramFiles%\Python313\Scripts;%PATH%"
exit /b 0


:find_python
set "SYSPY="
set "SYSPY_VER="

REM 1) Direct known install paths (most reliable after winget)
call :try_py_exe "%LocalAppData%\Programs\Python\Python312\python.exe"
if defined SYSPY goto :find_python_ok
call :try_py_exe "%LocalAppData%\Programs\Python\Python311\python.exe"
if defined SYSPY goto :find_python_ok
call :try_py_exe "%LocalAppData%\Programs\Python\Python313\python.exe"
if defined SYSPY goto :find_python_ok
call :try_py_exe "%ProgramFiles%\Python312\python.exe"
if defined SYSPY goto :find_python_ok
call :try_py_exe "%ProgramFiles%\Python311\python.exe"
if defined SYSPY goto :find_python_ok
call :try_py_exe "%ProgramFiles%\Python313\python.exe"
if defined SYSPY goto :find_python_ok
call :try_py_exe "%ProgramFiles(x86)%\Python312\python.exe"
if defined SYSPY goto :find_python_ok

REM 2) py launcher preferred versions
call :try_py_cmd py -3.12
if defined SYSPY goto :find_python_ok
call :try_py_cmd py -3.11
if defined SYSPY goto :find_python_ok
call :try_py_cmd py -3.13
if defined SYSPY goto :find_python_ok
call :try_py_cmd py -3
if defined SYSPY goto :find_python_ok

REM 3) PATH python (skip Windows Store stub)
call :try_py_cmd python
if defined SYSPY goto :find_python_ok
call :try_py_cmd python3
if defined SYSPY goto :find_python_ok

REM 4) PowerShell deep search under LocalAppData\Programs\Python
for /f "usebackq delims=" %%I in (`powershell -NoProfile -Command "$roots=@($env:LocalAppData+'\Programs\Python',$env:ProgramFiles+'\Python*'); Get-ChildItem -Path $roots -Filter python.exe -Recurse -EA SilentlyContinue | Where-Object { $_.FullName -notmatch 'WindowsApps|\\Lib\\|\\Scripts\\' } | Select-Object -ExpandProperty FullName"`) do (
  call :try_py_exe "%%I"
  if defined SYSPY goto :find_python_ok
)

exit /b 1

:find_python_ok
echo   Using system Python %SYSPY_VER%: %SYSPY%
exit /b 0


:try_py_exe
REM Absolute python.exe path
set "SYSPY="
set "SYSPY_VER="
set "_CAND=%~1"
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


:install_python312
where winget >nul 2>&1
if errorlevel 1 (
  echo ERROR: winget not found. Install Python 3.12 manually:
  echo   https://www.python.org/downloads/release/python-31210/
  echo Tick "Add python.exe to PATH", then reopen this bat.
  exit /b 1
)
echo   Running: winget install Python.Python.3.12 ...
winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
if errorlevel 1 (
  echo WARN: winget returned error — checking if Python 3.12 already exists...
)
REM Even if winget says "already installed", refresh and continue
call :refresh_path
exit /b 0
