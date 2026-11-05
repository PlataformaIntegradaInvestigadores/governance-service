import json
import sqlite3
from dataclasses import asdict, replace
from datetime import UTC
from pathlib import Path

import pytest

from app.application.record_qa_assessment import RecordQaAssessment
from app.domain.qa import (
    InvalidPolicyError,
    PolicyVersionConflictError,
    QaPolicy,
    Verdict,
)
from app.infrastructure.json_policy_repository import JsonPolicyRepository
from app.infrastructure.qa_response import assess_qa_response, retain_qa_content
from app.infrastructure.sqlite_assessment_repository import (
    SqliteAssessmentRepository,
    SqliteReviewCaseRepository,
)
from app.main import create_qa_assessor


@pytest.fixture
def policy_path(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(asdict(QaPolicy("qa-review", "draft-1", 3))))
    return path


@pytest.fixture
def database_path(tmp_path):
    return tmp_path / "assessments.sqlite3"


@pytest.fixture
def payload():
    return {
        "status": "success",
        "qa_pairs": [{"question": "Question", "answer": "Confidential answer"}] * 3,
        "source_docs": [{"article_id": "source-1"}],
        "raw_response": "Confidential raw output",
    }


def test_assessment_and_exact_policy_survive_repository_reopening(
    policy_path, database_path, payload
):
    record = create_qa_assessor(policy_path, database_path).execute(payload)

    loaded = SqliteAssessmentRepository(database_path).get(record.assessment_id)

    assert loaded == record
    assert loaded.created_at.tzinfo == UTC
    assert loaded.policy == QaPolicy("qa-review", "draft-1", 3)
    assert loaded.assessment.verdict == Verdict.SUSPEND
    with sqlite3.connect(database_path) as connection:
        snapshot = connection.execute("SELECT snapshot_json FROM policies").fetchone()[
            0
        ]
        assert json.loads(snapshot) == asdict(loaded.policy)
        assert "Confidential raw output" not in "\n".join(connection.iterdump())


def test_invalid_upstream_response_also_has_a_persisted_decision(
    policy_path, database_path
):
    record = create_qa_assessor(policy_path, database_path).execute({})

    loaded = SqliteAssessmentRepository(database_path).get(record.assessment_id)

    assert loaded.assessment.verdict == Verdict.DENY
    assert loaded.assessment.reason_codes == ("INVALID_UPSTREAM_RESPONSE",)


def test_conflicting_policy_version_is_rejected_without_another_decision(
    policy_path, database_path, payload
):
    assessor = create_qa_assessor(policy_path, database_path)
    first = assessor.execute(payload)
    policy_path.write_text(json.dumps(asdict(QaPolicy("qa-review", "draft-1", 4))))

    with pytest.raises(PolicyVersionConflictError):
        assessor.execute(payload)

    repository = SqliteAssessmentRepository(database_path)
    assert repository.get(first.assessment_id) == first
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM assessments").fetchone()[0] == 1


def test_new_policy_version_preserves_both_snapshots(
    policy_path, database_path, payload
):
    assessor = create_qa_assessor(policy_path, database_path)
    first = assessor.execute(payload)
    policy_path.write_text(json.dumps(asdict(QaPolicy("qa-review", "draft-2", 4))))
    second = assessor.execute(payload)

    repository = SqliteAssessmentRepository(database_path)
    assert repository.get(first.assessment_id).policy.expected_pairs == 3
    assert repository.get(second.assessment_id).policy.expected_pairs == 4
    assert "PARTIAL_GENERATION" in second.assessment.reason_codes


def test_policy_snapshot_is_the_one_used_by_the_evaluator(
    policy_path, database_path, payload
):
    repository = SqliteAssessmentRepository(database_path)

    def change_file_after_loading(response, policy):
        policy_path.write_text(json.dumps(asdict(QaPolicy("qa-review", "draft-2", 4))))
        return assess_qa_response(response, policy)

    assessor = RecordQaAssessment(
        JsonPolicyRepository(policy_path),
        repository,
        change_file_after_loading,
        retain_qa_content,
    )
    record = assessor.execute(payload)

    assert repository.get(record.assessment_id).policy.version == "draft-1"
    assert JsonPolicyRepository(policy_path).load().version == "draft-2"


