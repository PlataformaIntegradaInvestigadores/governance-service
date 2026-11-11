import json
import logging
import sqlite3
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app.application.resolve_review import ReviewCases
from app.core.config import Settings
from app.domain.qa import Assessment, QaPolicy, RecordedAssessment, Verdict
from app.domain.resolution import (
    JustificationRequiredError,
    ReviewCaseAlreadyResolvedError,
    ReviewCaseExpiredError,
    ReviewCaseNotFoundError,
    ReviewOutcome,
    ReviewResolution,
    justification_required,
)
from app.domain.review import (
    RetainedQaContent,
    RetainedQaPair,
    ReviewCase,
    ReviewCaseIntegrityError,
)
from app.domain.supervision import ADMIN_SUPERVISOR, Supervisor
from app.infrastructure.sqlite_assessment_repository import (
    SqliteAssessmentRepository,
    SqliteReviewCaseRepository,
    SqliteReviewResolutionRepository,
)
from app.main import create_app, create_qa_assessor, create_review_cases

WINDOW = timedelta(hours=24)
ANA = Supervisor("U000000001", "Ana Reviewer")
LUIS = Supervisor("U000000002", "Luis Reviewer")
TOKEN = "synthetic-access-token"  # noqa: S105 - test value, never a real credential
RETAINED_ANSWER = "Synthetic retained answer"


def payload(pairs=3, sources=("source-1",)):
    return {
        "status": "success",
        "qa_pairs": [
            {"question": f"Question {i}", "answer": RETAINED_ANSWER}
            for i in range(pairs)
        ],
        "source_docs": [{"article_id": article_id} for article_id in sources],
    }


class Clock:
    """Real time plus an offset, so cases created during a test stay ordered."""

    def __init__(self):
        self.offset = timedelta(0)
        self.fixed = None

    def __call__(self):
        return self.fixed if self.fixed is not None else datetime.now(UTC) + self.offset


@pytest.fixture
def database(tmp_path):
    return tmp_path / "governance.sqlite3"


@pytest.fixture
def assess(tmp_path, database):
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(asdict(QaPolicy("qa-review", "draft-1", 3))))
    return create_qa_assessor(policy, database).execute


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def reviews(database, clock):
    return ReviewCases(
        SqliteAssessmentRepository(database),
        SqliteReviewCaseRepository(database),
        SqliteReviewResolutionRepository(database),
        WINDOW,
        clock,
    )


def count_resolutions(database):
    with sqlite3.connect(database) as connection:
        return connection.execute("SELECT COUNT(*) FROM review_resolutions").fetchone()[
            0
        ]


# Domain rules


@pytest.mark.parametrize(
    ("outcome", "reasons", "required"),
    [
        (ReviewOutcome.APPROVE, ("HUMAN_REVIEW_REQUIRED",), False),
        (ReviewOutcome.APPROVE, ("HUMAN_REVIEW_REQUIRED", "PARTIAL_GENERATION"), True),
        (ReviewOutcome.APPROVE, ("SOURCE_EVIDENCE_INCOMPLETE",), True),
        (ReviewOutcome.MODIFY, ("HUMAN_REVIEW_REQUIRED",), True),
        (ReviewOutcome.REJECT, ("PARTIAL_GENERATION",), False),
    ],
)
def test_justification_is_required_when_overriding_the_evaluator(
    outcome, reasons, required
):
    assert justification_required(outcome, reasons) is required


NOW = datetime(2026, 11, 8, 12, tzinfo=UTC)
CONTENT = RetainedQaContent((RetainedQaPair("Q", "A"),), ())


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"resolved_at": datetime(2026, 11, 8)}, "timezone-aware"),  # noqa: DTZ001
        ({"outcome": ReviewOutcome.EXPIRED}, "without a supervisor"),
        ({"supervisor": None}, "without a supervisor"),
        ({"outcome": ReviewOutcome.MODIFY}, "only for modify"),
        ({"modified_content": CONTENT}, "only for modify"),
        ({"justification": "  "}, "nonblank"),
        ({"justification": "x" * 2001}, "at most 2000"),
    ],
)
def test_resolution_invariants(changes, message):
    fields = {
        "resolution_id": "r1",
        "case_id": "c1",
        "outcome": ReviewOutcome.APPROVE,
        "resolved_at": NOW,
        "expires_at": NOW + WINDOW,
        "supervisor": ANA,
    }
    with pytest.raises(ValueError, match=message):
        ReviewResolution(**(fields | changes))


