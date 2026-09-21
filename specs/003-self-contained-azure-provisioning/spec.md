# Spec 003: Self-contained Azure provisioning (Foundry project + models via Bicep)

- **Status:** abandoned (2026-09-18 — scrapped by owner before approval)
- **Author:** Anuj Panchal
- **Created:** 2026-09-18
- **Approved:** —
- **Implemented in:** —

## 1. Summary

Extend the deployment to **create-or-reuse** the Azure AI Foundry account, project,
and model deployments (chat + embedding) instead of requiring them as pre-provisioned
prerequisites. On the create path, the script derives `AZURE_AI_PROJECT_ENDPOINT`,
`AZURE_OPENAI_ENDPOINT`, and the API key from deployment outputs — the key goes
straight to Key Vault and never touches `.env`. The existing **BYO path** (user-supplied
values in `.env`) must keep working unchanged.

Goal: `./deploy/deploy.sh <rg> <location>` becomes a zero-Azure-prerequisite deploy.
MongoDB Atlas stays user-managed (out of ARM scope — non-goal).

## 2. Context & current state

Today the Foundry side is explicitly **reused, not deployed**:

- `deploy/deploy.sh:31-33` hard-fails without `AZURE_AI_PROJECT_ENDPOINT`,
  `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY` in `.env`.
- README "Prerequisites" lists a user-provisioned Foundry project + two model
  deployments (`gpt-5.6-sol`, `text-embedding-3-large` @ 3072).
- `main.bicep` consumes only the project **endpoint** (param) and the AOAI **key**
  (Key Vault secret pushed by deploy.sh).
- `destroy.sh` preserves the Foundry project/models (documented as
  "prerequisites, not stack-owned"); `--purge-foundry-agents` deletes only registered
  agents.

All required resources are ARM-deployable: `Microsoft.CognitiveServices/accounts`
(kind `AIServices`), `…/accounts/projects`, `…/accounts/deployments`, and the Agent
Service **capability hosts** (account + project scope) that the new Agent Service
requires. The account's keys are retrievable at deploy time via `listKeys()`.

## 3. Requirements

- **R1** — A new `deploy/foundry.bicep` module must provision, idempotently:
  an AIServices account, a Foundry project, Agent Service capability hosts
  (account + project scope, per current Agent Service requirements — see Q1), and two
  model deployments (chat: `AZURE_AI_MODEL_DEPLOYMENT_NAME` default `gpt-5.6-sol`;
  embedding: `EMBEDDING_MODEL` default `text-embedding-3-large` @ `EMBEDDING_DIMENSIONS`
  3072). Re-running against existing resources must be a no-op.
