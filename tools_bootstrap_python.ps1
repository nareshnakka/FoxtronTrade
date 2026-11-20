# Bootstrap a portable Python 3.12 under runtime\Python312 using the official embeddable zip.
# No admin / no PATH / no EXE installer (those often fail with TargetDir on locked-down PCs).
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Runtime = Join-Path $Root "runtime"
$Dest = Join-Path $Runtime "Python312"
$PyExe = Join-Path $Dest "python.exe"
$Ver = "3.12.10"
$ZipUrl = "https://www.python.org/ftp/python/$Ver/python-$Ver-embed-amd64.zip"
$ZipPath = Join-Path $Runtime "python-embed.zip"
$GetPipUrl = "https://bootstrap.pypa.io/get-pip.py"
$GetPip = Join-Path $Runtime "get-pip.py"

New-Item -ItemType Directory -Force -Path $Runtime | Out-Null

if (Test-Path $PyExe) {
  Write-Host "  Local Python already present: $PyExe"
  exit 0
}

Write-Host "  Downloading Python $Ver embeddable zip..."
Write-Host "  $ZipUrl"
$ProgressPreference = "SilentlyContinue"
try {
  Invoke-WebRequest -Uri $ZipUrl -OutFile $ZipPath -UseBasicParsing -TimeoutSec 180
} catch {
  Write-Host ("ERROR: download failed: " + $_.Exception.Message)
  Write-Host "Manual: open $ZipUrl and extract into $Dest"
  exit 1
}
$len = (Get-Item $ZipPath).Length
if ($len -lt 3MB) {
  Write-Host "ERROR: download too small ($len bytes)"
  exit 1
}
Write-Host ("  Downloaded {0:N1} MB" -f ($len / 1MB))

if (Test-Path $Dest) { Remove-Item -Recurse -Force $Dest }
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
Write-Host "  Extracting to $Dest ..."
Expand-Archive -LiteralPath $ZipPath -DestinationPath $Dest -Force
Remove-Item -Force $ZipPath -ErrorAction SilentlyContinue

if (-not (Test-Path $PyExe)) {
  Write-Host "ERROR: python.exe missing after extract: $PyExe"
  Get-ChildItem $Dest | Select-Object -First 20 Name | ForEach-Object { Write-Host ("    " + $_.Name) }
  exit 1
}

# Enable site-packages so pip works (embed ._pth disables it by default)
$pth = Get-ChildItem -Path $Dest -Filter "python*._pth" | Select-Object -First 1
if ($null -ne $pth) {
  $lines = @(
    "python312.zip",
    ".",
    "Lib\\site-packages",
    "import site"
  )
  # Keep zip name from existing pth if present
  $raw = Get-Content -LiteralPath $pth.FullName
  $zipLine = $raw | Where-Object { $_ -match '\.zip$' } | Select-Object -First 1
  if ($zipLine) { $lines[0] = $zipLine.Trim() }
  Set-Content -LiteralPath $pth.FullName -Value ($lines -join "`r`n") -Encoding ASCII
  Write-Host ("  Enabled site-packages in " + $pth.Name)
}

Write-Host "  Installing pip..."
try {
  Invoke-WebRequest -Uri $GetPipUrl -OutFile $GetPip -UseBasicParsing -TimeoutSec 120
} catch {
  Write-Host ("ERROR: get-pip.py download failed: " + $_.Exception.Message)
  exit 1
}
& $PyExe $GetPip --no-warn-script-location
if ($LASTEXITCODE -ne 0) {
  Write-Host "ERROR: get-pip failed"
  exit 1
}

Write-Host "  Installing virtualenv helper..."
& $PyExe -m pip install --upgrade pip virtualenv --no-warn-script-location
if ($LASTEXITCODE -ne 0) {
  Write-Host "ERROR: pip install virtualenv failed"
  exit 1
}

& $PyExe -c "import sys; print('local_python', sys.version)"
if ($LASTEXITCODE -ne 0) {
  Write-Host "ERROR: local python.exe does not run"
  exit 1
}

Remove-Item -Force $GetPip -ErrorAction SilentlyContinue
Write-Host "  Local Python ready."
exit 0
