import json
import sqlite3
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.governed_rag import RagRouteDependencies
from app.application.record_rag_assessment import RecordRagAssessment
from app.application.resolve_review import ReviewCases
from app.application.supervision import AuthorizeSupervisor
from app.core.config import Settings
from app.domain.qa import InvalidPolicyError
from app.domain.rag import RagPolicy
from app.domain.review import RetainedQaContent, RetainedQaPair
from app.domain.supervision import AdminTokenRejectedError
from app.infrastructure.json_policy_repository import JsonRagPolicyRepository
from app.infrastructure.rag_upstream import RagUpstream
from app.main import create_app

_ASK = "/api/v1/governed/rag/ask"
_QUERY = "Synthetic query"
_ANSWER = "Synthetic answer [1]."
_BLOCK_TEXT = "Synthetic protected fragment."
_UPSTREAM_DETAIL = "Synthetic upstream internal exception detail."
_ADMIN = {"X-Admin-Token": "synthetic-admin"}
_TIMING = {"search": 1, "rerank": 2, "generate": 3, "total": 6}


class ReviewStore:
    def __init__(self):
        self.assessments = {}
        self.cases = {}
        self.resolutions = {}
        self.fail_save = False

    def save(self, record, case):
        if self.fail_save:
            raise sqlite3.OperationalError(_UPSTREAM_DETAIL)
        self.assessments[record.assessment_id] = record
        if case is not None:
            self.cases[case.case_id] = case

    def save_resolution(self, resolution):
        self.resolutions[resolution.case_id] = resolution


@pytest.fixture
def rag(tmp_path):
    store = ReviewStore()
    policy = tmp_path / "rag.json"
    policy.write_text(json.dumps(asdict(RagPolicy("rag", "1"))), encoding="utf-8")
    assessments = SimpleNamespace(save=store.save, get=store.assessments.get)
    rag_routes = RagRouteDependencies(
        RagUpstream("http://rag.test", 2),
        RecordRagAssessment(JsonRagPolicyRepository(policy), assessments),
        b"synthetic-rag-release-test-material",
    )
    app = create_app(Settings(_env_file=None), rag=rag_routes)
    clock = SimpleNamespace(now=datetime(2026, 1, 1, tzinfo=UTC))
    app.state.review_cases = ReviewCases(
        assessments,
        SimpleNamespace(get=store.cases.get),
        SimpleNamespace(get=store.resolutions.get, save=store.save_resolution),
        timedelta(days=1),
        clock=lambda: clock.now,
    )

    def validate(token):
        if token != _ADMIN["X-Admin-Token"]:
            raise AdminTokenRejectedError("Synthetic administrator rejected")

    app.state.authorize_supervisor = AuthorizeSupervisor(
        SimpleNamespace(validate=validate)
    )
    return SimpleNamespace(
        client=TestClient(app), policy=policy, store=store, clock=clock
    )


def _answer(**changes):
    return {
        "query": _QUERY,
        "answer_text": _ANSWER,
        "blocks": [{"cite_id": "42", "title": "Synthetic title", "text": _BLOCK_TEXT}],
        "used_refs_report": _UPSTREAM_DETAIL,
        "timing_ms": _TIMING,
        **changes,
    }


def _ask_review(rag, respx_mock, answer):
    respx_mock.post("http://rag.test/ask").respond(200, json=answer)
    result = rag.client.post(_ASK, json={"query": _QUERY})
    assert result.status_code == 202
    assert result.headers["cache-control"] == "no-store"
    assert set(result.json()) == {"review_id", "review_token"}
    case_id = result.json()["review_id"]
    rag.clock.now = rag.store.cases[case_id].created_at
    ticket = {"Authorization": f"Bearer {result.json()['review_token']}"}
    return case_id, ticket


def test_allowed_response_is_saved_then_rebuilt_without_fragment_or_upstream_report(
    rag, respx_mock
):
    rag.policy.write_text(
        json.dumps(asdict(RagPolicy("rag", "2", allow_complete=True))), encoding="utf-8"
    )
    upstream = respx_mock.post("http://rag.test/ask").respond(200, json=_answer())

    result = rag.client.post(
        _ASK, json={"query": f" {_QUERY} ", "topk": 4, "stream": False}
    )

    assert result.status_code == 200
    assert result.headers["cache-control"] == "no-store"
    assert json.loads(upstream.calls.last.request.content) == {
        "query": _QUERY,
        "topk": 4,
    }
    assert result.json()["used_refs_report"] == "[1] Synthetic title"
    assert "text" not in result.json()["blocks"][0]
    assert _BLOCK_TEXT not in result.text
    assert _UPSTREAM_DETAIL not in result.text
    record = next(iter(rag.store.assessments.values()))
    assert record.assessment.reason_codes == ("RAG_STRUCTURALLY_COMPLETE",)
    assert record.policy.version == "2"
    assert not rag.store.cases


