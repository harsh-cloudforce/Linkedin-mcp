$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$scroller = Join-Path $root "scroller"

Write-Host "Setting up LinkedIn feed scroller in $scroller"

Set-Location $scroller

if (-not (Test-Path ".venv")) {
  python -m venv .venv
}

$py = Join-Path $scroller ".venv\Scripts\python.exe"
& $py -m pip install --upgrade pip
& $py -m pip install -r requirements.txt
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $scroller ".playwright-browsers"
New-Item -ItemType Directory -Force -Path $env:PLAYWRIGHT_BROWSERS_PATH | Out-Null
& $py -m playwright install chromium

if (-not (Test-Path ".env")) {
  Copy-Item ".env.example" ".env"
  Write-Host "Created scroller\.env from .env.example"
} else {
  $envText = Get-Content ".env" -Raw
  if ($envText -notmatch "PLAYWRIGHT_BROWSERS_PATH=") {
    Add-Content ".env" "`nPLAYWRIGHT_BROWSERS_PATH=.playwright-browsers"
  }
}

New-Item -ItemType Directory -Force -Path ".profiles" | Out-Null
Write-Host "Setup complete. Run .\scripts\run-scroller.ps1"