@pytest.mark.parametrize(
    "document",
    [
        "not JSON",
        "null",
        "{}",
        '{"policy_id":"qa", "version":"draft-1", "expected_pairs":true}',
        '{"policy_id":"qa", "version":"draft-1", "expected_pairs":"3"}',
        '{"policy_id":"qa", "version":"draft-1", "expected_pairs":0}',
        '{"policy_id":" ", "version":"draft-1", "expected_pairs":3}',
        '{"policy_id":"qa", "version":"draft-1", "expected_pairs":3,"allow":true}',
        '{"policy_id":"qa","version":"draft-1","version":"draft-2","expected_pairs":3}',
    ],
)
def test_invalid_policy_never_produces_a_record(policy_path, database_path, document):
    policy_path.write_text(document)
    assessor = create_qa_assessor(policy_path, database_path)

    with pytest.raises(InvalidPolicyError, match="Invalid policy file"):
        assessor.execute({})

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM assessments").fetchone()[0] == 0


def test_missing_policy_fails_without_a_fallback(policy_path, database_path):
    policy_path.unlink()
    with pytest.raises(FileNotFoundError):
        create_qa_assessor(policy_path, database_path).execute({})


def test_storage_exception_is_not_returned_as_a_saved_assessment(policy_path):
    class FailingRepository:
        def save(self, record, review_case):
            raise OSError("Storage unavailable")

    assessor = RecordQaAssessment(
        JsonPolicyRepository(policy_path),
        FailingRepository(),
        assess_qa_response,
        retain_qa_content,
    )
    with pytest.raises(OSError, match="Storage unavailable"):
        assessor.execute({})


def test_transaction_rolls_back_policy_when_assessment_insert_fails(
    policy_path, database_path
):
    assessor = create_qa_assessor(policy_path, database_path)
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """CREATE TRIGGER reject_assessment BEFORE INSERT ON assessments
               BEGIN SELECT RAISE(ABORT, 'Simulated write failure'); END;"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="Simulated write failure"):
        assessor.execute({})

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM policies").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM assessments").fetchone()[0] == 0


@pytest.mark.parametrize("table", ["policies", "assessments"])
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
def test_local_tables_reject_update_and_delete(
    policy_path, database_path, table, operation
):
    create_qa_assessor(policy_path, database_path).execute({})
    statements = {
        ("policies", "UPDATE"): "UPDATE policies SET version = 'changed'",
        ("policies", "DELETE"): "DELETE FROM policies",
        ("assessments", "UPDATE"): "UPDATE assessments SET verdict = 'suspend'",
        ("assessments", "DELETE"): "DELETE FROM assessments",
    }
    with (
        sqlite3.connect(database_path) as connection,
        pytest.raises(sqlite3.IntegrityError, match="append-only"),
    ):
        connection.execute(statements[table, operation])


def test_duplicate_decision_id_does_not_overwrite_original(
    policy_path, database_path, payload
):
    original = create_qa_assessor(policy_path, database_path).execute(payload)
    repository = SqliteAssessmentRepository(database_path)
    case = SqliteReviewCaseRepository(database_path).get(original.review_case_id)
    with pytest.raises(sqlite3.IntegrityError):
        repository.save(original, case)
    assert repository.get(original.assessment_id) == original


def test_mismatched_policy_identity_is_rejected(policy_path, database_path):
    record = create_qa_assessor(policy_path, database_path).execute({})
    mismatched = replace(record, policy=QaPolicy("another-policy", "draft-1", 3))
    with pytest.raises(ValueError, match="identities must match"):
        SqliteAssessmentRepository(database_path).save(mismatched, None)


def test_unknown_assessment_is_absent(database_path):
    assert SqliteAssessmentRepository(database_path).get("unknown") is None


def test_policy_checksum_mismatch_is_detected(policy_path, database_path):
    record = create_qa_assessor(policy_path, database_path).execute({})
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TRIGGER policies_no_update")
        connection.execute("UPDATE policies SET sha256 = 'tampered'")
    with pytest.raises(ValueError, match="digest mismatch"):
        SqliteAssessmentRepository(database_path).get(record.assessment_id)


def test_database_cannot_be_created_in_a_missing_parent(tmp_path, policy_path):
    with pytest.raises(sqlite3.OperationalError):
        create_qa_assessor(policy_path, tmp_path / "missing" / "assessments.sqlite3")


def test_checked_in_policy_has_the_documented_experimental_version():
    policy = JsonPolicyRepository(
        Path(__file__).resolve().parents[1] / "policies/search-qa-review.draft-1.json"
    ).load()
    assert policy == QaPolicy("search-qa-review", "draft-1", 3)
