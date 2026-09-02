#!/usr/bin/env bash
# =============================================================================
# Deploy the Multi-Agent Career Assistant supporting infra to Azure.
#
# Usage:  ./deploy.sh <resource-group> [location]
# Reads app config from ../.env (see .env.sample). The Foundry project + model
# deployments are REUSED; the MongoDB MCP server is SELF-HOSTED (deployed here as
# a Container App via mcp.bicep).
#
# Steps: RG -> ACR -> build+push API/UI images -> Bicep (env, function, apps)
#        -> deploy embedding function code -> write ../.env.live
# =============================================================================
set -euo pipefail

RG="${1:?Usage: ./deploy.sh <resource-group> [location]}"
LOCATION="${2:-eastus}"
PREFIX="${PREFIX:-mdbmaf}"
TAG="${IMAGE_TAG:-latest}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# ---- Load .env ----
if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a; source "$ROOT_DIR/.env"; set +a
else
  echo "ERROR: $ROOT_DIR/.env not found (copy .env.sample and fill it in)." >&2
  exit 1
fi

: "${AZURE_AI_PROJECT_ENDPOINT:?set in .env}"
: "${AZURE_OPENAI_ENDPOINT:?set in .env}"
: "${AZURE_OPENAI_API_KEY:?set in .env}"
: "${MONGODB_URI:?set in .env}"

# The self-hosted MCP is hardened with a shared API-key header. Generate one if not provided so the
# endpoint is never left unauthenticated by default. The MCP Container App validates this exact key.
MCP_API_KEY="${MCP_API_KEY:-$(openssl rand -hex 24)}"
MCP_API_KEY_HEADER="${MCP_API_KEY_HEADER:-X-MCP-Api-Key}"

echo "==> Resource group: $RG ($LOCATION)"
az group create -n "$RG" -l "$LOCATION" -o none

# ---- ACR (deterministic name from RG id) ----
ACR_NAME="$(echo "${PREFIX}acr$(az group show -n "$RG" --query id -o tsv | md5 2>/dev/null | cut -c1-10 || echo $RANDOM)" | tr -cd 'a-z0-9' | cut -c1-40)"
echo "==> ACR: $ACR_NAME"
az acr create -g "$RG" -n "$ACR_NAME" --sku Basic --admin-enabled true -o none 2>/dev/null || true

echo "==> Building images in ACR (remote build)…"
az acr build -r "$ACR_NAME" -t "career-api:$TAG" -f "$SCRIPT_DIR/Dockerfile.api" "$ROOT_DIR"
az acr build -r "$ACR_NAME" -t "career-ui:$TAG"  -f "$SCRIPT_DIR/Dockerfile.ui"  "$ROOT_DIR"

# ---- Key Vault + workload identity (secrets never transit Bicep params) ----
# Grant the deploying user rights to write secrets so `az keyvault secret set` works.
DEPLOYER_OID="$(az ad signed-in-user show --query id -o tsv 2>/dev/null || echo '')"
echo "==> Provisioning Key Vault + managed identity…"
if ! KV_OUTPUTS="$(az deployment group create \
  -g "$RG" \
  -f "$SCRIPT_DIR/keyvault.bicep" \
  -p prefix="$PREFIX" \
     adminObjectIds="$( [[ -n "$DEPLOYER_OID" ]] && echo "[\"$DEPLOYER_OID\"]" || echo '[]' )" \
  --query properties.outputs -o json)"; then
  echo "ERROR: Key Vault Bicep deployment failed. Inspect with:" >&2
  echo "       az deployment operation group list -g \"$RG\" -n keyvault \\" >&2
  echo "         --query \"[?properties.provisioningState=='Failed'].properties.statusMessage\" -o json" >&2
  exit 1
fi
if [[ -z "${KV_OUTPUTS// }" || "$KV_OUTPUTS" == "null" ]]; then
  echo "ERROR: Key Vault deployment returned no outputs (deployment likely did not complete)." >&2
  exit 1
fi

KV_NAME="$(echo "$KV_OUTPUTS"    | python3 -c 'import sys,json;print(json.load(sys.stdin)["keyVaultName"]["value"])')"
UAMI_ID="$(echo "$KV_OUTPUTS"    | python3 -c 'import sys,json;print(json.load(sys.stdin)["uamiId"]["value"])')"
echo "    Key Vault: $KV_NAME"

# Push secret VALUES into Key Vault (from .env). RBAC propagation can lag a few seconds.
echo "==> Writing secrets to Key Vault…"
for attempt in 1 2 3 4 5; do
  if az keyvault secret set --vault-name "$KV_NAME" --name azure-openai-api-key --value "$AZURE_OPENAI_API_KEY" -o none 2>/dev/null \
     && az keyvault secret set --vault-name "$KV_NAME" --name mongodb-uri --value "$MONGODB_URI" -o none 2>/dev/null \
     && az keyvault secret set --vault-name "$KV_NAME" --name mcp-api-key --value "$MCP_API_KEY" -o none 2>/dev/null; then
    echo "    secrets written."
    break
  fi
  echo "    RBAC not ready yet (attempt $attempt) — retrying in 10s…"
  sleep 10
