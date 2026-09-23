# Spec 007: Documentation refresh — accuracy, architecture & workflow diagrams

- **Status:** implemented
- **Author:** anuj.panchal
- **Created:** 2026-09-23
- **Approved:** 2026-09-23 (Q1: move port plan to `docs/`; Q2: replace ASCII with Mermaid)
- **Implemented in:** 2026-09-23, docs-only change (no code touched)

## 1. Summary

Bring all project documentation up to date with the implemented system (specs 001–006)
and make it diagram-first: every major doc gets accurate content plus Mermaid architecture
and workflow diagrams, one canonical end-to-end architecture diagram in the README, and an
index so readers can find the right doc. Docs currently drift from code (e.g. stale
deployed-state notes, no visual architecture, port-plan doc still reads as a proposal).

## 2. Context & current state

Existing docs and their gaps:

| Doc | State | Gap |
|---|---|---|
| `README.md` | Current, text-only | Architecture section has no diagram; repo layout table may lag reality |
| `docs/foundry-agents.md` | Mostly current | "Current deployed state" section goes stale on every redeploy; no diagram of the three consumption topologies |
| `docs/mcp-server.md` | Current | No sequence diagram of the agent → MCP → Atlas call path and auth header flow |
| `docs/memory.md` | Current | No diagram of the memory layers (chat_messages / agent_memory_facts / agent_traces / chat_sessions, Foundry vs mongo backend) |
| `docs/secrets.md` | Current | Rotation steps are command-accurate but lack a "which secret lives where" overview |
| `docs/HARDENING-PLAN.md` | Mixed | "Status (implemented in this pass)" unclear which items are done vs pending |
| `docs/service-map.md` | New (2026-09-23) | Accurate; not linked from README or other docs |
| `Azure-Foundry-MultiAgent-Port-PLAN.md` | Historical | Reads as a forward-looking plan although the port is built; no banner saying so |
| `AGENTS.md` | Current | Must stay in sync if docs structure changes |
| `specs/*/spec.md`, `specs/README.md` | Current | Cross-link targets; not part of content refresh except link fixes |

Root causes of drift: no rule tying doc updates to spec implementation, no diagrams
(text goes stale faster than pictures), no doc index, stale "deployed state" snapshots
embedded in how-to docs.

## 3. Requirements

- **R1** — Every factual claim in `README.md` and `docs/*.md` must be audited against the
  code/config it describes (env vars, ports, model names, collection names, commands) and
  corrected where stale. Audit must cover at minimum: `.env.sample`, `src/settings.py`,
  `deploy/*.bicep`, `deploy/*.sh`, `config/agents/`, `db_seeding/`.
- **R2** — `README.md` must gain one canonical Mermaid architecture diagram showing all
  components and their connections: Streamlit UI, FastAPI API, Azure OpenAI, Azure AI
  Foundry (prompt + hosted agents), mongodb-mcp-server, MongoDB Atlas (data + memory +
  vector search), Key Vault, App Insights, Container Apps/ACR.
- **R3** — Workflow (sequence/flow) Mermaid diagrams must be added for:
  (a) a chat turn end-to-end (UI → SSE `/chat` → orchestrator routing → retrieval →
  specialist → MCP tool call → synthesis → stream events);
  (b) agent provisioning (`provision_agents.py` sync, lock file, registered vs inline vs
  hosted paths);
  (c) deployment (`deploy.sh` → ACR build → Key Vault secrets → Bicep → smoke test).
  Diagram (a) goes in `README.md` or `docs/`; (b) in `docs/foundry-agents.md`; (c) in the
  deploy section of `README.md` or a deploy doc.
- **R4** — `docs/foundry-agents.md` must lose its point-in-time "Current deployed state"
  snapshot (or reduce it to a pointer to `az` commands that show live state). How-to docs
  must not embed environment-specific values that change per deploy.
- **R5** — `Azure-Foundry-MultiAgent-Port-PLAN.md` must get a top banner marking it as the
  historical port plan (implemented), or be moved under `docs/`. It must not read as a
  pending proposal.
