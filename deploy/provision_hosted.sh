#!/usr/bin/env bash
# =============================================================================
# Build + register the WHOLE-SOLUTION agent as a Foundry hosted agent.
#
# Usage:  ./deploy/provision_hosted.sh <resource-group> [image-tag]
#
# Reads app config from ../.env (see .env.sample). Builds deploy/Dockerfile.hosted in ACR,
# then registers it as a HostedAgentDefinition on the Foundry project and polls until ACTIVE.
# The Foundry project + model deployment and the MongoDB MCP server are REUSED, not created.
#
# Prereqs:  az login  •  pip install -r requirements-foundry.txt  •  an existing ACR
#           (reuses ACR_NAME from ../.env.live if deploy.sh created one; else pass ACR_NAME=…).
# =============================================================================
set -euo pipefail

RG="${1:?Usage: ./provision_hosted.sh <resource-group> [image-tag]}"
TAG="${2:-${IMAGE_TAG:-latest}}"
AGENT_NAME="${HOSTED_AGENT_NAME:-career-hosted-solution}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# ---- Load .env (runtime settings passed into the hosted container) ----
if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a; source "$ROOT_DIR/.env"; set +a
else
  echo "ERROR: $ROOT_DIR/.env not found (copy .env.sample and fill it in)." >&2
  exit 1
fi
: "${AZURE_AI_PROJECT_ENDPOINT:?set in .env}"
: "${MONGODB_MCP_URL:?set in .env}"

# ---- Resolve ACR (reuse deploy.sh's if present) ----
if [[ -z "${ACR_NAME:-}" && -f "$ROOT_DIR/.env.live" ]]; then
  ACR_NAME="$(grep -E '^ACR_NAME=' "$ROOT_DIR/.env.live" | cut -d= -f2- || true)"
fi
: "${ACR_NAME:?No ACR_NAME. Run deploy/deploy.sh first, or pass ACR_NAME=<registry> in the env.}"

ACR_LOGIN_SERVER="$(az acr show -n "$ACR_NAME" --query loginServer -o tsv)"
ACR_ID="$(az acr show -n "$ACR_NAME" --query id -o tsv)"
IMAGE="$ACR_LOGIN_SERVER/career-hosted:$TAG"

# ---- Let the Foundry PROJECT managed identity pull from the ACR --------------
# The hosted-agent image is pulled by the Foundry *project* sub-resource's system-assigned
# managed identity (NOT the account identity, NOT any per-agent identity). Without this it
# fails provisioning with a generic "ImageError: Container registry authentication failed".
# Two things are required: (1) ARM-audience AAD auth enabled on the ACR, and (2) the project MI
# granted pull rights. Both steps are idempotent, so re-running the deploy is safe.
ACCOUNT_NAME="$(echo "$AZURE_AI_PROJECT_ENDPOINT" | sed -E 's#https://([^.]+)\..*#\1#')"
PROJECT_NAME="$(echo "$AZURE_AI_PROJECT_ENDPOINT" | sed -E 's#.*/projects/([^/]+)/?.*#\1#')"
SUB_ID="$(az account show --query id -o tsv)"
PROJECT_RES_ID="/subscriptions/$SUB_ID/resourceGroups/$RG/providers/Microsoft.CognitiveServices/accounts/$ACCOUNT_NAME/projects/$PROJECT_NAME"
PROJECT_MI="$(az resource show --ids "$PROJECT_RES_ID" --query "identity.principalId" -o tsv 2>/dev/null || true)"

if [[ -z "$PROJECT_MI" ]]; then
  echo "WARN: could not resolve the Foundry project managed identity for $PROJECT_RES_ID." >&2
  echo "      If provisioning fails with ImageError, grant the project MI 'Container Registry" >&2
  echo "      Repository Reader' + 'AcrPull' on $ACR_NAME and enable ARM AAD auth on the ACR." >&2
else
  echo "==> Enabling ARM AAD auth on ACR $ACR_NAME (required for MI pull)"
  az acr config authentication-as-arm update -r "$ACR_NAME" --status enabled -o none 2>/dev/null || true
  echo "==> Granting Foundry project MI ($PROJECT_MI) pull rights on $ACR_NAME"
  for ROLE in "AcrPull" "Container Registry Repository Reader"; do
    az role assignment create --assignee-object-id "$PROJECT_MI" \
      --assignee-principal-type ServicePrincipal \
      --role "$ROLE" --scope "$ACR_ID" -o none 2>/dev/null \
      && echo "    granted: $ROLE" || echo "    (already present or skipped: $ROLE)"
  done
  echo "    note: RBAC/ARM-auth changes can take a few minutes to propagate."
fi

echo "==> Building hosted image in ACR: $IMAGE"
az acr build -r "$ACR_NAME" -t "career-hosted:$TAG" -f "$SCRIPT_DIR/Dockerfile.hosted" "$ROOT_DIR"

# ---- Register the hosted agent + poll to ACTIVE ----
PY="${PYTHON:-python}"
echo "==> Registering hosted agent '$AGENT_NAME' on $AZURE_AI_PROJECT_ENDPOINT"
"$PY" -m scripts.provision_hosted --image "$IMAGE" --name "$AGENT_NAME"

echo ""
echo "============================================================"
echo " Hosted whole-solution agent registered: $AGENT_NAME"
echo "   image: $IMAGE"
echo "   Callers invoke it by reference (Responses API) — see docs/foundry-agents.md."
echo "============================================================"
