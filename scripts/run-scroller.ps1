$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$scroller = Join-Path $root "scroller"
$py = Join-Path $scroller ".venv\Scripts\python.exe"

if (-not (Test-Path $py)) {
  Write-Host "Virtualenv missing. Run .\scripts\setup.ps1 first."
  exit 1
}

Set-Location $scroller

# Load simple KEY=VALUE pairs from .env if present
$hostName = "127.0.0.1"
$port = "8787"
$browsersPath = Join-Path $scroller ".playwright-browsers"
if (Test-Path ".env") {
  Get-Content ".env" | ForEach-Object {
    if ($_ -match '^\s*SCROLLER_HOST=(.+)$') { $hostName = $Matches[1].Trim() }
    if ($_ -match '^\s*SCROLLER_PORT=(.+)$') { $port = $Matches[1].Trim() }
    if ($_ -match '^\s*PLAYWRIGHT_BROWSERS_PATH=(.+)$') {
      $raw = $Matches[1].Trim()
      $browsersPath = if ([System.IO.Path]::IsPathRooted($raw)) { $raw } else { Join-Path $scroller $raw }
    }
  }
}

$env:PLAYWRIGHT_BROWSERS_PATH = $browsersPath
New-Item -ItemType Directory -Force -Path $browsersPath | Out-Null

Write-Host "Starting scroller at http://${hostName}:${port}"
Write-Host "Playwright browsers: $browsersPath"
Write-Host "Health:  Invoke-RestMethod http://${hostName}:${port}/health"
Write-Host "Scan:    see README.md"

# No --reload: Windows + Playwright need a stable process.
& $py -m uvicorn app.main:app --host $hostName --port $port
