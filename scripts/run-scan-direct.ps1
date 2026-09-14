$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$scroller = Join-Path $root "scroller"
$py = Join-Path $scroller ".venv\Scripts\python.exe"
$browsers = Join-Path $scroller ".playwright-browsers"

if (-not (Test-Path $py)) {
  Write-Host "Run .\scripts\setup.ps1 first."
  exit 1
}

$env:PLAYWRIGHT_BROWSERS_PATH = $browsers
Set-Location $scroller

Write-Host "Direct CLI scan (profiles under %LOCALAPPDATA%\LinkedInMarketPulse\profiles)"
Write-Host "Sign into LinkedIn in the Chromium window if prompted."

& $py .\run_scan_cli.py --userId $env:USERNAME --headed --maxPosts 40 --maxScrolls 12 --loginWaitSeconds 300
