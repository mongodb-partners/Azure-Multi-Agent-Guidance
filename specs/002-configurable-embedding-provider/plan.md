# Plan 002: Configurable embedding provider

Technical plan for [spec.md](spec.md) (approved). Traces to `R#`/`AC#` in the spec.

## 1. Design

### Provider facade (`src/embeddings.py`, R1–R4)

```
embed_text(text, input_type=None)  ──►  dispatch on settings.embedding_provider
                                        ├─ foundry    → _embed_via_aoai (direct REST; the
                                        │               Function path is deleted)
                                        ├─ voyage     → _embed_via_voyage (direct REST,
                                        │               Bearer key, input_type passed through)
                                        └─ atlas_auto → RuntimeError (client-side embedding
                                            is forbidden — R3 guard against accidental use)
embed_dimensions()                   ──►  foundry: EMBEDDING_DIMENSIONS
                                          voyage:  VOYAGE_DIMENSIONS, else one probe embed
                                          atlas_auto: n/a (index carries no numDimensions)
AUTO_EMBED_TEXT_FIELD = "embed_text" ──►  materialized text field the auto-embed index
                                          targets (seeding writes it; retrieval queries it)
```

- `input_type`: Voyage supports `query`/`document` asymmetry — retrieval passes
  `"query"`, seeding passes `"document"`; other providers ignore it. Signature stays
  backward-compatible (`embed_text(text)`).
- Voyage dims are **not** hard-coded per model: explicit `VOYAGE_DIMENSIONS` wins, else a
  single probe embedding's length is used (robust against new models).

### Relevance floor (R7)

`settings.retrieval_min_score` becomes `float | None` (env unset → None). Resolution in
`src/retrieval.py::resolve_min_score()`:

```
RETRIEVAL_MIN_SCORE set?  → use it (explicit override)
else FLOOR_DEFAULTS[provider]:  foundry → 0.60 (calibrated, preserves today's behavior)
                                voyage / atlas_auto → None → no filtering + one-time warning
```

`orchestrator.py::_relevant` treats `None` as "no floor" (all hits offered). The
calibration script prints provider + resolved floor and handles the `None` case.

### Retrieval (`src/retrieval.py`, R3, R-Q5)

- Query-stage construction becomes pure builder functions (offline-testable):
  `_vector_stage(cfg, query_vec)` (today) and `_auto_embed_stage(cfg, query_text)`
  (`$vectorSearch` with `query` on path `embed_text`).
- `hybrid_search` under `atlas_auto`: no `embed_text` call, text-query vector stage,
  RRF + recency as usual, **MMR skipped** (source docs carry no embeddings), no
  embedding strip needed.
- Other providers: unchanged pipeline (vector + BM25 → RRF → recency → MMR).

### Seeding & indexes (`db_seeding/`, R3, R5)

- `seed_all.py`: under `atlas_auto`, vector collections get `record["embed_text"] =
  text_builder(doc)` and `$unset: {embedding: ""}` (strips stale provider vectors,
  R-Q4); otherwise embed as today with `input_type="document"`.
- `indexes.py`: provider-aware definitions —
  `atlas_auto` → `{"type": "autoEmbed", "modality": "text", "path": "embed_text",
  "model": ATLAS_EMBED_MODEL}`; else vector index with provider dims.
- **R5 guard:** before creating, compare the existing `vector_index` definition
  (type/path/numDimensions/model) against the desired one; mismatch → RuntimeError
  with "re-run `python -m db_seeding.seed_all --recreate-indexes`". That flag drops the
  existing vector index before recreating.

### Deploy (R6)

- `main.bicep`: delete the Function App + plan resources, `embeddingFunctionUrl`
  var/outputs, and the `EMBEDDING_FUNCTION_URL` app setting. Add params
  `embeddingProvider`, `voyageModel`, `atlasEmbedModel`; when provider=`voyage`,
  conditionally add the `voyage-api-key` Key Vault secret ref + env var to the API app.
- `deploy.sh`: remove FUNC outputs + `func publish` step + `.env.live` Function line;
  write `voyage-api-key` secret when `VOYAGE_API_KEY` is set; pass the new params.
- `monitoring.bicep`: remove the embedding-Function alert.
- `embedding-function/`: deleted.
- `destroy.sh`: unchanged (its legacy function delete tolerates absence).

## 2. Files touched

| File | Change | Traces |
|---|---|---|
| `src/embeddings.py` | provider facade, voyage client, Function removed | R1–R4 |
| `src/settings.py` | new env vars; `retrieval_min_score` → `float \| None` | R1, R7 |
| `src/retrieval.py` | stage builders, atlas_auto branch, `resolve_min_score` | R3, R7, R-Q5 |
| `src/orchestrator.py` | `_relevant` uses `resolve_min_score()` | R7 |
| `src/app.py` | health: + `embedding_provider`, `embedding_dimensions` | R9 |
| `db_seeding/{seed_all,indexes}.py` | provider-aware seed/index + R5 guard + `--recreate-indexes` | R3, R5 |
| `deploy/{main.bicep,deploy.sh,monitoring.bicep}` | Function removal + provider params | R6 |
| `embedding-function/` | deleted | R6 |
| `scripts/calibrate_retrieval_floor.py` | provider-aware floor display | R7 |
| `tests/unit/test_embeddings.py` | rewritten (dispatch, voyage mock, atlas_auto guard) | R8, AC1–AC3 |
| `tests/unit/test_retrieval_math.py` | + stage builders, floor resolution | R3, R7 |
| `.env.sample`, `README.md`, `docs/secrets.md`, `docs/HARDENING-PLAN.md` | config + docs | R6, R7 |

## 3. Test strategy

- Offline (AC7): dispatch via `Settings(**overrides)` injection (repo pattern); voyage
  HTTP mocked at `_voyage_request`; atlas_auto guard asserts `embed_text` raises and
  `hybrid_search` never calls it (spy); stage builders are pure-function tests.
- Live (gated): existing `test_retrieval_live.py` covers `foundry`; add provider-gated
  round-trips for `voyage` and `atlas_auto` in `tests/integration/test_embeddings_live.py`.
- AC1 guard: default config (`foundry`, no new env vars) must keep the existing suite
  green with zero behavioral change.

## 4. Risks

| Risk | Mitigation |
|---|---|
| Voyage dims guessed wrong → index/vector mismatch | Probe-embed for dims; R5 fail-fast guard on index mismatch |
| `atlas_auto` initial sync latency (hours on large sets) | Docs note; demo corpus is tiny |
| Floor disabled under uncalibrated providers grounds on noise | One-time startup warning + calibration doc; foundry default preserved |
| Users mid-flight with the Function | `EMBEDDING_FUNCTION_URL` removal is breaking but the fallback path was already direct AOAI — same net behavior |
