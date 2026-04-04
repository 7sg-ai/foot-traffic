#!/usr/bin/env bash
# =============================================================================
# azd postdeploy hook
# Runs automatically after `azd deploy` completes.
# Prints the Streamlit dashboard URL and verifies health.
# =============================================================================
set -euo pipefail

BLUE='\033[0;34m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
log()    { echo -e "${BLUE}[postdeploy]${NC} $*"; }
success(){ echo -e "${GREEN}[postdeploy]${NC} ✓ $*"; }
warn()   { echo -e "${YELLOW}[postdeploy]${NC} ⚠ $*"; }

# azd injects AZURE_CONTAINER_APP_URI (or similar) for containerapp services.
# The exact variable name depends on the service name in azure.yaml.
STREAMLIT_URL="${SERVICE_STREAMLIT_DASHBOARD_URI:-${AZURE_CONTAINER_APP_URI:-}}"

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║              🎉 Deployment Complete!                      ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""

if [[ -n "$STREAMLIT_URL" ]]; then
  success "Streamlit Dashboard: $STREAMLIT_URL"

  # Quick health check
  log "Checking dashboard health..."
  for i in $(seq 1 6); do
    HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
      "${STREAMLIT_URL}/_stcore/health" 2>/dev/null || echo "000")

    if [[ "$HTTP_STATUS" == "200" ]]; then
      success "Dashboard is healthy (HTTP 200)"
      break
    fi

    if [[ $i -eq 6 ]]; then
      warn "Dashboard health check timed out — it may still be starting up."
      warn "Try opening $STREAMLIT_URL in a few minutes."
    else
      log "  Waiting for dashboard... (attempt $i/6, HTTP $HTTP_STATUS)"
      sleep 10
    fi
  done
else
  warn "Could not determine dashboard URL from azd environment."
  warn "Run: azd show  to see deployed service URLs."
fi

echo ""
log "The video analyzer runs every 5 minutes automatically."
log "Check the ⚙️ Monitor page in the dashboard to track analysis jobs."
echo ""
