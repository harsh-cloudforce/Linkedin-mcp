# Local MCP smoke test (PowerShell)
param(
  [string]$BaseUrl = "http://127.0.0.1:8000",
  [string]$Token = "local-dev-token"
)

$ErrorActionPreference = "Stop"
Write-Host "Health..."
Invoke-RestMethod "$BaseUrl/healthz" | ConvertTo-Json

$headers = @{
  Authorization  = "Bearer $Token"
  "Content-Type" = "application/json"
  Accept         = "application/json, text/event-stream"
}

function Invoke-Mcp([hashtable]$Body) {
  $json = $Body | ConvertTo-Json -Depth 8 -Compress
  # Prefer /mcp (no trailing slash) after mount fix
  return Invoke-WebRequest -Uri "$BaseUrl/mcp" -Method Post -Headers $headers -Body $json -UseBasicParsing
}

Write-Host "initialize..."
$init = Invoke-Mcp @{
  jsonrpc = "2.0"
  id      = 1
  method  = "initialize"
  params  = @{
    protocolVersion = "2025-06-18"
    capabilities    = @{}
    clientInfo      = @{ name = "local-test"; version = "1.0.0" }
  }
}
Write-Host "status=$($init.StatusCode)"
Write-Host $init.Content.Substring(0, [Math]::Min(600, $init.Content.Length))
$session = $init.Headers["mcp-session-id"]
if (-not $session) { $session = $init.Headers["Mcp-Session-Id"] }
if ($session) {
  $headers["mcp-session-id"] = $session
  Write-Host "session=$session"
}

Write-Host "notifications/initialized..."
try {
  $null = Invoke-WebRequest -Uri "$BaseUrl/mcp" -Method Post -Headers $headers `
    -Body '{"jsonrpc":"2.0","method":"notifications/initialized"}' -UseBasicParsing
} catch {
  # 202 is success for this notification; some stacks surface it as error
  Write-Host "initialized status note: $($_.Exception.Message)"
}

Write-Host "tools/list..."
$list = Invoke-Mcp @{
  jsonrpc = "2.0"
  id      = 2
  method  = "tools/list"
  params  = @{}
}
Write-Host $list.Content
Write-Host "LOCAL MCP SMOKE OK"
