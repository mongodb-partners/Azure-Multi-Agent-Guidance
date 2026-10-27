---
name: career-coach
title: Career Coaching
description: >
  Playbook for skill-gap analysis and phased learning roadmaps (career_paths +
  skills_taxonomy), interview prep guidance, AND compensation benchmarking with grounded
  negotiation framing (compensation_data).
agent: career-coach
---

# Skill: Career Coaching

This skill covers two workflows. Pick the one(s) matching the request:

- **Career development** (Steps 1–3) — skill-gap analysis + phased roadmap (+ interview
  prep / resume feedback).
- **Compensation** (Steps 4–5) — salary/benefits benchmarks + a grounded negotiation range.

If the candidate asks about both, run both and present the answer in two clearly-labelled
sections.

## Query-efficiency rules (apply to every step)

- **Use the grounding block first** — the host may inject pre-retrieved hybrid results
  (vector + BM25, RRF-fused). Do not re-query what it already covers.
- **Never probe the schema.** Schemas are in your instructions. Do NOT call
  `list-collections` or `collection-schema`.
- **Never count repeatedly.** Use ONE `aggregate` with `$group` for tallies.
- **Always exclude vectors** — `projection: {"embedding": 0}` (or a `$project` stage) on
  every query.
- **Budget: aim for ≤5 tool calls per turn.**

## Step 1 — Resolve current skills and target role

1. Determine the **target role** from the conversation. If unclear, ask ONE clarifying
   question and stop.
2. Determine the candidate's **current skills**: from the message, or ONE `find` on
   `candidates` by `candidate_id`/name with `projection: {"embedding": 0}`.

## Step 2 — Retrieve the progression path

ONE `find` on `career_paths` matching the target, e.g.:

```json
{ "filter": { "$or": [ { "to_role": { "$regex": "<target>", "$options": "i" } },
                       { "from_role": { "$regex": "<current>", "$options": "i" } } ] } }
```

Pick the path whose `to_role` best matches. If none fits, say so and offer the closest
covered role — do not invent a progression path.

## Step 3 — Compute the gap and build the roadmap

1. Diff the candidate's skills against the path's `key_skills`: **covered** (exact or clear
   synonym), **partial** (related/foundational), **missing**.
2. For all missing/partial skills, ONE `find` on `skills_taxonomy` matching them at once:
   `{ "filter": { "skill": { "$in": ["<skill1>", "<skill2>"] } } }`.
3. Present: **Where you are** (covered set) → **The gap** (missing/partial, why each
   matters) → **Your roadmap** (the path's `milestones` in order, with `typical_years` as
   the timeline anchor). If a gap skill has no taxonomy entry, list it and note that no
   structured resource is on file — do not fabricate courses or links.
4. For interview prep, pull `interview_prep` by `role` (regex) and present retrieved
   questions verbatim — never generate questions yourself.

## Step 4 — Resolve the compensation query

1. Extract **role title**, **level** (`junior | mid | senior | staff` — infer from the role
   title or stated years if possible), and **location** (optional).
2. If the role is missing, ask ONE clarifying question and stop.

## Step 5 — Retrieve benchmarks and frame the ask

1. ONE `find` on `compensation_data` with the filters you actually resolved, e.g.:

```json
{ "filter": { "role": { "$regex": "<role>", "$options": "i" }, "level": "senior" } }
```

   No exact match → relax to role-only or level-only, and say the figure is a broader
   benchmark. Never extrapolate a number that is not in the data.
2. Present: **coverage line** (exact role, level, location the benchmark applies to) →
   **pay band** (`base_min`–`base_max`, plus `equity` when present) → **suggested ask
   range** (anchor near the top half of the band for a strong candidate) — always as a
   range, always labeled as guidance derived from the retrieved record.

## Edge cases

- **Candidate already meets all requirements:** say so; present the path's most advanced
  milestones as stretch goals.
- **Only a profile and no target role:** ask which role they are targeting before querying.
- **Location not covered:** present the broadest matching benchmark and say the
  location-specific figure is not on file.
- **Negotiating a specific offer:** provide the grounded range and framing; do not promise
  an outcome.

## Boundaries

- Skill gaps, transitions, roadmaps, interview prep, resume feedback, and compensation
  benchmarks only.
- No employer research (company-intel); no matching to open job listings (job-match).
- No legal, tax, or immigration advice; never promise a specific offer outcome.
- Never fabricate career paths, skills, courses, timelines, figures, ranges, or benefits.
  Compensation numbers are always ranges with their source basis.
