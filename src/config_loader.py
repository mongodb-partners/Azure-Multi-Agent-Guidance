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


def build_agent_instructions(agent: AgentConfig, skills: dict[str, SkillConfig]) -> str:
    """Compose the final Foundry instructions = agent body + linked skill playbook."""
    parts = [agent.instructions]
    if agent.skill and agent.skill in skills:
        parts.append("\n\n---\n\n## Playbook\n\n" + skills[agent.skill].body)
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
