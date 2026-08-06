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


class Settings(BaseModel):
    # --- Foundry / Agent Service ---
    project_endpoint: str = os.getenv("AZURE_AI_PROJECT_ENDPOINT", "")
    tenant_id: str = os.getenv("AZURE_TENANT_ID", "")
    chat_deployment: str = os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-5.6-sol")
    classifier_deployment: str = os.getenv("CLASSIFIER_MODEL_DEPLOYMENT", "gpt-5.6-sol")

    # --- Azure OpenAI embeddings ---
    aoai_endpoint: str = os.getenv("AZURE_OPENAI_ENDPOINT", "")
    aoai_api_key: str = os.getenv("AZURE_OPENAI_API_KEY", "")
    aoai_api_version: str = os.getenv("AZURE_OPENAI_API_VERSION", "2024-06-01")
    embedding_model: str = os.getenv("EMBEDDING_MODEL", "text-embedding-3-large")
    embedding_dimensions: int = int(os.getenv("EMBEDDING_DIMENSIONS", "3072"))
    embedding_function_url: str = os.getenv("EMBEDDING_FUNCTION_URL", "")

    # --- MongoDB Atlas ---
    mongodb_uri: str = os.getenv("MONGODB_URI", "")
    mongodb_db: str = os.getenv("MONGODB_DB", "multi-agent")
    mongodb_mcp_url: str = os.getenv(
        "MONGODB_MCP_URL",
        "https://mongo-mcp-without-auth11may.happyground-0d54aef3.eastus.azurecontainerapps.io/mcp",
    )

    # --- Behavior toggles ---
    classifier_backend: str = os.getenv("CLASSIFIER_BACKEND", "auto")  # heuristic|llm|auto
    orchestrator_mode: str = os.getenv("ORCHESTRATOR_MODE", "classifier")  # classifier|maf-handoff
    # How specialists execute:
    #   registered-or-inline (default) — invoke registered Foundry prompt agents; fall back to inline
    #   registered — strict: only registered agents (error if unavailable)
    #   inline / disabled — always run inline via the raw Responses API (no Foundry registration)
    specialist_mode: str = os.getenv("SPECIALIST_MODE", "registered-or-inline")
    allow_write: bool = os.getenv("MONGODB_ALLOW_WRITE", "false").lower() == "true"
    port: int = int(os.getenv("PORT", "3000"))

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
