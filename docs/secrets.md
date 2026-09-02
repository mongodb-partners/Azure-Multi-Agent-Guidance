# Secret management & rotation

This project uses these long-lived secrets:

| Secret | Env var | Where it's used |
|---|---|---|
| Azure OpenAI API key | `AZURE_OPENAI_API_KEY` | `src/llm.py`, `src/embeddings.py`, embedding Function |
| MongoDB Atlas connection string (contains DB password) | `MONGODB_URI` | `src/memory.py`, `src/retrieval.py`, seeding |
| Self-hosted MCP API key | `MCP_API_KEY` | `src/mcp_client.py` (API-key header to the MCP endpoint); the MCP container validates it via `MDB_MCP_HTTP_HEADERS` |

Optional/derived: `EMBEDDING_FUNCTION_URL`, `MONGODB_MCP_URL` are endpoints, not secrets. The MCP
API key is a **static, long-lived** value (no token to refresh) — `deploy.sh` generates it and stores
it in Key Vault (`mcp-api-key`). See **[docs/mcp-server.md](mcp-server.md)**.

---

## Current posture

- Secrets live in a **local, untracked** `.env` (excluded by `.gitignore`).
  They are **not** committed to git history — verified with
  `git ls-files | grep .env` (only `.env.sample`, which is placeholder-only).
- In the deployed stack they are stored in **Azure Key Vault** (`deploy/keyvault.bicep`) and read
  by the API Container App + embedding Function via a **user-assigned managed identity** — no
  secret values transit Bicep parameters or land in container env. `deploy.sh` pushes the values
  from your local `.env` into Key Vault (see "Key Vault model" below).

> ⚠️ The values currently present in your local `.env` are **live**. Even though
> they are not in git, treat any credential that has been on disk / shared as
> exposed and rotate it (steps below).

---

## Rotate the Azure OpenAI API key

Azure OpenAI resources have two keys (key1/key2) to allow zero-downtime rotation.

```bash
RES_GROUP=<your-openai-rg>
ACCOUNT=anuj-oai            # your Azure OpenAI resource name

# 1. Regenerate the SECONDARY key first, switch apps to it, then regenerate primary.
az cognitiveservices account keys regenerate \
  -g "$RES_GROUP" -n "$ACCOUNT" --key-name Key2

# 2. Read the new key
NEW_KEY=$(az cognitiveservices account keys list -g "$RES_GROUP" -n "$ACCOUNT" --query key2 -o tsv)

# 3a. Local: update .env
#     AZURE_OPENAI_API_KEY=$NEW_KEY   (edit the file)

# 3b. Deployed (Key Vault): push + restart the revision
az keyvault secret set --vault-name <kv-name> --name azure-openai-api-key --value "$NEW_KEY"
az containerapp revision restart -g <rg> -n mdbmaf-api

# 4. Once everything is on Key2, regenerate Key1 too so the old value is dead.
az cognitiveservices account keys regenerate -g "$RES_GROUP" -n "$ACCOUNT" --key-name Key1
```

---

## Rotate the MongoDB Atlas database password

```bash
# Atlas UI: Database Access → edit the user → Edit Password → Autogenerate → Update.
# Or via Atlas Admin API / atlas CLI:
atlas dbusers update <username> --password "$(openssl rand -base64 24)" --projectId <projId>

# Rebuild the connection string with the new password, then:
# Local: update MONGODB_URI in .env
# Deployed (Key Vault):
az keyvault secret set --vault-name <kv-name> --name mongodb-uri --value "$NEW_URI"
az containerapp revision restart -g <rg> -n mdbmaf-api
```

Tip: prefer a **least-privilege**, **read-only** Atlas user scoped to the `multi-agent`
database rather than an org/project-admin user. The MCP is read-only by default.

Note: the MongoDB connection string is also consumed by the **self-hosted MCP Container App**
(`MDB_MCP_CONNECTION_STRING`), so restart it too after rotating: `az containerapp revision restart
-g <rg> -n mdbmaf-mcp`.

---

## Rotate the self-hosted MCP API key

The MCP endpoint is gated by a static API-key header. Rotate it by generating a new key, updating
Key Vault, and restarting BOTH the MCP (which validates it) and the API (which sends it):

```bash
NEW_KEY="$(openssl rand -hex 24)"
az keyvault secret set --vault-name <kv-name> --name mcp-api-key --value "$NEW_KEY"
az containerapp revision restart -g <rg> -n mdbmaf-mcp    # MCP re-reads MDB_MCP_HTTP_HEADERS
az containerapp revision restart -g <rg> -n mdbmaf-api    # API re-reads MCP_API_KEY
# If you run Foundry hosted agents, re-provision them so they pick up the new key:
#   ./deploy/provision_agents_hosted.sh <rg>
```

---

## Key Vault model (implemented)

Implemented in `deploy/keyvault.bicep` + `deploy/main.bicep`:

- Vault: `<prefix>-kv-<hash>` with RBAC authorization and 7-day soft-delete.
- Secrets: `azure-openai-api-key`, `mongodb-uri`, `mcp-api-key`.
- The API + MCP Container Apps + embedding Function use a **user-assigned managed identity**
  (`<prefix>-id`) granted **`Key Vault Secrets User`** (read-only — least privilege; the previous
  read/write grant existed only for the removed Atlas MCP token broker). A user-assigned identity is
  used deliberately: it can be created and granted the role *before* the apps exist, avoiding the
  chicken-and-egg with Container App Key Vault references.
- Container App secrets are **Key Vault references** (`keyVaultUrl` + `identity`),
  the Function uses `@Microsoft.KeyVault(SecretUri=...)` app settings.
- `deploy/deploy.sh` pushes the secret values into Key Vault (`az keyvault secret
  set`) and passes only the **vault name** to Bicep — no secret values transit
  Bicep parameters or land in container env vars.

Rotation then becomes: `az keyvault secret set ...` + `az containerapp revision
restart ...` (no redeploy, no code change).

---

## Do / Don't

- ✅ Keep `.env` and `.env.live` untracked (already in `.gitignore`).
- ✅ Commit only `.env.sample` with placeholders.
- ✅ Use two-key rotation for Azure OpenAI (zero downtime).
- ✅ Scope the Atlas user to the single app database.
- ❌ Never paste real keys into `.env.sample`, Bicep defaults, or docs.
- ❌ Never echo secrets in CI logs or `deploy.sh` output.
