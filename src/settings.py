"""Centralized environment configuration.

Loads `.env` (via python-dotenv) once and exposes typed settings used across the app.
Nothing here is Azure-specific beyond reading names; credentials come from
DefaultAzureCredential (az login) or explicit keys for the embedding endpoint.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel

# Load .env from the project root (parent of src/)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

_RETRIEVAL_MIN_SCORE_ENV = os.getenv("RETRIEVAL_MIN_SCORE", "")


class Settings(BaseModel):
    # --- Foundry / Agent Service ---
    project_endpoint: str = os.getenv("AZURE_AI_PROJECT_ENDPOINT", "")
    tenant_id: str = os.getenv("AZURE_TENANT_ID", "")
    chat_deployment: str = os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-5.6-sol")
    classifier_deployment: str = os.getenv("CLASSIFIER_MODEL_DEPLOYMENT", "gpt-5.6-sol")

    # --- Embeddings (provider-selected — specs/002, R1) ---
    # foundry (default) — direct Azure OpenAI embeddings (deployment = EMBEDDING_MODEL)
    # voyage            — direct Voyage AI API (VOYAGE_API_KEY + VOYAGE_MODEL)
    # atlas_auto        — Atlas Automated Embedding (Voyage, Atlas-managed): no
    #                     client-side embedding at seed or query time.
    embedding_provider: str = os.getenv("EMBEDDING_PROVIDER", "foundry")

    # --- Azure OpenAI embeddings (provider=foundry) ---
    aoai_endpoint: str = os.getenv("AZURE_OPENAI_ENDPOINT", "")
    aoai_api_key: str = os.getenv("AZURE_OPENAI_API_KEY", "")
    aoai_api_version: str = os.getenv("AZURE_OPENAI_API_VERSION", "2024-06-01")
    embedding_model: str = os.getenv("EMBEDDING_MODEL", "text-embedding-3-large")
    embedding_dimensions: int = int(os.getenv("EMBEDDING_DIMENSIONS", "3072"))

    # --- Voyage AI embeddings (provider=voyage) ---
    voyage_api_key: str = os.getenv("VOYAGE_API_KEY", "")
    voyage_model: str = os.getenv("VOYAGE_MODEL", "voyage-3-large")
    # 0 = infer dimensionality from a probe embedding (no static per-model table).
    voyage_dimensions: int = int(os.getenv("VOYAGE_DIMENSIONS", "0"))

    # --- Atlas Automated Embedding (provider=atlas_auto) ---
    # Voyage model the autoEmbed index declares (Atlas-managed, multi-tenant).
    atlas_embed_model: str = os.getenv("ATLAS_EMBED_MODEL", "voyage-4")

    # --- MongoDB Atlas ---
    mongodb_uri: str = os.getenv("MONGODB_URI", "")
    mongodb_db: str = os.getenv("MONGODB_DB", "multi-agent")

    # --- Self-hosted MongoDB MCP Server (Azure Container Apps) ---
    # The agents talk to a self-hosted `mongodb/mongodb-mcp-server` running in Azure Container Apps
    # (Streamable HTTP at `<fqdn>/mcp`). The MCP container holds the MongoDB connection string
    # (MDB_MCP_CONNECTION_STRING) itself, so agents do NOT authenticate to MongoDB — they only need
    # the MCP endpoint URL (and, if the endpoint requires it, a shared API-key header; see below).
    # No values are hardcoded — supply them via .env (see .env.sample).
    mongodb_mcp_url: str = os.getenv("MONGODB_MCP_URL", "")

    # --- MCP endpoint API key (external-ingress hardening) ---
    # When the self-hosted MCP is exposed with external ingress, it validates a shared secret sent
    # in a NON-`Authorization` header (Foundry rejects `Authorization` on registered MCP tools). The
    # header NAME is `mcp_api_key_header` and its VALUE is `mcp_api_key`. Leave the key empty for an
    # unauthenticated (e.g. internal-ingress or local dev) MCP.
    mcp_api_key: str = os.getenv("MCP_API_KEY", "")
    mcp_api_key_header: str = os.getenv("MCP_API_KEY_HEADER", "X-MCP-Api-Key")

    # --- Foundry project connection for the MCP tool (registered agents) ---
    # Foundry rejects raw auth headers on a *registered* MCP tool ("Headers that can include
    # sensitive information are not allowed … Use project_connection_id instead"). So the MCP
    # API-key header is stored in a Foundry project connection (CustomKeys) and the registered
    # MCPTool references it by name/id. Only used on the registered path; the inline path still
    # sends the header directly. Set to the connection NAME (e.g. "mongodb-mcp") or full ARM id.
    mcp_connection_id: str = os.getenv("MCP_CONNECTION_ID", "")

    # --- Azure Key Vault (secrets store) ---
    # Holds the deployed stack's secrets (Mongo URI, AOAI key, MCP API key). Empty for local runs.
    azure_key_vault_url: str = os.getenv("AZURE_KEY_VAULT_URL", "")

    # --- Behavior toggles ---
    classifier_backend: str = os.getenv("CLASSIFIER_BACKEND", "auto")  # heuristic|llm|auto
    # How specialists execute — agents are hosted on Azure AI Foundry Agent Service:
    #   registered-or-inline (default) — invoke registered Foundry prompt agents; fall back to inline
    #   registered — strict: only registered Foundry agents (error if unavailable)
    #   inline / disabled — always run inline via the raw Responses API (no Foundry registration)
    # With the self-hosted MCP the tool auth is a STATIC, long-lived API-key header (not an expiring
    # OAuth token), so the registered path no longer suffers the ~1h-token 424 problem. The header
    # name is non-`Authorization` (Foundry rejects `Authorization` on registered MCP tools).
    specialist_mode: str = os.getenv("SPECIALIST_MODE", "registered-or-inline")
    # Short-term (session) memory backend — specs/001 (R3):
    #   mongo (default) — per-session turns in the Atlas `chat_messages` collection
    #   foundry         — Foundry Conversations (managed); requires the optional Foundry
    #                     stack (requirements-foundry.txt) and AZURE_AI_PROJECT_ENDPOINT.
    #                     Mapping session_id -> conversation_id lives in `chat_sessions`.
    # Long-term facts and traces stay in Atlas regardless.
    short_term_memory_backend: str = os.getenv("SHORT_TERM_MEMORY_BACKEND", "mongo")
    allow_write: bool = os.getenv("MONGODB_ALLOW_WRITE", "false").lower() == "true"
    port: int = int(os.getenv("PORT", "3000"))

    # --- Observability (Azure Monitor / Application Insights + OpenTelemetry) ---
    # When the connection string is empty, telemetry is a no-op (clean local runs).
    applicationinsights_connection_string: str = os.getenv(
        "APPLICATIONINSIGHTS_CONNECTION_STRING", ""
    )
    otel_enabled: bool = os.getenv("OTEL_ENABLED", "true").lower() == "true"
    otel_service_name: str = os.getenv("OTEL_SERVICE_NAME", "career-assistant-api")

    # --- Cost estimation (USD per 1M tokens) ---
    # Used only to display an approximate spend in the metrics dashboard. Defaults track Azure
    # OpenAI gpt-4o list pricing ($2.50 in / $10.00 out per 1M tokens); override for your model.
    price_input_per_1m: float = float(os.getenv("PRICE_INPUT_PER_1M", "2.50"))
    price_output_per_1m: float = float(os.getenv("PRICE_OUTPUT_PER_1M", "10.00"))

    # --- Retrieval grounding (specs/002, R7) ---
    # Minimum Atlas vectorSearchScore for a host-retrieved row to be offered as a grounding
    # candidate. Below this the block is emitted empty, so the agent abstains instead of
    # grounding on noise. Atlas normalises cosine to (1+cos)/2 — 0.5 is orthogonal, so anything
    # under ~0.6 filters nothing. This is an OPTIONAL OVERRIDE: unset → the per-provider
    # default map in src/retrieval.py::FLOOR_DEFAULTS applies (foundry: 0.60, calibrated
    # against the seeded demo corpus — window: 0.579 best off-corpus `jobs` to 0.611 worst
    # on-corpus `interview_prep`; voyage/atlas_auto: uncalibrated → floor disabled with a
    # warning). Recalibrate per provider/corpus with
    # `python -m scripts.calibrate_retrieval_floor`, and go per-preset if the window closes.
    retrieval_min_score: float | None = (
        float(_RETRIEVAL_MIN_SCORE_ENV) if _RETRIEVAL_MIN_SCORE_ENV else None
    )

    # --- Paths ---
    config_dir: str = str(_PROJECT_ROOT / "config")

    @property
    def aoai_base(self) -> str:
        """Base Azure OpenAI resource endpoint, with any /openai or /openai/v1 suffix stripped.

        Accepts either the classic resource endpoint (https://<res>.openai.azure.com or
        https://<res>.services.ai.azure.com) or the OpenAI-compatible route ending in
        /openai/v1, and normalizes both to the base the AzureOpenAI client expects.
        """
        e = self.aoai_endpoint.rstrip("/")
        for suffix in ("/openai/v1", "/openai"):
            if e.endswith(suffix):
                e = e[: -len(suffix)]
        return e.rstrip("/")

    def require(self, *names: str) -> None:
        """Raise a clear error if any required setting is empty."""
        missing = [n for n in names if not getattr(self, n, None)]
        if missing:
            raise RuntimeError(
                "Missing required settings: "
                + ", ".join(missing)
                + ". Fill them in your .env (see .env.sample)."
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
