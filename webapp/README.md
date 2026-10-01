# Market Pulse webapp

PVT-style app: **SQLite database** + dashboard + **daily LinkedIn scrape** → dated brief documents. nebulaONE connects with an **API key** over REST or MCP (same pattern as [Territory Manager](https://nebulaone.onrender.com/)).

## Run locally

```powershell
cd "C:\Users\HarshShrishrimal\OneDrive - Cloudforce\Desktop\Linkedin Workflow"
.\scripts\run-webapp.ps1
```

- Dashboard: http://127.0.0.1:8790/
- Health: http://127.0.0.1:8790/healthz
- MCP (nebulaONE): http://127.0.0.1:8790/mcp/
  Header: `Authorization: Bearer <INTEGRATION_API_KEY>`
- REST: `/api/integration/v1/*` (same key)

Copy `webapp/.env.example` → `webapp/.env` and set:

- `INTEGRATION_API_KEY` — key you paste into nebulaONE
- `SCROLLER_BEARER_TOKEN` — same bearer as Azure LinkedIn scroller (`azure/.env`)

## Daily automation

APScheduler runs `run_daily_for_all_active_users` at `DAILY_SCAN_HOUR_UTC` (default 13:00 UTC). Every **active** user in the DB is scanned; posts + a **new** brief row are stored (no overwrite).

## MCP tools

| Tool | Purpose |
|---|---|
| `register_user` | Add user to daily roster |
| `start_market_pulse_scan` | Scrape LinkedIn → DB → brief |
| `list_briefs` | List dated reports |
| `get_brief` | Full markdown report |
| `get_scan` | Scan status / loginUrl |

## nebulaONE wiring (Azure)

1. Sign in at `https://mpulse.whitesand-f9361ec3.eastus2.azurecontainerapps.io/login` as admin
2. **Settings → Generate API key** (shown once — copy it)
3. Connections → MCP Server (HTTPS) → `https://mpulse.whitesand-f9361ec3.eastus2.azurecontainerapps.io/mcp/`
4. Header: `Authorization` = `Bearer <generated key>`

Scroller bearer token is **server-only** (Azure secret) — not editable in the UI.

Redeploy:

```powershell
.\scripts\deploy-webapp.ps1
```

## Layout

| Path | Role |
|---|---|
| `app/models.py` | users, sources, scans, posts, briefs |
| `app/services/pipeline.py` | scroller MCP → DB → brief |
| `app/services/scheduler.py` | daily cron |
| `app/mcp_tools.py` | FastMCP tools |
| `data/market_pulse.db` | SQLite (gitignored) |