def test_justification_is_redacted_from_repr():
    resolution = ReviewResolution(
        "r1", "c1", ReviewOutcome.REJECT, NOW, NOW + WINDOW, ANA, "Private reason"
    )
    assert "Private reason" not in repr(resolution)


def test_replay_matches_by_identity_not_by_snapshot():
    first = ReviewResolution("r1", "c1", ReviewOutcome.REJECT, NOW, NOW, ANA)
    renamed = replace(
        first, resolution_id="r2", supervisor=replace(ANA, display_name="Ana M.")
    )
    assert first.matches(renamed)
    assert not first.matches(replace(first, supervisor=LUIS))


def test_review_window_must_be_positive(database):
    with pytest.raises(ValueError, match="positive"):
        ReviewCases(None, None, None, timedelta(0))


# Resolving cases


def test_approval_of_clean_output_needs_no_justification_and_persists(
    assess, reviews, database
):
    record = assess(payload())

    result = reviews.resolve(record.review_case_id, ANA, ReviewOutcome.APPROVE)

    assert result.created
    stored = SqliteReviewResolutionRepository(database).get(record.review_case_id)
    assert stored == result.resolution
    assert stored.supervisor == ANA
    assert (
        stored.expires_at
        == SqliteReviewCaseRepository(database).get(record.review_case_id).created_at
        + WINDOW
    )


def test_approving_flagged_output_without_justification_stores_nothing(
    assess, reviews, database
):
    record = assess(payload(pairs=1))
    with pytest.raises(JustificationRequiredError):
        reviews.resolve(record.review_case_id, ANA, ReviewOutcome.APPROVE, "  ")
    assert count_resolutions(database) == 0

    result = reviews.resolve(
        record.review_case_id, ANA, ReviewOutcome.APPROVE, " Checked manually "
    )
    assert result.resolution.justification == "Checked manually"


def test_modification_keeps_original_sources_and_requires_justification(
    assess, reviews, database
):
    record = assess(payload(sources=("source-1", "source-2")))
    pairs = (RetainedQaPair("Edited question", "Edited answer"),)
    with pytest.raises(JustificationRequiredError):
        reviews.resolve(record.review_case_id, ANA, ReviewOutcome.MODIFY, None, pairs)

    reviews.resolve(record.review_case_id, ANA, ReviewOutcome.MODIFY, "Fixed", pairs)

    stored = SqliteReviewResolutionRepository(database).get(record.review_case_id)
    assert stored.modified_content == RetainedQaContent(pairs, ("source-1", "source-2"))


def test_modify_without_pairs_is_invalid(assess, reviews):
    record = assess(payload())
    with pytest.raises(ValueError, match="only for modify"):
        reviews.resolve(record.review_case_id, ANA, ReviewOutcome.MODIFY, "Fixed")


def test_supervisors_cannot_record_an_expiry(assess, reviews):
    record = assess(payload())
    with pytest.raises(ValueError, match="recorded by the service"):
        reviews.resolve(record.review_case_id, ANA, ReviewOutcome.EXPIRED)


def test_rejection_needs_no_justification(assess, reviews):
    record = assess(payload(pairs=1))
    result = reviews.resolve(record.review_case_id, ANA, ReviewOutcome.REJECT)
    assert result.resolution.outcome is ReviewOutcome.REJECT


def test_repeated_request_returns_the_original_resolution(assess, reviews, database):
    record = assess(payload())
    first = reviews.resolve(record.review_case_id, ANA, ReviewOutcome.REJECT)

    renamed = replace(ANA, display_name="Ana M. Reviewer")
    again = reviews.resolve(record.review_case_id, renamed, ReviewOutcome.REJECT)

    assert not again.created
    assert again.resolution == first.resolution
    assert count_resolutions(database) == 1


@pytest.mark.parametrize(
    ("supervisor", "outcome"),
    [(ANA, ReviewOutcome.APPROVE), (LUIS, ReviewOutcome.REJECT)],
)
def test_resolutions_are_final(assess, reviews, database, supervisor, outcome):
    record = assess(payload())
    reviews.resolve(record.review_case_id, ANA, ReviewOutcome.REJECT)
    with pytest.raises(ReviewCaseAlreadyResolvedError):
        reviews.resolve(record.review_case_id, supervisor, outcome)
    assert count_resolutions(database) == 1


