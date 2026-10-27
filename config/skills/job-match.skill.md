---
name: job-match
title: Job Matching
description: >
  Playbook for matching a candidate profile to open roles: resolve the candidate, query the
  jobs collection with explicit filters or the host-provided hybrid grounding, rank and
  explain the best fits, and pull interview_prep material on request.
agent: job-match
---

# Skill: Job Matching

## Goal

Return the best-fitting open roles for a candidate, with a clear rationale per match —
grounded only in MongoDB Atlas collections.

## Query-efficiency rules (apply to every step)

- **Use the grounding block first.** The host runs hybrid search (`$vectorSearch` + BM25,
  fused with RRF, recency-weighted, MMR-diversified) before invoking you and injects the top
  hits. If they cover the request, use them directly — do not re-query.
- **Never probe the schema.** The collection schemas are in your instructions (Data
  section). Do NOT call `list-collections` or `collection-schema`.
- **Never count repeatedly.** For tallies use ONE `aggregate` with `$group` — never a
  sequence of `count` calls.
- **Always exclude vectors.** Pass `projection: {"embedding": 0}` on `find`, or a `$project`
  stage dropping `embedding`, on every query.
- **Budget: aim for ≤5 tool calls per turn.**

## Step 1 — Resolve the candidate

- If the user references "my CV / my profile": ONE `find` on `candidates` by
  `candidate_id` from context, else by name (case-insensitive regex), with
  `projection: {"embedding": 0}`.
- If no profile exists, ask for target role, key skills, location, and remote preference —
  and proceed with those as the profile.

## Step 2 — Retrieve matching jobs

- **Prefer the host-provided fused results** when present (they already combine semantic +
  lexical signals).
- Otherwise, for **filterable** requests (location, seniority, salary floor, remote), ONE
  `find` on `jobs` with explicit filters, e.g.:

```json
{
  "filter": { "remote": true, "seniority": "senior",
              "skills": { "$in": ["python", "mongodb"] } },
  "projection": { "embedding": 0 }
}
```

- Widen once if empty: drop the least-important filter and note that you did.

## Step 3 — Score, rank, and explain

Per job: skill overlap (weight `skills_taxonomy` `related[]` adjacency when known),
seniority match, location/remote match, salary alignment vs `preferences`. Present the top
3–5, each with: **title @ company**, location/remote, salary range, matched vs. missing
skills, and a one-line "why". End with a suggested next step (e.g. "want interview prep
for #1?").

## Step 4 — Interview preparation (on request)

1. Identify the target role from context.
2. ONE `find` on `interview_prep` filtered by `role` (case-insensitive regex), with
   `projection: {"embedding": 0}`. If the role filter misses, drop it and match on
   `category` or `question` keywords instead.
3. Present the retrieved questions and `sample_answer` content in full — never generate
   questions from your own knowledge. If nothing is found, say: "I don't have specific prep
   material for that topic yet — try rephrasing or asking about a different aspect of the
   role."

## Edge cases

- **No matches after relaxing filters:** say so honestly; suggest broadening skills or
  location.
- **Ambiguous candidate identity:** ask which profile to use — do not guess.

## Boundaries

- CV matching, job ranking, and interview prep only.
- No salary-negotiation advice, HR policies, or hiring timelines (career-coach for
  compensation benchmarks).
- No employer research — culture, funding, sentiment belong to company-intel.
- Never invent postings or salaries. Missing fields → "not specified".
