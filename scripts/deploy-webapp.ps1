# Market Pulse webapp - Azure Container Apps deploy
# Prerequisites: az login (uses scroller\.venv\Scripts\az.bat if az not on PATH)
#
# Usage:
#   cd "Linkedin Workflow"
#   .\scripts\deploy-webapp.ps1
#
# Short hostname: mpulse.<env>.azurecontainerapps.io
# For a custom domain (e.g. pulse.gocloudforce.com), bind DNS after deploy.

param(
  [string]$ResourceGroup = "rg-linkedin-market-pulse",
  [string]$Location = "eastus2",
  [string]$AppName = "mpulse",
  [string]$EnvironmentName = "cae-linkedin-market-pulse",
  [string]$AcrName = "acrlinpulse35619",
  [string]$ImageTag = "",
  [string]$AdminEmail = "",
  [string]$AdminPassword = "",
  [switch]$SkipBuild,
  [switch]$RemoveLegacyApp
)

$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$root = Split-Path -Parent $PSScriptRoot
$webapp = Join-Path $root "webapp"
$azureEnv = Join-Path $root "azure\.env"
$webappEnv = Join-Path $webapp ".env"

$az = "az"
if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
  $venvAz = Join-Path $root "scroller\.venv\Scripts\az.bat"
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

