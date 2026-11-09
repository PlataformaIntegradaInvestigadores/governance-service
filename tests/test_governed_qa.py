import hashlib
import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.application.supervision import ManageSupervisors
from app.core.config import Settings
from app.domain.qa import QaPolicy
from app.domain.resolution import ReviewOutcome
from app.infrastructure.sqlite_assessment_repository import _SCHEMA
from app.infrastructure.sqlite_supervisor_directory import SqliteSupervisorDirectory
from app.main import create_app, create_qa_assessor

_GENERATE = "/api/v1/governed/qa/generate"
_UPSTREAM = "http://search.test/api-se/v1/llm-search/generate-qa/"
_REQUEST = {"query": "Synthetic question", "documents": [{"article_id": 1}]}
_ANSWER = "Synthetic private answer"
_QA = {
    "status": "success",
    "qa_pairs": [{"question": "Synthetic question", "answer": _ANSWER}],
    "source_docs": [{"article_id": 1, "title": "Internal title"}],
    "raw_response": "Synthetic unfiltered output",
}


@pytest.fixture
def settings(tmp_path):
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps({"policy_id": "qa", "version": "1", "expected_pairs": 1})
    )
    return Settings(
        _env_file=None,
        identity_base_url="http://identity.test",
        database=tmp_path / "governance.sqlite3",
        qa_upstream_base_url="http://search.test",
        qa_timeout_seconds=2,
        qa_policy_path=policy,
        qa_release_key="synthetic-test-material-of-at-least-32-bytes",
    )


@pytest.fixture
def client(settings):
    return TestClient(create_app(settings))


def _request_review(client, respx_mock):
    upstream = respx_mock.post(_UPSTREAM).respond(json=_QA)
    response = client.post(_GENERATE, json=_REQUEST)
    assert json.loads(upstream.calls.last.request.content) == _REQUEST
    assert upstream.calls.last.request.extensions["timeout"]["read"] == 2
    assert response.status_code == 202
    assert response.headers["cache-control"] == "no-store"
    assert _ANSWER not in response.text
    review = response.json()
    assert set(review) == {"review_id", "review_token"}
    return review["review_id"], {"Authorization": f"Bearer {review['review_token']}"}


def _audit(database):
    with sqlite3.connect(database) as connection:
        return (
            connection.execute(
                "SELECT verdict, reason_codes_json FROM assessments"
            ).fetchall(),
            connection.execute("SELECT COUNT(*) FROM review_cases").fetchone()[0],
        )


@pytest.mark.parametrize("outcome", list(ReviewOutcome))
def test_review_is_private_and_only_authorized_resolution_releases(
    client, settings, respx_mock, outcome, monkeypatch, caplog
):
    review_id, headers = _request_review(client, respx_mock)
    path = f"/api/v1/governed/qa/reviews/{review_id}"
    assert client.get(path).status_code == 403
    assert (
        client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 403
    )
    assert client.get(path, headers=headers).json() == {"review_id": review_id}
    another = create_qa_assessor(settings.qa_policy_path, settings.database).execute(
        _QA
    )
    assert (
        client.get(
            f"/api/v1/governed/qa/reviews/{another.review_case_id}", headers=headers
        ).status_code
        == 403
    )

    # Reload the app to exercise persisted decisions and stable credentials.
    restarted = TestClient(create_app(settings))
    reviews = restarted.app.state.review_cases
    if outcome is ReviewOutcome.EXPIRED:
        view = reviews.view(review_id)
        monkeypatch.setattr(reviews, "_clock", lambda: view.expires_at)
    else:
        resolution = {"outcome": outcome}
        if outcome is ReviewOutcome.MODIFY:
            resolution["justification"] = "Synthetic reason"
            resolution["qa_pairs"] = [
                {"question": "Corrected question", "answer": "Corrected answer"}
            ]
        resolution_path = f"/api/v1/review-cases/{review_id}/resolution"
        assert restarted.post(resolution_path, json=resolution).status_code == 401
        directory = SqliteSupervisorDirectory(settings.database)
        ManageSupervisors(directory).enable("reviewer", "Synthetic reviewer")
        respx_mock.get("http://identity.test/internal/auth/validate-token/").respond(
            204,
            headers={
                "X-Authenticated-User-Id": "reviewer",
                "X-Authenticated-Token-Type": "access",
            },
        )
        assert (
            restarted.post(
                resolution_path,
                json=resolution,
                headers={"Authorization": "Bearer synthetic-supervisor"},
            ).status_code
            == 201
        )

    result = restarted.get(path, headers=headers)
    assert result.headers["cache-control"] == "no-store"
    assert _ANSWER not in caplog.text
    assert headers["Authorization"] not in caplog.text
    if outcome in (ReviewOutcome.APPROVE, ReviewOutcome.MODIFY):
        assert result.status_code == 200
        answer = _ANSWER if outcome is ReviewOutcome.APPROVE else "Corrected answer"
        assert result.json()["qa_pairs"][0]["answer"] == answer
        assert result.json()["source_docs"] == [{"article_id": "1"}]
    else:
        assert result.status_code == 403
        assert _ANSWER not in result.text


