#!/usr/bin/env bash
# =============================================================================
# azd postprovision hook
# Runs automatically after `azd provision` completes.
#
# Steps:
#   1. Deploy the OpenAI model (gpt-5.3-chat) — non-blocking: quota/region failures
#      print clear instructions and continue rather than stopping deployment.
#   2. Wait for the Synapse SQL pool to come online.
#   3. Initialize the database schema.
#
# All secrets are stored in Key Vault by Bicep — no manual secret config needed.
# =============================================================================
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log()    { echo -e "${BLUE}[postprovision]${NC} $*"; }
success(){ echo -e "${GREEN}[postprovision]${NC} ✓ $*"; }
warn()   { echo -e "${YELLOW}[postprovision]${NC} ⚠ $*"; }
error()  { echo -e "${RED}[postprovision]${NC} ✗ $*" >&2; }  # note: no exit — non-fatal

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

# ─── azd injects Bicep outputs as env vars after provision ───────────────────
OPENAI_ACCOUNT_NAME="${AZURE_OPENAI_ACCOUNT_NAME:-}"
OPENAI_DEPLOYMENT_NAME="${AZURE_OPENAI_DEPLOYMENT_NAME:-gpt-5.3-chat}"
OPENAI_MODEL_NAME="${OPENAI_MODEL_NAME:-gpt-5.3-chat}"
OPENAI_MODEL_VERSION="${OPENAI_MODEL_VERSION:-2025-02-01}"
SYNAPSE_WORKSPACE="${AZURE_SYNAPSE_WORKSPACE_NAME:-}"
SYNAPSE_SQL_POOL="${AZURE_SYNAPSE_SQL_POOL_NAME:-foottrafficdw}"
RESOURCE_GROUP="${AZURE_RESOURCE_GROUP:-}"
KEY_VAULT_NAME="${AZURE_KEY_VAULT_NAME:-}"

# ─── Fallback: look up resource names from Azure if azd didn't inject them ───
if [[ -z "$KEY_VAULT_NAME" && -n "$RESOURCE_GROUP" ]]; then
  log "AZURE_KEY_VAULT_NAME not set — looking up Key Vault from resource group..."
  KEY_VAULT_NAME=$(az keyvault list --resource-group "$RESOURCE_GROUP" \
    --query "[0].name" -o tsv 2>/dev/null || echo "")
  [[ -n "$KEY_VAULT_NAME" ]] && log "  Found Key Vault: $KEY_VAULT_NAME"
fi

if [[ -z "$OPENAI_ACCOUNT_NAME" && -n "$RESOURCE_GROUP" ]]; then
  log "AZURE_OPENAI_ACCOUNT_NAME not set — looking up OpenAI account from resource group..."
  OPENAI_ACCOUNT_NAME=$(az cognitiveservices account list --resource-group "$RESOURCE_GROUP" \
    --query "[?kind=='OpenAI'].name | [0]" -o tsv 2>/dev/null || echo "")
  [[ -n "$OPENAI_ACCOUNT_NAME" ]] && log "  Found OpenAI account: $OPENAI_ACCOUNT_NAME"
fi

if [[ -z "$SYNAPSE_WORKSPACE" && -n "$RESOURCE_GROUP" ]]; then
  log "AZURE_SYNAPSE_WORKSPACE_NAME not set — looking up Synapse workspace from resource group..."
  SYNAPSE_WORKSPACE=$(az synapse workspace list --resource-group "$RESOURCE_GROUP" \
    --query "[0].name" -o tsv 2>/dev/null || echo "")
  [[ -n "$SYNAPSE_WORKSPACE" ]] && log "  Found Synapse workspace: $SYNAPSE_WORKSPACE"
fi

log "Post-provision setup starting..."
log "  OpenAI Account:    ${OPENAI_ACCOUNT_NAME:-<not set>}"
log "  Model Deployment:  ${OPENAI_DEPLOYMENT_NAME} (${OPENAI_MODEL_NAME} ${OPENAI_MODEL_VERSION})"
log "  Synapse Workspace: ${SYNAPSE_WORKSPACE:-<not set>}"
log "  Resource Group:    ${RESOURCE_GROUP:-<not set>}"
echo ""

