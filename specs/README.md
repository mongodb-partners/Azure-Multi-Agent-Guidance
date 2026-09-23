# Spec-Driven Development (SDD) in this repo

All non-trivial changes start as a **spec** before any code is written. Specs are the
source of truth for *what* and *why*; the code is the *how*.

## Workflow

```
specs/<NNN>-<slug>/
  spec.md    ← required: what & why (approved before coding starts)
  plan.md    ← optional: technical design, written after spec approval
  tasks.md   ← optional: ordered implementation checklist
```

1. **Specify** — copy `specs/_template/spec.md` into a new numbered folder
   (`specs/002-my-feature/spec.md`). Fill in context, requirements, non-goals,
   open questions, and acceptance criteria. Status starts as `draft`.
2. **Review** — a human reviews and marks the spec `approved`. Open questions
   must be resolved or explicitly deferred before approval.
3. **Plan (optional)** — for cross-cutting changes, add `plan.md` describing the
   technical design (modules touched, data flows, migration path).
4. **Implement** — code changes must trace back to requirement IDs (`R1`, `R2`, …).
   Every requirement maps to at least one acceptance criterion and one test.
5. **Close** — when acceptance criteria pass, set status to `implemented` and link
   the implementing commits/PRs. Per the repo's doc-maintenance rule (`AGENTS.md`,
   spec 007), the same change must update any docs whose contracts the spec touched
   (`README.md`, `docs/*.md`).

## Rules

- **No spec, no code** for features, behavior changes, or new integrations.
  Bug fixes, refactors with no behavior change, and docs-only changes are exempt.
- Specs are **implementation-agnostic about tools but concrete about contracts**:
  name the modules, function signatures, env vars, and collections affected.
- **Open questions are first-class.** An unanswered question blocks approval, not writing.
- Keep specs short. If a spec needs >2 pages, split the feature.
- When implementation deviates from an approved spec, update the spec in the same PR.
- Numbering: next available `NNN`, never reused, even if a spec is abandoned
  (mark it `abandoned` instead of deleting).

## Current specs

| # | Spec | Status |
|---|------|--------|
| 001 | [Foundry-managed short-term memory](001-foundry-short-term-memory/spec.md) | implemented |
| 002 | [Configurable embedding provider](002-configurable-embedding-provider/spec.md) | implemented |
| 003 | [Self-contained Azure provisioning](003-self-contained-azure-provisioning/spec.md) | abandoned |
| 004 | [Live reasoning streaming — direct replies and registered/inline parity](004-live-reasoning-streaming/spec.md) | implemented |
| 005 | [Developer trace — raw SSE event JSON in the chat UI](005-developer-trace-raw-events/spec.md) | abandoned |
| 006 | [QA & UAT plan](006-qa-uat-plan/spec.md) ([report](006-qa-uat-plan/qa-uat-report.md)) | approved |
| 007 | [Documentation refresh — accuracy, architecture & workflow diagrams](007-documentation-refresh/spec.md) | implemented |
