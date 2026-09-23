# AGENTS.md

## Project

Multi-agent "Career Assistant" — an Azure AI Foundry port of the MongoDB-partners
Bedrock AgentCore reference architecture. Four agents (orchestrator, job-match,
career-coach, company-intel) defined in `config/agents/*.agent.md` with skill
playbooks in `config/skills/`. FastAPI + SSE API (`src/app.py`), Streamlit UI (`ui/`),
MongoDB Atlas for data + long-term memory + traces, self-hosted mongodb-mcp-server
for agent tools, Bicep IaC in `deploy/`.

## Spec-driven development (mandatory)

This repo uses spec-driven development — see `specs/README.md`.

- **No spec, no code** for features or behavior changes. Before writing implementation
  code, check `specs/` for an approved spec covering the work; if none exists, draft one
  from `specs/_template/spec.md` and get human approval first.
- Implementation must trace to requirement IDs (`R1`, …) and satisfy every acceptance
  criterion (`AC1`, …) in the spec.
- If implementation deviates from an approved spec, update the spec in the same change.
- Bug fixes, behavior-preserving refactors, and docs-only changes are exempt.
- **Docs are part of the change.** Implementing a spec includes updating every doc its
  contracts touch (`README.md`, `docs/*.md`) — env vars, ports, model/collection names,
  commands, and diagrams must match the code (spec 007).

## Commands

- Tests (offline unit suite): `pytest -m unit`
- Lint/type: `ruff check .` / `mypy src`
- Live integration: `RUN_INTEGRATION=1 pytest -m integration`
- Post-deploy smoke: `python smoke/post_deploy_smoke.py`

## Conventions

- Python 3.10+, pymongo (no framework checkpointers), Azure OpenAI/Foundry only for LLMs.
- Agent behavior changes belong in `config/agents/*.agent.md` / `config/skills/`, not code.
- New env vars go in `src/settings.py` + `.env.sample` with defaults that keep
  `pytest -m unit` offline-safe.