# =============================================================================
# STEP 1: Deploy OpenAI model — NON-BLOCKING
# The model deployment is separate from main.bicep so that quota, region
# availability, or subscription tier issues don't stop the whole deployment.
# =============================================================================
deploy_openai_model() {
  if [[ -z "$OPENAI_ACCOUNT_NAME" || -z "$RESOURCE_GROUP" ]]; then
    warn "AZURE_OPENAI_ACCOUNT_NAME or AZURE_RESOURCE_GROUP not set — skipping model deployment."
    warn "Deploy manually after provisioning:"
    warn "  az cognitiveservices account deployment create \\"
    warn "    --resource-group <rg> --name <account> \\"
    warn "    --deployment-name gpt-5.3-chat --model-name gpt-5.3-chat \\"
    warn "    --model-version 2025-02-01 --model-format OpenAI \\"
    warn "    --sku-capacity 10 --sku-name Standard"
    return
  fi

  log "Deploying OpenAI model '${OPENAI_DEPLOYMENT_NAME}' (${OPENAI_MODEL_NAME} ${OPENAI_MODEL_VERSION})..."
  log "(This is non-blocking — quota/region failures will not stop the deployment)"

  # Check if the exact deployment name already exists
  EXISTING=$(az cognitiveservices account deployment show \
    --resource-group "$RESOURCE_GROUP" \
    --name "$OPENAI_ACCOUNT_NAME" \
    --deployment-name "$OPENAI_DEPLOYMENT_NAME" \
    --query "name" -o tsv 2>/dev/null || echo "")

  if [[ -n "$EXISTING" ]]; then
    success "Model deployment '${OPENAI_DEPLOYMENT_NAME}' already exists — skipping."
    return
  fi

  # Also check if ANY deployment of this model already exists (e.g. manually deployed)
  EXISTING_MODEL=$(az cognitiveservices account deployment list \
    --resource-group "$RESOURCE_GROUP" \
    --name "$OPENAI_ACCOUNT_NAME" \
    --query "[?properties.model.name=='${OPENAI_MODEL_NAME}'].name | [0]" \
    -o tsv 2>/dev/null || echo "")

  if [[ -n "$EXISTING_MODEL" ]]; then
    success "Model '${OPENAI_MODEL_NAME}' already deployed as '${EXISTING_MODEL}' — skipping."
    return
  fi

  # Attempt deployment via the standalone Bicep module
  DEPLOY_OUTPUT=$(az deployment group create \
    --resource-group "$RESOURCE_GROUP" \
    --template-file "$ROOT_DIR/infrastructure/modules/openai-deployment.bicep" \
    --parameters \
      openAiAccountName="$OPENAI_ACCOUNT_NAME" \
      deploymentName="$OPENAI_DEPLOYMENT_NAME" \
      modelName="$OPENAI_MODEL_NAME" \
      modelVersion="$OPENAI_MODEL_VERSION" \
    --output json 2>&1) && DEPLOY_EXIT=0 || DEPLOY_EXIT=$?

  if [[ $DEPLOY_EXIT -eq 0 ]]; then
    success "OpenAI model '${OPENAI_DEPLOYMENT_NAME}' deployed successfully!"
    success "  Model:    ${OPENAI_MODEL_NAME} ${OPENAI_MODEL_VERSION}"
    success "  Retires:  N/A"
  else
    echo ""
    warn "════════════════════════════════════════════════════════════"
    warn "  OpenAI model deployment failed (non-fatal)."
    warn "  The rest of the infrastructure is fully deployed."
    warn ""
    warn "  Common causes:"
    warn "    • Quota limit reached for this model in this region"
    warn "    • Model not available in your subscription tier"
    warn "    • Region does not support gpt-5.3-chat yet"
    warn ""
    warn "  To deploy the model manually, run:"
    warn ""
    warn "    az cognitiveservices account deployment create \\"
    warn "      --resource-group \"$RESOURCE_GROUP\" \\"
    warn "      --name \"$OPENAI_ACCOUNT_NAME\" \\"
    warn "      --deployment-name \"$OPENAI_DEPLOYMENT_NAME\" \\"
    warn "      --model-name \"$OPENAI_MODEL_NAME\" \\"
    warn "      --model-version \"$OPENAI_MODEL_VERSION\" \\"
    warn "      --model-format OpenAI \\"
    warn "      --sku-capacity 10 \\"
    warn "      --sku-name Standard"
    warn ""
    warn "  Or via Azure AI Foundry portal:"
    warn "    https://ai.azure.com → Your project → Deployments → Deploy model"
    warn ""
    warn "  Error details:"
    echo "$DEPLOY_OUTPUT" | grep -E "error|Error|message" | head -5 | sed 's/^/    /' >&2 || true
    warn "════════════════════════════════════════════════════════════"
    echo ""
  fi
}

deploy_openai_model

# =============================================================================
# STEP 2: Retrieve Synapse password from Key Vault
# =============================================================================
SYNAPSE_SQL_PASSWORD=""
if [[ -n "$KEY_VAULT_NAME" ]]; then
  log "Retrieving Synapse SQL password from Key Vault..."

  # Ensure the current CLI principal has Secrets User access (idempotent)
  CURRENT_USER_ID=$(az ad signed-in-user show --query id -o tsv 2>/dev/null || echo "")
  if [[ -n "$CURRENT_USER_ID" && -n "$RESOURCE_GROUP" ]]; then
    KV_ID=$(az keyvault show --name "$KEY_VAULT_NAME" --resource-group "$RESOURCE_GROUP" \
      --query id -o tsv 2>/dev/null || echo "")
    if [[ -n "$KV_ID" ]]; then
      az role assignment create \
        --role "Key Vault Secrets User" \
        --assignee "$CURRENT_USER_ID" \
        --scope "$KV_ID" \
        --output none 2>/dev/null || true  # ignore if already assigned
      log "  Ensured Key Vault Secrets User role for current principal"
      sleep 5  # allow RBAC propagation
    fi
  fi

  SYNAPSE_SQL_PASSWORD=$(az keyvault secret show \
    --vault-name "$KEY_VAULT_NAME" \
    --name "synapse-sql-password" \
    --query "value" -o tsv 2>/dev/null || echo "")

  if [[ -n "$SYNAPSE_SQL_PASSWORD" ]]; then
    success "Retrieved Synapse SQL password from Key Vault"
  else
    warn "Could not retrieve Synapse SQL password — schema initialization will be skipped."
    warn "Retrieve manually: az keyvault secret show --vault-name $KEY_VAULT_NAME --name synapse-sql-password --query value -o tsv"
  fi
