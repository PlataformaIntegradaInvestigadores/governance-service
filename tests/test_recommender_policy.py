import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from app.infrastructure.recommender_response import (
    assess_recommender_response,
    load_recommender_policy,
)

ROOT = Path(__file__).resolve().parents[1]
POLICY = load_recommender_policy(
    ROOT / "policies" / "recommender-fairness.draft-1.json"
)
CASES = json.loads(
    (ROOT / "tests" / "fixtures" / "recommender-reference.json").read_text(
        encoding="utf-8"
    )
)["cases"]


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_recommender_reference(case):
    assessment = assess_recommender_response(
        case["upstream_status"], json.dumps(case["upstream_response"]).encode(), POLICY
    )
    assert assessment.verdict == case["expected"]["verdict"]
    assert assessment.reasons == tuple(case["expected"]["reason_codes"])


def test_fair_mode_is_retained_as_weakened_relative_to_prefix_policy():
    response = deepcopy(CASES[0]["upstream_response"])
    response["parameters"]["rerank"]["fairness_mode"] = "fair"

    assessment = assess_recommender_response(200, json.dumps(response).encode(), POLICY)

    assert assessment.verdict == "retain"
    assert assessment.reasons[0] == "FAIRNESS_WEAKENED"


def test_baseline_can_be_permitted_with_notice():
    assessment = assess_recommender_response(
        200,
        json.dumps(CASES[2]["upstream_response"]).encode(),
        replace(POLICY, baseline="permit_with_notice"),
    )
    assert assessment.verdict == "permit"
    assert assessment.reasons == ("BASELINE_WITHOUT_FAIRNESS",)


@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [
        (503, b"", "UPSTREAM_ERROR"),
        (200, b"invalid", "UPSTREAM_CONTRACT_INVALID"),
        (200, b"{}", "UPSTREAM_CONTRACT_INVALID"),
    ],
)
def test_invalid_upstream_response_fails_closed(status, body, reason):
    assessment = assess_recommender_response(status, body, POLICY)
    assert assessment.verdict == "deny"
    assert assessment.reasons == (reason,)
