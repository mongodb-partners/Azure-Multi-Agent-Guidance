"""Unit tests for Settings normalization + require()."""
from __future__ import annotations

import pytest

from src.settings import Settings

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("https://r.openai.azure.com/", "https://r.openai.azure.com"),
        ("https://r.openai.azure.com/openai", "https://r.openai.azure.com"),
        ("https://r.openai.azure.com/openai/v1", "https://r.openai.azure.com"),
        ("https://r.services.ai.azure.com/openai/v1/", "https://r.services.ai.azure.com"),
    ],
)
def test_aoai_base_normalizes_suffixes(raw, expected):
    assert Settings(aoai_endpoint=raw).aoai_base == expected


def test_require_raises_on_missing():
    s = Settings(mongodb_uri="")
    with pytest.raises(RuntimeError) as ei:
        s.require("mongodb_uri")
    assert "mongodb_uri" in str(ei.value)


def test_require_passes_when_present():
    s = Settings(mongodb_uri="mongodb+srv://x")
    s.require("mongodb_uri")  # should not raise


def test_allow_write_defaults_false():
    assert Settings().allow_write is False
