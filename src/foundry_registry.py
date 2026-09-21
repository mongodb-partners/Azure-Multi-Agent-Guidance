"""Centralized adapter for the **new** Azure AI Foundry Agent Service (azure-ai-projects 2.x).

Every call into the preview `azure.ai.projects` surface lives here, behind a small, verifiable
interface — so if the preview API shifts, there is exactly one file to fix. Verified against
**azure-ai-projects 2.4.0** (see the `foundry-agent-service-api` memo for the pinned surface).

Two Foundry-native consumption options are built on top of this module:

  1. **prompt agents** — each routable specialist registered as a persistent, portal-visible
     agent (`create_or_update_agent`), invocable by name (`invoke_by_reference`). This is the
     "use one agent" option and also what the local app rewires onto (`SPECIALIST_MODE`).
  2. **hosted agent** — the whole orchestrator packaged as one container agent
     (`create_or_update_hosted_agent`). This is the "use the whole solution" option (Stage 3).

The new prompt-agent path is Responses-based, so it does NOT hit the classic top_p trap that
forced specialists onto the raw Responses API in the first place (see `responses-api-pivot`).

`azure-ai-projects` is an **optional** dependency (`requirements-foundry.txt`); it is imported
lazily so the core demo installs and runs (inline mode) without it.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any, Iterator

from .mcp_client import mcp_tool_dict
from .settings import get_settings

logger = logging.getLogger(__name__)


class FoundryUnavailable(RuntimeError):
    """Raised when azure-ai-projects 2.x isn't installed or the resource rejects provisioning."""


def _require_sdk():
    """Import the 2.x SDK lazily; raise a clear, actionable error if it's the wrong version."""
    try:
        from azure.ai.projects import AIProjectClient  # noqa: F401
        from azure.ai.projects.models import (  # noqa: F401
            MCPTool,
            PromptAgentDefinition,
        )
    except ImportError as e:  # pragma: no cover - env dependent
        raise FoundryUnavailable(
            "azure-ai-projects 2.x is not installed. Run `pip install -r requirements-foundry.txt` "
            "to enable Foundry prompt/hosted agents (SPECIALIST_MODE=registered)."
        ) from e
    return AIProjectClient, PromptAgentDefinition, MCPTool


def _mcp_tool_object():
    """Build the typed `MCPTool` from the SAME source as the inline path's `mcp_tool_dict()`.

    Reusing `mcp_tool_dict()` guarantees registered prompt agents get a byte-identical MCP tool
    surface to the inline specialists — no second definition to drift.

    Auth handling differs from the inline path because Foundry REJECTS raw auth headers on a
    *registered* MCP tool ("Headers that can include sensitive information are not allowed in the
    headers property for MCP tools. Use project_connection_id instead."). So:
      - If `MCP_CONNECTION_ID` is set, the API-key header is stored in a Foundry project
        connection (CustomKeys) and referenced via `project_connection_id` (the correct,
        Foundry-approved way to carry the secret).
      - Else, if the MCP needs no auth (empty key), we register a plain header-less tool.
      - Else we raise, because sending the header inline would be rejected by the service.
    """
    _, _, MCPTool = _require_sdk()
    s = get_settings()
    d = mcp_tool_dict()
    kwargs = dict(
        server_label=d["server_label"],
        server_url=d["server_url"],
        allowed_tools=d["allowed_tools"],
        require_approval=d["require_approval"],
    )

    conn = s.mcp_connection_id
    if conn:
        # Reference the stored connection; do NOT attach raw headers (service rejects them).
        try:
            return MCPTool(project_connection_id=conn, **kwargs)
        except TypeError as e:  # SDK too old to accept project_connection_id
            raise FoundryUnavailable(
                "Installed azure-ai-projects MCPTool does not support project_connection_id "
                f"({e}); upgrade the SDK to register an authenticated MCP tool."
            ) from e

    if d.get("headers"):
        raise FoundryUnavailable(
            "The MCP server requires an API-key header, but Foundry does not allow raw headers on "
            "registered MCP tools. Create a Foundry project connection holding the header and set "
            "MCP_CONNECTION_ID to its name/id (see scripts/provision_agents)."
        )

    # No auth required — register a plain tool.
    return MCPTool(**kwargs)


