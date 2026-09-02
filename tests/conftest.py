"""Shared pytest fixtures and gates.

Layer gating:
  * unit         — always run (offline; no creds).
  * integration  — skipped unless RUN_INTEGRATION=1 (needs real Mongo/Azure).
  * e2e          — skipped unless RUN_E2E=1 and API_URL is set.
"""
from __future__ import annotations

import os

import pytest

# Keep every test path offline-by-default: the heuristic classifier needs no creds.
os.environ.setdefault("CLASSIFIER_BACKEND", "heuristic")


def pytest_collection_modifyitems(config, items):
    run_integration = os.getenv("RUN_INTEGRATION") == "1"
    run_e2e = os.getenv("RUN_E2E") == "1"
    skip_integration = pytest.mark.skip(reason="set RUN_INTEGRATION=1 to run")
    skip_e2e = pytest.mark.skip(reason="set RUN_E2E=1 and API_URL to run")
    for item in items:
        if "integration" in item.keywords and not run_integration:
            item.add_marker(skip_integration)
        if "e2e" in item.keywords and not run_e2e:
            item.add_marker(skip_e2e)


@pytest.fixture
def api_url() -> str:
    url = os.getenv("API_URL")
    if not url:
        pytest.skip("API_URL not set")
    return url.rstrip("/")
