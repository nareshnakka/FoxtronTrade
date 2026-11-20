# Build a portable FoxtronTrade zip (excludes .venv / .git / caches).
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Stamp = Get-Date -Format "yyyyMMdd_HHmm"
$OutDir = Join-Path (Split-Path $Root -Parent) "FoxtronTrade_migrate"
if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir | Out-Null }
$ZipPath = Join-Path $OutDir "FoxtronTrade_MIGRATE_$Stamp.zip"
$Stage = Join-Path $env:TEMP "FoxtronTrade_pack_$Stamp"

Write-Host "Root : $Root"
Write-Host "Zip  : $ZipPath"
Write-Host "Stage: $Stage"

if (Test-Path $Stage) { Remove-Item -Recurse -Force $Stage }
New-Item -ItemType Directory -Path $Stage | Out-Null
$Dest = Join-Path $Stage "FoxtronTrade"
New-Item -ItemType Directory -Path $Dest | Out-Null

$excludeDirNames = @(
  ".venv", "venv", ".git", "__pycache__", ".pytest_cache", ".localmode",
  ".mypy_cache", ".ruff_cache", "node_modules", "agent-transcripts"
)
$excludeFileGlobs = @(
  "*.pyc", "*.pyo", "*.log", "*.bak_logout_test",
  ".setup_ok", "unattended.lock", "relaunch.lock",
  "chain.db-wal", "chain.db-shm", ".migrate_restore_ok",
  "_tmp_schema.py"
)

function ShouldSkip([System.IO.FileSystemInfo]$item, [string]$rel) {
  $parts = $rel -split "[\\/]"
  foreach ($p in $parts) {
    if ($excludeDirNames -contains $p) { return $true }
  }
  if (-not $item.PSIsContainer) {
    foreach ($g in $excludeFileGlobs) {
      if ($item.Name -like $g) { return $true }
    }
  }
  return $false
}

# Flush SQLite WAL into chain.db so the zip is self-contained on the new laptop
$chainDb = Join-Path $Root "data\option_chain_live\chain.db"
$chainZip = Join-Path $Root "data\option_chain_live\chain.db.zip"
$py = Join-Path $Root ".venv\Scripts\python.exe"
if ((Test-Path $chainDb) -and (Test-Path $py)) {
  Write-Host "Checkpointing option chain DB (WAL -> chain.db)..."
  & $py -c "import sqlite3; p=r'$chainDb'; c=sqlite3.connect(p, timeout=120); c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); c.close(); print('checkpoint_ok')"
  if ($LASTEXITCODE -ne 0) { Write-Host "WARN: checkpoint failed — packing main db anyway" }
  Write-Host "Refreshing GitHub seed zip (chain.db.zip)..."
  if (Test-Path $chainZip) { Remove-Item -Force $chainZip }
  Compress-Archive -Path $chainDb -DestinationPath $chainZip -CompressionLevel Optimal
  $zmb = [math]::Round((Get-Item $chainZip).Length / 1MB, 1)
  Write-Host "  chain.db.zip = $zmb MB"
}

Write-Host "Copying files..."
$copied = 0
$bytes = [int64]0
Get-ChildItem -LiteralPath $Root -Force | ForEach-Object {
  $top = $_.Name
  if ($excludeDirNames -contains $top) {
    Write-Host "  skip $top"
    return
  }
  $queue = New-Object System.Collections.Generic.Queue[string]
  $queue.Enqueue($_.FullName)
  while ($queue.Count -gt 0) {
    $cur = $queue.Dequeue()
    $item = Get-Item -LiteralPath $cur -Force
    $rel = $item.FullName.Substring($Root.Length).TrimStart("\", "/")
    if (ShouldSkip $item $rel) { continue }
    $target = Join-Path $Dest $rel
    if ($item.PSIsContainer) {
      if (-not (Test-Path $target)) { New-Item -ItemType Directory -Path $target | Out-Null }
      Get-ChildItem -LiteralPath $item.FullName -Force | ForEach-Object { $queue.Enqueue($_.FullName) }
    } else {
      $parent = Split-Path $target -Parent
      if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
      Copy-Item -LiteralPath $item.FullName -Destination $target -Force
      $copied++
      $bytes += $item.Length
    }
  }
}

# Ensure migrate docs / start bat are present
Copy-Item -LiteralPath (Join-Path $Root "MIGRATE.txt") -Destination (Join-Path $Dest "MIGRATE.txt") -Force -ErrorAction SilentlyContinue
foreach ($bat in @("Setup.bat","Start.bat","Stop.bat","Update.bat")) {
  $p = Join-Path $Root $bat
  if (Test-Path $p) { Copy-Item -LiteralPath $p -Destination (Join-Path $Dest $bat) -Force }
}

Write-Host ("Copied {0} files ({1:N1} MB)" -f $copied, ($bytes / 1MB))
Write-Host "Compressing zip (this can take a few minutes)..."

if (Test-Path $ZipPath) { Remove-Item -Force $ZipPath }

# Prefer tar (fast, built into Win10+)
$tar = Get-Command tar.exe -ErrorAction SilentlyContinue
Push-Location $Stage
try {
  if ($tar) {
    & tar.exe -a -c -f $ZipPath "FoxtronTrade"
    if ($LASTEXITCODE -ne 0) { throw "tar failed with code $LASTEXITCODE" }
  } else {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::CreateFromDirectory(
      $Dest,
      $ZipPath,
      [System.IO.Compression.CompressionLevel]::Optimal,
      $true
    )
  }
} finally {
  Pop-Location
}

Remove-Item -Recurse -Force $Stage -ErrorAction SilentlyContinue

$zipSize = (Get-Item $ZipPath).Length
Write-Host ""
Write-Host "READY:"
Write-Host "  $ZipPath"
Write-Host ("  Size: {0:N1} MB" -f ($zipSize / 1MB))
Write-Host ""
Write-Host "On the new laptop: extract zip -> Setup.bat then Start.bat"
exit 0
