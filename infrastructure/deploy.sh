#!/usr/bin/env bash
# =============================================================================
# Foot Traffic Analyzer - Azure Infrastructure Deployment Script
#
# PRIMARY method: Azure Developer CLI (azd)
#   azd up          → provision + deploy everything in one command
#   azd provision   → infrastructure only
#   azd deploy      → application code only
#   azd down        → tear down all resources
#
# FALLBACK method: raw Azure CLI (used when azd is not available)
#   ./infrastructure/deploy.sh
#
# NOTE: Azure OpenAI and Synapse are provisioned by Bicep automatically.
#       No pre-created resources or API keys are required.
#       The only required inputs are: Azure subscription + location.
# =============================================================================
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success(){ echo -e "${GREEN}[OK]${NC}    $*"; }
warn()   { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()  { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

# ─── Load .env / azd environment ─────────────────────────────────────────────
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

# ─── Configuration ────────────────────────────────────────────────────────────
RESOURCE_GROUP="${AZURE_RESOURCE_GROUP:-${RESOURCE_GROUP:-foot-traffic-rg}}"
LOCATION="${AZURE_LOCATION:-${LOCATION:-eastus2}}"
COGNITIVE_SERVICES_LOCATION="${AZURE_COGNITIVE_SERVICES_LOCATION:-${COGNITIVE_SERVICES_LOCATION:-eastus2}}"
BASE_NAME="${BASE_NAME:-foottraffic}"
ENVIRONMENT="${AZURE_ENV_NAME:-${ENVIRONMENT:-prod}}"
VIDEO_FEED_URLS="${VIDEO_FEED_URLS:-}"

# ─── Prefer azd if available ─────────────────────────────────────────────────
if command -v azd >/dev/null 2>&1; then
  echo ""
  echo "╔══════════════════════════════════════════════════════╗"
  echo "║     Foot Traffic Analyzer - Azure Deployment (azd)   ║"
  echo "╚══════════════════════════════════════════════════════╝"
  echo ""
  log "Azure Developer CLI (azd) detected."
  log "Bicep will provision Azure OpenAI + Synapse automatically."
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

  log "Provisioning infrastructure with azd..."
  log "(Azure OpenAI + Synapse will be created automatically)"
  azd provision --no-prompt

  success "Infrastructure provisioned via azd"
  log "Run 'azd deploy' to deploy application code, or 'azd up' to do both."
  exit 0
fi

# ─── Fallback: raw Azure CLI ──────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║   Foot Traffic Analyzer - Azure Deployment (az cli)  ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
warn "azd not found. Falling back to raw Azure CLI."
warn "Install azd for a better experience: https://aka.ms/azd-install"
echo ""

check_prerequisites() {
  log "Checking prerequisites..."
  command -v az  >/dev/null 2>&1 || error "Azure CLI not found. Install: https://aka.ms/install-azure-cli"
  command -v jq  >/dev/null 2>&1 || error "jq not found. Install: brew install jq"
  az account show >/dev/null 2>&1 || error "Not logged in to Azure. Run: az login"
  success "Prerequisites OK"
}

create_resource_group() {
  log "Creating resource group: $RESOURCE_GROUP in $LOCATION..."
  az group create \
    --name "$RESOURCE_GROUP" \
    --location "$LOCATION" \
    --tags application=foot-traffic-analyzer environment="$ENVIRONMENT" \
    --output none
  success "Resource group ready"
}

deploy_infrastructure() {
  log "Deploying Azure infrastructure (this may take 20-30 minutes)..."
  log "Bicep will provision: OpenAI, Synapse, Functions, Container Apps, Key Vault, and more."

  log "  Primary region:              $LOCATION"
  log "  Cognitive Services region:   $COGNITIVE_SERVICES_LOCATION"

  DEPLOYMENT_OUTPUT=$(az deployment group create \
    --resource-group "$RESOURCE_GROUP" \
    --template-file "$SCRIPT_DIR/main.bicep" \
    --parameters \
      baseName="$BASE_NAME" \
      location="$LOCATION" \
      cognitiveServicesLocation="$COGNITIVE_SERVICES_LOCATION" \
      environment="$ENVIRONMENT" \
      videoFeedUrls="$VIDEO_FEED_URLS" \
    --output json)

  success "Infrastructure deployed"

  FUNCTIONS_CA_NAME=$(echo "$DEPLOYMENT_OUTPUT" | jq -r '.properties.outputs.functionsContainerAppName.value')
  CONTAINER_APP_URL=$(echo "$DEPLOYMENT_OUTPUT" | jq -r '.properties.outputs.containerAppUrl.value')
  SYNAPSE_WORKSPACE=$(echo "$DEPLOYMENT_OUTPUT" | jq -r '.properties.outputs.synapseWorkspaceName.value')
  SYNAPSE_SQL_POOL=$(echo "$DEPLOYMENT_OUTPUT"  | jq -r '.properties.outputs.synapseSqlPoolName.value')
  KEY_VAULT_NAME=$(echo "$DEPLOYMENT_OUTPUT"    | jq -r '.properties.outputs.keyVaultName.value')
  STORAGE_ACCOUNT=$(echo "$DEPLOYMENT_OUTPUT"   | jq -r '.properties.outputs.storageAccountName.value')
  ACR_LOGIN_SERVER=$(echo "$DEPLOYMENT_OUTPUT"  | jq -r '.properties.outputs.containerRegistryLoginServer.value')
  OPENAI_ENDPOINT=$(echo "$DEPLOYMENT_OUTPUT"   | jq -r '.properties.outputs.openAiEndpoint.value')

  log "Functions Container App: $FUNCTIONS_CA_NAME"
  log "Container App URL:       $CONTAINER_APP_URL"
  log "Synapse Workspace: $SYNAPSE_WORKSPACE"
  log "Key Vault:         $KEY_VAULT_NAME"
  log "OpenAI Endpoint:   $OPENAI_ENDPOINT"
}

init_synapse_schema() {
  log "Waiting for Synapse SQL pool to be ready..."
  # Retrieve password from Key Vault (Bicep stored it there)
  SYNAPSE_SQL_PASSWORD=$(az keyvault secret show \
    --vault-name "$KEY_VAULT_NAME" \
    --name "synapse-sql-password" \
    --query "value" -o tsv 2>/dev/null || echo "")

  if [[ -z "$SYNAPSE_SQL_PASSWORD" ]]; then
    warn "Could not retrieve Synapse password from Key Vault. Skipping schema init."
    return
  fi

  for i in {1..60}; do
    STATUS=$(az synapse sql pool show \
      --workspace-name "$SYNAPSE_WORKSPACE" \
      --name "$SYNAPSE_SQL_POOL" \
      --resource-group "$RESOURCE_GROUP" \
      --query "status" -o tsv 2>/dev/null || echo "Unknown")

    [[ "$STATUS" == "Online" ]] && { success "SQL pool is online"; break; }
    [[ $i -eq 60 ]] && { warn "SQL pool not ready. Run schema manually."; return; }
    (( i % 5 == 0 )) && log "SQL pool status: $STATUS ($i/60)"
    sleep 10
  done

  SYNAPSE_SERVER="${SYNAPSE_WORKSPACE}.sql.azuresynapse.net"
  if command -v sqlcmd >/dev/null 2>&1; then
    sqlcmd -S "$SYNAPSE_SERVER" -d "$SYNAPSE_SQL_POOL" -U "sqladmin" \
      -P "$SYNAPSE_SQL_PASSWORD" -i "$ROOT_DIR/database/schema.sql" -I -C \
      || warn "Schema may already exist — OK on re-deploy."
    success "Schema initialized"
  else
    warn "sqlcmd not found. Run schema manually:"
    warn "  sqlcmd -S $SYNAPSE_SERVER -d $SYNAPSE_SQL_POOL -U sqladmin -P <from-keyvault> -i database/schema.sql -I"
  fi
}

save_outputs() {
  cat > "$ROOT_DIR/.deployment-outputs.json" <<EOF
{
  "resourceGroup":             "$RESOURCE_GROUP",
  "functionsContainerAppName": "$FUNCTIONS_CA_NAME",
  "containerAppUrl":           "$CONTAINER_APP_URL",
  "synapseWorkspace": "$SYNAPSE_WORKSPACE",
  "synapseSqlPool":   "$SYNAPSE_SQL_POOL",
  "keyVaultName":     "$KEY_VAULT_NAME",
  "storageAccount":   "$STORAGE_ACCOUNT",
  "acrLoginServer":   "$ACR_LOGIN_SERVER",
  "openAiEndpoint":   "$OPENAI_ENDPOINT"
}
EOF
  success "Outputs saved to .deployment-outputs.json"
}

main() {
  check_prerequisites
  create_resource_group
  deploy_infrastructure
  init_synapse_schema
  save_outputs

  echo ""
  echo "╔══════════════════════════════════════════════════════╗"
  echo "║             Infrastructure Deployed!                  ║"
  echo "╚══════════════════════════════════════════════════════╝"
  echo ""
  success "Container App URL:  $CONTAINER_APP_URL"
  success "OpenAI Endpoint:    $OPENAI_ENDPOINT"
  log "Next: run './scripts/deploy-all.sh' to deploy application code."
  echo ""
}

main "$@"
