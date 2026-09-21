# Spec 002: Configurable embedding provider (Voyage / Atlas auto-embedding / Foundry)

- **Status:** implemented
- **Author:** Anuj Panchal
- **Created:** 2026-09-18
- **Approved:** 2026-09-18 (Q2/Q3/Q6 resolved — see §6)
- **Implemented in:** working tree 2026-09-18. Offline ACs verified (AC1–AC4, AC7–AC9
  via `pytest -m unit`, 98 passed; Bicep compiles via `az bicep build`). Live ACs
  pending: AC2/AC3 live round-trips (`RUN_INTEGRATION=1` per provider) and AC5/AC6
  (fresh deploy + per-provider calibration) — see plan.md.

## 1. Summary

Make the embedding model/provider user-configurable with three options:

1. **Voyage models** (Voyage AI by MongoDB) via a model API key — embeddings generated
   client-side through the Voyage API at seed and query time.
2. **Atlas auto-embedding** — Atlas generates and maintains embeddings natively on the
   vector index; the application stops computing embeddings entirely (raw text in,
   text query out).
3. **Foundry models** — any embedding deployment available in the Azure AI Foundry /
   Azure OpenAI model catalog (today's behavior, generalized beyond
   `text-embedding-3-large`).

The selected provider must flow consistently through seeding, index creation, runtime
retrieval, deployment, and cost/calibration tooling — today these are hard-wired to
Azure OpenAI `text-embedding-3-large` @ 3072 dims.

**Decided at review (2026-09-18):** the Azure Function App embedding indirection is
**removed entirely** — under every provider the app calls the embedding API directly
(Q3 = option B, extended to the `foundry` provider). The relevance floor ships as a
**per-provider default map** with `RETRIEVAL_MIN_SCORE` as an explicit override (Q6).
Option 1 is scoped to the **direct Voyage API with a user-supplied API key** (Q2).

## 2. Context & current state

Embedding is hard-wired to Azure OpenAI across four places:

| Concern | Today | File |
|---|---|---|
| Generation | `embed_text`/`embed_many`: POST to embedding Function if `EMBEDDING_FUNCTION_URL` set, else direct AOAI REST (`api-key` header). `dimensions` param sent only for `text-embedding-3-*` models. | `src/embeddings.py` |
| Seeding | Seeder computes `record["embedding"] = embed_text(...)` for the 4 vector collections (`jobs`, `companies`, `interview_prep`, `candidates`) | `db_seeding/seed_all.py:54-59` |
| Indexes | `vector_index` on `embedding`, `numDimensions` from `EMBEDDING_DIMENSIONS` (3072), cosine | `db_seeding/indexes.py:27,48` |
| Query-time | `hybrid_search` embeds the query (`retrieval.py:176`), runs `$vectorSearch` with `queryVector`, then **client-side MMR reads each doc's stored `embedding` field** for cosine diversity (`retrieval.py:147-156`) before stripping it (`:184`) | `src/retrieval.py` |
| Deploy | Embedding Function App + `EMBEDDING_*` app settings always provisioned | `deploy/main.bicep:28-31,180,187,253-255` |
| Calibration | `RETRIEVAL_MIN_SCORE=0.60` measured against `text-embedding-3-large` score distribution (`scripts/calibrate_retrieval_floor.py`) | `src/settings.py:103` |

Why this matters: provider choice changes **dimensionality, index definitions, the
query API shape, deploy topology, and the calibrated relevance floor** — so this is a
cross-cutting config concern, not just a new client function.

## 3. Requirements

- **R1** — A single config switch `EMBEDDING_PROVIDER` = `foundry` | `voyage` |
  `atlas_auto`, defaulting to `foundry` (today's behavior; zero-change upgrade path).
  `src/embeddings.py` becomes a provider-dispatched facade; `embed_text`/`embed_many`
  signatures are unchanged for callers.
- **R2** — `voyage` provider: embeddings via the Voyage API using a user-supplied key
  (`VOYAGE_API_KEY`) and model (`VOYAGE_MODEL`, e.g. `voyage-3-large`). The provider
  must expose the model's native dimensionality for index creation (per-model dims
  table or `VOYAGE_DIMENSIONS` override where the model supports reduced dims).
  Key material must go through Key Vault in deployed environments (never Bicep
  params in plaintext — same pattern as `azure-openai-api-key`).
- **R3** — `atlas_auto` provider: the application must **not** compute embeddings at
  seed or query time. Seeding writes raw documents (no `embedding` field computed
  client-side); index definitions declare the auto-embedding (Voyage) model; the
  retrieval query path uses `$vectorSearch` with a **`query` text field** instead of
  `queryVector` (verified against MongoDB Vector Search "Automated Embedding" docs,
  2026-09-18 — see §6 R-Q1). MMR behavior under this provider follows R-Q5.
