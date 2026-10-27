# Spec 008: Agent config alignment with the Bedrock-AgentCore reference + tool-call efficiency

- **Status:** implemented
- **Author:** anuj.panchal
- **Created:** 2026-09-23
- **Approved:** 2026-09-23
- **Implemented in:** 2026-09-23 — config rewrite + classifier/trace code changes; hosted agents re-provisioned (`provision_agents_hosted.sh`), API image rebuilt (`career-api`, revision `mdbmaf-api--cfg2`)

## 1. Summary

Port the richer agent/skill content from the reference repo
(`mongodb-partners/Multi-Agent-Architectural-Guidance-Bedrock-AgentCore`, `config/agents/` +
`config/skills/`) into this project's `config/agents/*.agent.md` and `config/skills/*.skill.md`,
adapted to the Azure/self-hosted-MCP stack, and fix the tool-call inefficiency observed in QA
(a single company-intel turn made 22 MCP calls incl. 16 sequential `count`s and 2 schema
probes — 57 s, 14.7k tokens). Also improve trace observability of tool calls.

## 2. Context & current state

Observed 2026-09-23 on "Compare Helio Robotics and Nimbus Data as employers, and suggest
which of their open roles fit a backend engineer": routing sent the two-domain question to
`company-intel` only (job-match scored 0.56 vs margin 0.75); company-intel's config documents
only `companies`/`compensation_data`, so it probed (`list-collections`, 2× `collection-schema`)
and then issued 16 sequential `count` calls instead of one `aggregate $group`. Root causes:

1. Our agent descriptions/keywords are thin vs the reference's (which carry full routing
   signal and per-domain "Signals:" lists), so the heuristic classifier under-selects.
2. Agent/skill configs lack query-efficiency rules (projection, `$group` over N× `count`,
   tool-call budget) and cross-collection awareness (`jobs` for company-intel).
3. The `tool_call` trace event records only tool name, not arguments — loops aren't debuggable
   from `agent_traces` without the raw-events blob.

Current files: `config/agents/{orchestrator,job-match,career-coach,company-intel}.agent.md`,
`config/skills/{job-match,career-coach,company-intel}.skill.md`. Parser: `src/config_loader.py`
(`AgentConfig`: name/description/model/tools/collections/keywords/routable/skill;
`SkillConfig`: name/title/description/agent). Reference files fetched from GitHub `main`
(agents: 4 `.agent.md`; skills: `config/skills/<name>/SKILL.md` directories).

## 3. Requirements

