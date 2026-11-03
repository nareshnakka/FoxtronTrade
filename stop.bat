@echo off
setlocal
cd /d "%~dp0"

echo Stopping FoxtronTrade on port 5050...
powershell -NoProfile -Command ^
  "Get-NetTCPConnection -LocalPort 5050 -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"

timeout /t 1 /nobreak >nul
echo Stopped.
endlocal
