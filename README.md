# LinkedIn / Market Pulse (nebulaONE)

Daily **feed intelligence** for sales, marketing, and recruiting — inspired by James’s Claude Cowork flow, productized like PVT Territory Manager.

## Architecture

```
User → Market Pulse webapp (dashboard + SQLite DB)
         ├─ Daily scheduler → LinkedIn scroller MCP (Azure)
         ├─ Stores raw posts + dated briefs (no overwrite)
         └─ MCP / REST + INTEGRATION_API_KEY → nebulaONE Official Agent

Later: more sources (RSS, other platforms) behind the same DB + API.
```

| Piece | Path |
|---|---|
| **Webapp + DB + daily jobs + MCP** | `webapp/` |
| **LinkedIn feed scroller (ingest)** | `scroller/` + Azure Container App |
| **Agent copy** | `nebulaone/AGENT.md` |

## Quick start — webapp

```powershell
cd "C:\Users\HarshShrishrimal\OneDrive - Cloudforce\Desktop\Linkedin Workflow"
.\scripts\run-webapp.ps1
```

Open http://127.0.0.1:8790/ — register a user, run a scan, read briefs.

nebulaONE: MCP URL `http://127.0.0.1:8790/mcp/` (or your deployed host) with  
`Authorization: Bearer <INTEGRATION_API_KEY>`.

See `webapp/README.md`.

## LinkedIn scroller (Azure)

See `scroller/` and `scripts/deploy-containerapp.ps1`. Required for live LinkedIn home-feed scrapes from the webapp.

## Deploy webapp (Azure)

```powershell
cd "C:\Users\HarshShrishrimal\OneDrive - Cloudforce\Desktop\Linkedin Workflow"
.\scripts\deploy-webapp.ps1
```

Creates/updates short app name **`mpulse`** → `https://mpulse.<env>.azurecontainerapps.io/`.  
No Render needed — this is one FastAPI app (HTML UI + API + MCP).

**Durable data:** SQLite lives on the container’s local disk and is synced every ~20s to Azure Files at `/persist` (`mpulse-data` share). Users, scans, briefs, and settings survive image redeploys. Settings shows persist status (scans/briefs counts).

For an even shorter vanity host (e.g. `pulse.gocloudforce.com`), add a **Custom domain** on the Container App in Azure Portal.

After deploy: sign in as admin → **Settings → Generate API key** → paste into nebulaONE (`/mcp` + Bearer header).

## nebulaONE Official Agent

See `nebulaone/AGENT.md` for the system message and MCP connection steps.

- MCP URL: `https://mpulse.whitesand-f9361ec3.eastus2.azurecontainerapps.io/mcp/`
- Auth: `Authorization: Bearer <INTEGRATION_API_KEY>`

LinkedIn login is saved server-side after the first successful sign-in (or by pasting `li_at` in Settings), so the agent should not need remote login on every scan.