- **R1** — Rewrite all four `config/agents/*.agent.md` bodies to the reference structure
  (Responsibilities / Communication Style / Workflow / Output Rules / Guardrails), keeping
  **our** frontmatter schema (no `handoffs`/`memory`/`maxTokens`/`temperature` keys — the
  loader doesn't parse them). Content must be adapted to this stack:
  - Tools are the self-hosted MCP tools (`find`, `aggregate`, `count`, `list-collections`,
    `collection-schema`) — not `mongodb_query`/`mongodb_vector_search`.
  - No `bedrock_kb_retrieve`, `read_skill_resource`, `activate_skill`, or
    `references/collections-schema.md` — those mechanisms don't exist here. Schemas are
    inlined in each agent's Data section (as today).
  - No server-side hybrid search tool: agents are told to prefer the host-provided fused
    grounding block (RRF/recency/MMR, `src/retrieval.py`) and to use `find`/`aggregate` for
    filterable queries.
  - MongoDB-only grounding remains enforced by `config_loader.GROUNDING_RULES` (unchanged).
- **R2** — Expand each agent's frontmatter `description` and `keywords` with the reference's
  routing signal (per-domain intents and keyword lists), so `classifier.py` heuristic scoring
  improves. Orchestrator `description`/`keywords` updated for the direct-reply vs route
  distinction.
- **R3** — Rewrite the three `config/skills/*.skill.md` to the reference's step-by-step form,
  adapted per R1's tool mapping, and add **query-efficiency rules** to every skill:
  (a) always pass `projection: {"embedding": 0}` (already in GROUNDING_RULES — reinforce per
  skill); (b) never issue repeated `count` calls — use one `aggregate` with `$group`;
  (c) never call `list-collections`/`collection-schema` — schemas are in the instructions;
  (d) budget: aim for ≤5 MCP calls per turn.
- **R4** — `company-intel.agent.md` gains a read-only `jobs` cross-reference (`title, company,
  seniority, remote, salary_min, salary_max`) so "which of their roles fit" questions are
  answerable without schema probing. Add `jobs` to its `collections` list.
- **R5** — `career-coach` config explicitly covers both reference workflows: career
  development (skill gap + roadmap over `career_paths`/`skills_taxonomy`) AND compensation
  (`compensation_data`), incl. ranges-not-single-numbers output rules.
- **R6** — `orchestrator.agent.md` ports the reference's routing doctrine adapted to this
  host-side classifier: multi-domain messages SHOULD fan out (e.g. "match jobs AND tell me
  about those companies" → job-match + company-intel), single-domain → single specialist,
  small talk/capability questions → direct reply.
- **R7** — Lower `MULTI_SELECT_MARGIN` in `src/classifier.py` from 0.75 to 0.55 so
  near-threshold second specialists co-select (the observed 0.56 case). Update the unit
  tests that pin the margin.
- **R8** — The `tool_call` trace event must include the tool-call **arguments** when the
  underlying specialist event provides them (`src/orchestrator.py` / `src/foundry_agents.py`),
  so loops are debuggable from `GET /sessions/{id}/traces` and the Sessions UI. No UI schema
  change required beyond tolerating an optional `args` field.
- **R9** — Docs sync (spec 007 rule): README and `docs/foundry-agents.md` stay accurate;
  no structure changes expected. `config/foundry_agents.lock.json` hash changes are expected
  on next `provision_agents sync` — README's D7 note already covers this.
- **R10** — Offline-safe: `pytest -m unit` must pass with no credentials; keyword/margin
  changes update existing unit tests only.

## 4. Non-goals

- No frontmatter schema changes (no `handoffs`, `memory`, `maxTokens`, `temperature`,
  `id` fields; `config_loader.py` parser untouched).
- No new tools on the MCP server (no server-side hybrid search tool).
- No CV-insert workflow from the reference job-match skill (`insertOne` into `candidates`) —
  the demo keeps `MONGODB_ALLOW_WRITE=false`.
- No re-seeding or schema changes to Atlas collections.
- No changes to agent model assignments (`gpt-5.6-sol` stays; reference's Claude models are
  AWS-specific).

## 5. Contracts affected

| Contract | Today | After |
|---|---|---|
| `config/agents/*.agent.md` (all 4) | Thin bodies + sparse keywords | Reference-structure bodies, rich descriptions/keywords (R1, R2, R4, R5, R6) |
| `config/skills/*.skill.md` (all 3) | Short playbooks | Step-by-step recipes + efficiency rules (R3) |
| `src/classifier.py` `MULTI_SELECT_MARGIN` | 0.75 | 0.55 (R7) |
| `tool_call` SSE/trace event | `{agent, tool}` | `{agent, tool, args?}` (R8) |
| `agent_traces` documents | tool names only | + tool args where available (R8) |
| Registered/hosted agents in Foundry | current instructions | re-provision needed (`sync --force` / re-run hosted provisioning) to take effect |

## 6. Open questions

| # | Question | Owner | Resolution |
|---|----------|-------|-----------|
| Q1 | Also port the reference's `demo-prompts.yaml` into docs? | human | defer — not requested |
| Q2 | After config changes, re-provision the 4 hosted agents on `anuj-westus2-project` (images unchanged; instructions are baked at registration)? | human | yes, re-run `provision_agents_hosted.sh` |

## 7. Acceptance criteria

- **AC1** (R1, R3–R6): `python -c "from src.config_loader import load_agents, load_skills; ..."`
  parses all configs; each agent body contains the five reference sections; no mention of
  `bedrock_kb_retrieve`, `read_skill_resource`, `mongodb_query`, `mongodb_vector_search`,
  or Claude models anywhere in `config/`.
- **AC2** (R3): every skill contains the efficiency rules (projection, `$group`-over-count,
  no schema probing, ≤5-call budget).
- **AC3** (R4): company-intel instructions name the `jobs` collection and its fields.
- **AC4** (R2, R7): unit tests for the classifier pass; a heuristic score of ~0.56 relative
  to 1.0 now co-selects the second specialist.
- **AC5** (R8): a mocked specialist turn in unit tests yields `tool_call` trace entries with
  `args`; `pytest -m unit` green.
- **AC6** (R1): replaying the observed prompt on the local app routes to
  company-intel + job-match (or company-intel answers without schema probing), with ≤8 MCP
  calls for the turn (manual check, documented in PR).

## 8. Test plan

- `tests/unit/`: update classifier margin tests (R7); add tool_call-args trace test (R8);
  config-loader parsing tests already cover frontmatter — extend to assert no AWS-only tool
  names appear in built instructions (AC1).
- `tests/integration/` (gated): one live company-comparison turn; assert tool-call count
  dropped vs the 22-call baseline.
- Smoke: unchanged.
