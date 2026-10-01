# LinkedIn Market Pulse scroller — Azure Container Apps (local deploy, Confluence-style)
# Prerequisites: az login (uses scroller\.venv\Scripts\az.bat if az not on PATH)
#
# Usage:
#   cd "Linkedin Workflow"
#   .\scripts\deploy-containerapp.ps1

param(
  [string]$ResourceGroup = "rg-linkedin-market-pulse",
  [string]$Location = "eastus2",
  [string]$AppName = "linkedin-feed-scroller",
  [string]$EnvironmentName = "cae-linkedin-market-pulse",
  [string]$AcrName = "acrlinpulse35619",
  [string]$ImageTag = "",
  [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$root = Split-Path -Parent $PSScriptRoot
$scroller = Join-Path $root "scroller"
$azureEnv = Join-Path $root "azure\.env"

# Prefer global az; fall back to pip-installed CLI in scroller\.venv (same as Confluence)
$az = "az"
if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
  $venvAz = Join-Path $scroller ".venv\Scripts\az.bat"
  if (Test-Path $venvAz) {
    $az = $venvAz
  } else {
    throw "Azure CLI not found. Install without admin: scroller\.venv\Scripts\pip.exe install azure-cli"
  }
}

function Az {
  & $az @args
  if ($LASTEXITCODE -ne 0) { throw "az command failed: az $($args -join ' ')" }
}

function Get-DotEnvValue {
  param([string]$Path, [string]$Name)
  if (-not (Test-Path $Path)) { return $null }
  foreach ($line in Get-Content $Path) {
    if ($line -match '^\s*#' -or $line -notmatch '=') { continue }
    $parts = $line.Split('=', 2)
    if ($parts[0].Trim() -eq $Name) { return $parts[1].Trim().Trim('"').Trim("'") }
  }
  return $null
}

# Prefer values from azure\.env when present
$fromEnv = @{
  ResourceGroup = Get-DotEnvValue $azureEnv "AZURE_RESOURCE_GROUP"
  AppName       = Get-DotEnvValue $azureEnv "AZURE_CONTAINER_APP"
  AcrName       = Get-DotEnvValue $azureEnv "AZURE_ACR"
  Location      = Get-DotEnvValue $azureEnv "AZURE_LOCATION"
  Bearer        = Get-DotEnvValue $azureEnv "MCP_BEARER_TOKEN"
  PublicBase    = Get-DotEnvValue $azureEnv "PUBLIC_BASE_URL"
}
if ($fromEnv.ResourceGroup) { $ResourceGroup = $fromEnv.ResourceGroup }
if ($fromEnv.AppName) { $AppName = $fromEnv.AppName }
if ($fromEnv.AcrName) { $AcrName = $fromEnv.AcrName }
if ($fromEnv.Location) { $Location = $fromEnv.Location }

Write-Host "Checking Azure CLI login..." -ForegroundColor Cyan
Az account show -o none

if (-not $ImageTag) {
  $ImageTag = "deploy-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
}

$image = "$AcrName.azurecr.io/${AppName}:$ImageTag"

if (-not $SkipBuild) {
  # Stage outside OneDrive — ACR Tasks + OneDrive tree is extremely slow
  Write-Host "Staging build context outside OneDrive..." -ForegroundColor Cyan
  $BuildDir = Join-Path $env:TEMP "linkedin-scroller-build"
  if (Test-Path $BuildDir) { Remove-Item $BuildDir -Recurse -Force }
  New-Item -ItemType Directory -Path $BuildDir | Out-Null
  Copy-Item (Join-Path $scroller "app") (Join-Path $BuildDir "app") -Recurse
  Copy-Item (Join-Path $scroller "Dockerfile") $BuildDir
  Copy-Item (Join-Path $scroller "requirements.txt") $BuildDir
  Copy-Item (Join-Path $scroller "mcp_server.py") $BuildDir
  Copy-Item (Join-Path $scroller ".dockerignore") $BuildDir -ErrorAction SilentlyContinue

  Write-Host "Building + pushing image via ACR Tasks: $image" -ForegroundColor Cyan
  Az acr build -r $AcrName -t "${AppName}:$ImageTag" -f (Join-Path $BuildDir "Dockerfile") $BuildDir
  Remove-Item $BuildDir -Recurse -Force -ErrorAction SilentlyContinue
}

$fqdnHint = Get-DotEnvValue $azureEnv "MCP_FQDN"
$publicBase = if ($fromEnv.PublicBase) {
  $fromEnv.PublicBase.TrimEnd('/')
} elseif ($fqdnHint) {
  "https://$fqdnHint"
} else {
  "https://placeholder.invalid"
}

Write-Host "Updating Container App $AppName ..." -ForegroundColor Cyan
Az containerapp update `
  -n $AppName `
  -g $ResourceGroup `
  --image $image `
  --set-env-vars `
    "DEFAULT_HEADED=false" `
    "REMOTE_LOGIN_ENABLED=true" `
    "PROFILES_DIR=/data/profiles" `
    "WORK_PROFILES_DIR=/tmp/li-profiles" `
    "DEFAULT_LOGIN_WAIT_SECONDS=600" `
    "DEFAULT_MAX_POSTS=100" `
    "DEFAULT_MAX_SCROLLS=40" `
    "PUBLIC_BASE_URL=$publicBase"

Write-Host "Ensuring persistent LinkedIn profiles on Azure Files..." -ForegroundColor Cyan
& (Join-Path $PSScriptRoot "ensure-scroller-storage.ps1") `
  -ResourceGroup $ResourceGroup `
  -Location $Location `
  -AppName $AppName `
  -EnvironmentName $EnvironmentName

$fqdn = & $az containerapp show -n $AppName -g $ResourceGroup --query properties.configuration.ingress.fqdn -o tsv
if ($publicBase -like "*placeholder*") {
  $publicBase = "https://$fqdn"
  Az containerapp update -n $AppName -g $ResourceGroup --set-env-vars "PUBLIC_BASE_URL=$publicBase"
}

Write-Host ""
Write-Host "Verifying health..." -ForegroundColor Cyan
try {
  Start-Sleep -Seconds 8
  $health = Invoke-RestMethod -Uri "https://$fqdn/healthz" -TimeoutSec 30
  Write-Host ($health | ConvertTo-Json -Compress) -ForegroundColor Green
} catch {
  Write-Host ("Health check pending: " + $_.Exception.Message) -ForegroundColor Yellow
}

# Confirm login UI is public (no longer 401)
try {
  $r = Invoke-WebRequest -Uri "https://$fqdn/login/vnc.html" -UseBasicParsing -TimeoutSec 30
  Write-Host "login/vnc.html status=$($r.StatusCode) len=$($r.RawContentLength)" -ForegroundColor Green
} catch {
  Write-Host ("login/vnc.html: " + $_.Exception.Message) -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Deployed." -ForegroundColor Green
Write-Host "Health:  https://$fqdn/healthz"
Write-Host "MCP URL: https://$fqdn/mcp"
Write-Host "Keep existing MCP_BEARER_TOKEN from azure\.env (not rotated by this script)."
Write-Host "LinkedIn browser profiles persist on Azure Files at /data/profiles."