def test_missing_positions_are_protected_and_correction_releases_only_corrected_output(
    rag, respx_mock
):
    big = "9" * 4500
    case_id, ticket = _ask_review(
        rag, respx_mock, _answer(answer_text=f"[\u0661] [01] [42] [0] [{big}]")
    )
    detail_url = f"/api/v1/review-cases/{case_id}"
    release_url = f"/api/v1/governed/rag/reviews/{case_id}"

    assert rag.client.get(release_url).status_code == 403
    assert rag.client.get(release_url, headers=ticket).json() == {"review_id": case_id}
    assert rag.client.get(detail_url).status_code == 401
    assert (
        rag.client.get(detail_url, headers={"X-Admin-Token": "fake"}).status_code == 403
    )
    detail = rag.client.get(detail_url, headers=_ADMIN)
    assert detail.status_code == 200
    assert detail.json()["reason_codes"] == ["RAG_CITATION_WITHOUT_BLOCK"]
    assert detail.json()["rag"]["cited_positions"] == [1]
    assert detail.json()["rag"]["unresolved_positions"] == ["42", "0", big]
    assert detail.json()["rag"]["blocks"][0]["text"] == _BLOCK_TEXT
    assert _BLOCK_TEXT not in repr(rag.store.cases[case_id])
    assert _QUERY not in repr(rag.store.assessments)

    resolution_url = f"{detail_url}/resolution"
    unreasoned = rag.client.post(
        resolution_url, headers=_ADMIN, json={"outcome": "approve"}
    )
    assert unreasoned.json()["code"] == "JUSTIFICATION_REQUIRED"
    invalid = rag.client.post(
        resolution_url,
        headers=_ADMIN,
        json={
            "outcome": "modify",
            "answer_text": "Still unresolved [2]",
            "justification": "Test",
        },
    )
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "INVALID_RAG_CORRECTION"
    wrong_kind = rag.client.post(
        resolution_url,
        headers=_ADMIN,
        json={"outcome": "modify", "qa_pairs": [{"question": "Q", "answer": "A"}]},
    )
    assert wrong_kind.json()["code"] == "INVALID_REQUEST"
    assert not rag.store.resolutions
    fixed = "Corrected synthetic answer [1]."
    request = {"outcome": "modify", "answer_text": fixed, "justification": "Test"}
    assert (
        rag.client.post(resolution_url, headers=_ADMIN, json=request).status_code == 201
    )
    assert (
        rag.client.post(resolution_url, headers=_ADMIN, json=request).status_code == 200
    )
    result = rag.client.get(release_url, headers=ticket)
    assert result.status_code == 200
    assert result.json()["answer_text"] == fixed
    assert _BLOCK_TEXT not in result.text
    assert rag.store.cases[case_id].content.answer_text != fixed
    assert rag.store.resolutions[case_id].modified_content.answer_text == fixed

    original_resolution = rag.store.resolutions[case_id]
    rag.store.resolutions[case_id] = replace(
        original_resolution,
        modified_content=RetainedQaContent((RetainedQaPair("Q", "A"),), ()),
    )
    assert rag.client.get(release_url, headers=ticket).status_code == 403


def test_justified_approval_reports_only_resolved_positions(rag, respx_mock):
    answer = "Synthetic answer [2] and [1]."
    case_id, ticket = _ask_review(rag, respx_mock, _answer(answer_text=answer))
    resolution = rag.client.post(
        f"/api/v1/review-cases/{case_id}/resolution",
        headers=_ADMIN,
        json={"outcome": "approve", "answer_text": None, "justification": "Test"},
    )

    assert resolution.status_code == 201
    result = rag.client.get(f"/api/v1/governed/rag/reviews/{case_id}", headers=ticket)
    assert result.status_code == 200
    assert result.json()["answer_text"] == answer
    assert result.json()["used_refs_report"] == "[1] Synthetic title"


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (_answer(), "RAG_REVIEW_REQUIRED"),
        (_answer(answer_text="Uncited"), "RAG_CITATIONS_MISSING"),
    ],
)
def test_retention_reason_and_rejection_prevent_release(
    rag, respx_mock, answer, reason
):
    case_id, ticket = _ask_review(rag, respx_mock, answer)
    resolution = rag.client.post(
        f"/api/v1/review-cases/{case_id}/resolution",
        headers=_ADMIN,
        json={"outcome": "reject", "justification": "Synthetic review"},
    )

    assert resolution.status_code == 201
    assert next(iter(rag.store.assessments.values())).assessment.reason_codes == (
        reason,
    )
    assert (
        rag.client.get(
            f"/api/v1/governed/rag/reviews/{case_id}", headers=ticket
        ).status_code
        == 403
    )


