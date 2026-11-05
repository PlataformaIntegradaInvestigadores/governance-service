import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from app.domain.qa import (
    Assessment,
    PolicyVersionConflictError,
    QaPolicy,
    RecordedAssessment,
    Verdict,
)
from app.domain.review import (
    RetainedQaContent,
    RetainedQaPair,
    ReviewCase,
    ReviewCaseIntegrityError,
    require_review_case_link,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS policies (
    policy_id TEXT NOT NULL,
    version TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    PRIMARY KEY (policy_id, version)
);
CREATE TABLE IF NOT EXISTS assessments (
    assessment_id TEXT PRIMARY KEY NOT NULL,
    created_at TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('deny', 'suspend')),
    reason_codes_json TEXT NOT NULL,
    FOREIGN KEY (policy_id, policy_version) REFERENCES policies(policy_id, version)
);
CREATE TABLE IF NOT EXISTS review_cases (
    case_id TEXT PRIMARY KEY NOT NULL,
    assessment_id TEXT NOT NULL UNIQUE REFERENCES assessments(assessment_id),
    created_at TEXT NOT NULL,
    content_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS review_cases_need_suspension
BEFORE INSERT ON review_cases
WHEN (SELECT verdict FROM assessments WHERE assessment_id = NEW.assessment_id)
     IS NOT 'suspend'
BEGIN SELECT RAISE(ABORT, 'Review cases require a suspended assessment'); END;
CREATE TRIGGER IF NOT EXISTS review_cases_no_update BEFORE UPDATE ON review_cases
BEGIN SELECT RAISE(ABORT, 'Review cases are append-only'); END;
CREATE TRIGGER IF NOT EXISTS review_cases_no_delete BEFORE DELETE ON review_cases
BEGIN SELECT RAISE(ABORT, 'Review cases are append-only'); END;
CREATE TRIGGER IF NOT EXISTS policies_no_update BEFORE UPDATE ON policies
BEGIN SELECT RAISE(ABORT, 'Policies are append-only'); END;
CREATE TRIGGER IF NOT EXISTS policies_no_delete BEFORE DELETE ON policies
BEGIN SELECT RAISE(ABORT, 'Policies are append-only'); END;
CREATE TRIGGER IF NOT EXISTS assessments_no_update BEFORE UPDATE ON assessments
BEGIN SELECT RAISE(ABORT, 'Assessments are append-only'); END;
CREATE TRIGGER IF NOT EXISTS assessments_no_delete BEFORE DELETE ON assessments
BEGIN SELECT RAISE(ABORT, 'Assessments are append-only'); END;
"""


def _initialize(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(_SCHEMA)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _content_json(content: RetainedQaContent) -> str:
    document = {
        "qa_pairs": [
            {"question": pair.question, "answer": pair.answer}
            for pair in content.qa_pairs
        ],
        "source_article_ids": list(content.source_article_ids),
    }
    return json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


class SqliteAssessmentRepository:
    """Local transactional storage, not the planned PostgreSQL forensic audit log.

    Queries return decision metadata and the linked case ID, never retained content.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        _initialize(path)

    def save(self, record: RecordedAssessment, review_case: ReviewCase | None) -> None:
        require_review_case_link(record, review_case)
        policy = record.policy
        assessment = record.assessment
        if (assessment.policy_id, assessment.policy_version) != (
            policy.policy_id,
            policy.version,
        ):
            raise ValueError("Assessment and policy identities must match")
        snapshot = json.dumps(asdict(policy), sort_keys=True, separators=(",", ":"))
        digest = _sha256(snapshot)
        with closing(sqlite3.connect(self._path)) as connection, connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                "INSERT OR IGNORE INTO policies VALUES (?, ?, ?, ?)",
                (policy.policy_id, policy.version, snapshot, digest),
            )
            stored = connection.execute(
                "SELECT snapshot_json, sha256 FROM policies "
                "WHERE policy_id = ? AND version = ?",
                (policy.policy_id, policy.version),
            ).fetchone()
            if stored != (snapshot, digest):
                raise PolicyVersionConflictError(
                    "Policy content changed without a new version"
                )
            connection.execute(
                "INSERT INTO assessments VALUES (?, ?, ?, ?, ?, ?)",
                (
                    record.assessment_id,
                    record.created_at.isoformat(),
                    policy.policy_id,
                    policy.version,
                    assessment.verdict.value,
                    json.dumps(assessment.reason_codes),
                ),
            )
            if review_case is not None:
                content = _content_json(review_case.content)
                connection.execute(
                    "INSERT INTO review_cases VALUES (?, ?, ?, ?, ?)",
                    (
                        review_case.case_id,
                        review_case.assessment_id,
                        review_case.created_at.isoformat(),
                        content,
                        _sha256(content),
                    ),
                )

    def get(self, assessment_id: str) -> RecordedAssessment | None:
        with closing(sqlite3.connect(self._path)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """SELECT a.*, p.snapshot_json, p.sha256, r.case_id
                   FROM assessments a JOIN policies p
                   ON a.policy_id = p.policy_id AND a.policy_version = p.version
                   LEFT JOIN review_cases r ON r.assessment_id = a.assessment_id
                   WHERE a.assessment_id = ?""",
                (assessment_id,),
            ).fetchone()
        if row is None:
            return None
        if _sha256(row["snapshot_json"]) != row["sha256"]:
            raise ValueError("Stored policy snapshot digest mismatch")
        return RecordedAssessment(
            row["assessment_id"],
            datetime.fromisoformat(row["created_at"]),
            QaPolicy(**json.loads(row["snapshot_json"])),
            Assessment(
                Verdict(row["verdict"]),
                row["policy_id"],
                row["policy_version"],
                tuple(json.loads(row["reason_codes_json"])),
            ),
            row["case_id"],
        )


class SqliteReviewCaseRepository:
    """Protected in-process reads of retained content, separate from audit queries.

    shortcut: no authentication or authorization and no encryption at rest; any
    process with file access can read it. Upgrade with identity validation, a
    reviewer allowlist and the PostgreSQL store before exposing an API route.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        _initialize(path)

    def get(self, case_id: str) -> ReviewCase | None:
        with closing(sqlite3.connect(self._path)) as connection:
            row = connection.execute(
                "SELECT case_id, assessment_id, created_at, content_json, "
                "content_sha256 FROM review_cases WHERE case_id = ?",
                (case_id,),
            ).fetchone()
        if row is None:
            return None
        case_id, assessment_id, created_at, content, digest = row
        if _sha256(content) != digest:
            raise ReviewCaseIntegrityError("Stored review case digest mismatch")
        document = json.loads(content)
        return ReviewCase(
            case_id,
            assessment_id,
            datetime.fromisoformat(created_at),
            RetainedQaContent(
                tuple(
                    RetainedQaPair(pair["question"], pair["answer"])
                    for pair in document["qa_pairs"]
                ),
                tuple(document["source_article_ids"]),
            ),
        )