def test_complete_output_is_allowed_only_by_explicit_version(
    client, settings, respx_mock
):
    settings.qa_policy_path.write_text(
        json.dumps(
            {
                "policy_id": "qa",
                "version": "2",
                "expected_pairs": 1,
                "allow_complete": True,
            }
        )
    )
    respx_mock.post(_UPSTREAM).respond(json=_QA)
    assert client.get("/api/v1/status").json()["governance_enabled"] is True
    response = client.post(_GENERATE, json=_REQUEST)
    assert response.status_code == 200
    assert response.json() == {
        "status": "success",
        "qa_pairs": _QA["qa_pairs"],
        "source_docs": [{"article_id": "1"}],
    }
    assert _audit(settings.database) == (
        [("allow", '["COMPLETE_GENERATION_ALLOWED"]')],
        0,
    )


@pytest.mark.parametrize(
    ("upstream_response", "reason"),
    [
        (httpx.Response(503, text=_ANSWER), "UPSTREAM_GENERATION_FAILED"),
        (
            httpx.Response(302, headers={"Location": "http://elsewhere.test"}),
            "UPSTREAM_GENERATION_FAILED",
        ),
        (httpx.Response(200, text=_ANSWER), "INVALID_UPSTREAM_RESPONSE"),
        (
            httpx.Response(200, content=b" " * (1024**2 + 1)),
            "INVALID_UPSTREAM_RESPONSE",
        ),
        (
            httpx.Response(200, text=r'{"invalid":"\ud800"}'),
            "INVALID_UPSTREAM_RESPONSE",
        ),
        (httpx.Response(200, json={"qa_pairs": _ANSWER}), "INVALID_UPSTREAM_RESPONSE"),
        (
            httpx.Response(200, json={"status": "error", "qa_pairs": []}),
            "UPSTREAM_GENERATION_FAILED",
        ),
    ],
)
def test_invalid_or_failed_upstream_denies_and_audits(
    client, settings, respx_mock, upstream_response, reason
):
    respx_mock.post(_UPSTREAM).mock(return_value=upstream_response)
    result = client.post(_GENERATE, json=_REQUEST)
    assert result.status_code == 403
    assert result.json() == {
        "code": "QA_DENIED",
        "trace_id": result.headers["x-trace-id"],
    }
    assert _audit(settings.database) == ([("deny", json.dumps([reason]))], 0)


@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ReadTimeout])
def test_upstream_transport_failure_denies_and_audits(
    client, settings, respx_mock, failure
):
    respx_mock.post(_UPSTREAM).mock(side_effect=failure("Synthetic upstream failure"))
    assert client.post(_GENERATE, json=_REQUEST).status_code == 403
    assert _audit(settings.database) == (
        [("deny", '["UPSTREAM_GENERATION_FAILED"]')],
        0,
    )