- **R4** — `foundry` provider: current behavior generalized — any embedding deployment
  in the project's model catalog (not just `text-embedding-3-*`), selected by
  `EMBEDDING_MODEL` + `EMBEDDING_DIMENSIONS`. **Direct Azure OpenAI calls only** — the
  embedding Function App is removed from code and deployment (see §6 Q3).
- **R5** — **Dimensionality guard:** index creation and seeding must derive
  `numDimensions` from the active provider, and must fail fast with a clear error when
  the existing index dimensions don't match the configured provider (no silent
  re-index, no mixed-dimension vectors). Changing providers requires an explicit
  re-seed + re-index step (documented command, e.g. `python -m db_seeding.seed_all
  --recreate-indexes`).
- **R6** — **Deploy topology simplifies: the embedding Function App is removed from
  `main.bicep`, `deploy.sh`, and the repo (`embedding-function/`) for ALL providers.**
  - `foundry` → API app calls Azure OpenAI directly (key already in Key Vault).
  - `voyage` → `VOYAGE_API_KEY` stored in Key Vault and exposed to the API app as a
    Key Vault secret reference (only when provider = `voyage`).
  - `atlas_auto` → no embedding credentials or app settings on the API container.
- **R7** — **Relevance floor ships as a per-provider default map** calibrated on the
  demo corpus (`foundry: 0.60`; `voyage`/`atlas_auto`: `None` = floor disabled +
  startup warning until calibrated). `RETRIEVAL_MIN_SCORE`, when explicitly set,
  overrides the map. The calibration script must run under any provider, and docs must
  state that switching providers invalidates a previously tuned floor.
- **R8** — Unit tests run fully offline for all three providers (Voyage client mocked
  at the HTTP/SDK boundary; `atlas_auto` asserts no embedding calls are made and the
  query payload contains text, not vectors). Live verification is gated per provider
  (`RUN_INTEGRATION=1` + provider-specific env).
- **R9** — `GET /health` must report the active provider and effective dimensions
  (additive fields).

## 4. Non-goals

- Changing the hybrid-search fusion math (RRF k=60, recency decay, MMR λ=0.7) — only
  its *inputs* change per provider.
- Voyage **reranker** models, or any reranking stage — separate future spec.
- Running multiple providers/indexes simultaneously (one active provider per deployment).
- Migrating existing embedded data automatically (re-seed is explicit, per R5).
- Changing `GROUNDING_RULES`, citation verification, or agent configs.
- Atlas cluster provisioning or Atlas networking (still user-managed).

## 5. Contracts affected

| Contract | Today | After |
|---|---|---|
| `src/embeddings.py` | AOAI-only, Function-first fallback | Provider facade: `foundry` (direct AOAI only) / `voyage` (direct Voyage API) / `atlas_auto` (raises if called — R3 forbids client-side embedding; guards against accidental use). `EMBEDDING_FUNCTION_URL` support removed. |
| `src/settings.py`, `.env.sample` | `EMBEDDING_MODEL`, `EMBEDDING_DIMENSIONS`, `EMBEDDING_FUNCTION_URL` | + `EMBEDDING_PROVIDER`, `VOYAGE_API_KEY`, `VOYAGE_MODEL`, `VOYAGE_DIMENSIONS`, `ATLAS_EMBED_MODEL`; − `EMBEDDING_FUNCTION_URL`; `RETRIEVAL_MIN_SCORE` becomes an optional override over a per-provider default map |
| `src/orchestrator.py` (`_relevant`, line 89) | `>= settings.retrieval_min_score` (always a float) | Floor resolved via provider map; `None` = no filtering (+ warning) |
| `db_seeding/seed_all.py` | Always embeds at seed | `atlas_auto` → writes raw `embed_text` field, `$unset`s stale `embedding`; others → provider embed |
| `db_seeding/indexes.py` | Static vector index, dims from env | Provider-aware definitions: `autoEmbed` field type with `model` (path `embed_text`) for `atlas_auto`; vector index with provider dims otherwise. Index/provider mismatch fails fast; `--recreate-indexes` flow (R5) |
| `src/retrieval.py` | `queryVector` + client-side MMR over stored `embedding` | `atlas_auto` → `$vectorSearch` with `query` text on path `embed_text`, MMR skipped (R-Q5); others unchanged |
| `deploy/main.bicep`, `deploy/deploy.sh` | Function App always deployed, `func publish` step | Function App + plan + `func publish` removed; `EMBEDDING_PROVIDER`/model params added; `voyage-api-key` secret wired when provider=voyage (R6) |
| `embedding-function/` | Azure Function (`POST /embed`) | **Deleted** (R6) |
| `deploy/monitoring.bicep` | Embedding-Function failure alert | Alert removed |
| `scripts/calibrate_retrieval_floor.py` | AOAI-assumed, float floor | Provider-aware; handles `None` floor (R7) |
| `GET /health` | — | + `embedding_provider`, `embedding_dimensions` (R9) |
| `tests/unit/test_embeddings.py` | Function/AOAI paths | Rewritten: provider dispatch, voyage mocked HTTP, atlas_auto no-call guard |
| `tests/unit/test_retrieval_math.py` | fusion math | + query-stage builders (text vs vector payload), floor resolution |
| `tests/integration/` | retrieval live test | provider-gated live tests (R8) |
| Docs | README, `docs/secrets.md`, `docs/HARDENING-PLAN.md` | Provider selection guide + recalibration instructions; Function references removed |

