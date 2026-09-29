# Ensure Azure Files mount at /persist for Market Pulse.
# Live SQLite stays on local disk (DATA_DIR); persist.py syncs to /persist.
#   .\scripts\ensure-webapp-storage.ps1

param(
  [string]$ResourceGroup = "rg-linkedin-market-pulse",
  [string]$Location = "eastus2",
  [string]$AppName = "mpulse",
  [string]$EnvironmentName = "cae-linkedin-market-pulse",
  [string]$StorageAccount = "stlinpulse12449",
  [string]$ShareName = "mpulse-data",
  [string]$EnvStorageName = "mpulse-files"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$azureEnv = Join-Path $root "azure\.env"

$azCmd = "az"
if (-not (Get-Command az.exe -ErrorAction SilentlyContinue) -and -not (Get-Command az.cmd -ErrorAction SilentlyContinue)) {
  $venvAz = Join-Path $root "scroller\.venv\Scripts\az.bat"
  if (Test-Path $venvAz) { $azCmd = $venvAz } else { throw "Azure CLI not found" }
}

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
  # Prefer splatting remaining args without binding -o to PowerShell common params
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

$fromRg = Get-DotEnvValue $azureEnv "AZURE_RESOURCE_GROUP"
$fromLoc = Get-DotEnvValue $azureEnv "AZURE_LOCATION"
$fromSa = Get-DotEnvValue $azureEnv "WEBAPP_STORAGE_ACCOUNT"
if ($fromRg) { $ResourceGroup = $fromRg }
if ($fromLoc) { $Location = $fromLoc }
if ($fromSa) { $StorageAccount = $fromSa }

Write-Host "Ensuring storage account $StorageAccount ..." -ForegroundColor Cyan
$prev = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$null = & $azResolved storage account show -n $StorageAccount -g $ResourceGroup -o none 2>&1
$saExists = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = $prev
if (-not $saExists) {
  Invoke-AzCli storage account create -n $StorageAccount -g $ResourceGroup -l $Location --sku Standard_LRS --kind StorageV2 --allow-blob-public-access false
}

$storageKey = & $azResolved storage account keys list -n $StorageAccount -g $ResourceGroup --query "[0].value" -o tsv
if (-not $storageKey) { throw "Could not read storage account key" }

Write-Host "Ensuring file share $ShareName ..." -ForegroundColor Cyan
$ErrorActionPreference = "Continue"
& $azResolved storage share create --account-name $StorageAccount --account-key $storageKey --name $ShareName --quota 50 -o none 2>&1 | Out-Null
$ErrorActionPreference = $prev

Write-Host "Registering Azure Files on Container Apps env ..." -ForegroundColor Cyan
Invoke-AzCli containerapp env storage set `
  -g $ResourceGroup `
  -n $EnvironmentName `
  --storage-name $EnvStorageName `
  --azure-file-account-name $StorageAccount `
  --azure-file-account-key $storageKey `
  --azure-file-share-name $ShareName `
  --access-mode ReadWrite

Write-Host "Patching $AppName to mount /persist ..." -ForegroundColor Cyan
$jsonPath = Join-Path $env:TEMP "mpulse-app.json"
$yamlPath = Join-Path $env:TEMP "mpulse-app-update.yaml"
Invoke-AzCli containerapp show -n $AppName -g $ResourceGroup -o json | Set-Content -Path $jsonPath -Encoding UTF8

$py = Join-Path $root "webapp\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

$patchScript = Join-Path $env:TEMP "patch_mpulse_volume.py"
@'
import json
from pathlib import Path

src = Path(r"""JSON_PATH""")
dst = Path(r"""YAML_PATH""")
app = json.loads(src.read_text(encoding="utf-8-sig"))
template = app.setdefault("properties", {}).setdefault("template", {})
vols = template.setdefault("volumes", []) or []
if not any(v.get("name") == "mpulse-data-vol" for v in vols):
    vols.append({"name": "mpulse-data-vol", "storageName": "ENV_STORAGE", "storageType": "AzureFile"})
else:
    for v in vols:
        if v.get("name") == "mpulse-data-vol":
            v["storageName"] = "ENV_STORAGE"
            v["storageType"] = "AzureFile"
template["volumes"] = vols

containers = template.get("containers") or []
if not containers:
    raise SystemExit("no containers")
c0 = containers[0]
mounts = []
for m in (c0.get("volumeMounts") or []):
    if m.get("volumeName") == "mpulse-data-vol":
        m = dict(m)
        m["mountPath"] = "/persist"
    mounts.append(m)
if not any(m.get("volumeName") == "mpulse-data-vol" for m in mounts):
    mounts.append({"volumeName": "mpulse-data-vol", "mountPath": "/persist"})
c0["volumeMounts"] = mounts

envs = c0.setdefault("env", []) or []
wanted = {"DATA_DIR": "/tmp/mpulse-data", "PERSIST_DIR": "/persist"}
seen = set()
for e in envs:
    n = e.get("name")
    if n in wanted:
        e["value"] = wanted[n]
        e.pop("secretRef", None)
        seen.add(n)
for n, v in wanted.items():
    if n not in seen:
        envs.append({"name": n, "value": v})
c0["env"] = envs

lines = ["properties:", "  template:", "    containers:"]
for c in containers:
    lines.append(f"    - name: {c['name']}")
    lines.append(f"      image: {c['image']}")
    if c.get("resources"):
        lines.append("      resources:")
        if c["resources"].get("cpu") is not None:
            lines.append(f"        cpu: {c['resources']['cpu']}")
        if c["resources"].get("memory") is not None:
            lines.append(f"        memory: {c['resources']['memory']}")
    lines.append("      env:")
    for e in c.get("env") or []:
        lines.append(f"      - name: {e['name']}")
        if "secretRef" in e:
            lines.append(f"        secretRef: {e['secretRef']}")
        elif e.get("value") is not None:
            val = str(e["value"]).replace('"', '\\"')
            lines.append(f'        value: "{val}"')
    lines.append("      volumeMounts:")
    for m in c.get("volumeMounts") or []:
        lines.append(f"      - volumeName: {m['volumeName']}")
        lines.append(f"        mountPath: {m['mountPath']}")
lines.append("    volumes:")
for v in template["volumes"]:
    lines.append(f"    - name: {v['name']}")
    lines.append(f"      storageName: {v['storageName']}")
    lines.append(f"      storageType: {v['storageType']}")
dst.write_text("\n".join(lines) + "\n", encoding="utf-8")
print("patched", dst)
'@ -replace 'JSON_PATH', $jsonPath.Replace('\', '\\') -replace 'YAML_PATH', $yamlPath.Replace('\', '\\') -replace 'ENV_STORAGE', $EnvStorageName |
  Set-Content -Path $patchScript -Encoding UTF8

& $py $patchScript
if ($LASTEXITCODE -ne 0) { throw "Failed to patch container app volume config" }

Invoke-AzCli containerapp update -n $AppName -g $ResourceGroup --yaml $yamlPath

if (Test-Path $azureEnv) {
  Set-Or-AddEnvLine $azureEnv "WEBAPP_STORAGE_ACCOUNT" $StorageAccount
  Set-Or-AddEnvLine $azureEnv "WEBAPP_FILE_SHARE" $ShareName
  Set-Or-AddEnvLine $azureEnv "WEBAPP_ENV_STORAGE" $EnvStorageName
}

Write-Host ""
Write-Host "Persistent /persist mount ready (DB synced from local SQLite)." -ForegroundColor Green
Write-Host "Re-add users once if needed; they will survive redeploys."
