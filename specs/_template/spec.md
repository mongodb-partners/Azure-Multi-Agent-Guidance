# Spec NNN: <Title>

- **Status:** draft | approved | implemented | abandoned
- **Author:** <name>
- **Created:** <YYYY-MM-DD>
- **Approved:** <YYYY-MM-DD or —>
- **Implemented in:** <commit/PR links or —>

## 1. Summary

One paragraph: what changes and why, in plain language.

## 2. Context & current state

What exists today (with file paths), and the problem or gap that motivates this change.

## 3. Requirements

Numbered, testable, unambiguous. Use "must" / "must not". Each requirement gets an ID.

- **R1** …
- **R2** …

## 4. Non-goals

What this change explicitly does NOT do (prevents scope creep).

## 5. Contracts affected

Modules, function signatures, env vars, collections, API endpoints, UI pages touched.
Be concrete — this is the traceability surface.

| Contract | Today | After |
|---|---|---|
| | | |

## 6. Open questions

| # | Question | Owner | Resolution |
|---|----------|-------|-----------|
| Q1 | | | |

## 7. Acceptance criteria

Verifiable statements. Each maps to at least one requirement.

- **AC1** (R1): …
- **AC2** (R2): …

## 8. Test plan

Which test layers cover what (`tests/unit/`, `tests/integration/`, `tests/e2e/`, `smoke/`).
Unit tests must run offline (no live Azure/Atlas dependency).
