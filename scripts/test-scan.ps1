# Live / first-time scan (headed so you can log into LinkedIn once).
# userId is generic — any teammate; defaults to Windows username.
param(
  [string]$UserId = $env:USERNAME,
  [int]$MaxPosts = 40,
  [int]$MaxScrolls = 12,
  [bool]$Headed = $true
)

$ErrorActionPreference = "Stop"
$userId = ($UserId.Trim().ToLower() -replace '\s+', '-')

$body = @{
  userId            = $userId
  maxPosts          = $MaxPosts
  maxScrolls        = $MaxScrolls
  headed            = $Headed
  loginWaitSeconds  = 300
} | ConvertTo-Json

Write-Host "Scanning LinkedIn feed for userId='$userId' (headed=$Headed)..."
Write-Host "If a browser opens on the login page, sign in within ~5 minutes."

$result = Invoke-RestMethod -Method POST -Uri "http://127.0.0.1:8787/scan" `
  -ContentType "application/json" `
  -Body $body `
  -TimeoutSec 600

$outDir = Join-Path (Split-Path -Parent $PSScriptRoot) "scroller\out"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$stamp = Get-Date -Format "yyyy-MM-dd-HHmmss"
$outFile = Join-Path $outDir "$userId-$stamp-scan.json"
$result | ConvertTo-Json -Depth 8 | Set-Content -Path $outFile -Encoding UTF8

Write-Host "Saved: $outFile"
Write-Host "postCount=$($result.postCount) loginRequired=$($result.loginRequired) message=$($result.message)"
$result | ConvertTo-Json -Depth 6
