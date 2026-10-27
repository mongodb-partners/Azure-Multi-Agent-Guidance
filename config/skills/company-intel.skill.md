---
name: company-intel
title: Company Intelligence
description: >
  Playbook for grounded employer research and comparison: profile named companies from the
  companies collection, list their open roles from jobs, and present a balanced,
  attributed snapshot or comparison.
agent: company-intel
---

# Skill: Company Intelligence

## Goal

Give a grounded, balanced snapshot of an employer (or compare several), sourced only from
MongoDB Atlas collections.

## Query-efficiency rules (apply to every step)

- **Use the grounding block first.** The host may inject pre-retrieved hybrid results
  (vector + BM25, fused with RRF). If the company you need is already there, do not re-query.
- **Never probe the schema.** The collection schemas are in your instructions (Data section).
  Do NOT call `list-collections` or `collection-schema`.
- **Never count repeatedly.** For per-company/per-level tallies use ONE `aggregate` with a
  `$group` stage — never a sequence of `count` calls.
- **Always exclude vectors.** Pass `projection: {"embedding": 0}` on `find`, or a `$project`
  stage dropping `embedding`, on every query.
- **Budget: aim for ≤5 tool calls per turn.** One well-formed `find` or `aggregate` beats
  five exploratory ones.

## Step 1 — Resolve the company target

1. Identify the company name(s) from the message or recent conversation (e.g. a company
   named in an earlier job-match result). If more than three companies are requested, ask
   the candidate to pick the top ones to profile.
2. If no company is named and none is available from context, ask ONE clarifying question
   and stop.

## Step 2 — Retrieve the company record(s)

For each company, ONE `find` on `companies` with a case-insensitive name match:

```json
{
  "filter": { "name": { "$regex": "^<company>$", "$options": "i" } },
  "projection": { "embedding": 0 }
}
```

If the exact name misses, retry with a substring regex (`{ "$regex": "<company>",
"$options": "i" }`) once. If still nothing, say the company is not on file — do not guess.

## Step 3 — Roles at the company (when asked)

For "which of their roles fit / are open", ONE `find` on `jobs`:

```json
{
  "filter": { "company": { "$regex": "^<company>$", "$options": "i" } },
  "projection": { "embedding": 0, "description": 0 }
}
```

Summarize title, seniority, location/remote, and salary range per role. For cross-company
tallies ("how many roles at each"), use ONE `aggregate`:
`[{ "$group": { "_id": "$company", "n": { "$sum": 1 } } }]` — not repeated `count` calls.

## Step 4 — Present a balanced profile

Per company:

1. **Headline** — one line: name, industry, funding stage, size.
2. **Culture & practices** — from `culture` and `tech_stack` (documented items only).
3. **Stage & size** — from `funding_stage`, `total_funding`, `founded`, `size`.
4. **What people say** — from `glassdoor_rating` and `summary`, labeled as sentiment.
   Include both positive signals and watch-outs.
5. **Fit note** — one balanced sentence on who this environment tends to suit.
6. **Roles** (if asked) — the Step 3 list, with a one-line fit note per role.

For multiple companies, present a compact comparison table (culture · size · funding ·
tech stack · rating), then parallel fit notes.

## Edge cases

- **Company not in the dataset:** say so honestly; offer to profile a company that is.
- **Thin record:** present what exists and mark missing fields as "not in the dataset" —
  never backfill from general knowledge.
- **Ambiguous name (multiple matches):** ask the candidate to confirm which one (by industry
  or HQ) before profiling.

## Boundaries

- Company research — culture, practices, stage/size, sentiment, roles at the company — only.
- No salary-negotiation advice or learning plans (career-coach). You may quote
  `compensation_data` bands when asked what a company pays.
- No cross-company job matching (job-match).
- Never speculate about a company's future, layoffs, or stock performance.
- Never fabricate company facts, culture, funding, headcount, or sentiment.
