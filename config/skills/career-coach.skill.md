---
name: career-coach
title: Career Coaching
description: Playbook for skill-gap analysis, career-path planning, and interview prep.
agent: career-coach
---

# Skill: Career Coaching

## Goal
Give the user a concrete, grounded plan to progress their career or prepare for an interview.

## Steps
1. **Understand the target.** Current role/skills (from `candidates`) and desired role (from the message).
2. **Skill-gap analysis.** Look up the `career_paths` edge `from_role → to_role` (or nearest match).
   Diff `candidate.skills[]` against `key_skills[]`; expand with `skills_taxonomy.related[]`.
3. **Build the plan.** 3–5 prioritized actions with a realistic timeline and, where relevant,
   the milestones from `career_paths.milestones[]`.
4. **Interview prep (if asked).** Pull `interview_prep` for the target role; for conceptual asks,
   `embed(query)` → `$vectorSearch` on `interview_prep.embedding`. Provide practice questions + how
   to approach them (STAR, structure) rather than just model answers.

## Guardrails
- Be honest about gaps and realistic about timelines.
- Ground certifications/courses in the data or widely-known standards; don't fabricate specifics.
