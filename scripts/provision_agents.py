"""Provision the routable specialists as persistent Foundry **prompt agents**.

Reuses the EXACT runtime pipeline — `load_agents()` + `load_skills()` +
`build_agent_instructions()` + `mcp_tool_dict()` — so the registered prompt agents are, by
construction, the same agents the local app runs inline. There is no second definition to drift.

Idempotency: a lock file (`config/foundry_agents.lock.json`) records `{name: {version, hash,
updated}}`. Each agent's hash = sha256(instructions + sorted MCP tool names + model). A re-run
skips unchanged agents and only bumps a version when the hash changed (`--force` overrides).

Commands:
    python -m scripts.provision_agents verify      # Stage-1 gate: resource accepts agent ops?
    python -m scripts.provision_agents sync         # create/update all routable specialists
    python -m scripts.provision_agents sync --force # force a version bump for every agent
    python -m scripts.provision_agents list         # list registered agents + versions

Requires: `pip install -r requirements-foundry.txt` and `az login` (DefaultAzureCredential).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow running as `python scripts/provision_agents.py` (not just `-m`).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config_loader import build_agent_instructions, load_agents, load_skills  # noqa: E402
from src.mcp_client import allowed_mcp_tools  # noqa: E402
from src.settings import get_settings  # noqa: E402

logger = logging.getLogger("provision_agents")

_LOCK_PATH = Path(get_settings().config_dir) / "foundry_agents.lock.json"


def _instr_hash(instructions: str, model: str) -> str:
    payload = instructions + "\n::model=" + model + "\n::tools=" + ",".join(sorted(allowed_mcp_tools()))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_lock() -> dict:
    if _LOCK_PATH.exists():
        try:
            return json.loads(_LOCK_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("Lock file corrupt — ignoring: %s", _LOCK_PATH)
    return {}


def _save_lock(lock: dict) -> None:
    _LOCK_PATH.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _registry():
    from src.foundry_registry import FoundryUnavailable, get_agent_registry

    return get_agent_registry(), FoundryUnavailable


def cmd_verify(_args) -> int:
    reg, FoundryUnavailable = _registry()
    try:
        reg.verify_capability()
    except FoundryUnavailable as e:
        print(f"✗ NOT a usable new-Foundry project:\n  {e}")
        return 1
    print(f"✓ Resource accepts agent operations: {reg.endpoint}")
    return 0


def cmd_sync(args) -> int:
    reg, FoundryUnavailable = _registry()
    try:
        reg.verify_capability()
    except FoundryUnavailable as e:
        print(f"✗ Cannot provision — {e}")
        return 1

    agents = load_agents()
    skills = load_skills()
    routable = {n: a for n, a in agents.items() if a.routable}
    if not routable:
        print("No routable agents found in config/agents/.")
        return 1

    lock = _load_lock()
    print(f"Provisioning {len(routable)} routable specialists to {reg.endpoint}\n")
    for name, cfg in routable.items():
        instructions = build_agent_instructions(cfg, skills)
        model = cfg.model or get_settings().chat_deployment
        h = _instr_hash(instructions, model)
        prev = (lock.get(name) or {}).get("hash")
        try:
            result = reg.create_or_update_agent(
                name,
                description=cfg.description,
                instructions=instructions,
                model=model,
                instr_hash=h,
                prev_hash=prev,
                force=args.force,
            )
        except FoundryUnavailable as e:
            print(f"  ✗ {name}: {e}")
            return 1
        lock[name] = {
            "version": result["version"],
            "hash": result["hash"],
            "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        icon = {"created": "＋", "updated": "↑", "unchanged": "="}.get(result["action"], "•")
        print(f"  {icon} {name:14s} v{result['version']:<4} {result['action']}")

    _save_lock(lock)
    print(f"\nLock written: {_LOCK_PATH}")
    print("These agents are now visible in the Foundry portal (new prompt agents, not classic).")
    return 0


def cmd_list(_args) -> int:
    reg, FoundryUnavailable = _registry()
    try:
        agents = reg.list_agents()
    except FoundryUnavailable as e:
        print(f"✗ {e}")
        return 1
    if not agents:
        print("(no registered agents)")
        return 0
    for name, versions in sorted(agents.items()):
        print(f"  {name:20s} versions: {', '.join(versions) or '(none)'}")
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("azure").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description="Provision Foundry prompt agents from config/agents/.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("verify", help="Stage-1 gate: does the resource accept agent operations?")
    p_sync = sub.add_parser("sync", help="Create/update all routable specialists (idempotent).")
    p_sync.add_argument("--force", action="store_true", help="Bump a version even if unchanged.")
    sub.add_parser("list", help="List registered agents and their versions.")

    args = parser.parse_args()
    return {"verify": cmd_verify, "sync": cmd_sync, "list": cmd_list}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
