# Local headed LinkedIn login (browser opens on YOUR PC)
#
# Use this when you want Chromium to pop up locally instead of the cloud noVNC link.
# nebulaONE (cloud) cannot open a window on your laptop by itself — the MCP process
# that launches Playwright must be running on this machine.
#
# Usage:
#   .\scripts\login-headed.ps1 -UserId "you@example.com"
#
param(
  [Parameter(Mandatory = $true)]
  [string]$UserId,
  [int]$MaxPosts = 5,
  [int]$MaxScrolls = 2,
  [int]$LoginWaitSeconds = 600
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$scroller = Join-Path $root "scroller"
$py = Join-Path $scroller ".venv\Scripts\python.exe"

if (-not (Test-Path $py)) {
  Write-Host "Virtualenv missing. Run .\scripts\setup.ps1 first."
  exit 1
}

Set-Location $scroller
$browsers = Join-Path $scroller ".playwright-browsers"
$env:PLAYWRIGHT_BROWSERS_PATH = $browsers
$env:PROFILES_DIR = Join-Path $env:LOCALAPPDATA "LinkedInMarketPulse\profiles"
New-Item -ItemType Directory -Force -Path $env:PROFILES_DIR, $browsers | Out-Null

Write-Host "Opening headed Chromium for userId=$UserId"
Write-Host "Sign into LinkedIn in the window that appears (2FA OK)."
Write-Host "Profile saved under: $env:PROFILES_DIR"
Write-Host ""

& $py .\run_scan_cli.py `
  --userId $UserId `
  --headed `
  --maxPosts $MaxPosts `
  --maxScrolls $MaxScrolls `
  --loginWaitSeconds $LoginWaitSeconds

Write-Host ""
Write-Host "Done. This profile is LOCAL only."
Write-Host "Cloud Azure scans use a different profile store unless you run the MCP locally"
Write-Host "and point nebulaONE at that local/tunneled MCP URL."
