#!/usr/bin/env bash
# =============================================================================
# Tear down the Multi-Agent Career Assistant stack created by deploy.sh.
#
# Usage:
#   ./destroy.sh <resource-group> [flags]
#
# Flags:
#   --yes                 Skip the interactive confirmation.
#   --keep-rg             Delete only stack-created resources, keep the RG itself
#                         (and any pre-existing resources in it).
#   --purge-keyvault      After deletion, PURGE the soft-deleted Key Vault so the
#                         name is immediately reusable (irreversible).
#   --purge-foundry-agents  Also delete the registered Foundry prompt agents
#                           (uses scripts/provision_agents; requires creds).
#   --drop-atlas          Drop the SEEDED collections from the Atlas DB
#                         (irreversible; extra confirmation required).
#
# DELETED (stack-owned): the self-hosted MongoDB MCP Container App is created by deploy.sh, so it
# is torn down with the rest of the stack (full-RG delete, or via --keep-rg cleanup).
#
# ALWAYS PRESERVED (these are prerequisites, not stack-owned):
#   • The Azure AI Foundry project + model deployments
#   • MongoDB Atlas cluster + data (unless --drop-atlas)
# =============================================================================
set -euo pipefail

RG="${1:?Usage: ./destroy.sh <resource-group> [--yes] [--keep-rg] [--purge-keyvault] [--purge-foundry-agents] [--drop-atlas]}"
shift || true

ASSUME_YES=false
KEEP_RG=false
PURGE_KV=false
PURGE_FOUNDRY=false
DROP_ATLAS=false

for arg in "$@"; do
  case "$arg" in
    --yes) ASSUME_YES=true ;;
    --keep-rg) KEEP_RG=true ;;
    --purge-keyvault) PURGE_KV=true ;;
    --purge-foundry-agents) PURGE_FOUNDRY=true ;;
    --drop-atlas) DROP_ATLAS=true ;;
    *) echo "Unknown flag: $arg" >&2; exit 2 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PREFIX="${PREFIX:-mdbmaf}"

# ---- Load .env / .env.live for names (best-effort) ----
for f in "$ROOT_DIR/.env" "$ROOT_DIR/.env.live"; do
  if [[ -f "$f" ]]; then set -a; source "$f"; set +a; fi
done

if ! az group show -n "$RG" -o none 2>/dev/null; then
  echo "Resource group '$RG' not found — nothing to do."
  exit 0
fi

echo "============================================================"
echo " DESTROY plan for resource group: $RG"
echo "   Mode           : $( $KEEP_RG && echo 'keep RG, delete stack resources' || echo 'DELETE ENTIRE resource group' )"
echo "   Purge KeyVault : $PURGE_KV"
echo "   Foundry agents : $( $PURGE_FOUNDRY && echo 'DELETE' || echo 'preserve' )"
echo "   Atlas data     : $( $DROP_ATLAS && echo 'DROP seeded collections' || echo 'preserve' )"
echo "   Preserved      : Foundry project/models, Atlas cluster (MCP app is stack-owned → deleted)"
echo "============================================================"

if ! $ASSUME_YES; then
  read -r -p "Type the resource group name to confirm deletion: " CONFIRM
  [[ "$CONFIRM" == "$RG" ]] || { echo "Confirmation mismatch — aborting."; exit 1; }
fi

# ---- Optional: delete registered Foundry prompt agents ----
if $PURGE_FOUNDRY; then
  echo "==> Deleting registered Foundry prompt agents…"
  if python3 -c 'import scripts.provision_agents' 2>/dev/null; then
    python3 -m scripts.provision_agents delete-all \
      || echo "   (delete-all failed — check azure-ai-projects is installed and the 'Cognitive Services User' role is granted; or remove agents in the Foundry portal)"
  else
    echo "   (scripts.provision_agents not importable in this env — skipping; run: pip install -r requirements-foundry.txt)"
  fi
  rm -f "$ROOT_DIR/config/foundry_agents.lock.json" 2>/dev/null || true
fi

