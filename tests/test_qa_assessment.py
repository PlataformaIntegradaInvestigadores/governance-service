from dataclasses import asdict

import pytest

from app.domain.qa import QaPolicy, Verdict
from app.infrastructure.qa_response import assess_qa_response


@pytest.fixture
def policy():
    return QaPolicy("search-qa-review", "draft-1", 3)


@pytest.fixture
def response():
    return {
        "status": "success",
        "qa_pairs": [
            {"question": f"Question {i}", "answer": f"Answer {i}"} for i in range(3)
        ],
        "source_docs": [{"article_id": "article-1", "title": "Source"}],
        "raw_response": "Sensitive output must not appear in the assessment",
    }


def test_complete_generation_is_retained_without_claiming_factual_quality(
    response, policy
):
    result = assess_qa_response(response, policy)

    assert result.verdict == Verdict.SUSPEND
    assert result.reason_codes == ("HUMAN_REVIEW_REQUIRED",)
    assert result.policy_id == policy.policy_id
    assert result.policy_version == policy.version
    assert set(asdict(result)) == {
        "verdict",
        "policy_id",
        "policy_version",
        "reason_codes",
    }
    assert "Sensitive" not in str(asdict(result))


@pytest.mark.parametrize("status", ["partial", "partial: only two pairs", "success"])
def test_partial_generation_requires_review(response, policy, status):
    response["status"] = status
    response["qa_pairs"].pop()

    result = assess_qa_response(response, policy)

    assert result.verdict == Verdict.SUSPEND
    assert "PARTIAL_GENERATION" in result.reason_codes


@pytest.mark.parametrize(
    "sources", [[], [{}], [{"article_id": None}], [{"article_id": " "}]]
)
def test_missing_source_identifiers_are_not_treated_as_verified(
    response, policy, sources
):
    response["source_docs"] = sources

    result = assess_qa_response(response, policy)

    assert result.verdict == Verdict.SUSPEND
    assert "SOURCE_EVIDENCE_INCOMPLETE" in result.reason_codes


def test_missing_source_list_requires_review(response, policy):
    response.pop("source_docs")
    assert (
        "SOURCE_EVIDENCE_INCOMPLETE"
        in assess_qa_response(response, policy).reason_codes
    )


def test_integer_source_identifiers_are_supported(response, policy):
    response["source_docs"] = [{"article_id": 12}]
    assert assess_qa_response(response, policy).reason_codes == (
        "HUMAN_REVIEW_REQUIRED",
    )


@pytest.mark.parametrize("status", ["error", "error: provider unavailable"])
def test_body_error_is_denied_even_when_transport_was_successful(
    response, policy, status
):
    response["status"] = status
    result = assess_qa_response(response, policy)
    assert result.verdict == Verdict.DENY
    assert result.reason_codes == ("UPSTREAM_GENERATION_FAILED",)


def test_explicit_error_overrides_success_status(response, policy):
    response["error"] = "Provider failed"
    assert assess_qa_response(response, policy).verdict == Verdict.DENY


@pytest.mark.parametrize(
    ("count", "reason"), [(0, "NO_VALID_PAIRS"), (4, "UNEXPECTED_PAIR_COUNT")]
)
def test_unusable_pair_counts_are_denied(response, policy, count, reason):
    response["qa_pairs"] = [{"question": "Q", "answer": "A"}] * count
    result = assess_qa_response(response, policy)
    assert result.verdict == Verdict.DENY
    assert result.reason_codes == (reason,)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"status": "unknown", "qa_pairs": []},
        {"status": "success", "qa_pairs": "not a list"},
        {"status": "success", "qa_pairs": [{"question": " ", "answer": "A"}]},
        {"status": "success", "qa_pairs": [{"question": "Q", "answer": 123}]},
        {"status": "success", "qa_pairs": [], "source_docs": [{"article_id": True}]},
        {"status": "success", "qa_pairs": [], "error": False},
    ],
)
def test_malformed_payloads_fail_closed(policy, payload):
    result = assess_qa_response(payload, policy)
    assert result.verdict == Verdict.DENY
    assert result.reason_codes == ("INVALID_UPSTREAM_RESPONSE",)


@pytest.mark.parametrize(
    ("policy_id", "version", "count"),
    [
        (" ", "draft-1", 3),
        ("qa", " ", 3),
        ("qa", "draft-1", 0),
        ("qa", "draft-1", -1),
        ("qa", "draft-1", True),
        ("qa", "draft-1", 3.0),
    ],
)
def test_invalid_policy_configuration_is_rejected(policy_id, version, count):
    with pytest.raises(ValueError, match=r"must not be blank|must be a positive"):
        QaPolicy(policy_id, version, count)
