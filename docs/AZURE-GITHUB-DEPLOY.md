# One-time: let teammates deploy to your Azure via GitHub Actions

Goal: anyone with **write** access to [harsh-cloudforce/Linkedin-mcp](https://github.com/harsh-cloudforce/Linkedin-mcp) can push to `main` (or run the workflow) and update **your** Container App — without sharing your personal Azure login.

## 1. Create an Azure app registration + federated credential (OIDC)

In Azure Cloud Shell or a machine with `az`:

```bash
SUB=$(az account show --query id -o tsv)
TENANT=$(az account show --query tenantId -o tsv)
RG=rg-linkedin-market-pulse
ACR=acrlinpulse12449
APP_NAME=github-linkedin-mcp-deploy

az ad app create --display-name "$APP_NAME" -o none
APP_ID=$(az ad app list --display-name "$APP_NAME" --query [0].appId -o tsv)
az ad sp create --id "$APP_ID" -o none

# Federated credential for this GitHub repo + main branch (and workflow_dispatch on main)
az ad app federated-credential create --id "$APP_ID" --parameters "{
  \"name\": \"github-linkedin-mcp-main\",
  \"issuer\": \"https://token.actions.githubusercontent.com\",
  \"subject\": \"repo:harsh-cloudforce/Linkedin-mcp:ref:refs/heads/main\",
  \"audiences\": [\"api://AzureADTokenExchange\"]
}"

# Also allow environment-less workflow_dispatch from the same repo (optional second cred)
az ad app federated-credential create --id "$APP_ID" --parameters "{
  \"name\": \"github-linkedin-mcp-env-production\",
  \"issuer\": \"https://token.actions.githubusercontent.com\",
  \"subject\": \"repo:harsh-cloudforce/Linkedin-mcp:environment:production\",
  \"audiences\": [\"api://AzureADTokenExchange\"]
}"

SP_OID=$(az ad sp show --id "$APP_ID" --query id -o tsv)
az role assignment create --assignee-object-id "$SP_OID" --assignee-principal-type ServicePrincipal \
  --role Contributor --scope "/subscriptions/$SUB/resourceGroups/$RG"
az role assignment create --assignee-object-id "$SP_OID" --assignee-principal-type ServicePrincipal \
  --role AcrPush --scope "/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.ContainerRegistry/registries/$ACR"

echo "AZURE_CLIENT_ID=$APP_ID"
echo "AZURE_TENANT_ID=$TENANT"
echo "AZURE_SUBSCRIPTION_ID=$SUB"
```

## 2. Add GitHub secrets + optional environment

Repo → **Settings** → **Secrets and variables** → **Actions**:

| Secret | Value |
|---|---|
| `AZURE_CLIENT_ID` | App (client) ID from above |
| `AZURE_TENANT_ID` | Tenant ID |
| `AZURE_SUBSCRIPTION_ID` | Subscription ID |

Optional but recommended: create GitHub Environment **`production`** (Settings → Environments) and restrict it to `main` / required reviewers. The workflow uses that environment name.

## 3. Invite colleagues

- GitHub: **Settings → Collaborators** → add teammate with **Write**
- Azure (if they should also deploy from a laptop): grant **Contributor** on `rg-linkedin-market-pulse`

They never need `azure/.env` committed. For calling the live MCP from tools, each person copies `azure/.env.example` → `azure/.env` and pastes the shared bearer token out-of-band (1Password / Teams).

## 4. Verify

Push a trivial commit to `main`, or **Actions → Deploy to Azure Container Apps → Run workflow**.  
Check the run log, then:

```powershell
Invoke-RestMethod https://linkedin-feed-scroller.icyplant-a283531a.eastus2.azurecontainerapps.io/healthz
```

## Fallback without GitHub Actions

Share Azure RBAC on the resource group and use:

```powershell
.\scripts\deploy-containerapp.ps1
```

See the README “Option A” section.