@pytest.mark.parametrize(
    ("upstream", "reason"),
    [
        (httpx.Response(500, text=_UPSTREAM_DETAIL), "UPSTREAM_CONTRACT_INVALID"),
        (httpx.Response(200, text="malformed"), "UPSTREAM_CONTRACT_INVALID"),
        (
            httpx.Response(200, json=_answer(query="Other query")),
            "UPSTREAM_CONTRACT_INVALID",
        ),
        (
            httpx.Response(200, json=_answer(blocks=[], answer_text="Abstention")),
            "RAG_NO_SOURCES",
        ),
        (httpx.ReadTimeout("Synthetic timeout"), "UPSTREAM_CONTRACT_INVALID"),
    ],
)
def test_denial_is_audited_without_generated_content(rag, respx_mock, upstream, reason):
    respx_mock.post("http://rag.test/ask").mock(side_effect=[upstream])

    result = rag.client.post(_ASK, json={"query": _QUERY})

    assert result.status_code == 403
    assert result.json() == {
        "code": "RAG_DENIED",
        "trace_id": result.headers["x-trace-id"],
    }
    assert result.headers["cache-control"] == "no-store"
    assert next(iter(rag.store.assessments.values())).assessment.reason_codes == (
        reason,
    )
    assert _UPSTREAM_DETAIL not in result.text
    assert not rag.store.cases


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"query": "  "}, "INVALID_REQUEST"),
        ({"query": _QUERY, "topk": True}, "INVALID_REQUEST"),
        ({"query": _QUERY, "stream": True}, "RAG_STREAMING_UNSUPPORTED"),
    ],
)
def test_invalid_input_and_streaming_never_call_upstream(
    rag, respx_mock, payload, code
):
    result = rag.client.post(_ASK, json=payload)

    assert result.status_code == 422
    assert result.json()["code"] == code
    assert result.headers["cache-control"] == "no-store"
    assert not respx_mock.calls


def test_review_release_denied_when_case_missing_expired_or_wrong_kind(rag, respx_mock):
    case_id, ticket = _ask_review(rag, respx_mock, _answer())
    release_url = f"/api/v1/governed/rag/reviews/{case_id}"
    assert (
        rag.client.get(
            release_url, headers={"Authorization": "Bearer invalid"}
        ).status_code
        == 403
    )
    original = rag.store.cases.pop(case_id)
    assert rag.client.get(release_url, headers=ticket).status_code == 403
    rag.store.cases[case_id] = replace(
        original, content=RetainedQaContent((RetainedQaPair("Q", "A"),), ())
    )
    assert rag.client.get(release_url, headers=ticket).status_code == 403
    wrong_modification = rag.client.post(
        f"/api/v1/review-cases/{case_id}/resolution",
        headers=_ADMIN,
        json={"outcome": "modify", "answer_text": "[1]", "justification": "Test"},
    )
    assert wrong_modification.json()["code"] == "INVALID_REQUEST"
    rag.store.cases[case_id] = original
    rag.clock.now = original.created_at + timedelta(days=1)
    assert rag.client.get(release_url, headers=ticket).status_code == 403


def test_storage_failure_and_absent_route_dependencies_fail_closed(rag, respx_mock):
    rag.store.fail_save = True
    respx_mock.post("http://rag.test/ask").respond(200, json=_answer())

    result = rag.client.post(_ASK, json={"query": _QUERY})
    disabled = TestClient(create_app(Settings(_env_file=None)))

    assert result.status_code == 403
    assert _UPSTREAM_DETAIL not in result.text
    assert not rag.store.assessments
    assert not rag.store.cases
    assert disabled.post(_ASK, json={"query": _QUERY}).status_code == 403
    assert disabled.get(f"/api/v1/governed/rag/reviews/{uuid4()}").status_code == 403


@pytest.mark.parametrize(
    "policy",
    [
        '{"policy_id":"rag","version":"1","version":"2"}',
        '{"policy_id":"rag","version":"1","allow_complete":"true"}',
        '{"policy_id":"rag","version":"1","expected_pairs":1}',
        '{"policy_id":"","version":"1"}',
    ],
)
def test_rag_policy_rejects_invalid_documents(tmp_path, policy):
    path = tmp_path / "rag.json"
    path.write_text(policy, encoding="utf-8")

    with pytest.raises(InvalidPolicyError, match="Invalid policy file"):
        JsonRagPolicyRepository(path).load()


def test_rag_policy_flag_requires_a_boolean():
    with pytest.raises(ValueError, match="must be a boolean"):
        RagPolicy("rag", "1", allow_complete=1)