def test_unknown_case_is_not_found(reviews):
    with pytest.raises(ReviewCaseNotFoundError):
        reviews.resolve("unknown", ANA, ReviewOutcome.REJECT)
    with pytest.raises(ReviewCaseNotFoundError):
        reviews.view("unknown")


# Expiry


def test_late_decision_records_expiry_and_denies(assess, reviews, database, clock):
    record = assess(payload())
    case = SqliteReviewCaseRepository(database).get(record.review_case_id)
    clock.fixed = case.created_at + WINDOW

    with pytest.raises(ReviewCaseExpiredError):
        reviews.resolve(record.review_case_id, ANA, ReviewOutcome.APPROVE)
    with pytest.raises(ReviewCaseExpiredError):
        reviews.resolve(record.review_case_id, ANA, ReviewOutcome.APPROVE)

    stored = SqliteReviewResolutionRepository(database).get(record.review_case_id)
    assert stored.outcome is ReviewOutcome.EXPIRED
    assert stored.supervisor is None
    assert reviews.view(record.review_case_id).expired


def test_viewing_an_overdue_case_reports_expiry_without_writing(
    assess, reviews, database, clock
):
    record = assess(payload())
    assert not reviews.view(record.review_case_id).expired

    clock.offset += WINDOW + timedelta(seconds=1)

    view = reviews.view(record.review_case_id)
    assert view.expired
    assert view.resolution is None
    assert count_resolutions(database) == 0


def saved_case_aged(database, age):
    """A suspended decision and case whose creation time lies in the past."""
    created_at = datetime.now(UTC) - age
    policy = QaPolicy("qa-review", "draft-1", 3)
    record = RecordedAssessment(
        str(uuid4()),
        created_at,
        policy,
        Assessment(Verdict.SUSPEND, "qa-review", "draft-1", ("HUMAN_REVIEW_REQUIRED",)),
        str(uuid4()),
    )
    content = RetainedQaContent((RetainedQaPair("Q", RETAINED_ANSWER),), ())
    case = ReviewCase(record.review_case_id, record.assessment_id, created_at, content)
    SqliteAssessmentRepository(database).save(record, case)
    return case.case_id


def test_sweep_expires_only_overdue_pending_cases(reviews, database):
    old_pending = saved_case_aged(database, WINDOW + timedelta(minutes=1))
    old_resolved = saved_case_aged(database, WINDOW + timedelta(minutes=1))
    new_pending = saved_case_aged(database, timedelta(minutes=1))
    repository = SqliteReviewResolutionRepository(database)
    repository.save(
        ReviewResolution("r1", old_resolved, ReviewOutcome.REJECT, NOW, NOW, ANA)
    )

    assert reviews.expire_overdue() == 1
    assert reviews.expire_overdue() == 0

    assert repository.get(old_pending).outcome is ReviewOutcome.EXPIRED
    assert repository.get(old_resolved).outcome is ReviewOutcome.REJECT
    assert repository.get(new_pending) is None


def test_repository_rejects_a_second_resolution(assess, database):
    case_id = assess(payload()).review_case_id
    repository = SqliteReviewResolutionRepository(database)
    repository.save(
        ReviewResolution("r1", case_id, ReviewOutcome.REJECT, NOW, NOW, ANA)
    )
    with pytest.raises(ReviewCaseAlreadyResolvedError):
        repository.save(
            ReviewResolution("r2", case_id, ReviewOutcome.REJECT, NOW, NOW, ANA)
        )


def test_default_clock_is_used_by_the_composed_workflow(assess, tmp_path, database):
    record = assess(payload())
    settings = Settings(
        _env_file=None,
        admin_verifier_base_url="http://search.test",
        database=database,
    )
    assert not create_review_cases(settings).view(record.review_case_id).expired


# Concurrency and consistency, with doubles around the real repositories


class RacingResolutions:
    """Simulates another request resolving the case between read and insert."""

    def __init__(self, inner, competing):
        self.inner = inner
        self.competing = competing
        self.raced = False

    def get(self, case_id):
        return self.inner.get(case_id) if self.raced else None

    def save(self, resolution):
        if not self.raced:
            self.raced = True
            if self.competing is not None:
                self.inner.save(replace(self.competing, case_id=resolution.case_id))
            raise ReviewCaseAlreadyResolvedError("raced")
        self.inner.save(resolution)

    def pending_case_ids(self, created_before):
        return self.inner.pending_case_ids(created_before)