done

# ---- Bicep deploy ----
echo "==> Deploying infrastructure (Bicep)…"
# NOTE: capture the az exit status explicitly. Inside `OUTPUTS="$(az ...)"` the command
# substitution masks az's non-zero exit from `set -e`, so a failed deploy would otherwise
# fall through with an empty $OUTPUTS and later trip `set -u` on an unset var (misleading).
if ! OUTPUTS="$(az deployment group create \
  -g "$RG" \
  -f "$SCRIPT_DIR/main.bicep" \
  -p prefix="$PREFIX" \
     acrName="$ACR_NAME" \
     imageTag="$TAG" \
     projectEndpoint="$AZURE_AI_PROJECT_ENDPOINT" \
     chatDeployment="${AZURE_AI_MODEL_DEPLOYMENT_NAME:-gpt-5.6-sol}" \
     embeddingModel="${EMBEDDING_MODEL:-text-embedding-3-large}" \
     embeddingDimensions="${EMBEDDING_DIMENSIONS:-3072}" \
     azureOpenAIEndpoint="$AZURE_OPENAI_ENDPOINT" \
     keyVaultName="$KV_NAME" \
     workloadIdentityId="$UAMI_ID" \
     mongoDb="${MONGODB_DB:-multi-agent}" \
     mcpApiKeyHeader="$MCP_API_KEY_HEADER" \
     mcpApiKey="$MCP_API_KEY" \
     mcpReadOnly="${MCP_READ_ONLY:-true}" \
     specialistMode="${SPECIALIST_MODE:-registered-or-inline}" \
  --query properties.outputs -o json)"; then
  echo "ERROR: Bicep 'main' deployment failed. Inspect the operation details with:" >&2
  echo "       az deployment operation group list -g \"$RG\" -n main \\" >&2
  echo "         --query \"[?properties.provisioningState=='Failed'].properties.statusMessage\" -o json" >&2
  echo "  Tip: if the Container Apps environment (${PREFIX}-cae) was still 'Updating', wait for it" >&2
  echo "       to reach 'Succeeded' and re-run ./deploy.sh — the script is idempotent." >&2
  exit 1
fi

if [[ -z "${OUTPUTS// }" || "$OUTPUTS" == "null" ]]; then
  echo "ERROR: Bicep 'main' deployment returned no outputs (deployment likely did not complete)." >&2
  echo "       Check: az deployment group show -g \"$RG\" -n main --query properties.provisioningState -o tsv" >&2
  exit 1
fi

# Extract required outputs; fail clearly if any expected key is missing.
_extract() {  # $1 = json key
  echo "$OUTPUTS" | python3 -c "import sys,json;print(json.load(sys.stdin)['$1']['value'])" 2>/dev/null || {
    echo "ERROR: deployment output '$1' missing — cannot continue." >&2
    exit 1
  }
}
API_URL="$(_extract apiUrl)"
UI_URL="$(_extract uiUrl)"
FUNC_NAME="$(_extract functionAppName)"
FUNC_URL="$(_extract embeddingFunctionUrl)"
MCP_URL="$(_extract mcpUrl)"

# ---- Deploy embedding function code ----
echo "==> Publishing embedding function code to $FUNC_NAME…"
if command -v func >/dev/null 2>&1; then
  ( cd "$ROOT_DIR/embedding-function" && func azure functionapp publish "$FUNC_NAME" --python )
else
  echo "   (Azure Functions Core Tools 'func' not found — publish embedding-function/ manually.)"
fi

# ---- Emit .env.live ----
cat > "$ROOT_DIR/.env.live" <<EOF
# Generated by deploy.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)
API_URL=$API_URL
UI_URL=$UI_URL
EMBEDDING_FUNCTION_URL=$FUNC_URL
MONGODB_MCP_URL=$MCP_URL
MCP_API_KEY_HEADER=$MCP_API_KEY_HEADER
ACR_NAME=$ACR_NAME
KEY_VAULT_NAME=$KV_NAME
EOF

echo ""
echo "============================================================"
echo " Deployment complete"
echo "   API : $API_URL   (try $API_URL/health, /agents)"
echo "   UI  : $UI_URL"
echo "   Fn  : $FUNC_URL"
echo "   -> endpoints written to .env.live"
echo "============================================================"

# ---- Post-deploy smoke test (non-fatal warning if it can't run) ----
if [[ "${SKIP_SMOKE:-0}" != "1" ]]; then
  echo "==> Running post-deploy smoke test…"
  if command -v python3 >/dev/null 2>&1; then
    API_URL="$API_URL" python3 "$ROOT_DIR/smoke/post_deploy_smoke.py" \
      || echo "   ⚠️  Smoke test FAILED — investigate before using the deployment."
  else
    echo "   (python3 not found — skipping smoke test)"
  fi
fi
echo "Reminder: seed Atlas first if you haven't:  python -m db_seeding.seed_all"