function New-RandomHex([int]$Bytes = 24) {
  $buf = New-Object byte[] $Bytes
  [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($buf)
  return (($buf | ForEach-Object { $_.ToString("x2") }) -join "")
}

function Set-Or-AddEnvLine {
  param([string]$Path, [string]$Name, [string]$Value)
  $lines = @()
  if (Test-Path $Path) { $lines = Get-Content $Path }
  $found = $false
  $out = foreach ($line in $lines) {
    if ($line -match "^\s*$([regex]::Escape($Name))\s*=") {
      $found = $true
      "$Name=$Value"
    } else { $line }
  }
  if (-not $found) { $out = @($out) + "$Name=$Value" }
  Set-Content -Path $Path -Value $out -Encoding UTF8
}

$fromAzure = @{
  ResourceGroup  = Get-DotEnvValue $azureEnv "AZURE_RESOURCE_GROUP"
  AcrName        = Get-DotEnvValue $azureEnv "AZURE_ACR"
  Location       = Get-DotEnvValue $azureEnv "AZURE_LOCATION"
  ScrollerBearer = Get-DotEnvValue $azureEnv "MCP_BEARER_TOKEN"
  ScrollerFqdn   = Get-DotEnvValue $azureEnv "MCP_FQDN"
  IntegrationKey = Get-DotEnvValue $azureEnv "INTEGRATION_API_KEY"
  AdminEmail     = Get-DotEnvValue $azureEnv "ADMIN_EMAIL"
  AdminPassword  = Get-DotEnvValue $azureEnv "ADMIN_PASSWORD"
  SessionSecret  = Get-DotEnvValue $azureEnv "SESSION_SECRET"
}
if ($fromAzure.ResourceGroup) { $ResourceGroup = $fromAzure.ResourceGroup }
if ($fromAzure.AcrName) { $AcrName = $fromAzure.AcrName }
if ($fromAzure.Location) { $Location = $fromAzure.Location }

$integrationKey = Get-DotEnvValue $webappEnv "INTEGRATION_API_KEY"
if (-not $integrationKey -or $integrationKey -eq "change-me-market-pulse") {
  $integrationKey = $fromAzure.IntegrationKey
}
if (-not $integrationKey -or $integrationKey -eq "change-me-market-pulse") {
  $integrationKey = New-RandomHex 24
  Write-Host "Generated INTEGRATION_API_KEY (also generate from Settings later): $integrationKey" -ForegroundColor Yellow
} else {
  Write-Host "Reusing INTEGRATION_API_KEY from env." -ForegroundColor Green
}

$scrollerBearer = Get-DotEnvValue $webappEnv "SCROLLER_BEARER_TOKEN"
if (-not $scrollerBearer) { $scrollerBearer = $fromAzure.ScrollerBearer }
if (-not $scrollerBearer) { throw "SCROLLER_BEARER_TOKEN missing (webapp/.env or azure/.env MCP_BEARER_TOKEN)" }

$scrollerMcp = Get-DotEnvValue $webappEnv "SCROLLER_MCP_URL"
if (-not $scrollerMcp) {
  if ($fromAzure.ScrollerFqdn) {
    $scrollerMcp = "https://$($fromAzure.ScrollerFqdn)/mcp"
  } else {
    $scrollerMcp = "https://linkedin-feed-scroller.whitesand-f9361ec3.eastus2.azurecontainerapps.io/mcp"
  }
}

if (-not $AdminEmail) {
  $AdminEmail = Get-DotEnvValue $webappEnv "ADMIN_EMAIL"
}
if (-not $AdminEmail) { $AdminEmail = $fromAzure.AdminEmail }
if (-not $AdminEmail) { $AdminEmail = "hshrishrimal@gocloudforce.com" }

if (-not $AdminPassword) {
  $AdminPassword = Get-DotEnvValue $webappEnv "ADMIN_PASSWORD"
}
if (-not $AdminPassword) { $AdminPassword = $fromAzure.AdminPassword }
$adminPasswordGenerated = $false
if (-not $AdminPassword) {
  $AdminPassword = New-RandomHex 12
  $adminPasswordGenerated = $true
  Write-Host "Generated ADMIN_PASSWORD (save this): $AdminPassword" -ForegroundColor Yellow
}

$sessionSecret = Get-DotEnvValue $webappEnv "SESSION_SECRET"
if (-not $sessionSecret) { $sessionSecret = $fromAzure.SessionSecret }
if (-not $sessionSecret) {
  $sessionSecret = New-RandomHex 32
  Write-Host "Generated SESSION_SECRET once - saved to azure/.env (do not rotate; rotating logs everyone out)." -ForegroundColor Yellow
} else {
  Write-Host "Reusing stable SESSION_SECRET from env (sessions survive redeploy)." -ForegroundColor Green
}

# Prefer durable Postgres when the subscription allows it; otherwise Azure Files + SQLite.
$skipPg = Get-DotEnvValue $azureEnv "SKIP_POSTGRES"
$databaseUrl = Get-DotEnvValue $azureEnv "DATABASE_URL"
if (-not $databaseUrl -and $skipPg -ne "1" -and $skipPg -ne "true") {
  Write-Host "Ensuring PostgreSQL (optional)..." -ForegroundColor Cyan
  try {
    $pgOut = & (Join-Path $PSScriptRoot "ensure-postgres.ps1") `
      -ResourceGroup $ResourceGroup
    if ($pgOut) { $databaseUrl = ($pgOut | Select-Object -Last 1).ToString().Trim() }
  } catch {
    Write-Host "Postgres unavailable ($($_.Exception.Message)). Using Azure Files SQLite persist instead." -ForegroundColor Yellow
    $databaseUrl = $null
    if (Test-Path $azureEnv) { Set-Or-AddEnvLine $azureEnv "SKIP_POSTGRES" "1" }
  }
} elseif (-not $databaseUrl) {
  Write-Host "SKIP_POSTGRES set - using Azure Files SQLite persist." -ForegroundColor Green
}
if ($databaseUrl) {
  Write-Host "Using DATABASE_URL (Postgres)." -ForegroundColor Green
} else {
  Write-Host "Using local SQLite synced to Azure Files /persist." -ForegroundColor Green
}

$dailyHour = Get-DotEnvValue $webappEnv "DAILY_SCAN_HOUR_UTC"
if (-not $dailyHour) { $dailyHour = "13" }
$dailyMinute = Get-DotEnvValue $webappEnv "DAILY_SCAN_MINUTE_UTC"
if (-not $dailyMinute) { $dailyMinute = "0" }
$dailyScanEnabled = Get-DotEnvValue $azureEnv "DAILY_SCAN_ENABLED"
if (-not $dailyScanEnabled) { $dailyScanEnabled = Get-DotEnvValue $webappEnv "DAILY_SCAN_ENABLED" }
if (-not $dailyScanEnabled) { $dailyScanEnabled = "false" }
$dailyScanEnabled = if ($dailyScanEnabled.Trim().ToLowerInvariant() -in @("1", "true", "yes", "on")) { "true" } else { "false" }

Write-Host "Checking Azure CLI login..." -ForegroundColor Cyan
Az account show -o none

if (-not $ImageTag) {
  $ImageTag = "deploy-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
}

$image = "$AcrName.azurecr.io/${AppName}:$ImageTag"

if (-not $SkipBuild) {
  Write-Host "Staging build context outside OneDrive..." -ForegroundColor Cyan
  $BuildDir = Join-Path $env:TEMP "market-pulse-webapp-build"
  if (Test-Path $BuildDir) { Remove-Item $BuildDir -Recurse -Force }
  New-Item -ItemType Directory -Path $BuildDir | Out-Null
  Copy-Item (Join-Path $webapp "app") (Join-Path $BuildDir "app") -Recurse
  Copy-Item (Join-Path $webapp "Dockerfile") $BuildDir
  Copy-Item (Join-Path $webapp "requirements.txt") $BuildDir
  Copy-Item (Join-Path $webapp ".dockerignore") $BuildDir -ErrorAction SilentlyContinue

  Write-Host "Building + pushing image via ACR Tasks: $image" -ForegroundColor Cyan
  Az acr build -r $AcrName -t "${AppName}:$ImageTag" -f (Join-Path $BuildDir "Dockerfile") $BuildDir
  Remove-Item $BuildDir -Recurse -Force -ErrorAction SilentlyContinue
}

$prevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$null = & $az containerapp show -n $AppName -g $ResourceGroup -o none 2>&1
$appExists = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = $prevEap

$secretArgs = @(
  "integration-api-key=$integrationKey",
  "scroller-bearer=$scrollerBearer",
  "admin-password=$AdminPassword",
  "session-secret=$sessionSecret"
)
if ($databaseUrl) {
  $secretArgs += "database-url=$databaseUrl"
}

$envArgs = @(
  "WEBAPP_HOST=0.0.0.0",
  "WEBAPP_PORT=8790",
  "DATA_DIR=/tmp/mpulse-data",
  "PERSIST_DIR=/persist",
  "DAILY_SCAN_ENABLED=$dailyScanEnabled",
  "DAILY_SCAN_HOUR_UTC=$dailyHour",
  "DAILY_SCAN_MINUTE_UTC=$dailyMinute",
  "SCROLLER_MCP_URL=$scrollerMcp",
  "ADMIN_EMAIL=$AdminEmail",
  "REQUIRE_API_KEY=true",
  "SESSION_SECURE=true",
  "INTEGRATION_API_KEY=secretref:integration-api-key",
  "SCROLLER_BEARER_TOKEN=secretref:scroller-bearer",
  "ADMIN_PASSWORD=secretref:admin-password",
  "SESSION_SECRET=secretref:session-secret"
)
if ($databaseUrl) {
  $envArgs += "DATABASE_URL=secretref:database-url"
}

$forceAdminPw = Get-DotEnvValue $azureEnv "ADMIN_PASSWORD_FORCE"
if (-not $forceAdminPw) { $forceAdminPw = Get-DotEnvValue $webappEnv "ADMIN_PASSWORD_FORCE" }
$forceNorm = if ($forceAdminPw) { $forceAdminPw.Trim().ToLowerInvariant() } else { "" }
if ($forceNorm -eq "1" -or $forceNorm -eq "true" -or $forceNorm -eq "yes") {
  $envArgs += "ADMIN_PASSWORD_FORCE=1"
  Write-Host "ADMIN_PASSWORD_FORCE=1 - admin password will be reset from ADMIN_PASSWORD on boot." -ForegroundColor Yellow
} else {
  # Explicitly clear stale FORCE from a prior revision
  $envArgs += "ADMIN_PASSWORD_FORCE=0"
}

if (-not $appExists) {
  Write-Host "Creating Container App $AppName ..." -ForegroundColor Cyan
  $acrUser = & $az acr credential show -n $AcrName --query username -o tsv
  $acrPass = & $az acr credential show -n $AcrName --query passwords[0].value -o tsv
  if ($LASTEXITCODE -ne 0 -or -not $acrPass) { throw "Could not read ACR credentials for $AcrName" }

  Az containerapp create `
    -n $AppName `
    -g $ResourceGroup `
    --environment $EnvironmentName `
    --image $image `
    --registry-server "$AcrName.azurecr.io" `
    --registry-username $acrUser `
    --registry-password $acrPass `
    --target-port 8790 `
    --ingress external `
    --cpu 0.5 --memory 1.0Gi `
    --min-replicas 1 --max-replicas 1 `
    --secrets @secretArgs `
    --env-vars @envArgs

  # Mount durable storage before the app takes traffic / writes data
  Write-Host "Ensuring persistent /persist storage (first create)..." -ForegroundColor Cyan
  & (Join-Path $PSScriptRoot "ensure-webapp-storage.ps1") `
    -ResourceGroup $ResourceGroup `
    -Location $Location `
    -AppName $AppName `
    -EnvironmentName $EnvironmentName
} else {
  # Keep /persist mounted BEFORE swapping the image so a new revision never
  # boots without Azure Files and accidentally flushes an empty DB.
  Write-Host "Ensuring persistent /persist storage before image update..." -ForegroundColor Cyan
  & (Join-Path $PSScriptRoot "ensure-webapp-storage.ps1") `
    -ResourceGroup $ResourceGroup `
    -Location $Location `
    -AppName $AppName `
    -EnvironmentName $EnvironmentName

  Write-Host "Updating Container App $AppName ..." -ForegroundColor Cyan
  Az containerapp secret set -n $AppName -g $ResourceGroup --secrets @secretArgs
  Az containerapp update -n $AppName -g $ResourceGroup --image $image --set-env-vars @envArgs
}

# Multiple-revision mode can leave traffic on an old revision; pin 100% to newest
$latestRev = & $az containerapp revision list -n $AppName -g $ResourceGroup `
  --query "reverse(sort_by(@, &properties.createdTime))[0].name" -o tsv
if ($latestRev) {
  Write-Host "Routing 100% traffic to $latestRev ..." -ForegroundColor Cyan
  $ErrorActionPreference = "Continue"
  & $az containerapp revision set-mode -n $AppName -g $ResourceGroup --mode multiple 2>&1 | Out-Null
  & $az containerapp ingress traffic set -n $AppName -g $ResourceGroup --revision-weight "$latestRev=100" 2>&1 | Out-Null
  $ErrorActionPreference = "Stop"
}

$fqdn = & $az containerapp show -n $AppName -g $ResourceGroup --query properties.configuration.ingress.fqdn -o tsv
$publicBase = "https://$fqdn"
Az containerapp update -n $AppName -g $ResourceGroup --set-env-vars "PUBLIC_BASE_URL=$publicBase" "WEBAPP_URL=$publicBase"

if ($RemoveLegacyApp) {
  Write-Host "Removing legacy app market-pulse-webapp (if present)..." -ForegroundColor Cyan
  $ErrorActionPreference = "Continue"
  & $az containerapp delete -n market-pulse-webapp -g $ResourceGroup --yes 2>&1 | Out-Null
  $ErrorActionPreference = "Stop"
}

if (Test-Path $azureEnv) {
  Set-Or-AddEnvLine $azureEnv "WEBAPP_FQDN" $fqdn
  Set-Or-AddEnvLine $azureEnv "WEBAPP_URL" $publicBase
  Set-Or-AddEnvLine $azureEnv "WEBAPP_MCP_URL" "$publicBase/mcp/"
  Set-Or-AddEnvLine $azureEnv "INTEGRATION_API_KEY" $integrationKey
  Set-Or-AddEnvLine $azureEnv "ADMIN_EMAIL" $AdminEmail
  Set-Or-AddEnvLine $azureEnv "SESSION_SECRET" $sessionSecret
  if ($databaseUrl) {
    Set-Or-AddEnvLine $azureEnv "DATABASE_URL" $databaseUrl
  }
  if ($adminPasswordGenerated) {
    Set-Or-AddEnvLine $azureEnv "ADMIN_PASSWORD" $AdminPassword
  }
}

Write-Host ""
Write-Host "Verifying health..." -ForegroundColor Cyan
try {
  Start-Sleep -Seconds 12
  $health = Invoke-RestMethod -Uri "$publicBase/healthz" -TimeoutSec 45
  Write-Host ($health | ConvertTo-Json -Compress) -ForegroundColor Green
} catch {
  Write-Host ("Health check pending: " + $_.Exception.Message) -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Deployed." -ForegroundColor Green
Write-Host "Dashboard: $publicBase/"
Write-Host "Login:     $publicBase/login"
Write-Host "MCP URL:   $publicBase/mcp/"
Write-Host "Admin:     $AdminEmail"
if ($adminPasswordGenerated) {
  Write-Host "Admin password (save now): $AdminPassword" -ForegroundColor Yellow
}
Write-Host "Integration API key: $integrationKey"
Write-Host ""
Write-Host "Optional shorter vanity URL: bind a custom domain in Azure Portal -> Container App -> Custom domains."
Write-Host "SQLite runs locally and syncs to Azure Files at /persist - users and briefs persist across redeploys."
