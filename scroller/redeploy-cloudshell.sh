#!/usr/bin/env bash
# Rebuild + update existing LinkedIn Feed Scroller MCP (remote login image)
# Run in Azure Cloud Shell (Bash) after uploading/unzipping the new scroller sources.

set -euo pipefail

RG="${RG:-rg-linkedin-market-pulse}"
APP="${APP:-linkedin-feed-scroller}"
ACR="${ACR:-acrlinpulse12449}"
FQDN_DEFAULT="linkedin-feed-scroller.icyplant-a283531a.eastus2.azurecontainerapps.io"

cd "$(dirname "$0")"
if [[ ! -f Dockerfile ]]; then
  echo "Run this from the scroller directory (Dockerfile missing)."
  exit 1
fi

echo "Building image in ACR: $ACR ..."
az acr build -r "$ACR" -t "${APP}:latest" -f Dockerfile .

echo "Updating Container App: $APP ..."
az containerapp update \
  -n "$APP" \
  -g "$RG" \
  --image "$ACR.azurecr.io/${APP}:latest" \
  --set-env-vars \
    "DEFAULT_HEADED=false" \
    "REMOTE_LOGIN_ENABLED=true" \
    "PROFILES_DIR=/data/profiles" \
    "PUBLIC_BASE_URL=https://${FQDN_DEFAULT}" \
    "DEFAULT_LOGIN_WAIT_SECONDS=600"

FQDN=$(az containerapp show -n "$APP" -g "$RG" --query properties.configuration.ingress.fqdn -o tsv)
echo ""
echo "Deployed."
echo "Health: https://$FQDN/healthz"
echo "MCP:    https://$FQDN/mcp"
echo "Set PUBLIC_BASE_URL if FQDN differs: https://$FQDN"
curl -sS "https://$FQDN/healthz" || true
echo ""