def racing_reviews(database, clock, competing):
    return ReviewCases(
        SqliteAssessmentRepository(database),
        SqliteReviewCaseRepository(database),
        RacingResolutions(SqliteReviewResolutionRepository(database), competing),
        WINDOW,
        clock,
    )


def test_concurrent_identical_resolution_is_a_replay(assess, database, clock):
    record = assess(payload())
    competing = ReviewResolution(
        "other", "x", ReviewOutcome.REJECT, clock(), clock(), ANA
    )

    result = racing_reviews(database, clock, competing).resolve(
        record.review_case_id, ANA, ReviewOutcome.REJECT
    )

    assert not result.created
    assert result.resolution.resolution_id == "other"


def test_concurrent_conflict_without_a_readable_winner_is_rejected(
    assess, database, clock
):
    record = assess(payload())
    with pytest.raises(ReviewCaseAlreadyResolvedError, match="concurrently"):
        racing_reviews(database, clock, None).resolve(
            record.review_case_id, ANA, ReviewOutcome.REJECT
        )


def test_sweep_skips_a_case_resolved_concurrently(assess, database, clock):
    assess(payload())
    clock.offset += WINDOW + timedelta(minutes=1)
    assert racing_reviews(database, clock, None).expire_overdue() == 0


def test_case_without_assessment_is_not_found(assess, database, clock):
    record = assess(payload())

    class NoAssessments:
        def get(self, assessment_id):
            return None

    reviews = ReviewCases(
        NoAssessments(),
        SqliteReviewCaseRepository(database),
        SqliteReviewResolutionRepository(database),
        WINDOW,
        clock,
    )
    with pytest.raises(ReviewCaseNotFoundError):
        reviews.view(record.review_case_id)


# SQLite storage


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE review_resolutions SET outcome = 'approve'",
        "DELETE FROM review_resolutions",
    ],
)
def test_resolutions_are_append_only(assess, reviews, database, statement):
    record = assess(payload())
    reviews.resolve(record.review_case_id, ANA, ReviewOutcome.REJECT)
    with (
        sqlite3.connect(database) as connection,
        pytest.raises(sqlite3.IntegrityError, match="append-only"),
    ):
        connection.execute(statement)


def test_database_rejects_an_expiry_with_a_supervisor(assess, database):
    record = assess(payload())
    with (
        sqlite3.connect(database) as connection,
        pytest.raises(sqlite3.IntegrityError, match="CHECK"),
    ):
        connection.execute(
            "INSERT INTO review_resolutions VALUES "
            "('r1', ?, 'expired', 't', 't', 'U1', '{}', NULL, NULL, NULL)",
            (record.review_case_id,),
        )


def test_resolution_for_unknown_case_is_rejected_by_foreign_key(database):
    repository = SqliteReviewResolutionRepository(database)
    resolution = ReviewResolution("r1", "missing", ReviewOutcome.REJECT, NOW, NOW, ANA)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        repository.save(resolution)


def test_tampered_modified_content_is_detected(assess, reviews, database):
    record = assess(payload())
    pairs = (RetainedQaPair("Edited", RETAINED_ANSWER),)
    reviews.resolve(record.review_case_id, ANA, ReviewOutcome.MODIFY, "Fixed", pairs)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER review_resolutions_no_update")
        connection.execute(
            "UPDATE review_resolutions SET modified_content_json = "
            "replace(modified_content_json, 'Edited', 'Altered')"
        )
    with pytest.raises(ReviewCaseIntegrityError) as raised:
        SqliteReviewResolutionRepository(database).get(record.review_case_id)
    assert RETAINED_ANSWER not in str(raised.value)


# HTTP routes


VERIFIER_URL = "http://search.test/internal/auth/validate-admin-token/"
AUTH = {"X-Admin-Token": TOKEN}


@pytest.fixture
def api(database, reviews, respx_mock):
    settings = Settings(
        _env_file=None, admin_verifier_base_url="http://search.test", database=database
    )
    application = create_app(settings)
    application.state.review_cases = reviews
    verifier = respx_mock.get(VERIFIER_URL).respond(204)
    return TestClient(application), verifier


