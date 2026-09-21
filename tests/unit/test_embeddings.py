"""Unit tests for the embedding provider facade (specs/002, R1–R4, R8) — no network."""
from __future__ import annotations

import pytest

import src.embeddings as emb
from src.settings import Settings

pytestmark = pytest.mark.unit


def _use(monkeypatch, provider: str, **overrides):
    monkeypatch.setattr(emb, "get_settings",
                        lambda: Settings(embedding_provider=provider, **overrides))


# --- dispatch --------------------------------------------------------------- #
def test_foundry_is_default_and_dispatches_to_aoai(monkeypatch):
    _use(monkeypatch, "foundry")
    monkeypatch.setattr(emb, "_embed_via_aoai", lambda text: [1.0, 2.0])
    monkeypatch.setattr(emb, "_embed_via_voyage",
                        lambda text, input_type=None: pytest.fail("voyage should not be called"))
    assert emb.embed_text("hi") == [1.0, 2.0]


def test_voyage_dispatches_with_input_type(monkeypatch):
    _use(monkeypatch, "voyage", voyage_api_key="k", voyage_model="voyage-3-large")
    seen = {}

    def _fake(text, input_type=None):
        seen["input_type"] = input_type
        return [0.1]

    monkeypatch.setattr(emb, "_embed_via_voyage", _fake)
    assert emb.embed_text("hi", input_type="query") == [0.1]
    assert seen["input_type"] == "query"


def test_atlas_auto_forbids_client_side_embedding(monkeypatch):
    _use(monkeypatch, "atlas_auto")
    with pytest.raises(RuntimeError, match="atlas_auto"):
        emb.embed_text("hi")


def test_unknown_provider_raises(monkeypatch):
    _use(monkeypatch, "nope")
    with pytest.raises(RuntimeError, match="Unknown EMBEDDING_PROVIDER"):
        emb.embed_text("hi")


def test_embed_text_rejects_empty():
    with pytest.raises(ValueError):
        emb.embed_text("   ")


# --- voyage HTTP boundary --------------------------------------------------- #
def test_voyage_request_shape(monkeypatch):
    _use(monkeypatch, "voyage", voyage_api_key="secret", voyage_model="voyage-3-large",
         voyage_dimensions=256)
    captured = {}

    def _fake_request(payload, api_key):
        captured.update(payload=payload, api_key=api_key)
        return {"data": [{"embedding": [0.0] * 256}]}

    monkeypatch.setattr(emb, "_voyage_request", _fake_request)
    assert emb.embed_text("hello", input_type="document") == [0.0] * 256
    assert captured["api_key"] == "secret"
    assert captured["payload"]["model"] == "voyage-3-large"
    assert captured["payload"]["input_type"] == "document"
    assert captured["payload"]["output_dimension"] == 256


def test_voyage_requires_api_key(monkeypatch):
    _use(monkeypatch, "voyage", voyage_api_key="")
    with pytest.raises(RuntimeError, match="voyage_api_key"):
        emb.embed_text("hi")


# --- dimensions for index creation ------------------------------------------ #
def test_embed_dimensions_foundry(monkeypatch):
    _use(monkeypatch, "foundry", embedding_dimensions=3072)
    assert emb.embed_dimensions() == 3072


def test_embed_dimensions_voyage_explicit(monkeypatch):
    _use(monkeypatch, "voyage", voyage_dimensions=512)
    assert emb.embed_dimensions() == 512


def test_embed_dimensions_voyage_probe(monkeypatch):
    """VOYAGE_DIMENSIONS=0 → probe the API once and use the response length."""
    _use(monkeypatch, "voyage", voyage_api_key="k", voyage_dimensions=0)
    monkeypatch.setattr(emb, "_embed_via_voyage",
                        lambda text, input_type=None: [0.0] * 1024)
    assert emb.embed_dimensions() == 1024


def test_embed_dimensions_atlas_auto_is_none(monkeypatch):
    _use(monkeypatch, "atlas_auto")
    assert emb.embed_dimensions() is None