- **R2** — **Create-or-reuse semantics.** If `AZURE_AI_PROJECT_ENDPOINT` +
  `AZURE_OPENAI_ENDPOINT` are set in `.env`, the BYO path is taken and
  `foundry.bicep` is skipped entirely (today's behavior). If unset, deploy.sh deploys
  `foundry.bicep` and derives all three values from its outputs; the AOAI key is
  obtained via `listKeys()` and pushed to Key Vault exactly like today (never printed
  to logs; `@secure()` handling in Bicep outputs).
- **R3** — **Keyless auth must keep working on the create path:** the workload
  managed identity (from `keyvault.bicep`) must be granted the data-plane roles the
  app needs on the account (e.g. *Cognitive Services OpenAI User* / *Azure AI User*),
  so `DefaultAzureCredential` paths (`src/llm.py`, `src/foundry_agents.py`, spec-001
  conversations) work without the key.
- **R4** — **Clear failure on quota/region problems.** Model-deployment failures due to
  quota or regional availability must surface with an actionable message (which model,
  which region, where to request quota), not a raw ARM error. deploy.sh must fail fast
  before building images when the Foundry deploy fails.
- **R5** — `destroy.sh` keeps **preserve-by-default** semantics for the
  account/project/models; a new explicit flag (e.g. `--purge-foundry-infra`) deletes
  them. Default destroy output must state they were preserved.
- **R6** — Region consistency: the Foundry account deploys to the same `<location>`
  passed to deploy.sh; docs must note that Agent Service features (e.g. Memory preview,
  spec 001) and model availability constrain location choice.
- **R7** — Naming: account/project names derive from `PREFIX` + RG hash (globally
  unique pattern as used for ACR), and must be stable across re-runs of the same RG.

## 4. Non-goals

- MongoDB Atlas cluster provisioning (outside ARM; would need Atlas Admin API /
  Terraform — separate discussion).
- Requesting/managing model **quota** (a subscription-level, partially manual process).
- Deploying agents themselves (already covered by `scripts/provision_agents.py` and
  `deploy/provision_*hosted*.sh`).
- Foundry **hub**-based (legacy) projects, or multi-project setups.
- Migrating existing BYO deployments onto script-managed resources.

## 5. Contracts affected

| Contract | Today | After |
|---|---|---|
| `deploy/foundry.bicep` | — | New module: account + project + capability hosts + 2 model deployments + RBAC (R3); outputs: `projectEndpoint`, `aoaiEndpoint`, `@secure()` key |
| `deploy/deploy.sh` | Requires the 3 env vars (lines 31–33) | Conditional: BYO if set, else deploy foundry.bicep first, derive values, push key to KV; fail fast on Foundry failure (R4) |
| `deploy/main.bicep` | unchanged params | unchanged (consumes the same params either path) |
| `deploy/keyvault.bicep` | — | unchanged (deploy.sh may need the account key before KV exists on create path — ordering: foundry → keyvault → secret push, same as today's KV-first order) |
| `deploy/destroy.sh` | preserves Foundry | + `--purge-foundry-infra` flag; default prints "preserved" (R5) |
| `.env.sample` | 3 required vars | marked optional (BYO override); documented create-path behavior |
| `README.md` prerequisites | "must exist before running" | BYO vs auto-provision documented |
| `docs/secrets.md` | AOAI key from user `.env` | create path: key sourced from `listKeys()`, same KV storage |

## 6. Open questions

| # | Question | Owner | Resolution |
|---|----------|-------|-----------|
| Q1 | Exact ARM surface for Agent Service capability hosts in the current API version (`CognitiveServices/accounts/capabilityHosts` + project-level)? Verify against current docs/templates before plan — this is the highest-risk item. | | |
| Q2 | Model version pinning: deploy latest by name only, or pin `model.version` (reproducibility vs. availability drift)? | | |
| Q3 | SKU/scale defaults for model deployments (Global Standard vs Standard; TPM capacity)? Cost-sensitive default needed. | | |
| Q4 | Mixed mode: support "existing account, new project" (user has an AIServices account but wants the script to add project + deployments)? Or strictly all-BYO vs all-create? | | |
| Q5 | Should the create path also verify `listKeys()`-derived key works (a one-call canary) before proceeding to image builds? | | |

## 7. Acceptance criteria

- **AC1** (R1, R2): In a subscription with sufficient quota, `cp .env.sample .env`
  with **only** `MONGODB_URI` filled, then `./deploy/deploy.sh <rg> <location>`,
  completes: Foundry account/project/models created, secrets in Key Vault, smoke test
  passes.
- **AC2** (R2): With the 3 BYO vars set, deploy output shows the Foundry module was
  skipped and behavior is identical to today.
- **AC3** (R1, R7): Re-running deploy.sh against the same RG succeeds with no changes
  (idempotent; stable names).
- **AC4** (R3): Post-deploy, the deployed API answers a `/chat` turn using
  `DefaultAzureCredential` (no key) — verifiable by removing `AZURE_OPENAI_API_KEY`
  from the app env in a test deploy.
- **AC5** (R4): Deploying with an unavailable model name/region fails before image
  builds with a message naming the model, region, and quota-remediation link.
- **AC6** (R5): `destroy.sh` preserves Foundry resources by default and prints so;
  with `--purge-foundry-infra` they are deleted.
- **AC7** (R2): The AOAI key never appears in deploy logs or Bicep deployment outputs
  in plaintext (secure output → KV direct).

## 8. Test plan

- Bicep: `az bicep build` + `what-if` against a test RG (create + no-op re-run, AC3).
- Live: fresh-subscription deploy (AC1), BYO regression deploy (AC2), quota-failure
  drill (AC5, use a bogus model name), keyless drill (AC4).
- Unit/offline: not applicable (IaC) — deploy-script logic branches (BYO vs create)
  get a shell-level dry-run mode or are covered by the live matrix.
- `smoke/post_deploy_smoke.py`: unchanged — it is the AC1 gate.