def assert_error(response, status_code, code):
    assert response.status_code == status_code
    assert response.json() == {"code": code, "trace_id": response.headers["X-Trace-ID"]}


@pytest.mark.parametrize(
    ("upstream", "status", "code"),
    [
        (403, 403, "ADMIN_TOKEN_REJECTED"),
        (302, 503, "ADMIN_VERIFIER_UNAVAILABLE"),
        (200, 503, "ADMIN_VERIFIER_UNAVAILABLE"),
        (None, 503, "ADMIN_VERIFIER_UNAVAILABLE"),
    ],
)
def test_failed_admin_validation_denies_read_and_write(
    api, assess, database, upstream, status, code
):
    client, verifier = api
    case_id = assess(payload()).review_case_id
    if upstream is None:
        verifier.mock(side_effect=httpx.ConnectError("Synthetic connection failure"))
    else:
        verifier.respond(upstream, headers={"Location": "http://elsewhere.test/"})
    url = f"/api/v1/review-cases/{case_id}"
    for denied in (
        client.get(url, headers=AUTH),
        client.post(f"{url}/resolution", json={"outcome": "reject"}, headers=AUTH),
    ):
        assert_error(denied, status, code)
        assert denied.headers["Cache-Control"] == "no-store"
        assert RETAINED_ANSWER not in denied.text
        assert TOKEN not in denied.text
    assert verifier.call_count == 2
    assert count_resolutions(database) == 0


def test_admin_can_read_historical_attribution_after_reopening(api, assess, reviews):
    client, _ = api
    case_id = assess(payload()).review_case_id
    historical = reviews.resolve(case_id, ANA, ReviewOutcome.REJECT).resolution
    settings = client.app.state.settings
    reopened = TestClient(create_app(settings))
    url = f"/api/v1/review-cases/{case_id}"
    response = reopened.get(url, headers=AUTH)
    assert response.status_code == 200
    assert response.json()["resolution"]["supervisor"] == {
        "identity_id": ANA.identity_id,
        "display_name": ANA.display_name,
    }
    assert response.json()["resolution"]["resolution_id"] == historical.resolution_id
    assert (
        reopened.post(
            f"{url}/resolution", json={"outcome": "reject"}, headers=AUTH
        ).status_code
        == 409
    )


def test_unauthenticated_callers_cannot_probe_case_ids(api):
    client, _ = api
    assert_error(
        client.get("/api/v1/review-cases/unknown"), 401, "AUTHENTICATION_REQUIRED"
    )
    assert_error(
        client.get("/api/v1/review-cases/unknown", headers=AUTH), 404, "CASE_NOT_FOUND"
    )


def test_supervisor_sees_content_reasons_and_state_without_caching(api, assess):
    client, _ = api
    record = assess(payload(pairs=1))

    response = client.get(f"/api/v1/review-cases/{record.review_case_id}", headers=AUTH)

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert body["status"] == "pending"
    assert body["assessment_id"] == record.assessment_id
    assert body["policy"] == {"policy_id": "qa-review", "version": "draft-1"}
    assert body["reason_codes"] == ["HUMAN_REVIEW_REQUIRED", "PARTIAL_GENERATION"]
    assert body["qa_pairs"] == [{"question": "Question 0", "answer": RETAINED_ANSWER}]
    assert body["source_article_ids"] == ["source-1"]
    assert body["resolution"] is None


def test_overdue_case_is_shown_as_expired(api, assess, clock):
    client, _ = api
    record = assess(payload())
    clock.offset += WINDOW
    body = client.get(
        f"/api/v1/review-cases/{record.review_case_id}", headers=AUTH
    ).json()
    assert body["status"] == "expired"


def test_resolution_route_creates_then_replays(api, assess, caplog):
    client, _ = api
    case_id = assess(payload()).review_case_id
    url = f"/api/v1/review-cases/{case_id}/resolution"

    with caplog.at_level(logging.DEBUG):
        created = client.post(url, json={"outcome": "reject"}, headers=AUTH)
        replayed = client.post(url, json={"outcome": "reject"}, headers=AUTH)

    assert created.status_code == 201
    assert created.headers["Cache-Control"] == "no-store"
    assert created.json()["supervisor"] == {
        "identity_id": ADMIN_SUPERVISOR.identity_id,
        "display_name": ADMIN_SUPERVISOR.display_name,
    }
    assert replayed.status_code == 200
    assert replayed.json() == created.json()
    assert TOKEN not in caplog.text
    assert RETAINED_ANSWER not in caplog.text
    view = client.get(f"/api/v1/review-cases/{case_id}", headers=AUTH).json()
    assert view["status"] == "reject"
    assert view["resolution"]["resolution_id"] == created.json()["resolution_id"]


