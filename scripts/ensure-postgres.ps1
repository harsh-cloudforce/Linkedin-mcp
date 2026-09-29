# Ensure Azure Database for PostgreSQL (Flexible Server) for Market Pulse.
# Survives redeploys; login sessions stay valid when SESSION_SECRET is stable.
#   .\scripts\ensure-postgres.ps1

param(
  [string]$ResourceGroup = "rg-linkedin-market-pulse",
  [string]$Location = "eastus",
  [string]$ServerName = "psql-mpulse-12449",
  [string]$AdminUser = "mpulseadmin",
  [string]$DbName = "marketpulse",
  [string]$AdminPassword = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$azureEnv = Join-Path $root "azure\.env"

$azCmd = "az"
if (-not (Get-Command az.exe -ErrorAction SilentlyContinue) -and -not (Get-Command az.cmd -ErrorAction SilentlyContinue)) {
  $venvAz = Join-Path $root "scroller\.venv\Scripts\az.bat"
  if (Test-Path $venvAz) { $azCmd = $venvAz } else { throw "Azure CLI not found" }
}

# Resolve to a real executable path so we never call a parent-scope Az {} wrapper function
$azApp = Get-Command $azCmd -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $azApp) {
  $azApp = Get-Command "az.exe" -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
}
if (-not $azApp -and (Test-Path $azCmd)) {
  $azResolved = $azCmd
} elseif ($azApp) {
  $azResolved = $azApp.Source
} else {
  throw "Azure CLI executable not found"
}

function Invoke-AzCli {
  $azArguments = $args
  & $azResolved @azArguments
  if ($LASTEXITCODE -ne 0) { throw "az failed: $($azArguments -join ' ')" }
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

function Set-Or-AddEnvLine {
  param([string]$Path, [string]$Name, [string]$Value)
  $lines = @()
  if (Test-Path $Path) { $lines = Get-Content $Path }
  $found = $false
  $out = foreach ($line in $lines) {
    if ($line -match "^\s*$([regex]::Escape($Name))\s*=") { $found = $true; "$Name=$Value" } else { $line }
  }
  if (-not $found) { $out = @($out) + "$Name=$Value" }
  Set-Content -Path $Path -Value $out -Encoding UTF8
}

function New-RandomHex([int]$Bytes = 16) {
  $buf = New-Object byte[] $Bytes
  [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($buf)
  return (($buf | ForEach-Object { $_.ToString("x2") }) -join "")
}

$fromRg = Get-DotEnvValue $azureEnv "AZURE_RESOURCE_GROUP"
$fromLoc = Get-DotEnvValue $azureEnv "POSTGRES_LOCATION"
if (-not $fromLoc) { $fromLoc = "eastus" }
$fromServer = Get-DotEnvValue $azureEnv "POSTGRES_SERVER"
$fromPass = Get-DotEnvValue $azureEnv "POSTGRES_PASSWORD"
$fromUrl = Get-DotEnvValue $azureEnv "DATABASE_URL"
if ($fromRg) { $ResourceGroup = $fromRg }
if ($fromLoc) { $Location = $fromLoc }
if ($fromServer) { $ServerName = $fromServer }
if (-not $AdminPassword) { $AdminPassword = $fromPass }
if (-not $AdminPassword) {
  # Azure requires complexity
  $AdminPassword = "Mp!" + (New-RandomHex 12) + "A1"
}

if ($fromUrl) {
  Write-Host "DATABASE_URL already set in azure/.env - reusing." -ForegroundColor Green
  Write-Output $fromUrl
  return
}

Write-Host "Ensuring PostgreSQL flexible server $ServerName ..." -ForegroundColor Cyan

# Subscription must have the Postgres RP registered
$ErrorActionPreference = "Continue"
$rpState = & $azResolved provider show -n Microsoft.DBforPostgreSQL --query registrationState -o tsv 2>$null
$ErrorActionPreference = "Stop"
if ($rpState -ne "Registered") {
  Write-Host "Registering Microsoft.DBforPostgreSQL provider (one-time)..." -ForegroundColor Yellow
  Invoke-AzCli provider register --namespace Microsoft.DBforPostgreSQL --wait
}

$prev = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$null = & $azResolved postgres flexible-server show -n $ServerName -g $ResourceGroup -o none 2>&1
$exists = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = $prev

if (-not $exists) {
  Invoke-AzCli postgres flexible-server create `
    -g $ResourceGroup `
    -n $ServerName `
    -l $Location `
    --admin-user $AdminUser `
    --admin-password $AdminPassword `
    --sku-name Standard_B1ms `
    --tier Burstable `
    --storage-size 32 `
    --version 16 `
    --public-access 0.0.0.0-255.255.255.255 `
    --yes
} else {
  Write-Host "Server exists." -ForegroundColor Green
}

$ErrorActionPreference = "Continue"
& $azResolved postgres flexible-server db show -g $ResourceGroup -s $ServerName -d $DbName -o none 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
  $ErrorActionPreference = "Stop"
  Invoke-AzCli postgres flexible-server db create -g $ResourceGroup -s $ServerName -d $DbName
} else {
  $ErrorActionPreference = "Stop"
}

# Allow Azure services / public (Container Apps egress)
$ErrorActionPreference = "Continue"
& $azResolved postgres flexible-server firewall-rule create `
  -g $ResourceGroup -n $ServerName `
  -r AllowAzureServices `
  --start-ip-address 0.0.0.0 --end-ip-address 0.0.0.0 -o none 2>&1 | Out-Null
$ErrorActionPreference = "Stop"

$fqdn = & $azResolved postgres flexible-server show -g $ResourceGroup -n $ServerName --query fullyQualifiedDomainName -o tsv
$encPass = [uri]::EscapeDataString($AdminPassword)
$databaseUrl = "postgresql+psycopg://" + $AdminUser + ":" + $encPass + "@" + $fqdn + ":5432/" + $DbName + "?sslmode=require"

if (Test-Path $azureEnv) {
  Set-Or-AddEnvLine $azureEnv "POSTGRES_SERVER" $ServerName
  Set-Or-AddEnvLine $azureEnv "POSTGRES_LOCATION" $Location
  Set-Or-AddEnvLine $azureEnv "POSTGRES_PASSWORD" $AdminPassword
  Set-Or-AddEnvLine $azureEnv "DATABASE_URL" $databaseUrl
}

Write-Host ("Postgres ready: {0} / {1}" -f $fqdn, $DbName) -ForegroundColor Green
Write-Output $databaseUrl
