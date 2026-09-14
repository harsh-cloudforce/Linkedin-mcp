# LinkedIn Feed Scroller (MCP)

Playwright service that scrolls a user’s LinkedIn **home feed** and returns posts as JSON over **MCP** (Streamable HTTP). Built for cloud agents such as nebulaONE.

Repo: https://github.com/harsh-cloudforce/Linkedin-mcp

## Tools

| Tool | Use |
|---|---|
| `start_linkedin_feed_scan` | Start background scroll |
| `get_linkedin_feed_scan` | Poll until `completed` / `failed` / `awaiting_login` |
| `run_linkedin_feed_scan` | Sync scan (may time out for agents) |

### Remote LinkedIn login (cloud)

If there is no LinkedIn session for a `userId`:

1. Job status → `awaiting_login`
2. Response includes `loginUrl` (remote Chromium via noVNC)
3. User opens the link and signs in (+ 2FA)
4. Scan continues automatically — **no browser opens on the user’s PC**

Each `userId` has its own profile under `PROFILES_DIR`. Mount Azure Files at `/data/profiles` in production so sessions survive restarts.

## Colleague quick start (local)

```powershell
git clone https://github.com/harsh-cloudforce/Linkedin-mcp.git
cd Linkedin-mcp
.\scripts\setup.ps1
Copy-Item .\azure\.env.example .\azure\.env   # fill token/FQDN only if calling the deployed MCP
Copy-Item .\scroller\.env.example .\scroller\.env
```

### Headed CLI scan (first LinkedIn login on your machine)

```powershell
cd .\scroller
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path (Get-Location) ".playwright-browsers"
.\.venv\Scripts\python.exe .\run_scan_cli.py --userId "you@example.com" --headed --maxPosts 60 --maxScrolls 20
```

### Local MCP server

```powershell
.\scripts\run-scroller.ps1
# Health: http://127.0.0.1:8000/healthz
# MCP:    http://127.0.0.1:8000/mcp
```

## Deploy to Azure (shared Container App)

The live app lives in resource group `rg-linkedin-market-pulse` (ACR `acrlinpulse12449`, app `linkedin-feed-scroller`).

### Option A — deploy from your laptop (same as Confluence)

1. Ask the Azure subscription owner to grant you **Contributor** on `rg-linkedin-market-pulse` (and **AcrPush** on the ACR if needed).
2. Install Azure CLI into the venv if needed:  
   `.\scroller\.venv\Scripts\pip.exe install azure-cli`
3. `.\scroller\.venv\Scripts\az.bat login`
4. Copy `azure\.env.example` → `azure\.env` and fill `MCP_FQDN` / `PUBLIC_BASE_URL` (do **not** commit `.env`).
5. Deploy:

```powershell
.\scripts\deploy-containerapp.ps1
```

This builds in ACR (no local Docker required) and updates the Container App. Bearer token is **not** rotated by the script.

### Option B — deploy via GitHub Actions (recommended for teammates)

After the repo owner configures Azure credentials once (see [`docs/AZURE-GITHUB-DEPLOY.md`](docs/AZURE-GITHUB-DEPLOY.md)):

- Push (or merge) to `main`, **or**
- Actions → **Deploy to Azure Container Apps** → **Run workflow**

Teammates only need **write** access to this GitHub repo; they do not need Azure Portal access if Actions is set up.

## nebulaONE wiring

- MCP URL: `https://{fqdn}/mcp`
- Secure header: `Authorization: Bearer <MCP_BEARER_TOKEN>`
- Token is stored as Container App secret `mcp-bearer` and in each developer’s local `azure/.env` (gitignored).

## Layout

| Path | Purpose |
|---|---|
| `scroller/` | MCP server, Playwright feed scanner, Dockerfile |
| `scripts/deploy-containerapp.ps1` | Local Azure deploy |
| `scripts/setup.ps1` | Local Python/Playwright setup |
| `.github/workflows/deploy.yml` | CI deploy to Azure |
| `azure/` | Env examples + Cloud Shell notes |
| `docs/AZURE-GITHUB-DEPLOY.md` | One-time Azure ↔ GitHub setup for shared deploys |