def test_modify_route_stores_edited_pairs(api, assess, database):
    client, _ = api
    case_id = assess(payload()).review_case_id
    response = client.post(
        f"/api/v1/review-cases/{case_id}/resolution",
        json={
            "outcome": "modify",
            "justification": "Clarified answer",
            "qa_pairs": [{"question": " Edited ", "answer": "Edited answer"}],
        },
        headers=AUTH,
    )
    assert response.status_code == 201
    stored = SqliteReviewResolutionRepository(database).get(case_id)
    assert stored.modified_content.qa_pairs == (
        RetainedQaPair("Edited", "Edited answer"),
    )


@pytest.mark.parametrize(
    ("first", "second", "status_code", "code"),
    [
        ({"outcome": "reject"}, {"outcome": "approve"}, 409, "CASE_ALREADY_RESOLVED"),
        (None, {"outcome": "approve"}, 422, "JUSTIFICATION_REQUIRED"),
    ],
)
def test_resolution_route_errors(api, assess, first, second, status_code, code):
    client, _ = api
    case_id = assess(payload(pairs=1)).review_case_id
    url = f"/api/v1/review-cases/{case_id}/resolution"
    if first is not None:
        client.post(url, json={**first, "justification": "Reviewed"}, headers=AUTH)
    assert_error(client.post(url, json=second, headers=AUTH), status_code, code)


def test_resolution_after_the_window_is_denied(api, assess, clock):
    client, _ = api
    case_id = assess(payload()).review_case_id
    clock.offset += WINDOW
    response = client.post(
        f"/api/v1/review-cases/{case_id}/resolution",
        json={"outcome": "approve"},
        headers=AUTH,
    )
    assert_error(response, 409, "CASE_EXPIRED")


@pytest.mark.parametrize(
    "body",
    [
        {"outcome": "expired"},
        {"outcome": "modify", "justification": "Fixed"},
        {"outcome": "reject", "qa_pairs": [{"question": "Q", "answer": "A"}]},
        {"outcome": "modify", "justification": "Fixed", "qa_pairs": []},
        {"outcome": "reject", "justification": " "},
        {"outcome": "reject", "extra": True},
        {"outcome": "modify", "justification": "x", "qa_pairs": [{"question": "Q"}]},
    ],
)
def test_invalid_resolution_requests_do_not_echo_input(api, assess, database, body):
    client, _ = api
    case_id = assess(payload()).review_case_id
    response = client.post(
        f"/api/v1/review-cases/{case_id}/resolution", json=body, headers=AUTH
    )
    assert_error(response, 422, "INVALID_REQUEST")
    assert count_resolutions(database) == 0


def test_authentication_is_checked_before_the_body(api, assess):
    client, _ = api
    case_id = assess(payload()).review_case_id
    response = client.post(
        f"/api/v1/review-cases/{case_id}/resolution", json={"outcome": "nope"}
    )
    assert_error(response, 401, "AUTHENTICATION_REQUIRED")


def test_review_routes_are_unavailable_without_a_database(respx_mock):
    settings = Settings(_env_file=None, admin_verifier_base_url="http://search.test")
    client = TestClient(create_app(settings))
    respx_mock.get(VERIFIER_URL).respond(204)
    response = client.get("/api/v1/review-cases/any", headers=AUTH)
    assert_error(response, 503, "SUPERVISION_NOT_CONFIGURED")


def test_settings_compose_the_review_workflow(tmp_path):
    database = tmp_path / "governance.sqlite3"
    configured = Settings(
        _env_file=None,
        admin_verifier_base_url="http://search.test",
        database=database,
        review_window_minutes=30,
    )
    assert isinstance(create_review_cases(configured), ReviewCases)
    assert create_review_cases(Settings(_env_file=None)) is None


@pytest.mark.parametrize("minutes", [0, 10081])
def test_review_window_setting_is_bounded(minutes):
    with pytest.raises(ValueError, match="review_window_minutes"):
        Settings(_env_file=None, review_window_minutes=minutes)
