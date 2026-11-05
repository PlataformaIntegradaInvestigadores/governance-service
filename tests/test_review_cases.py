import hashlib
import json
import logging
import sqlite3
from dataclasses import asdict, replace
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.application.record_qa_assessment import RecordQaAssessment
from app.domain.qa import Assessment, QaPolicy, RecordedAssessment, Verdict
from app.domain.review import (
    RetainedQaContent,
    RetainedQaPair,
    ReviewCase,
    ReviewCaseIntegrityError,
)
from app.infrastructure.json_policy_repository import JsonPolicyRepository
from app.infrastructure.qa_response import assess_qa_response, retain_qa_content
from app.infrastructure.sqlite_assessment_repository import (
    SqliteAssessmentRepository,
    SqliteReviewCaseRepository,
)
from app.main import create_qa_assessor

RETAINED_ANSWER = "Respuesta sintética confidencial ¿única?"
POLICY = QaPolicy("qa-review", "draft-1", 3)


@pytest.fixture
def policy_path(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(asdict(POLICY)))
    return path


@pytest.fixture
def database_path(tmp_path):
    return tmp_path / "assessments.sqlite3"


def synthetic_response(answer=RETAINED_ANSWER, article_id="synthetic-article-1"):
    return {
        "status": "success",
        "qa_pairs": [
            {"question": f"Synthetic question {i}", "answer": f"{answer} {i}"}
            for i in range(3)
        ],
        "source_docs": [{"article_id": article_id}, {"article_id": None}],
        "raw_response": "Synthetic raw output never retained",
    }


def table_dump(database_path, table):
    with sqlite3.connect(database_path) as connection:
        return json.dumps(
            connection.execute(f"SELECT * FROM {table}").fetchall(),  # noqa: S608
            ensure_ascii=False,
        )


def row_counts(database_path):
    with sqlite3.connect(database_path) as connection:
        return {
            table: connection.execute(
                f"SELECT COUNT(*) FROM {table}"  # noqa: S608
            ).fetchone()[0]
            for table in ("policies", "assessments", "review_cases")
        }


def test_suspended_assessment_persists_a_linked_case_after_reopening(
    policy_path, database_path
):
    record = create_qa_assessor(policy_path, database_path).execute(
        synthetic_response()
    )

    loaded = SqliteAssessmentRepository(database_path).get(record.assessment_id)
    case = SqliteReviewCaseRepository(database_path).get(record.review_case_id)

    assert record.assessment.verdict == Verdict.SUSPEND
    assert loaded == record
    assert case.case_id == record.review_case_id != record.assessment_id
    assert case.assessment_id == record.assessment_id
    assert case.created_at == record.created_at
    assert case.created_at.tzinfo == UTC
    assert [pair.answer for pair in case.content.qa_pairs] == [
        f"{RETAINED_ANSWER} {i}" for i in range(3)
    ]
    assert case.content.source_article_ids == ("synthetic-article-1",)


def test_each_case_holds_the_content_of_its_own_assessment(policy_path, database_path):
    assessor = create_qa_assessor(policy_path, database_path)
    first = assessor.execute(synthetic_response("First answer", "article-a"))
    second = assessor.execute(synthetic_response("Second answer", "article-b"))
    cases = SqliteReviewCaseRepository(database_path)

    for record, answer, article_id in (
        (first, "First answer", "article-a"),
        (second, "Second answer", "article-b"),
    ):
        case = cases.get(record.review_case_id)
        assert case.assessment_id == record.assessment_id
        assert case.content.qa_pairs[0].answer == f"{answer} 0"
        assert case.content.source_article_ids == (article_id,)


def test_denied_assessment_has_no_review_case(policy_path, database_path):
    record = create_qa_assessor(policy_path, database_path).execute({})

    assert record.assessment.verdict == Verdict.DENY
    assert record.review_case_id is None
    assert SqliteAssessmentRepository(database_path).get(record.assessment_id) == (
        record
    )
    assert row_counts(database_path)["review_cases"] == 0


def test_general_queries_and_representations_exclude_retained_content(
    policy_path, database_path
):
    record = create_qa_assessor(policy_path, database_path).execute(
        synthetic_response()
    )
    loaded = SqliteAssessmentRepository(database_path).get(record.assessment_id)
    case = SqliteReviewCaseRepository(database_path).get(record.review_case_id)

    for text in (
        repr(record),
        repr(loaded),
        repr(case),
        str(case.content),
        table_dump(database_path, "assessments"),
        table_dump(database_path, "policies"),
    ):
        assert RETAINED_ANSWER not in text
    assert RETAINED_ANSWER in table_dump(database_path, "review_cases")
    with sqlite3.connect(database_path) as connection:
        assert "never retained" not in "\n".join(connection.iterdump())


def test_retained_content_is_not_logged(policy_path, database_path, caplog):
    caplog.set_level(logging.DEBUG)

    record = create_qa_assessor(policy_path, database_path).execute(
        synthetic_response()
    )
    SqliteReviewCaseRepository(database_path).get(record.review_case_id)

    assert RETAINED_ANSWER not in caplog.text


def test_case_write_failure_leaves_no_partial_records(policy_path, database_path):
    assessor = create_qa_assessor(policy_path, database_path)
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """CREATE TRIGGER reject_case BEFORE INSERT ON review_cases
               BEGIN SELECT RAISE(ABORT, 'Simulated case write failure'); END;"""
        )

    with pytest.raises(sqlite3.IntegrityError) as error:
        assessor.execute(synthetic_response())

    assert "Simulated case write failure" in str(error.value)
    assert RETAINED_ANSWER not in str(error.value)
    SqliteAssessmentRepository(database_path)
    assert row_counts(database_path) == {
        "policies": 0,
        "assessments": 0,
        "review_cases": 0,
    }