# ---- Optional: drop seeded Atlas collections ----
if $DROP_ATLAS; then
  echo ""
  echo "⚠️  --drop-atlas will DROP seeded collections from database '${MONGODB_DB:-multi-agent}'."
  if ! $ASSUME_YES; then
    read -r -p "Type 'DROP' to confirm dropping Atlas collections: " C2
    [[ "$C2" == "DROP" ]] || { echo "Atlas drop not confirmed — skipping."; DROP_ATLAS=false; }
  fi
  if $DROP_ATLAS; then
    echo "==> Dropping seeded collections…"
    python3 - <<'PY' || echo "   (Atlas drop failed/skipped — check MONGODB_URI)"
import os
from pymongo import MongoClient
uri = os.environ.get("MONGODB_URI", "")
db_name = os.environ.get("MONGODB_DB", "multi-agent")
seeded = ["jobs", "companies", "interview_prep", "candidates",
          "chat_messages", "agent_memory_facts"]
if not uri:
    raise SystemExit("MONGODB_URI not set")
db = MongoClient(uri)[db_name]
for c in seeded:
    db.drop_collection(c)
    print(f"   dropped {db_name}.{c}")
PY
  fi
fi

# ---- Azure resource teardown ----
if $KEEP_RG; then
  echo "==> Deleting stack-created resources (keeping RG '$RG')…"
  # Container Apps
  for app in "${PREFIX}-api" "${PREFIX}-ui"; do
    az containerapp delete -g "$RG" -n "$app" --yes -o none 2>/dev/null \
      && echo "   deleted containerapp $app" || true
  done
  # Container Apps environment
  az containerapp env delete -g "$RG" -n "${PREFIX}-cae" --yes -o none 2>/dev/null \
    && echo "   deleted env ${PREFIX}-cae" || true
  # (The embedding Function App was removed in specs/002 — nothing to delete.)
  # Log Analytics + App Insights
  az monitor app-insights component delete -g "$RG" --app "${PREFIX}-appi" -o none 2>/dev/null || true
  az monitor log-analytics workspace delete -g "$RG" -n "${PREFIX}-logs" --yes --force true -o none 2>/dev/null || true
  # ACR
  [[ -n "${ACR_NAME:-}" ]] && az acr delete -g "$RG" -n "$ACR_NAME" --yes -o none 2>/dev/null \
    && echo "   deleted ACR $ACR_NAME" || true
  # Key Vault (soft-delete)
  [[ -n "${KEY_VAULT_NAME:-}" ]] && az keyvault delete -g "$RG" -n "$KEY_VAULT_NAME" -o none 2>/dev/null \
    && echo "   deleted (soft) Key Vault $KEY_VAULT_NAME" || true
  # Managed identity
  az identity delete -g "$RG" -n "${PREFIX}-id" -o none 2>/dev/null || true
  echo "   stack resources removed (RG kept)."
else
  echo "==> Deleting entire resource group '$RG' (async)…"
  az group delete -n "$RG" --yes --no-wait
  echo "   deletion started (runs in background)."
fi

# ---- Optional: purge soft-deleted Key Vault ----
# Use a local, always-defined copy so `set -u` never trips even if KEY_VAULT_NAME is unset
# (e.g. no .env.live present). Fall back to discovering the vault by the stack's prefix.
KV_NAME_TO_PURGE="${KEY_VAULT_NAME:-}"
if $PURGE_KV; then
  if [[ -z "$KV_NAME_TO_PURGE" ]]; then
    KV_NAME_TO_PURGE="$(az keyvault list-deleted \
      --query "[?starts_with(name, '${PREFIX}-kv')].name | [0]" -o tsv 2>/dev/null || true)"
  fi
  if [[ -n "$KV_NAME_TO_PURGE" ]]; then
    echo "==> Purging soft-deleted Key Vault $KV_NAME_TO_PURGE…"
    az keyvault purge -n "$KV_NAME_TO_PURGE" -o none 2>/dev/null \
      && echo "   purged." \
      || echo "   (purge failed or not yet soft-deleted; retry after RG deletion completes)"
  else
    echo "==> --purge-keyvault requested but no Key Vault name known (set KEY_VAULT_NAME or wait"
    echo "    for the RG delete to soft-delete it, then run: az keyvault purge -n <name>)."
  fi
fi

# ---- Clean local generated file ----
rm -f "$ROOT_DIR/.env.live" 2>/dev/null && echo "==> Removed local .env.live" || true

echo ""
echo "============================================================"
echo " Teardown complete."
echo "   Preserved: Foundry project/models, Atlas cluster$( $DROP_ATLAS && echo ' (seeded collections dropped)' )."
echo "============================================================"
