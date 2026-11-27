import json
import sqlite3
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.application.explain_decision import ExplainDecision
from app.core.config import Settings
from app.infrastructure.sqlite_assessment_repository import SqliteReviewCaseRepository
from app.main import create_app, create_qa_assessor

_ADMIN = {"X-Admin-Token": "synthetic-admin-session"}
_VERIFIER = "http://search.test/internal/auth/validate-admin-token/"
_PRIVATE_ANSWER = "Synthetic retained answer text"
_PRIVATE_QUESTION = "Synthetic retained question text"
_SOURCE = "synthetic-article"
_BODY = {
    "status": "success",
    "qa_pairs": [{"question": _PRIVATE_QUESTION, "answer": _PRIVATE_ANSWER}],
    "source_docs": [{"article_id": _SOURCE}],
}


@pytest.fixture
def setup(tmp_path):
    settings = Settings(
        _env_file=None,
        database=tmp_path / "decisions.sqlite3",
        admin_verifier_base_url="http://search.test",
    )
    policy_path = tmp_path / "policy.json"
    policy = {"policy_id": "synthetic-qa", "version": "1", "expected_pairs": 2}
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    assessor = create_qa_assessor(policy_path, settings.database)
    return SimpleNamespace(
        policy_path=policy_path,
        settings=settings,
        assessor=assessor,
        client=TestClient(create_app(settings)),
    )


def _url(assessment_id):
    return f"/api/v1/assessments/{assessment_id}/explanation"


def test_explanation_uses_original_decision_after_policy_change_and_reopen(
    setup, respx_mock
):
    record = setup.assessor.execute(_BODY)
    setup.policy_path.write_text(
        json.dumps({"policy_id": "synthetic-qa", "version": "2", "expected_pairs": 8}),
        encoding="utf-8",
    )
    reopened = TestClient(create_app(setup.settings))
    respx_mock.get(_VERIFIER).respond(204)
    resolved = reopened.post(
        f"/api/v1/review-cases/{record.review_case_id}/resolution",
        headers=_ADMIN,
        json={"outcome": "reject", "justification": "Synthetic resolution"},
    )
    assert resolved.status_code == 201

    result = reopened.get(_url(record.assessment_id), headers=_ADMIN)

    assert result.status_code == 200
    assert result.headers["cache-control"] == "no-store"
    assert result.json() == {
        "assessment_id": record.assessment_id,
        "review_case_id": record.review_case_id,
        "created_at": record.created_at.isoformat(),
        "case_type": "qa",
        "verdict": "suspend",
        "reason_codes": ["HUMAN_REVIEW_REQUIRED", "PARTIAL_GENERATION"],
        "policy": {
            "policy_id": "synthetic-qa",
            "version": "1",
            "parameters": {"expected_pairs": 2, "allow_complete": False},
        },
        "applied_parameters": None,
        "evidence": {"pair_count": 1, "source_article_ids": [_SOURCE]},
    }
    assert _PRIVATE_QUESTION not in result.text
    assert _PRIVATE_ANSWER not in result.text
    assert "Synthetic resolution" not in result.text


def test_explanation_remains_available_for_a_decision_without_a_case(setup, respx_mock):
    record = setup.assessor.execute({"status": "error", "qa_pairs": []})
    respx_mock.get(_VERIFIER).respond(204)

    result = setup.client.get(_url(record.assessment_id), headers=_ADMIN)

    assert result.status_code == 200
    assert result.json()["verdict"] == "deny"
    assert result.json()["reason_codes"] == ["UPSTREAM_GENERATION_FAILED"]
    assert result.json()["review_case_id"] is None
    assert result.json()["evidence"] is None


def test_authentication_required_before_reading_any_decision(setup, respx_mock):
    record = setup.assessor.execute(_BODY)
    respx_mock.get(_VERIFIER).respond(403)

    anonymous = setup.client.get(_url(record.assessment_id))
    rejected = setup.client.get(_url(record.assessment_id), headers=_ADMIN)

    assert anonymous.status_code == 401
    assert anonymous.headers["www-authenticate"] == "X-Admin-Token"
    assert anonymous.json()["code"] == "AUTHENTICATION_REQUIRED"
    assert rejected.status_code == 403
    assert rejected.json()["code"] == "ADMIN_TOKEN_REJECTED"
    assert rejected.headers["cache-control"] == "no-store"
    assert _SOURCE not in anonymous.text
    assert _SOURCE not in rejected.text


def test_unknown_and_invalid_ids_have_generic_errors(setup, respx_mock):
    respx_mock.get(_VERIFIER).respond(204)
    unknown = setup.client.get(_url(uuid4()), headers=_ADMIN)
    malformed = setup.client.get(_url("synthetic-invalid-id"), headers=_ADMIN)

    assert unknown.status_code == 404
    assert unknown.json()["code"] == "ASSESSMENT_NOT_FOUND"
    assert malformed.status_code == 422
    assert malformed.json()["code"] == "INVALID_REQUEST"
    assert "synthetic-invalid-id" not in malformed.text
    assert malformed.headers["cache-control"] == "no-store"


def test_unavailable_explanation_hides_driver_details(setup, respx_mock):
    respx_mock.get(_VERIFIER).respond(204)
    record = setup.assessor.execute(_BODY)

    def unavailable(_assessment_id):
        raise sqlite3.OperationalError(_PRIVATE_ANSWER)

    setup.client.app.state.decision_explainer = SimpleNamespace(execute=unavailable)
    without_store = TestClient(
        create_app(
            Settings(_env_file=None, admin_verifier_base_url="http://search.test")
        )
    )

    for client in (setup.client, without_store):
        result = client.get(_url(record.assessment_id), headers=_ADMIN)
        assert result.status_code == 503
        assert result.json()["code"] == "EXPLANATION_UNAVAILABLE"
        assert result.headers["cache-control"] == "no-store"
        assert _PRIVATE_ANSWER not in result.text


def test_removed_case_leaves_decision_and_omits_evidence(setup, respx_mock):
    respx_mock.get(_VERIFIER).respond(204)
    record = setup.assessor.execute(_BODY)
    setup.client.app.state.decision_explainer = ExplainDecision(
        SimpleNamespace(get=lambda _: record), SimpleNamespace(get=lambda _: None)
    )

    result = setup.client.get(_url(record.assessment_id), headers=_ADMIN)

    assert result.status_code == 200
    assert result.json()["review_case_id"] == record.review_case_id
    assert result.json()["evidence"] is None
    assert _SOURCE not in result.text


def test_inconsistent_links_never_expose_a_different_case(setup, respx_mock):
    respx_mock.get(_VERIFIER).respond(204)
    record = setup.assessor.execute(_BODY)
    case = SqliteReviewCaseRepository(setup.settings.database).get(
        record.review_case_id
    )
    for returned_record, returned_case in (
        (replace(record, assessment_id=str(uuid4())), case),
        (record, replace(case, assessment_id=str(uuid4()))),
        (record, replace(case, case_id=str(uuid4()))),
    ):
        setup.client.app.state.decision_explainer = ExplainDecision(
            SimpleNamespace(get=lambda _, value=returned_record: value),
            SimpleNamespace(get=lambda _, value=returned_case: value),
        )
        result = setup.client.get(_url(record.assessment_id), headers=_ADMIN)
        assert result.status_code == 503
        assert result.json()["code"] == "EXPLANATION_UNAVAILABLE"
        assert _SOURCE not in result.text
