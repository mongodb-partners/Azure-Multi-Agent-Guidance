#!/usr/bin/env bash
# =============================================================================
# Build + register ALL FOUR agents as SEPARATE Foundry HOSTED agents (managed compute).
#
#   orchestrator, job-match, career-coach, company-intel  → one hosted container each.
#
# This is the "four separate hosted agents" architecture (not prompt agents, not the single
# whole-solution container). The orchestrator hosted agent invokes the specialist hosted agents
# by reference. Every agent answers strictly from MongoDB (grounding contract in config_loader).
#
# Usage:  ./deploy/provision_agents_hosted.sh <resource-group> [image-tag]
#
# Reads app config from ../.env (see .env.sample). Builds deploy/Dockerfile.agent in ACR once
# per agent (CAREER_AGENT build arg), registers each as a HostedAgentDefinition, polls to ACTIVE.
# The Foundry project + model deployment are REUSED. The self-hosted MongoDB MCP server must
# already be deployed (deploy.sh) and reachable at MONGODB_MCP_URL.
#
# Prereqs:  az login  •  pip install -r requirements-foundry.txt  •  an existing ACR
#           (reuses ACR_NAME from ../.env.live if deploy.sh created one; else pass ACR_NAME=…)
#           •  the 'Cognitive Services User' role on the Foundry account (agent data actions).
# =============================================================================
set -euo pipefail

RG="${1:?Usage: ./provision_agents_hosted.sh <resource-group> [image-tag]}"
TAG="${2:-${IMAGE_TAG:-latest}}"

# The four agents to host (names must match config/agents/*.agent.md).
AGENTS=(orchestrator job-match career-coach company-intel)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# ---- Load .env (runtime settings passed into each hosted container) ----
if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a; source "$ROOT_DIR/.env"; set +a
else
  echo "ERROR: $ROOT_DIR/.env not found (copy .env.sample and fill it in)." >&2
  exit 1
fi
: "${AZURE_AI_PROJECT_ENDPOINT:?set in .env}"
# The self-hosted MCP endpoint (deploy.sh writes it to .env.live) — reuse it if present.
if [[ -z "${MONGODB_MCP_URL:-}" && -f "$ROOT_DIR/.env.live" ]]; then
  MONGODB_MCP_URL="$(grep -E '^MONGODB_MCP_URL=' "$ROOT_DIR/.env.live" | cut -d= -f2- || true)"
  MCP_API_KEY_HEADER="${MCP_API_KEY_HEADER:-$(grep -E '^MCP_API_KEY_HEADER=' "$ROOT_DIR/.env.live" | cut -d= -f2- || true)}"
fi
: "${MONGODB_MCP_URL:?set in .env / .env.live (self-hosted MCP endpoint, e.g. https://<app>.azurecontainerapps.io/mcp)}"
# The hosted agents need the MCP API-key VALUE to authenticate to the MCP endpoint. Read it from
# Key Vault if not already in the environment (deploy.sh stored it as the 'mcp-api-key' secret).
if [[ -z "${MCP_API_KEY:-}" && -n "${AZURE_KEY_VAULT_URL:-}" ]]; then
  KV_HOST="$(echo "$AZURE_KEY_VAULT_URL" | sed -E 's#https?://([^./]+)\..*#\1#')"
  MCP_API_KEY="$(az keyvault secret show --vault-name "$KV_HOST" --name mcp-api-key --query value -o tsv 2>/dev/null || true)"
  export MCP_API_KEY
fi

# ---- Resolve ACR (reuse deploy.sh's if present) ----
if [[ -z "${ACR_NAME:-}" && -f "$ROOT_DIR/.env.live" ]]; then
  ACR_NAME="$(grep -E '^ACR_NAME=' "$ROOT_DIR/.env.live" | cut -d= -f2- || true)"
fi
: "${ACR_NAME:?No ACR_NAME. Run deploy/deploy.sh first, or pass ACR_NAME=<registry> in the env.}"

ACR_LOGIN_SERVER="$(az acr show -n "$ACR_NAME" --query loginServer -o tsv)"
ACR_ID="$(az acr show -n "$ACR_NAME" --query id -o tsv)"

