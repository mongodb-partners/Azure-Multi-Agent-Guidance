"""Embedding generation via Azure OpenAI.

Two backends, chosen automatically:
  1. If EMBEDDING_FUNCTION_URL is set → POST {"text": ...} to the deployed embedding Function
     (mirrors the reused `mongodb-foundry-agent` embedding function's /embed contract).
  2. Otherwise → call Azure OpenAI embeddings REST endpoint directly (endpoint + api-key).

`text-embedding-3-large` yields 3072-dim vectors; EMBEDDING_DIMENSIONS can request a
reduced dimensionality (Atlas index must match).
"""
from __future__ import annotations

import json
import urllib.request

from .settings import get_settings


def _embed_via_function(text: str, url: str) -> list[float]:
    data = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode())
    return result["embedding"]


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


def embed_text(text: str) -> list[float]:
    """Return an embedding vector for `text` using the configured backend."""
    if not text or not text.strip():
        raise ValueError("embed_text: empty text")
    s = get_settings()
    if s.embedding_function_url:
        return _embed_via_function(text, s.embedding_function_url)
    return _embed_via_aoai(text)


def embed_many(texts: list[str]) -> list[list[float]]:
    """Convenience batch wrapper (sequential; fine for seeding/demo volumes)."""
    return [embed_text(t) for t in texts]


if __name__ == "__main__":
    v = embed_text("Senior Python engineer with MongoDB and Azure experience")
    print(f"dims={len(v)} first3={v[:3]}")
