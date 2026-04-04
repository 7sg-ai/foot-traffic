#!/usr/bin/env bash
# =============================================================================
# Foot Traffic Analyzer - Full Deployment Script
#
# PRIMARY method: Azure Developer CLI (azd)
#   azd up   →  provisions ALL infrastructure + deploys all services
#
# FALLBACK method: raw Azure CLI (when azd is not installed)
#   ./scripts/deploy-all.sh
#
# NOTE: Azure OpenAI and Synapse are provisioned by Bicep automatically.
#       No pre-created resources or API keys are required.
#
# Usage (azd — recommended):
#   azd up
#
# Usage (script — no azd):
#   export AZURE_LOCATION="eastus2"         # optional, defaults to eastus2
#   export VIDEO_FEED_URLS="https://..."    # optional, has defaults
#   ./scripts/deploy-all.sh
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success(){ echo -e "${GREEN}[OK]${NC}    $*"; }
warn()   { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()  { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

# ─── Load environment ─────────────────────────────────────────────────────────
AZD_ENV_FILE=""
if [[ -d "$ROOT_DIR/.azure" ]]; then
  AZD_ENV_FILE=$(find "$ROOT_DIR/.azure" -name ".env" -maxdepth 3 | head -1)
fi

if [[ -f "$AZD_ENV_FILE" ]]; then
  log "Loading azd environment from $AZD_ENV_FILE"
  set -a; source "$AZD_ENV_FILE"; set +a
elif [[ -f "$ROOT_DIR/.env" ]]; then
  log "Loading environment from .env"
  set -a; source "$ROOT_DIR/.env"; set +a
fi

# ─── Config ───────────────────────────────────────────────────────────────────
LOCATION="${AZURE_LOCATION:-${LOCATION:-eastus2}}"
COGNITIVE_SERVICES_LOCATION="${AZURE_COGNITIVE_SERVICES_LOCATION:-${COGNITIVE_SERVICES_LOCATION:-eastus2}}"
ENVIRONMENT="${AZURE_ENV_NAME:-${ENVIRONMENT:-prod}}"
VIDEO_FEED_URLS="${VIDEO_FEED_URLS:-}"
RESOURCE_GROUP="${AZURE_RESOURCE_GROUP:-${RESOURCE_GROUP:-foot-traffic-rg}}"

# =============================================================================
# PATH A: azd is installed → delegate everything to azd
# =============================================================================
if command -v azd >/dev/null 2>&1; then
  echo ""
  echo "╔══════════════════════════════════════════════════════════╗"
  echo "║     Foot Traffic Analyzer - Full Deployment (azd)        ║"
  echo "╚══════════════════════════════════════════════════════════╝"
  echo ""
  log "Azure Developer CLI (azd) detected."
  log "Bicep will provision Azure OpenAI + Synapse automatically."
  log "No pre-created resources or API keys required."
  echo ""

  cd "$ROOT_DIR"

  # Initialize azd environment if needed
  if [[ ! -d "$ROOT_DIR/.azure" ]]; then
    log "Initializing azd environment '$ENVIRONMENT'..."
    azd env new "$ENVIRONMENT" --no-prompt 2>/dev/null || true
  fi

  # Only optional overrides — no secrets needed
  [[ -n "$LOCATION" ]]                     && azd env set AZURE_LOCATION                    "$LOCATION"
  [[ -n "$COGNITIVE_SERVICES_LOCATION" ]]  && azd env set AZURE_COGNITIVE_SERVICES_LOCATION "$COGNITIVE_SERVICES_LOCATION"
  [[ -n "$VIDEO_FEED_URLS" ]]              && azd env set VIDEO_FEED_URLS                   "$VIDEO_FEED_URLS"

  log "Running 'azd up' (provision + deploy + hooks)..."
  azd up --no-prompt

  echo ""
  success "Deployment complete via azd!"
  log "Run 'azd show'    to see all deployed service URLs."
  log "Run 'azd monitor' to open Azure Monitor dashboards."
  log "Run 'azd env list' to see all provisioned resource names."
  echo ""
  exit 0
fi

# =============================================================================
# PATH B: azd not installed → raw Azure CLI fallback
# =============================================================================
echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║   Foot Traffic Analyzer - Full Deployment (az cli)        ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
warn "azd not found. Using raw Azure CLI."
warn "Install azd for a better experience: https://aka.ms/azd-install"
echo ""

command -v az     >/dev/null 2>&1 || error "Azure CLI not found."
command -v jq     >/dev/null 2>&1 || error "jq not found. Install: brew install jq"
command -v docker >/dev/null 2>&1 || error "Docker not found."
az account show   >/dev/null 2>&1 || error "Not logged in to Azure. Run: az login"

# ─── Step 1: Infrastructure (Bicep provisions OpenAI + Synapse) ───────────────
log "Step 1/4: Deploying Azure infrastructure (OpenAI + Synapse + all services)..."
LOCATION="$LOCATION" \
COGNITIVE_SERVICES_LOCATION="$COGNITIVE_SERVICES_LOCATION" \
VIDEO_FEED_URLS="$VIDEO_FEED_URLS" \
RESOURCE_GROUP="$RESOURCE_GROUP" \
ENVIRONMENT="$ENVIRONMENT" \
  bash "$ROOT_DIR/infrastructure/deploy.sh"

[[ -f "$ROOT_DIR/.deployment-outputs.json" ]] || error ".deployment-outputs.json not found after infra deploy."
FUNCTIONS_CA_NAME=$(jq -r '.functionsContainerAppName' "$ROOT_DIR/.deployment-outputs.json")
CONTAINER_APP_URL=$(jq -r '.containerAppUrl'           "$ROOT_DIR/.deployment-outputs.json")
SYNAPSE_WORKSPACE=$(jq -r '.synapseWorkspace'          "$ROOT_DIR/.deployment-outputs.json")
SYNAPSE_SQL_POOL=$(jq -r  '.synapseSqlPool'            "$ROOT_DIR/.deployment-outputs.json")
KEY_VAULT_NAME=$(jq -r    '.keyVaultName'              "$ROOT_DIR/.deployment-outputs.json")
ACR_LOGIN_SERVER=$(jq -r  '.acrLoginServer'            "$ROOT_DIR/.deployment-outputs.json")
OPENAI_ENDPOINT=$(jq -r   '.openAiEndpoint'            "$ROOT_DIR/.deployment-outputs.json")
success "Infrastructure deployed (OpenAI: $OPENAI_ENDPOINT)"

# ─── Step 2: Database Schema ──────────────────────────────────────────────────
log "Step 2/4: Initializing Synapse database schema..."
SYNAPSE_SERVER="${SYNAPSE_WORKSPACE}.sql.azuresynapse.net"

# Retrieve password from Key Vault — no manual password needed
SYNAPSE_SQL_PASSWORD=$(az keyvault secret show \
  --vault-name "$KEY_VAULT_NAME" \
  --name "synapse-sql-password" \
  --query "value" -o tsv 2>/dev/null || echo "")

if [[ -n "$SYNAPSE_SQL_PASSWORD" ]] && command -v sqlcmd >/dev/null 2>&1; then
  sqlcmd \
    -S "$SYNAPSE_SERVER" -d "$SYNAPSE_SQL_POOL" \
    -U "sqladmin" -P "$SYNAPSE_SQL_PASSWORD" \
    -i "$ROOT_DIR/database/schema.sql" -I -C 2>&1 \
    || warn "Schema may already exist — OK on re-deploy."
  success "Database schema initialized"
else
  warn "sqlcmd not found or password unavailable. Run schema manually:"
  warn "  sqlcmd -S $SYNAPSE_SERVER -d $SYNAPSE_SQL_POOL -U sqladmin -P <from-keyvault> -i database/schema.sql -I"
fi

# ─── Step 3: Functions Container App ─────────────────────────────────────────
log "Step 3/4: Building and deploying Azure Functions container..."
az acr login --name "${ACR_LOGIN_SERVER%%.*}" --output none

docker build \
  -t "${ACR_LOGIN_SERVER}/foot-traffic-functions:latest" \
  -f "$ROOT_DIR/functions/Dockerfile" \
  "$ROOT_DIR/functions" --quiet

docker push "${ACR_LOGIN_SERVER}/foot-traffic-functions:latest" --quiet

az containerapp update \
  --resource-group "$RESOURCE_GROUP" \
  --name "$FUNCTIONS_CA_NAME" \
  --image "${ACR_LOGIN_SERVER}/foot-traffic-functions:latest" \
  --output none

success "Azure Functions container deployed"

# ─── Step 4: Streamlit Container App ─────────────────────────────────────────
log "Step 4/4: Building and deploying Streamlit app..."
az acr login --name "${ACR_LOGIN_SERVER%%.*}" --output none

docker build \
  -t "${ACR_LOGIN_SERVER}/foot-traffic-streamlit:latest" \
  -f "$ROOT_DIR/streamlit_app/Dockerfile" \
  "$ROOT_DIR/streamlit_app" --quiet

docker push "${ACR_LOGIN_SERVER}/foot-traffic-streamlit:latest" --quiet

CONTAINER_APP_NAME=$(az containerapp list \
  --resource-group "$RESOURCE_GROUP" \
  --query "[0].name" -o tsv)

az containerapp update \
  --resource-group "$RESOURCE_GROUP" \
  --name "$CONTAINER_APP_NAME" \
  --image "${ACR_LOGIN_SERVER}/foot-traffic-streamlit:latest" \
  --output none

success "Streamlit app deployed"

# ─── Summary ─────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║                  🎉 Deployment Complete!                  ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
success "Streamlit Dashboard: $CONTAINER_APP_URL"
success "Azure OpenAI:        $OPENAI_ENDPOINT"
echo ""
log "The video analyzer will start collecting data every 5 minutes."
log "Check the ⚙️ Monitor page in the dashboard to track analysis jobs."
echo ""
