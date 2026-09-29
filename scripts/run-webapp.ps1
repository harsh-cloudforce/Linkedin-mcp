$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$webapp = Join-Path $root "webapp"
Set-Location $webapp

if (-not (Test-Path ".venv")) {
  python -m venv .venv
}
$py = Join-Path $webapp ".venv\Scripts\python.exe"
& $py -m pip install --upgrade pip
& $py -m pip install -r requirements.txt

if (-not (Test-Path ".env")) {
  Copy-Item ".env.example" ".env"
  Write-Host "Created webapp\.env — set INTEGRATION_API_KEY and SCROLLER_BEARER_TOKEN"
}

# Pull scroller bearer from azure/.env if present and webapp value empty
$azureEnv = Join-Path $root "azure\.env"
if (Test-Path $azureEnv) {
  $map = @{}
  Get-Content $azureEnv | ForEach-Object {
    if ($_ -match '^\s*([^#=]+)=(.*)$') { $map[$Matches[1].Trim()] = $Matches[2].Trim().Trim('"') }
  }
  $webEnv = Get-Content ".env" -Raw
  if ($webEnv -notmatch '(?m)^SCROLLER_BEARER_TOKEN=.+' -or $webEnv -match '(?m)^SCROLLER_BEARER_TOKEN=\s*$') {
    if ($map.ContainsKey("MCP_BEARER_TOKEN") -and $map["MCP_BEARER_TOKEN"]) {
      Add-Content ".env" "`nSCROLLER_BEARER_TOKEN=$($map['MCP_BEARER_TOKEN'])"
      Write-Host "Copied SCROLLER_BEARER_TOKEN from azure\.env"
    }
  }
}

$hostName = "127.0.0.1"
$port = "8790"
Get-Content ".env" | ForEach-Object {
  if ($_ -match '^\s*WEBAPP_HOST=(.+)$') { $hostName = $Matches[1].Trim() }
  if ($_ -match '^\s*WEBAPP_PORT=(.+)$') { $port = $Matches[1].Trim() }
}

Write-Host "Market Pulse → http://${hostName}:${port}/"
Write-Host "MCP        → http://${hostName}:${port}/mcp"
& $py -m uvicorn app.main:app --host $hostName --port $port --app-dir $webapp
