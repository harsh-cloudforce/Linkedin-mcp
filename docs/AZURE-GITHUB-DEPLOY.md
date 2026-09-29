# One-time: let teammates deploy to your Azure via GitHub Actions

Goal: anyone with **write** access to [harsh-cloudforce/Linkedin-mcp](https://github.com/harsh-cloudforce/Linkedin-mcp) can push to `main` (or run the workflow) and update **your** Container App — without sharing your personal Azure login.

## Prerequisite (Cloudforce / Entra)

Creating the GitHub deploy identity needs permission to **register applications** in Microsoft Entra ID.

If `az ad app create` fails with `Insufficient privileges` / `Directory permission is needed`, ask IT (or a Global/Application Administrator) to either:

- Grant you the **Application Developer** directory role, **or**
- Run the commands in §1 for you and send back the three IDs

RG Contributor alone is **not** enough for this step.

## 1. Create an Azure app registration + federated credential (OIDC)

In **Azure Cloud Shell (Bash)** while signed into the Cloudforce subscription:

```bash
SUB=$(az account show --query id -o tsv)
TENANT=$(az account show --query tenantId -o tsv)
RG=rg-linkedin-market-pulse
ACR=acrlinpulse12449
APP_NAME=github-linkedin-mcp-deploy

az ad app create --display-name "$APP_NAME" -o none
APP_ID=$(az ad app list --display-name "$APP_NAME" --query [0].appId -o tsv)
APP_OBJ=$(az ad app list --display-name "$APP_NAME" --query [0].id -o tsv)
az ad sp create --id "$APP_ID" -o none

az ad app federated-credential create --id "$APP_OBJ" --parameters '{
  "name": "github-linkedin-mcp-main",
  "issuer": "https://token.actions.githubusercontent.com",
  "subject": "repo:harsh-cloudforce/Linkedin-mcp:ref:refs/heads/main",
  "audiences": ["api://AzureADTokenExchange"]
}'

az ad app federated-credential create --id "$APP_OBJ" --parameters '{
  "name": "github-linkedin-mcp-env-production",
  "issuer": "https://token.actions.githubusercontent.com",
  "subject": "repo:harsh-cloudforce/Linkedin-mcp:environment:production",
  "audiences": ["api://AzureADTokenExchange"]
}'

SP_OID=$(az ad sp show --id "$APP_ID" --query id -o tsv)
az role assignment create --assignee-object-id "$SP_OID" --assignee-principal-type ServicePrincipal \
  --role Contributor --scope "/subscriptions/$SUB/resourceGroups/$RG"
az role assignment create --assignee-object-id "$SP_OID" --assignee-principal-type ServicePrincipal \
  --role AcrPush --scope "/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.ContainerRegistry/registries/$ACR"

echo "AZURE_CLIENT_ID=$APP_ID"
echo "AZURE_TENANT_ID=$TENANT"
echo "AZURE_SUBSCRIPTION_ID=$SUB"
```

## 2. Add GitHub secrets + `production` environment

1. Repo → **Settings** → **Environments** → create **`production`** (if missing).
2. Repo → **Settings** → **Secrets and variables** → **Actions** → add:

| Secret | Value |
|---|---|
| `AZURE_CLIENT_ID` | App (client) ID from the echo above |
| `AZURE_TENANT_ID` | Tenant ID |
| `AZURE_SUBSCRIPTION_ID` | Subscription ID |

## 3. Invite colleagues

- GitHub: **Settings → Collaborators** → **Write**
- Azure (laptop deploy): **Contributor** on `rg-linkedin-market-pulse`

Share `MCP_BEARER_TOKEN` out-of-band only (never commit `azure/.env`).

## 4. Verify

**Actions → Deploy to Azure Container Apps → Run workflow** (or push a change under `scroller/`).

```powershell
Invoke-RestMethod https://linkedin-feed-scroller.icyplant-a283531a.eastus2.azurecontainerapps.io/healthz
```

## Fallback without GitHub Actions

```powershell
.\scripts\deploy-containerapp.ps1
```