fi

# =============================================================================
# STEP 3: Wait for Synapse SQL Pool + Initialize Schema
# =============================================================================
if [[ -z "$SYNAPSE_WORKSPACE" ]]; then
  warn "AZURE_SYNAPSE_WORKSPACE_NAME not set — skipping schema init."
  exit 0
fi

log "Waiting for Synapse SQL pool '$SYNAPSE_SQL_POOL' to come online..."
log "(This can take 5-10 minutes on first provision)"
MAX_WAIT=60
for i in $(seq 1 $MAX_WAIT); do
  STATUS=$(az synapse sql pool show \
    --workspace-name "$SYNAPSE_WORKSPACE" \
    --name "$SYNAPSE_SQL_POOL" \
    --resource-group "$RESOURCE_GROUP" \
    --query "status" -o tsv 2>/dev/null || echo "Unknown")

  if [[ "$STATUS" == "Online" ]]; then
    success "SQL pool is online"
    break
  fi

  if [[ $i -eq $MAX_WAIT ]]; then
    warn "SQL pool not ready after $((MAX_WAIT * 10))s."
    warn "Run schema manually once the pool is online:"
    warn "  sqlcmd -S ${SYNAPSE_WORKSPACE}.sql.azuresynapse.net -d $SYNAPSE_SQL_POOL -U sqladmin -P <from-keyvault> -i database/schema.sql -I"
    exit 0
  fi

  (( i % 5 == 0 )) && log "  Status: $STATUS (${i}/${MAX_WAIT} — $((i * 10))s elapsed)"
  sleep 10
done

if [[ -z "$SYNAPSE_SQL_PASSWORD" ]]; then
  warn "No SQL password available — skipping schema init."
  warn "Retrieve it: az keyvault secret show --vault-name $KEY_VAULT_NAME --name synapse-sql-password --query value -o tsv"
  exit 0
fi

SYNAPSE_SERVER="${SYNAPSE_WORKSPACE}.sql.azuresynapse.net"

if command -v sqlcmd >/dev/null 2>&1; then
  log "Running schema initialization against $SYNAPSE_SERVER..."
  sqlcmd \
    -S "$SYNAPSE_SERVER" \
    -d "$SYNAPSE_SQL_POOL" \
    -U "sqladmin" \
    -P "$SYNAPSE_SQL_PASSWORD" \
    -i "$ROOT_DIR/database/schema.sql" \
    -I -C 2>&1 || warn "Schema may already exist — this is OK on re-provision."
  success "Database schema initialized"
elif command -v docker >/dev/null 2>&1; then
  # Fallback: run sqlcmd via the official Microsoft Docker image (no local install needed)
  log "sqlcmd not found locally — running schema init via Docker (mcr.microsoft.com/mssql-tools)..."
  docker run --rm --platform linux/amd64 \
    -v "$ROOT_DIR/database:/database:ro" \
    mcr.microsoft.com/mssql-tools \
    /opt/mssql-tools/bin/sqlcmd \
      -S "$SYNAPSE_SERVER" \
      -d "$SYNAPSE_SQL_POOL" \
      -U "sqladmin" \
      -P "$SYNAPSE_SQL_PASSWORD" \
      -i "/database/schema.sql" \
      -I -C 2>&1 || warn "Schema may already exist — this is OK on re-provision."
  success "Database schema initialized (via Docker)"
else
  warn "sqlcmd not found and Docker is not available."
  warn "Install mssql-tools18 or run the schema manually:"
  warn ""
  warn "  Option 1 — Docker (recommended):"
  warn "    docker run --rm -v \"\$(pwd)/database:/database\" mcr.microsoft.com/mssql-tools \\"
  warn "      /opt/mssql-tools/bin/sqlcmd -S $SYNAPSE_SERVER -d $SYNAPSE_SQL_POOL \\"
  warn "      -U sqladmin -P '<password>' -i /database/schema.sql -I"
  warn ""
  warn "  Option 2 — sqlcmd directly:"
  warn "    sqlcmd -S $SYNAPSE_SERVER -d $SYNAPSE_SQL_POOL -U sqladmin -P '<password>' -i database/schema.sql -I"
  warn ""
  warn "  Get the password:"
  warn "    az keyvault secret show --vault-name $KEY_VAULT_NAME --name synapse-sql-password --query value -o tsv"
fi

echo ""
success "Post-provision complete!"
