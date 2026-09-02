"""Offline classifier routing tests (heuristic backend — no Azure creds needed).

Run:  CLASSIFIER_BACKEND=heuristic python -m pytest tests/test_classifier.py -v
"""
import os

os.environ.setdefault("CLASSIFIER_BACKEND", "heuristic")

import pytest

from src.classifier import classify

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "query, expected",
    [
        ("find remote python jobs that match my resume", {"job-match"}),
        ("how do I move from data analyst to data scientist?", {"career-coach"}),
        ("what's the culture like at MongoDB?", {"company-intel"}),
        ("find jobs matching my CV and tell me about those companies",
         {"job-match", "company-intel"}),
        ("help me prep for a staff engineer interview", {"career-coach"}),
        ("compare Nimbus Data and Helio Robotics as employers", {"company-intel"}),
        ("show me senior backend openings in Austin", {"job-match"}),
        # Plural forms must route the same as singular (roles→role, openings→opening).
        ("find remote senior python roles", {"job-match"}),
        ("find remote senior python roles and tell me about those companies",
         {"job-match", "company-intel"}),
    ],
)
def test_routes_to_expected_specialists(query, expected):
    assert set(classify(query).specialists) == expected


@pytest.mark.parametrize("query", ["hello", "hi there, what can you do?", "who are you?"])
def test_small_talk_returns_direct(query):
    c = classify(query)
    assert c.specialists == []          # empty → orchestrator replies directly
    assert c.backend.endswith(":direct")


def test_multi_intent_includes_coach():
    # Ambiguous "skills for a role" may co-select job-match; career-coach must be present.
    assert "career-coach" in classify("what skills am I missing for a staff role?").specialists


def test_at_most_two_specialists():
    for q in ["find jobs and companies and career advice all at once please"]:
        assert len(classify(q).specialists) <= 2
