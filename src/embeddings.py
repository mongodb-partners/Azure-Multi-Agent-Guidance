"""Embedding provider facade (specs/002, R1–R4).

Selected by EMBEDDING_PROVIDER (`foundry` default):
  - foundry    → direct Azure OpenAI embeddings REST (deployment = EMBEDDING_MODEL).
  - voyage     → direct Voyage AI API (VOYAGE_API_KEY + VOYAGE_MODEL).
  - atlas_auto → client-side embedding is FORBIDDEN: Atlas generates embeddings at
                 index- and query-time (Automated Embedding); calling embed_text raises.

`input_type` ("query" | "document") is honored by providers with asymmetric models
(Voyage); others ignore it.

Dimensions: `embed_dimensions()` feeds index creation (db_seeding/indexes.py). For
`voyage`, an explicit VOYAGE_DIMENSIONS wins; otherwise one probe embedding's length is
used (robust against new models). `atlas_auto` indexes carry no numDimensions.
"""
from __future__ import annotations

import json
import logging
import urllib.request

from .settings import get_settings

logger = logging.getLogger(__name__)

# Field the seeder materializes raw text into, and the auto-embed index/retrieval
# target, when EMBEDDING_PROVIDER=atlas_auto (specs/002 R3).
AUTO_EMBED_TEXT_FIELD = "embed_text"

def _voyage_url() -> str:
    """Embeddings endpoint from the configurable base (VOYAGE_API_BASE, R2).

    Voyage-platform keys (pa-*) hit api.voyageai.com; Atlas-issued keys (al-*) are
    rejected there with 403 and must use https://ai.mongodb.com/v1 instead.
    """
    return f"{get_settings().voyage_api_base.rstrip('/')}/embeddings"


def embedding_provider() -> str:
    return get_settings().embedding_provider


# --------------------------------------------------------------------------- #
# Provider: foundry (direct Azure OpenAI)
# --------------------------------------------------------------------------- #
def _embed_via_aoai(text: str) -> list[float]:
    s = get_settings()
    s.require("aoai_endpoint", "aoai_api_key")
    url = (
        f"{s.aoai_base}/openai/deployments/{s.embedding_model}"
        f"/embeddings?api-version={s.aoai_api_version}"
    )
    payload: dict = {"input": text}
    # text-embedding-3-* support the `dimensions` reduction param.
    if s.embedding_dimensions and "text-embedding-3" in s.embedding_model:
        payload["dimensions"] = s.embedding_dimensions
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"api-key": s.aoai_api_key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode())
    return result["data"][0]["embedding"]


# --------------------------------------------------------------------------- #
# Provider: voyage (direct Voyage AI API)
# --------------------------------------------------------------------------- #
def _voyage_request(payload: dict, api_key: str) -> dict:
    """HTTP boundary for the Voyage embeddings API (kept tiny for offline mocking — R8)."""
    req = urllib.request.Request(
        _voyage_url(),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _embed_via_voyage(text: str, input_type: str | None = None) -> list[float]:
    s = get_settings()
    s.require("voyage_api_key")
    payload: dict = {"input": text, "model": s.voyage_model}
    if input_type:
        payload["input_type"] = input_type
    if s.voyage_dimensions:
        payload["output_dimension"] = s.voyage_dimensions
    result = _voyage_request(payload, s.voyage_api_key)
    return result["data"][0]["embedding"]


# --------------------------------------------------------------------------- #
# Facade
# --------------------------------------------------------------------------- #
def embed_text(text: str, input_type: str | None = None) -> list[float]:
    """Return an embedding vector for `text` using the configured provider.

    Raises under `atlas_auto`: that provider must never embed client-side (R3) —
    retrieval passes query text straight to $vectorSearch and the seeder writes raw
    text fields instead.
    """
    if not text or not text.strip():
        raise ValueError("embed_text: empty text")
    provider = embedding_provider()
    if provider == "foundry":
        return _embed_via_aoai(text)
    if provider == "voyage":
        return _embed_via_voyage(text, input_type=input_type)
    if provider == "atlas_auto":
        raise RuntimeError(
            "embed_text called with EMBEDDING_PROVIDER=atlas_auto — client-side "
            "embedding is disabled under Atlas automated embedding (specs/002 R3)."
        )
    raise RuntimeError(f"Unknown EMBEDDING_PROVIDER: {provider!r}")


def embed_many(texts: list[str], input_type: str | None = None) -> list[list[float]]:
    """Convenience batch wrapper (sequential; fine for seeding/demo volumes)."""
    return [embed_text(t, input_type=input_type) for t in texts]


def embed_dimensions() -> int | None:
    """Vector dimensionality for index creation (R5). None under `atlas_auto`."""
    s = get_settings()
    provider = s.embedding_provider
    if provider == "foundry":
        return s.embedding_dimensions
    if provider == "voyage":
        if s.voyage_dimensions:
            return s.voyage_dimensions
        # No static per-model table: probe once so new Voyage models just work.
        return len(_embed_via_voyage("dimension probe"))
    return None  # atlas_auto — autoEmbed indexes carry no numDimensions


if __name__ == "__main__":
    v = embed_text("Senior Python engineer with MongoDB and Azure experience")
    print(f"provider={embedding_provider()} dims={len(v)} first3={v[:3]}")
