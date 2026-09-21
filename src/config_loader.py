"""Load agent/skill markdown configs (frontmatter + body).

Port of the reference `config-scan.ts`: reads `config/agents/*.agent.md` and
`config/skills/*.skill.md`, parses YAML frontmatter, and returns typed records the
rest of the app (classifier, foundry_agents, orchestrator) consumes.
"""
from __future__ import annotations

import glob
import os
from dataclasses import dataclass, field
from typing import Any

import yaml

from .settings import get_settings

_FRONTMATTER_DELIM = "---"


@dataclass
class AgentConfig:
    name: str
    description: str
    model: str
    instructions: str
    tools: list[str] = field(default_factory=list)
    collections: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    routable: bool = True
    skill: str | None = None
    path: str = ""


@dataclass
class SkillConfig:
    name: str
    title: str
    description: str
    agent: str
    body: str
    path: str = ""


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Return (frontmatter_dict, body). Tolerates a leading blank line/BOM."""
    stripped = text.lstrip("﻿").lstrip("\n")
    if not stripped.startswith(_FRONTMATTER_DELIM):
        return {}, text
    # Split on the delimiter: ['', '<yaml>', '<body>...']
    parts = stripped.split(_FRONTMATTER_DELIM, 2)
    if len(parts) < 3:
        return {}, text
    meta = yaml.safe_load(parts[1]) or {}
    body = parts[2].lstrip("\n")
    return meta, body


def load_agents(config_dir: str | None = None) -> dict[str, AgentConfig]:
    """Load all agent configs, keyed by agent name."""
    base = config_dir or get_settings().config_dir
    agents: dict[str, AgentConfig] = {}
    for fp in sorted(glob.glob(os.path.join(base, "agents", "*.agent.md"))):
        with open(fp, encoding="utf-8") as f:
            meta, body = _split_frontmatter(f.read())
        name = meta.get("name") or os.path.basename(fp).replace(".agent.md", "")
        agents[name] = AgentConfig(
            name=name,
            description=(meta.get("description") or "").strip(),
            model=meta.get("model") or get_settings().chat_deployment,
            instructions=body.strip(),
            tools=list(meta.get("tools") or []),
            collections=list(meta.get("collections") or []),
            keywords=[k.lower() for k in (meta.get("keywords") or [])],
            routable=bool(meta.get("routable", True)),
            skill=meta.get("skill"),
            path=fp,
        )
    return agents


def load_skills(config_dir: str | None = None) -> dict[str, SkillConfig]:
    """Load all skill configs, keyed by skill name."""
    base = config_dir or get_settings().config_dir
    skills: dict[str, SkillConfig] = {}
    for fp in sorted(glob.glob(os.path.join(base, "skills", "*.skill.md"))):
        with open(fp, encoding="utf-8") as f:
            meta, body = _split_frontmatter(f.read())
        name = meta.get("name") or os.path.basename(fp).replace(".skill.md", "")
        skills[name] = SkillConfig(
            name=name,
            title=meta.get("title") or name,
            description=(meta.get("description") or "").strip(),
            agent=meta.get("agent") or name,
            body=body.strip(),
            path=fp,
        )
    return skills


# Non-negotiable grounding contract injected into EVERY agent's instructions. Enforces that
# answers come only from data retrieved via the MongoDB MCP server — no general/pretrained
# knowledge fallback. Kept here (single source) so it can never drift or be forgotten per file.
# The self-hosted MongoDB MCP server has a PRECONFIGURED connection (its container holds the
# connection string), so agents query directly — no `connect` handshake is needed. `{db_hint}` names
# the target database so tool calls include the right `database` arg.
GROUNDING_RULES = """\
---

## STRICT GROUNDING — MongoDB-only (highest priority; overrides everything else)

You may ONLY answer using data you retrieve from MongoDB through the MongoDB MCP tools
(e.g. `find`, `aggregate`, `count`) — or grounding rows the host explicitly provides in the
prompt (they originate from the same MongoDB).
{db_hint}
Hard rules:
1. Before answering any substantive question, you MUST query MongoDB via the MCP tools.
2. Base every fact, name, number, and claim strictly on the returned documents. Do NOT use
   your own general/pretrained knowledge to add, infer, or "fill in" anything.
3. If the MCP query returns NO documents (or the tool errors / returns empty), you MUST NOT
   answer from general knowledge. Reply exactly with:
   "I couldn't find any relevant information in the MongoDB database for that request."
   Optionally suggest how to broaden the query (e.g. relax a filter). Nothing else.
4. Never fabricate, guess, or approximate data that is not present in the retrieved documents.
5. If only partial data is found, answer only from what was returned and explicitly note what
   is unknown/missing — do not backfill from general knowledge.
6. ALWAYS exclude large vector fields from your query results — they are noise that crowds out
   the real fields and can cause the tool response to be truncated so you see NO usable values.
   - For `find`: pass a projection that excludes the embedding, e.g.
     `projection: {{"embedding": 0}}` (and exclude any other `*_embedding` / vector fields).
   - For `aggregate`: add a `$project` stage that drops `embedding` (e.g. `{{"$project": {{"embedding": 0}}}}`),
     or `$unset: "embedding"`.
   Every collection here stores an `embedding` vector — never let it into the returned documents.
   If a `find`/`aggregate` result looks empty or field values appear missing, re-run the query WITH
   the embedding excluded before concluding the data is unavailable.
"""

# Database hint injected into GROUNDING_RULES. The self-hosted MCP server already holds the
# connection, so agents only need to know which database/collection to target per tool call.
_DB_HINT = """
The MongoDB MCP server is already connected to the cluster (no `connect` step needed). Pass
`database` = "{database}" and the target `collection` to every data tool call
(`find`, `aggregate`, `count`, `list-collections`, `collection-schema`).
"""


def build_agent_instructions(agent: AgentConfig, skills: dict[str, SkillConfig]) -> str:
    """Compose final instructions = agent body + linked skill playbook + strict grounding rules.

    The grounding block is appended LAST so it takes precedence and applies uniformly to every
    agent (specialists and the orchestrator alike).
    """
    s = get_settings()
    db_hint = _DB_HINT.format(database=s.mongodb_db)
    grounding = GROUNDING_RULES.format(db_hint=db_hint)

    parts = [agent.instructions]
    if agent.skill and agent.skill in skills:
        parts.append("\n\n---\n\n## Playbook\n\n" + skills[agent.skill].body)
    parts.append("\n\n" + grounding)
    return "\n".join(parts).strip()


def routable_agents(agents: dict[str, AgentConfig]) -> dict[str, AgentConfig]:
    """Only the specialists that the classifier is allowed to select."""
    return {n: a for n, a in agents.items() if a.routable}


if __name__ == "__main__":
    # Quick sanity dump
    ags = load_agents()
    sks = load_skills()
    print(f"Loaded {len(ags)} agents, {len(sks)} skills")
    for n, a in ags.items():
        print(f"  agent: {n:14s} routable={a.routable} tools={a.tools} skill={a.skill}")
    for n, s in sks.items():
        print(f"  skill: {n:14s} -> agent={s.agent}")
