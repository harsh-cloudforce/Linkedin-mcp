# Deploy LinkedIn Feed Scroller MCP — no local Azure CLI / no admin

Local machine often cannot install `az` (MSI needs elevation). Use **Azure Cloud Shell** instead (browser, already has `az` + Docker-less `az acr build`).

## Prerequisites

- Access to an Azure subscription in the portal
- Permission to create: Resource Group, ACR, Container Apps Environment, Container App

## Steps

### 1. Open Cloud Shell

1. Go to https://portal.azure.com  
2. Click the **Cloud Shell** icon (`>_`)  
3. Choose **Bash** (or PowerShell)

### 2. Upload the `scroller` folder

In Cloud Shell:

```bash
mkdir -p ~/linkedin-market-pulse && cd ~/linkedin-market-pulse
```

Upload via Cloud Shell **Upload/Download** button: zip the local `scroller` folder first, then:

```powershell
# On your Windows machine (local) — creates scroller.zip
cd "C:\Users\HarshShrishrimal\OneDrive - Cloudforce\Desktop\Linkedin Workflow"
Compress-Archive -Path .\scroller\* -DestinationPath .\scroller.zip -Force
```

Upload `scroller.zip` into Cloud Shell, then:

```bash
cd ~/linkedin-market-pulse
unzip -o scroller.zip -d scroller
cd scroller
```

### 3. Run deploy script in Cloud Shell

```bash
# edit names if you want
RG=rg-linkedin-market-pulse
LOC=eastus2
APP=linkedin-feed-scroller
ENV=cae-linkedin-market-pulse
ACR=acrlinpulse$RANDOM   # must be globally unique, lowercase alphanumeric

az group create -n "$RG" -l "$LOC"

az acr create -n "$ACR" -g "$RG" --sku Basic
az acr update -n "$ACR" -g "$RG" --admin-enabled true

az acr build -r "$ACR" -t "${APP}:latest" -f Dockerfile .

az containerapp env create -n "$ENV" -g "$RG" -l "$LOC"

# generate MCP bearer token
TOKEN=$(openssl rand -hex 24)
echo "SAVE THIS MCP BEARER TOKEN: $TOKEN"

ACR_USER=$(az acr credential show -n "$ACR" --query username -o tsv)
ACR_PASS=$(az acr credential show -n "$ACR" --query passwords[0].value -o tsv)

az containerapp create \
  -n "$APP" \
  -g "$RG" \
  --environment "$ENV" \
  --image "$ACR.azurecr.io/${APP}:latest" \
  --registry-server "$ACR.azurecr.io" \
  --registry-username "$ACR_USER" \
  --registry-password "$ACR_PASS" \
  --target-port 8000 \
  --ingress external \
  --cpu 1.0 --memory 2.0Gi \
  --min-replicas 1 --max-replicas 1 \
  --secrets mcp-bearer="$TOKEN" \
  --env-vars \
    MCP_BEARER_TOKEN=secretref:mcp-bearer \
    DEFAULT_HEADED=false \
    REMOTE_LOGIN_ENABLED=true \
    PROFILES_DIR=/data/profiles \
    PUBLIC_BASE_URL=https://PLACEHOLDER

FQDN=$(az containerapp show -n "$APP" -g "$RG" --query properties.configuration.ingress.fqdn -o tsv)
# set real public URL for login links
az containerapp update -n "$APP" -g "$RG" \
  --set-env-vars "PUBLIC_BASE_URL=https://$FQDN"

echo "Health: https://$FQDN/healthz"
echo "MCP URL: https://$FQDN/mcp"
echo "Bearer token (Authorization header): Bearer $TOKEN"
```

Save FQDN + token into local `azure/.env` (gitignored). Attach the MCP URL in your client UI yourself.

### Remote LinkedIn login (nebulaONE)

When a user has no saved session, the job status becomes `awaiting_login` and includes `loginUrl`.
The agent must show that link; the user opens it, signs into LinkedIn in the **remote** Chromium (noVNC), then the scan continues.

**Profiles are ephemeral** on the container filesystem unless you mount Azure Files at `/data/profiles`. Without a volume, logins are lost on restart/redeploy.

### Rebuild after code changes

```bash
cd ~/linkedin-market-pulse/scroller   # with updated sources unzipped
az acr build -r "$ACR" -t "${APP}:latest" -f Dockerfile .
az containerapp update -n "$APP" -g "$RG" --image "$ACR.azurecr.io/${APP}:latest"
```

## Alternate: Portal-only (clickops)

1. Portal → Container Apps → Create  
2. Create ACR, build with “Azure Container Registry Tasks” from uploaded source, or push image from Cloud Shell  
3. Ingress: external, port **8000**  
4. Env: `MCP_BEARER_TOKEN`, `DEFAULT_HEADED=false`, `PROFILES_DIR=/data/profiles`  

## Local was already verified

On your PC, MCP smoke test passed:

- `GET /healthz` → ok  
- `initialize` + `tools/list` → `start_linkedin_feed_scan`, `get_linkedin_feed_scan`, `run_linkedin_feed_scan`