def test_content_extraction_failure_saves_nothing(policy_path, database_path):
    def failing_retain(payload):
        raise ValueError("Simulated extraction failure")

    assessor = RecordQaAssessment(
        JsonPolicyRepository(policy_path),
        SqliteAssessmentRepository(database_path),
        assess_qa_response,
        failing_retain,
    )

    with pytest.raises(ValueError, match="Simulated extraction failure"):
        assessor.execute(synthetic_response())
    assert set(row_counts(database_path).values()) == {0}


def _suspended_record(case_id="case-1"):
    return RecordedAssessment(
        "assessment-1",
        datetime.now(UTC),
        POLICY,
        Assessment(Verdict.SUSPEND, POLICY.policy_id, POLICY.version, ("R",)),
        case_id,
    )


def _case(assessment_id="assessment-1", case_id="case-1"):
    content = RetainedQaContent((RetainedQaPair("Q", RETAINED_ANSWER),), ())
    return ReviewCase(case_id, assessment_id, datetime.now(UTC), content)


@pytest.mark.parametrize(
    ("record", "case", "message"),
    [
        (_suspended_record(), None, "exactly one review case"),
        (
            replace(
                _suspended_record(None),
                assessment=Assessment(Verdict.DENY, "qa-review", "draft-1", ("R",)),
            ),
            _case(),
            "exactly one review case",
        ),
        (_suspended_record(), _case(assessment_id="other"), "not linked"),
        (_suspended_record("other-case"), _case(), "not linked"),
    ],
)
def test_inconsistent_links_are_rejected_before_writing(
    database_path, record, case, message
):
    repository = SqliteAssessmentRepository(database_path)

    with pytest.raises(ValueError, match=message):
        repository.save(record, case)
    assert set(row_counts(database_path).values()) == {0}


def test_database_rejects_a_case_for_a_denied_assessment(policy_path, database_path):
    record = create_qa_assessor(policy_path, database_path).execute({})

    with (
        sqlite3.connect(database_path) as connection,
        pytest.raises(sqlite3.IntegrityError, match="require a suspended"),
    ):
        connection.execute(
            "INSERT INTO review_cases VALUES ('c', ?, '2026-11-05', '{}', 'x')",
            (record.assessment_id,),
        )


@pytest.mark.parametrize(
    "statement",
    ["UPDATE review_cases SET content_json = '{}'", "DELETE FROM review_cases"],
)
def test_review_cases_are_append_only(policy_path, database_path, statement):
    create_qa_assessor(policy_path, database_path).execute(synthetic_response())

    with (
        sqlite3.connect(database_path) as connection,
        pytest.raises(sqlite3.IntegrityError, match="append-only"),
    ):
        connection.execute(statement)


def test_tampered_case_is_detected_without_revealing_content(
    policy_path, database_path
):
    record = create_qa_assessor(policy_path, database_path).execute(
        synthetic_response()
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TRIGGER review_cases_no_update")
        connection.execute(
            "UPDATE review_cases SET content_json = replace(content_json, ?, ?)",
            (RETAINED_ANSWER, "Altered answer"),
        )

    with pytest.raises(ReviewCaseIntegrityError) as error:
        SqliteReviewCaseRepository(database_path).get(record.review_case_id)
    assert RETAINED_ANSWER not in str(error.value)


def test_unknown_review_case_is_absent(database_path):
    assert SqliteReviewCaseRepository(database_path).get("unknown") is None


def test_suspension_stored_before_review_cases_reads_without_a_case(database_path):
    repository = SqliteAssessmentRepository(database_path)
    snapshot = json.dumps(asdict(POLICY), sort_keys=True, separators=(",", ":"))
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO policies VALUES (?, ?, ?, ?)",
            (
                POLICY.policy_id,
                POLICY.version,
                snapshot,
                hashlib.sha256(snapshot.encode()).hexdigest(),
            ),
        )
        connection.execute(
            "INSERT INTO assessments VALUES "
            "('legacy', '2026-11-03T00:00:00+00:00', ?, ?, 'suspend', '[]')",
            (POLICY.policy_id, POLICY.version),
        )

    assert repository.get("legacy").review_case_id is None


@pytest.mark.parametrize(
    ("pairs", "article_ids", "message"),
    [
        ((), (), "at least one pair"),
        ((RetainedQaPair("Q", "A"),), (" ",), "must not be blank"),
    ],
)
def test_invalid_retained_content_is_rejected(pairs, article_ids, message):
    with pytest.raises(ValueError, match=message):
        RetainedQaContent(pairs, article_ids)


@pytest.mark.parametrize(("question", "answer"), [(" ", "A"), ("Q", "")])
def test_blank_retained_pairs_are_rejected(question, answer):
    with pytest.raises(ValueError, match="must not be blank"):
        RetainedQaPair(question, answer)


def test_retention_keeps_only_pairs_and_present_source_identifiers():
    response = synthetic_response()
    response["source_docs"] = [
        {"article_id": 42, "title": "Synthetic title"},
        {"article_id": "  "},
        {"article_id": None},
    ]

    content = retain_qa_content(response)

    assert len(content.qa_pairs) == 3
    assert content.source_article_ids == ("42",)
    assert repr(content) == "RetainedQaContent(pairs=3, sources=1)"


def test_retention_rejects_an_invalid_payload():
    with pytest.raises(ValidationError):
        retain_qa_content({"status": "success", "qa_pairs": "invalid"})