class FoundryAgentRegistry:
    """Thin wrapper over `AIProjectClient.agents` — the one place that touches azure-ai-projects."""

    def __init__(self, endpoint: str | None = None, credential: Any = None):
        s = get_settings()
        self.endpoint = endpoint or s.project_endpoint
        if not self.endpoint:
            raise FoundryUnavailable(
                "AZURE_AI_PROJECT_ENDPOINT is not set — cannot reach the Foundry project."
            )
        self._credential = credential
        self._client = None  # lazy

    # ----------------------------------------------------------------- client
    def client(self):
        if self._client is None:
            AIProjectClient, _, _ = _require_sdk()
            cred = self._credential
            if cred is None:
                from azure.identity import DefaultAzureCredential

                cred = DefaultAzureCredential()
            # allow_preview=True is required for the new agent-provisioning operations.
            self._client = AIProjectClient(
                endpoint=self.endpoint, credential=cred, allow_preview=True
            )
        return self._client

    # -------------------------------------------------------------- capability
    def verify_capability(self) -> bool:
        """Stage-1 gate: prove the resource is a new-Foundry project that accepts agent ops.

        Returns True on success; raises FoundryUnavailable with a clear message otherwise.
        """
        try:
            list(self.client().agents.list())  # forces a round-trip
            return True
        except FoundryUnavailable:
            raise
        except Exception as e:  # noqa: BLE001
            raise FoundryUnavailable(
                f"The resource at {self.endpoint} rejected agent operations ({type(e).__name__}: {e}). "
                "It is likely NOT enrolled as a new-Foundry project — create/select a new-Foundry "
                "project to register prompt/hosted agents."
            ) from e

    # ----------------------------------------------------------------- CRUD
    def create_or_update_agent(
        self,
        agent_name: str,
        *,
        description: str,
        instructions: str,
        model: str,
        instr_hash: str,
        prev_hash: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Idempotent register: create/bump a prompt-agent version only when the hash changed.

        Returns {name, version, hash, action} where action ∈ {created, updated, unchanged}.
        The caller owns the hash store (`config/foundry_agents.lock.json`); we compare against
        `prev_hash` so a re-run with unchanged instructions is a no-op.
        """
        _, PromptAgentDefinition, _ = _require_sdk()

        if prev_hash == instr_hash and not force:
            # Confirm the version still exists server-side; if it does, skip.
            existing = self._latest_version(agent_name)
            if existing is not None:
                return {
                    "name": agent_name,
                    "version": existing,
                    "hash": instr_hash,
                    "action": "unchanged",
                }

        definition = PromptAgentDefinition(
            model=model,
            instructions=instructions,
            tools=[_mcp_tool_object()],
        )
        ver = self.client().agents.create_version(
            agent_name=agent_name,
            definition=definition,
            description=description or None,
            metadata={"instr_hash": instr_hash},
        )
        version = str(getattr(ver, "version", "") or "")
        action = "created" if prev_hash is None else "updated"
        logger.info("Foundry agent %s → version %s (%s)", agent_name, version, action)
        return {"name": agent_name, "version": version, "hash": instr_hash, "action": action}

    def _latest_version(self, agent_name: str) -> str | None:
        """Return the newest version id for an agent, or None if it doesn't exist yet."""
        try:
            versions = list(self.client().agents.list_versions(agent_name, limit=1))
        except Exception:  # noqa: BLE001 - not found / no versions
            return None
        if not versions:
            return None
        return str(getattr(versions[0], "version", "") or "") or None

    def get_agent_version(self, agent_name: str, version: str | None = None) -> dict[str, Any]:
        """Fetch one agent version's details (latest if version omitted)."""
        if version in (None, "latest", ""):
            version = self._latest_version(agent_name)
            if version is None:
                raise FoundryUnavailable(f"Agent {agent_name} has no registered versions.")
        details = self.client().agents.get_version(agent_name, version)
        return {
            "name": agent_name,
            "version": str(getattr(details, "version", version)),
            "description": getattr(details, "description", None),
        }

    def list_agents(self) -> dict[str, list[str]]:
        """Map each registered agent name → list of its version ids."""
        out: dict[str, list[str]] = {}
        for a in self.client().agents.list():
            name = getattr(a, "name", None)
            if not name:
                continue
            try:
                vers = [
                    str(getattr(v, "version", "")) for v in self.client().agents.list_versions(name)
                ]
            except Exception:  # noqa: BLE001
                vers = []
            out[name] = vers
        return out

    def delete_agent(self, agent_name: str, *, force: bool = False) -> None:
        self.client().agents.delete(agent_name=agent_name, force=force)

    # ----------------------------------------------------------- invoke by ref
    def invoke_by_reference(
        self,
        agent_name: str,
        *,
        input: str,
        previous_response_id: str | None = None,
        stream: bool = True,
    ):
        """Invoke a registered prompt agent by name via its bound OpenAI Responses client.

        Returns whatever `openai_client.responses.create(...)` returns — a stream iterator when
        `stream=True`, else a Response object. The caller maps events (the FoundrySpecialist
        registered path reuses the exact same dispatch as the inline path).

        The self-hosted MCP tool uses a STATIC, long-lived API-key header baked into the stored
        agent version, so — unlike the old expiring-OAuth-token setup — the registered path does not
        go stale between turns. Rotate the key by re-provisioning (`scripts.provision_agents sync`).
        """
        oai = self.client().get_openai_client(agent_name=agent_name)
        # specs/004 R2: request a reasoning summary so the registered path emits the same
        # live `reasoning` events as the inline path (`_consume_stream` maps them). If the
        # agent-bound client rejects the per-request override (400), strip and retry (R3).
        kwargs: dict[str, Any] = {
            "input": input,
            "stream": stream,
            "reasoning": {"summary": "auto"},
        }
        if previous_response_id:
            kwargs["previous_response_id"] = previous_response_id
        from openai import BadRequestError

        try:
            return oai.responses.create(**kwargs)
        except BadRequestError as e:
            msg = str(getattr(e, "message", "") or e).lower()
            if "reasoning" in kwargs and "reasoning" in msg:
                kwargs.pop("reasoning", None)
                return oai.responses.create(**kwargs)
            raise

    # ------------------------------------------------------------ hosted agent
    def create_or_update_hosted_agent(
        self,
        agent_name: str,
        *,
        image: str,
        cpu: str = "1",
        memory: str = "2Gi",
        environment_variables: dict[str, str] | None = None,
        description: str = "",
        protocol_version: str = "2.0.0",
    ) -> dict[str, Any]:
        """Register/bump the whole-solution hosted agent from a built container image (Stage 3)."""
        _require_sdk()
        from azure.ai.projects.models import (
            ContainerConfiguration,
            HostedAgentDefinition,
            ProtocolVersionRecord,
        )

        definition = HostedAgentDefinition(
            cpu=cpu,
            memory=memory,
            environment_variables=environment_variables or {},
            container_configuration=ContainerConfiguration(image=image),
            protocol_versions=[
                ProtocolVersionRecord(protocol="responses", version=protocol_version)
            ],
        )
        ver = self.client().agents.create_version(
            agent_name=agent_name,
            definition=definition,
            description=description or None,
        )
        return {"name": agent_name, "version": str(getattr(ver, "version", ""))}


@lru_cache(maxsize=1)
def get_agent_registry() -> FoundryAgentRegistry:
    """Process-wide singleton (built lazily; only touched when SPECIALIST_MODE != inline)."""
    return FoundryAgentRegistry()
