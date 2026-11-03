import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from app.domain.qa import (
    Assessment,
    PolicyVersionConflict,
    QaPolicy,
    RecordedAssessment,
    Verdict,
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
CREATE TRIGGER IF NOT EXISTS policies_no_update BEFORE UPDATE ON policies
BEGIN SELECT RAISE(ABORT, 'Policies are append-only'); END;
CREATE TRIGGER IF NOT EXISTS policies_no_delete BEFORE DELETE ON policies
BEGIN SELECT RAISE(ABORT, 'Policies are append-only'); END;
CREATE TRIGGER IF NOT EXISTS assessments_no_update BEFORE UPDATE ON assessments
BEGIN SELECT RAISE(ABORT, 'Assessments are append-only'); END;
CREATE TRIGGER IF NOT EXISTS assessments_no_delete BEFORE DELETE ON assessments
BEGIN SELECT RAISE(ABORT, 'Assessments are append-only'); END;
"""


class SqliteAssessmentRepository:
    """Local transactional storage, not the planned PostgreSQL forensic audit log."""

    def __init__(self, path: Path) -> None:
        self._path = path
        with closing(sqlite3.connect(self._path)) as connection:
            connection.executescript(_SCHEMA)

    def save(self, record: RecordedAssessment) -> None:
        policy = record.policy
        assessment = record.assessment
        if (assessment.policy_id, assessment.policy_version) != (
            policy.policy_id,
            policy.version,
        ):
            raise ValueError("Assessment and policy identities must match")
        snapshot = json.dumps(asdict(policy), sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(snapshot.encode("utf-8")).hexdigest()
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
                raise PolicyVersionConflict(
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

    def get(self, assessment_id: str) -> RecordedAssessment | None:
        with closing(sqlite3.connect(self._path)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """SELECT a.*, p.snapshot_json, p.sha256
                   FROM assessments a JOIN policies p
                   ON a.policy_id = p.policy_id AND a.policy_version = p.version
                   WHERE a.assessment_id = ?""",
                (assessment_id,),
            ).fetchone()
        if row is None:
            return None
        digest = hashlib.sha256(row["snapshot_json"].encode("utf-8")).hexdigest()
        if digest != row["sha256"]:
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
        )
