---
name: company-intel
title: Company Intelligence
description: Playbook for grounded employer research and comparison using Atlas vector search.
agent: company-intel
---

# Skill: Company Intelligence

## Goal
Give a grounded, balanced snapshot of an employer (or compare several), sourced only from Atlas.

## Steps
1. **Named company.** `mongodb_find` on `companies` by case-insensitive `name`. Return culture,
   mission, size, funding, tech stack, and `glassdoor_rating`.
2. **Thematic search.** For "companies like…" or attribute-based asks, `embed(query)` →
   `$vectorSearch` on `companies.embedding`. Use host-provided fused results when present.
3. **Compensation.** If pay comes up, join `compensation_data` on `company` + `role`/`level`.
4. **Comparison.** For 2+ companies, present a compact table: culture · size · funding · tech · rating.

## Guardrails
- Balanced: state both attractive points and things to watch.
- Never speculate past the data. Missing field → "unknown / not in dataset".
