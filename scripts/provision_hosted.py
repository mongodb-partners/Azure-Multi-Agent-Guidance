"""Register the whole-solution container as a Foundry **hosted agent**, then poll to active.

Invoked by `deploy/provision_hosted.sh` after the image is built+pushed to ACR. Kept as a
separate Python entrypoint (like `provision_agents.py`) so the azure-ai-projects calls stay in
one verifiable place — `src.foundry_registry.FoundryAgentRegistry.create_or_update_hosted_agent`.

Usage:
    python -m scripts.provision_hosted --image <acr>.azurecr.io/career-hosted:latest \
        [--name career-hosted-solution] [--cpu 1] [--memory 2Gi]

The container's runtime settings (Mongo URI, Azure OpenAI, MCP URL, …) are passed to the hosted
agent as environment variables, read from the current environment (the shell wrapper `source`s
.env first). Requires `pip install -r requirements-foundry.txt` and `az login`.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logger = logging.getLogger("provision_hosted")

# Runtime settings the hosted container needs. Only those present in the environment are passed.
_ENV_PASSTHROUGH = [
    "SPECIALIST_MODE",
    "ORCHESTRATOR_MODE",
    "CLASSIFIER_BACKEND",
    "AZURE_AI_PROJECT_ENDPOINT",
    "AZURE_AI_MODEL_DEPLOYMENT_NAME",
    "CLASSIFIER_MODEL_DEPLOYMENT",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_API_VERSION",
    "EMBEDDING_MODEL",
    "EMBEDDING_DIMENSIONS",
    "EMBEDDING_FUNCTION_URL",
    "MONGODB_URI",
    "MONGODB_DB",
    "MONGODB_MCP_URL",
    "MONGODB_ALLOW_WRITE",
    "HOSTED_STREAM_TRACE",
]


def _collect_env() -> dict[str, str]:
    env = {k: os.environ[k] for k in _ENV_PASSTHROUGH if os.environ.get(k)}
    # The hosted whole-solution agent defaults to inline specialists unless overridden.
    env.setdefault("SPECIALIST_MODE", "inline")
    return env


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("azure").setLevel(logging.WARNING)

    ap = argparse.ArgumentParser(description="Register the whole-solution hosted agent.")
    ap.add_argument("--image", required=True, help="Full container image ref (ACR path:tag).")
    ap.add_argument("--name", default="career-hosted-solution", help="Hosted agent name.")
    ap.add_argument("--cpu", default="1")
    ap.add_argument("--memory", default="2Gi")
    ap.add_argument("--timeout", type=int, default=600, help="Seconds to wait for ACTIVE.")
    args = ap.parse_args()

    from src.foundry_registry import FoundryUnavailable, get_agent_registry

    try:
        reg = get_agent_registry()
        reg.verify_capability()
    except FoundryUnavailable as e:
        print(f"✗ Cannot provision hosted agent — {e}")
        return 1

    env = _collect_env()
    print(f"Registering hosted agent '{args.name}'")
    print(f"  image : {args.image}")
    print(f"  cpu/mem: {args.cpu} / {args.memory}")
    print(f"  env    : {', '.join(sorted(env)) or '(none)'}")

    try:
        result = reg.create_or_update_hosted_agent(
            args.name,
            image=args.image,
            cpu=args.cpu,
            memory=args.memory,
            environment_variables=env,
            description="Whole-solution multi-agent career assistant (classify → fan-out → synthesize).",
        )
    except FoundryUnavailable as e:
        print(f"✗ Registration failed — {e}")
        return 1
    version = result["version"]
    print(f"  → registered version {version}; polling for ACTIVE…")

    # Poll provisioning status until active/failed/timeout.
    deadline = time.time() + args.timeout
    last = None
    while time.time() < deadline:
        try:
            details = reg.client().agents.get_version(args.name, version)
            raw = getattr(details, "status", "")
            # `status` may be an enum (AgentVersionStatus.ACTIVE) whose str() is the *repr*, not
            # the value — normalize to the bare token so "active"/"failed" matching is reliable.
            status = str(getattr(raw, "value", raw) or "").rsplit(".", 1)[-1]
        except Exception as e:  # noqa: BLE001
            status = f"(status check error: {e})"
        if status != last:
            print(f"    status: {status}")
            last = status
        if status.lower() == "active":
            print(f"\n✓ Hosted agent ACTIVE: {args.name} v{version}")
            print("  Invoke it by reference like any prompt agent (get_openai_client(agent_name=...).responses.create).")
            return 0
        if status.lower() == "failed":
            print(f"\n✗ Hosted agent provisioning FAILED for {args.name} v{version}.")
            return 1
        time.sleep(10)

    print(f"\n⚠️ Timed out after {args.timeout}s waiting for ACTIVE (last status: {last}).")
    print("  Check the Foundry portal; provisioning may still complete.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