def test_failed_case_write_leaves_no_partial_audit_or_output(
    client, settings, respx_mock
):
    with sqlite3.connect(settings.database) as connection:
        connection.execute("""CREATE TRIGGER fail_case BEFORE INSERT ON review_cases
            BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END""")
    respx_mock.post(_UPSTREAM).respond(json=_QA)
    result = client.post(_GENERATE, json=_REQUEST)
    assert result.status_code == 403
    assert _ANSWER not in result.text
    assert _audit(settings.database) == ([], 0)
    with sqlite3.connect(settings.database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM policies").fetchone()[0] == 0


def test_missing_policy_fails_closed(client, settings, respx_mock):
    settings.qa_policy_path.unlink()
    respx_mock.post(_UPSTREAM).respond(json=_QA)
    assert client.post(_GENERATE, json=_REQUEST).status_code == 403
    assert _audit(settings.database) == ([], 0)


def test_corrupt_retained_output_denies_polling(client, settings, respx_mock):
    review_id, headers = _request_review(client, respx_mock)
    with sqlite3.connect(settings.database) as connection:
        connection.execute("DROP TRIGGER review_cases_no_update")
        connection.execute("UPDATE review_cases SET content_sha256 = 'corrupted'")
    result = client.get(f"/api/v1/governed/qa/reviews/{review_id}", headers=headers)
    assert result.status_code == 403
    assert _ANSWER not in result.text


def test_disabled_route_denies_and_invalid_requests_do_not_echo_content():
    client = TestClient(create_app(Settings(_env_file=None)))
    assert client.post(_GENERATE, json=_REQUEST).status_code == 403
    assert (
        client.get(
            "/api/v1/governed/qa/reviews/5cf90677-17a5-48c8-89f1-14cd3c38fa13"
        ).status_code
        == 403
    )
    invalid = client.post(_GENERATE, json=[_ANSWER])
    assert invalid.status_code == 422
    assert _ANSWER not in invalid.text


@pytest.mark.parametrize(
    "base_url", ["http://search.test/subpath", "http://user@search.test"]
)
def test_configuration_rejects_non_origin_upstream(base_url):
    with pytest.raises(ValidationError, match="upstream origin"):
        Settings(_env_file=None, qa_upstream_base_url=base_url)


def test_upstream_requires_supervision_and_release_key():
    with pytest.raises(ValidationError, match="requires supervision"):
        Settings(_env_file=None, qa_upstream_base_url="http://search.test")


def test_policy_rejects_non_boolean_allow_flag():
    with pytest.raises(ValueError, match="must be a boolean"):
        QaPolicy("qa", "2", 1, allow_complete="true")


def test_migrates_legacy_audit_and_preserves_pending_case(settings, respx_mock):
    with sqlite3.connect(settings.database) as connection:
        connection.executescript(_SCHEMA.replace("'suspend', 'allow'", "'suspend'"))
        snapshot = '{"expected_pairs":1,"policy_id":"qa","version":"1"}'
        connection.execute(
            "INSERT INTO policies VALUES ('qa', '1', ?, ?)",
            (snapshot, hashlib.sha256(snapshot.encode()).hexdigest()),
        )
        connection.execute(
            "INSERT INTO assessments VALUES "
            "('legacy', '2026-11-08T00:00:00+00:00', 'qa', '1', 'suspend', '[]')"
        )
        connection.execute(
            "INSERT INTO review_cases VALUES "
            "('legacy-case', 'legacy', '2026-11-08T00:00:00+00:00', '{}', 'fixture')"
        )

    client = TestClient(create_app(settings))
    _request_review(client, respx_mock)
    with sqlite3.connect(settings.database) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT case_id FROM review_cases WHERE assessment_id = 'legacy'"
        ).fetchone() == ("legacy-case",)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM assessments WHERE assessment_id = 'legacy'")