- **R6** — `docs/HARDENING-PLAN.md` status section must be reconciled: each item marked
  done / open / deferred, consistent with the README security section (S1–S14).
- **R7** — A documentation index must exist: a "Documentation" section in `README.md`
  linking every doc in `docs/` with a one-line purpose, including `service-map.md`.
- **R8** — All cross-doc and intra-repo links must be valid relative links; all Mermaid
  blocks must render on GitHub (valid syntax, no unsupported features).
- **R9** — `AGENTS.md` and `specs/README.md` must be updated to state the doc-maintenance
  rule: implementing a spec includes updating the docs its contracts touch (R1 surface).
- **R10** — Content refresh must preserve existing style: concise Markdown, fenced
  commands, no emojis, no images hosted externally.

## 4. Non-goals

- No code, config, or behavior changes. Docs-only (exempt per SDD rules, but tracked here
  because it spans many files).
- No new doc tooling (no MkDocs/Docusaurus, no CI link-checker action — manual check only).
- No rewriting of `specs/*/spec.md` content (historical record), except broken-link fixes.
- No API reference generation from docstrings.
- No translation or restructure of the docs/ folder hierarchy beyond what R5/R7 require.

## 5. Contracts affected

| Contract | Today | After |
|---|---|---|
| `README.md` | Text-only architecture; no doc index | Canonical architecture diagram, chat-turn workflow, Documentation index |
| `docs/foundry-agents.md` | Stale "Current deployed state" snapshot | Provisioning workflow diagram; live-state via `az` commands only |
| `docs/mcp-server.md` | Prose-only call path | Sequence diagram: agent → MCP → Atlas incl. `X-MCP-Api-Key` |
| `docs/memory.md` | Prose-only layers | Memory-layer diagram (collections, backend toggle) |
| `docs/secrets.md` | Commands only | "Secret → location → consumer" overview table |
| `docs/HARDENING-PLAN.md` | Ambiguous status | Per-item done/open/deferred status |
| `Azure-Foundry-MultiAgent-Port-PLAN.md` | Reads as proposal | Banner marking it historical/implemented (or moved to `docs/`) |
| `AGENTS.md`, `specs/README.md` | No doc-maintenance rule | Rule added: spec implementation updates touched docs |

## 6. Open questions

| # | Question | Owner | Resolution |
|---|----------|-------|-----------|
| Q1 | Move `Azure-Foundry-MultiAgent-Port-PLAN.md` into `docs/` or banner in place at root? | human | Yes, move it|
| Q2 | Keep the ASCII diagram in `docs/service-map.md` or replace with Mermaid? | human | replace it with Mermaid one|

## 7. Acceptance criteria

- **AC1** (R1): Spot-check of every env var, port, model name, collection name, and shell
  command mentioned in `README.md`/`docs/*.md` matches the current code/config; a list of
  corrected inaccuracies is in the implementing PR description.
- **AC2** (R2, R8): `README.md` renders a Mermaid architecture diagram on GitHub naming all
  components from R2, and it matches `docs/service-map.md`.
- **AC3** (R3, R8): All three workflow diagrams exist in the specified docs and render.
- **AC4** (R4): `docs/foundry-agents.md` contains no environment-specific deployed-state
  snapshot.
- **AC5** (R5): The port-plan doc is unambiguously marked historical.
- **AC6** (R6): Every item in `docs/HARDENING-PLAN.md` has an explicit status consistent
  with README's security list.
- **AC7** (R7): README Documentation section links every file in `docs/`; every link
  resolves (no 404 on GitHub).
- **AC8** (R9): `AGENTS.md` contains the doc-maintenance rule and `specs/README.md`
  workflow step 5 references it.

## 8. Test plan

Docs-only change; no unit/integration tests. Verification is manual:

1. Render check: open each changed file in a Markdown preview (GitHub or VS Code) — all
   Mermaid blocks render, all links resolve.
2. Accuracy check: run the commands shown in quick-start/deploy/rotation sections against a
   dev environment (or dry-run where destructive), per AC1 spot-check list.
3. `smoke/post_deploy_smoke.py` unchanged and unaffected (no code touched).