# ---- Let the Foundry managed identity pull from the ACR (idempotent) --------------
# The hosted-agent image is pulled by the Foundry project sub-resource's managed identity. NOTE:
# the Foundry account/project may live in a DIFFERENT resource group than $RG, so we discover the
# account's real RG via `az cognitiveservices account list` rather than assuming $RG. We grant BOTH
# the project MI and the account MI (whichever the runtime uses) to be safe.
ACCOUNT_NAME="$(echo "$AZURE_AI_PROJECT_ENDPOINT" | sed -E 's#https://([^.]+)\..*#\1#')"
PROJECT_NAME="$(echo "$AZURE_AI_PROJECT_ENDPOINT" | sed -E 's#.*/projects/([^/]+)/?.*#\1#')"
SUB_ID="$(az account show --query id -o tsv)"
FOUNDRY_RG="$(az cognitiveservices account list \
  --query "[?name=='$ACCOUNT_NAME'].resourceGroup | [0]" -o tsv 2>/dev/null || true)"

MIS=()
if [[ -n "$FOUNDRY_RG" ]]; then
  PROJECT_RES_ID="/subscriptions/$SUB_ID/resourceGroups/$FOUNDRY_RG/providers/Microsoft.CognitiveServices/accounts/$ACCOUNT_NAME/projects/$PROJECT_NAME"
  PROJECT_MI="$(az resource show --ids "$PROJECT_RES_ID" --query "identity.principalId" -o tsv 2>/dev/null || true)"
  ACCOUNT_MI="$(az cognitiveservices account show -n "$ACCOUNT_NAME" -g "$FOUNDRY_RG" \
    --query "identity.principalId" -o tsv 2>/dev/null || true)"
  [[ -n "$PROJECT_MI" ]] && MIS+=("$PROJECT_MI")
  [[ -n "$ACCOUNT_MI" && "$ACCOUNT_MI" != "$PROJECT_MI" ]] && MIS+=("$ACCOUNT_MI")
fi

if [[ ${#MIS[@]} -eq 0 ]]; then
  echo "WARN: could not resolve the Foundry managed identity (account RG='$FOUNDRY_RG')." >&2
  echo "      If provisioning fails with ImageError, grant the Foundry project/account MI 'AcrPull'" >&2
  echo "      + 'Container Registry Repository Reader' on $ACR_NAME and enable ARM AAD auth." >&2
else
  echo "==> Enabling ARM AAD auth on ACR $ACR_NAME (required for MI pull)"
  az acr config authentication-as-arm update -r "$ACR_NAME" --status enabled -o none 2>/dev/null || true
  for MI in "${MIS[@]}"; do
    echo "==> Granting Foundry MI ($MI) pull rights on $ACR_NAME"
    for ROLE in "AcrPull" "Container Registry Repository Reader"; do
      az role assignment create --assignee-object-id "$MI" \
        --assignee-principal-type ServicePrincipal \
        --role "$ROLE" --scope "$ACR_ID" -o none 2>/dev/null \
        && echo "    granted: $ROLE" || echo "    (already present or skipped: $ROLE)"
    done
  done
  echo "    note: RBAC/ARM-auth changes can take a few minutes to propagate."
fi

# Note: the self-hosted MCP uses a static API-key header passed as container env (MCP_API_KEY), so
# the Foundry hosted agents no longer need Key Vault access (the old token broker was removed).

PY="${PYTHON:-python}"

# ---- Build + register each agent as its own hosted agent ----
for AGENT in "${AGENTS[@]}"; do
  IMAGE="$ACR_LOGIN_SERVER/career-agent-$AGENT:$TAG"
  echo ""
  echo "============================================================"
  echo " Agent: $AGENT"
  echo "============================================================"
  echo "==> Building image in ACR: $IMAGE (CAREER_AGENT=$AGENT)"
  az acr build -r "$ACR_NAME" -t "career-agent-$AGENT:$TAG" \
    --build-arg "CAREER_AGENT=$AGENT" \
    -f "$SCRIPT_DIR/Dockerfile.agent" "$ROOT_DIR"

  echo "==> Registering hosted agent '$AGENT'"
  # CAREER_AGENT is baked into the image at build time (ENV in Dockerfile.agent), so
  # src.agent_host serves the right agent WITHOUT passing a reserved AGENT_* runtime env
  # (Foundry rejects AGENT_*/FOUNDRY_* env vars on hosted agents). We also pass it explicitly
  # via CAREER_AGENT for clarity. Run from the project root (PYTHONPATH) so `-m` resolves.
  CAREER_AGENT="$AGENT" PYTHONPATH="$ROOT_DIR" "$PY" -m scripts.provision_hosted \
    --image "$IMAGE" --name "$AGENT"
done

echo ""
echo "============================================================"
echo " All four agents registered as Foundry hosted agents:"
echo "   ${AGENTS[*]}"
echo "   Invoke the orchestrator by reference; it routes to the specialist hosted agents."
echo "   Every agent answers strictly from MongoDB (MCP) — no general-knowledge fallback."
echo "============================================================"