## 6. Open questions

**Resolved during drafting** (verified against MongoDB Vector Search "Automated
Embedding" documentation, 2026-09-18):

| # | Question | Resolution |
|---|----------|-----------|
| R-Q1 | Auto-embedding query syntax | `$vectorSearch` accepts a **`query` field with raw text** in place of `queryVector` (optional `model` override). Models are **Voyage AI, hosted by MongoDB** (multi-tenant embedding service on Google Cloud, US region) — see data-residency note below. |
| R-Q4 | Backfill on index creation | **Automatic.** Creating an auto-embedding index triggers an initial sync that embeds all existing documents (batched, rate-limited, can take hours on large collections). No app re-seed needed *for embedding generation* — but a re-seed is still required to strip stale `embedding` fields left by a previous provider. |
| R-Q5 | MMR under `atlas_auto` | Source documents do **not** carry the generated embeddings (they live in the reserved internal `__mdb_internal_search` namespace, quantized — app code must not depend on it). Therefore **client-side MMR is skipped under `atlas_auto`** (RRF + recency ordering only), documented as a known ranking-quality trade-off. |

**Resolved at approval (2026-09-18):**

| # | Question | Resolution |
|---|----------|-----------|
| Q2 | Scope of option 1 | **Direct Voyage API** (`https://api.voyageai.com/v1/embeddings`) with a user-supplied `VOYAGE_API_KEY`. |
| Q3 | Embedding Function under `voyage` | **Option B: direct API-only — and extended: the Function is removed for the `foundry` provider too.** Key custody stays solved via Key Vault secret references on the API container. |
| Q6 | Relevance floor shape | **Per-provider default map** (`foundry: 0.60`; `voyage`/`atlas_auto`: uncalibrated → floor disabled + warning) with `RETRIEVAL_MIN_SCORE` as explicit override. |

**Data-residency / compliance note (R-Q1 consequence):** under `atlas_auto` (and
`voyage`), document text is sent to Voyage AI's embedding service (MongoDB-hosted,
multi-tenant, US region; not used for training, TLS in transit). For Azure-locked
deployments this may be a blocker — the `foundry` provider remains the
sovereign option. This must be documented in the provider selection guide.

## 7. Acceptance criteria

- **AC1** (R1, R4): With `EMBEDDING_PROVIDER=foundry` (default) and no other config
  change, the full unit suite passes and runtime behavior is byte-identical to today.
- **AC2** (R2): With `EMBEDDING_PROVIDER=voyage`, `embed_text` returns a vector of the
  Voyage model's dimensionality using only `VOYAGE_API_KEY`/`VOYAGE_MODEL` (unit:
  mocked HTTP; integration: live round-trip gated).
- **AC3** (R3): With `EMBEDDING_PROVIDER=atlas_auto`, seeding writes no `embedding`
  field and retrieval issues no client-side embedding call (unit: spy on the provider
  facade; integration: live hybrid search returns ranked hits).
- **AC4** (R5): Pointing the app at an Atlas index whose `numDimensions` mismatches the
  active provider fails fast at seed/startup with an actionable error message.
- **AC5** (R6): A fresh `deploy/deploy.sh` run with each provider produces the matching
  topology (**no Function App under any provider**; `voyage-api-key` present in Key
  Vault and referenced by the API app only when provider = `voyage`) and passes
  `smoke/post_deploy_smoke.py`.
- **AC6** (R7): `python -m scripts.calibrate_retrieval_floor` runs to completion under
  each configured provider and prints a provider-specific recommended floor.
- **AC7** (R8): `pytest -m unit` passes with network disabled for all providers.
- **AC8** (R9): `/health` includes `embedding_provider` and effective dimensions.

## 8. Test plan

- `tests/unit/test_embeddings.py` — extend: provider dispatch, voyage mocked client,
  `atlas_auto` guard (client-side embed must raise), dimension metadata per provider.
- `tests/unit/test_retrieval_math.py` — unchanged (fusion math is provider-agnostic);
  add provider-aware query-stage construction tests (text vs vector payload).
- `tests/unit/` new: index-definition builder per provider (dims + auto-embedding model
  declaration), dimension-mismatch guard (AC4).
- `tests/integration/test_embeddings_live.py` — provider-gated round-trips
  (`RUN_INTEGRATION=1` + `EMBEDDING_PROVIDER=<p>`), including a live `atlas_auto`
  hybrid search (AC3).
- `smoke/post_deploy_smoke.py` — unchanged; implicitly exercises the active provider
  via the live `/chat` turn (AC5).
