---
name: job-match
title: Job Matching
description: Playbook for ranking job postings against a candidate profile using hybrid search.
agent: job-match
---

# Skill: Job Matching

## Goal
Return the best-fitting open roles for a candidate, with a clear rationale per match.

## Steps
1. **Resolve the candidate.** Use `candidate_id` from context, else look up by name in `candidates`.
   If no profile exists, ask the user for target role, key skills, location, and remote preference.
2. **Retrieve candidates jobs.**
   - Semantic: `embed(candidate.summary or query)` → `$vectorSearch` on `jobs.embedding` (k≈50).
   - Lexical/filter: `$search` (BM25) on title/description + hard filters (location, remote, seniority, salary_min).
   - The host may hand you a fused (RRF) result set — use it directly when present.
3. **Score fit** per job: skill overlap (weighted by `skills_taxonomy` adjacency), seniority match,
   location/remote match, salary alignment.
4. **Rank & explain.** Top 3–5, each: matched skills, missing skills, and one-line "why".

## Guardrails
- Never invent postings or salaries. Missing fields → "not specified".
- If the pool is empty after filters, relax the least-important filter and note that you did.
