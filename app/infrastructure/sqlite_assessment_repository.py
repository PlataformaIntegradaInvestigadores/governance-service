import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from app.domain.qa import (
    Assessment,
    PolicyVersionConflictError,
    QaPolicy,
    RecordedAssessment,
    Verdict,
)
from app.domain.qa_sampling import QaSample, QaSampleNotFoundError, QaSamplingPolicy
from app.domain.recommender import RecommenderPolicy
from app.domain.resolution import (
    ReviewCaseAlreadyResolvedError,
    ReviewOutcome,
    ReviewResolution,
)
from app.domain.review import (
    RecommendationSelection,
    RetainedQaContent,
    RetainedQaPair,
    RetainedRecommendation,
    ReviewCase,
    ReviewCaseIntegrityError,
    require_review_case_link,
)
from app.domain.supervision import Supervisor

_ASSESSMENTS = """
CREATE TABLE IF NOT EXISTS assessments (
    assessment_id TEXT PRIMARY KEY NOT NULL,
    created_at TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('deny', 'suspend', 'allow')),
    reason_codes_json TEXT NOT NULL,
    FOREIGN KEY (policy_id, policy_version) REFERENCES policies(policy_id, version)
);
"""
_CASE_GUARD = """
CREATE TRIGGER IF NOT EXISTS review_cases_need_suspension
BEFORE INSERT ON review_cases
WHEN (SELECT verdict FROM assessments WHERE assessment_id = NEW.assessment_id)
     IS NOT 'suspend'
BEGIN SELECT RAISE(ABORT, 'Review cases require a suspended assessment'); END;
"""
_ASSESSMENT_GUARDS = (
    """CREATE TRIGGER IF NOT EXISTS assessments_no_update BEFORE UPDATE ON assessments
       BEGIN SELECT RAISE(ABORT, 'Assessments are append-only'); END;""",
    """CREATE TRIGGER IF NOT EXISTS assessments_no_delete BEFORE DELETE ON assessments
       BEGIN SELECT RAISE(ABORT, 'Assessments are append-only'); END;""",
)
_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS policies (
    policy_id TEXT NOT NULL,
    version TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    PRIMARY KEY (policy_id, version)
);
{_ASSESSMENTS}
CREATE TABLE IF NOT EXISTS recommender_audit (
    assessment_id TEXT PRIMARY KEY NOT NULL REFERENCES assessments(assessment_id),
    metadata_json TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS recommender_audit_no_update
BEFORE UPDATE ON recommender_audit
BEGIN SELECT RAISE(ABORT, 'Recommendation audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS recommender_audit_no_delete
BEFORE DELETE ON recommender_audit
BEGIN SELECT RAISE(ABORT, 'Recommendation audit is append-only'); END;
CREATE TABLE IF NOT EXISTS qa_deliveries (
    assessment_id TEXT PRIMARY KEY NOT NULL REFERENCES assessments(assessment_id),
    prepared_at TEXT NOT NULL,
    response_sha256 TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS qa_deliveries_no_update BEFORE UPDATE ON qa_deliveries
BEGIN SELECT RAISE(ABORT, 'Q&A deliveries are append-only'); END;
CREATE TRIGGER IF NOT EXISTS qa_deliveries_no_delete BEFORE DELETE ON qa_deliveries
BEGIN SELECT RAISE(ABORT, 'Q&A deliveries are append-only'); END;
CREATE TABLE IF NOT EXISTS qa_samples (
    sample_id TEXT PRIMARY KEY NOT NULL,
    snapshot_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS qa_delivery_content (
    assessment_id TEXT PRIMARY KEY NOT NULL REFERENCES qa_deliveries(assessment_id),
    response_body BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS qa_sample_reviews (
    sample_id TEXT NOT NULL REFERENCES qa_samples(sample_id),
    assessment_id TEXT NOT NULL REFERENCES qa_delivery_content(assessment_id),
    decision_json TEXT NOT NULL,
    resolved_at TEXT NOT NULL,
    PRIMARY KEY (sample_id, assessment_id)
);
CREATE TRIGGER IF NOT EXISTS qa_sample_reviews_no_update
BEFORE UPDATE ON qa_sample_reviews
BEGIN SELECT RAISE(ABORT, 'Q&A sample reviews are append-only'); END;
CREATE TRIGGER IF NOT EXISTS qa_sample_reviews_no_delete
BEFORE DELETE ON qa_sample_reviews
BEGIN SELECT RAISE(ABORT, 'Q&A sample reviews are append-only'); END;
CREATE TRIGGER IF NOT EXISTS qa_samples_no_update BEFORE UPDATE ON qa_samples
BEGIN SELECT RAISE(ABORT, 'Q&A samples are append-only'); END;
CREATE TRIGGER IF NOT EXISTS qa_samples_no_delete BEFORE DELETE ON qa_samples
BEGIN SELECT RAISE(ABORT, 'Q&A samples are append-only'); END;
CREATE TABLE IF NOT EXISTS review_cases (
    case_id TEXT PRIMARY KEY NOT NULL,
    assessment_id TEXT NOT NULL UNIQUE REFERENCES assessments(assessment_id),
    created_at TEXT NOT NULL,
    content_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL
);
{_CASE_GUARD}
CREATE TRIGGER IF NOT EXISTS review_cases_no_update BEFORE UPDATE ON review_cases
BEGIN SELECT RAISE(ABORT, 'Review cases are append-only'); END;
CREATE TRIGGER IF NOT EXISTS review_cases_no_delete BEFORE DELETE ON review_cases
BEGIN SELECT RAISE(ABORT, 'Review cases are append-only'); END;
CREATE TABLE IF NOT EXISTS review_resolutions (
    resolution_id TEXT PRIMARY KEY NOT NULL,
    case_id TEXT NOT NULL UNIQUE REFERENCES review_cases(case_id),
    outcome TEXT NOT NULL
        CHECK (outcome IN ('approve', 'modify', 'reject', 'expired')),
    resolved_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    supervisor_identity_id TEXT,
    supervisor_snapshot_json TEXT,
    justification TEXT,
    modified_content_json TEXT,
    modified_content_sha256 TEXT,
    CHECK ((outcome = 'expired') = (supervisor_identity_id IS NULL)),
    CHECK ((supervisor_identity_id IS NULL) = (supervisor_snapshot_json IS NULL)),
    CHECK ((outcome = 'modify') = (modified_content_json IS NOT NULL)),
    CHECK ((modified_content_json IS NULL) = (modified_content_sha256 IS NULL))
);
CREATE TRIGGER IF NOT EXISTS review_resolutions_no_update
BEFORE UPDATE ON review_resolutions
BEGIN SELECT RAISE(ABORT, 'Review resolutions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS review_resolutions_no_delete
BEFORE DELETE ON review_resolutions
BEGIN SELECT RAISE(ABORT, 'Review resolutions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS policies_no_update BEFORE UPDATE ON policies
BEGIN SELECT RAISE(ABORT, 'Policies are append-only'); END;
CREATE TRIGGER IF NOT EXISTS policies_no_delete BEFORE DELETE ON policies
BEGIN SELECT RAISE(ABORT, 'Policies are append-only'); END;
{''.join(_ASSESSMENT_GUARDS)}
"""


def _initialize(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(_SCHEMA)
        connection.execute("BEGIN IMMEDIATE")
        (schema,) = connection.execute(
            "SELECT sql FROM sqlite_schema "
            "WHERE type = 'table' AND name = 'assessments'"
        ).fetchone()
        if "'allow'" not in schema:
            _migrate_assessment_verdicts(connection)


def _migrate_assessment_verdicts(connection: sqlite3.Connection) -> None:
    # Keep review_cases' FK pointed at the original table name during the rebuild.
    connection.execute("PRAGMA legacy_alter_table = ON")
    connection.execute("DROP TRIGGER review_cases_need_suspension")
    connection.execute("ALTER TABLE assessments RENAME TO assessments_previous")
    connection.execute(_ASSESSMENTS)
    connection.execute("INSERT INTO assessments SELECT * FROM assessments_previous")
    connection.execute("DROP TABLE assessments_previous")
    connection.execute(_CASE_GUARD)
    for statement in _ASSESSMENT_GUARDS:
        connection.execute(statement)
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise sqlite3.IntegrityError("Assessment migration failed foreign key check")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _content_json(
    content: RetainedQaContent | RetainedRecommendation | RecommendationSelection,
) -> str:
    if isinstance(content, RecommendationSelection):
        return json.dumps({"retained_ranks": content.ranks})
    if isinstance(content, RetainedRecommendation):
        return json.dumps({"recommendation": asdict(content)}, sort_keys=True)
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


def _read_content(
    document_json: str,
) -> RetainedQaContent | RetainedRecommendation | RecommendationSelection:
    document = json.loads(document_json)
    if "retained_ranks" in document:
        return RecommendationSelection(tuple(document["retained_ranks"]))
    if "recommendation" in document:
        recommendation = document["recommendation"]
        recommendation["allowed_ranks"] = tuple(recommendation["allowed_ranks"])
        return RetainedRecommendation(**recommendation)
    return RetainedQaContent(
        tuple(
            RetainedQaPair(pair["question"], pair["answer"])
            for pair in document["qa_pairs"]
        ),
        tuple(document["source_article_ids"]),
    )


class SqliteAssessmentRepository:
    """Local transactional storage, not the planned PostgreSQL forensic audit log.

    Queries return decision metadata and the linked case ID, never retained content.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        _initialize(path)

    def save(
        self,
        record: RecordedAssessment,
        review_case: ReviewCase | None,
        recommendation_audit: dict[str, object] | None = None,
    ) -> None:
        require_review_case_link(record, review_case)
        policy = record.policy
        assessment = record.assessment
        if (assessment.policy_id, assessment.policy_version) != (
            policy.policy_id,
            policy.version,
        ):
            raise ValueError("Assessment and policy identities must match")
        policy_fields = asdict(policy)
        if isinstance(policy, QaPolicy) and not policy.allow_complete:
            # Keep existing review-only snapshots byte-for-byte compatible.
            del policy_fields["allow_complete"]
        snapshot = json.dumps(policy_fields, sort_keys=True, separators=(",", ":"))
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
            if recommendation_audit is not None:
                connection.execute(
                    "INSERT INTO recommender_audit VALUES (?, ?)",
                    (record.assessment_id, json.dumps(recommendation_audit)),
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
        fields = json.loads(row["snapshot_json"])
        policy = (
            RecommenderPolicy(**fields)
            if "protected_attribute" in fields
            else QaPolicy(**fields)
        )
        return RecordedAssessment(
            row["assessment_id"],
            datetime.fromisoformat(row["created_at"]),
            policy,
            Assessment(
                Verdict(row["verdict"]),
                row["policy_id"],
                row["policy_version"],
                tuple(json.loads(row["reason_codes_json"])),
            ),
            row["case_id"],
        )


class SqliteQaDeliveryRepository:
    def __init__(self, path: Path, retain_content: bool = False) -> None:
        self._path = path
        self._retain_content = retain_content
        _initialize(path)

    def record(self, assessment_id: str, response_body: bytes) -> None:
        with closing(sqlite3.connect(self._path)) as connection, connection:
            connection.execute("PRAGMA foreign_keys = ON")
            inserted = connection.execute(
                """
                INSERT INTO qa_deliveries (assessment_id, prepared_at, response_sha256)
                VALUES (?, ?, ?)
                ON CONFLICT(assessment_id) DO NOTHING
                """,
                (
                    assessment_id,
                    datetime.now(UTC).isoformat(),
                    hashlib.sha256(response_body).hexdigest(),
                ),
            )
            if inserted.rowcount and self._retain_content:
                connection.execute(
                    "INSERT INTO qa_delivery_content VALUES (?, ?)",
                    (assessment_id, response_body),
                )

    def review_content(self, assessment_id: str) -> dict:
        with closing(sqlite3.connect(self._path)) as connection:
            row = connection.execute(
                """
                SELECT response_body, response_sha256
                FROM qa_delivery_content JOIN qa_deliveries USING (assessment_id)
                WHERE assessment_id = ?
                """,
                (assessment_id,),
            ).fetchone()
        if row is None:
            raise QaSampleNotFoundError
        if hashlib.sha256(row[0]).hexdigest() != row[1]:
            raise ReviewCaseIntegrityError("Delivery content integrity check failed")
        return json.loads(row[0])

    def save_review(self, sample_id: UUID, assessment_id: str, decision: dict) -> dict:
        document = json.dumps(decision, sort_keys=True)
        with closing(sqlite3.connect(self._path)) as connection, connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                """
                INSERT INTO qa_sample_reviews VALUES (?, ?, ?, ?)
                ON CONFLICT(sample_id, assessment_id) DO NOTHING
                """,
                (
                    str(sample_id),
                    assessment_id,
                    document,
                    datetime.now(UTC).isoformat(),
                ),
            )
            saved, resolved_at = connection.execute(
                """
                SELECT decision_json, resolved_at FROM qa_sample_reviews
                WHERE sample_id = ? AND assessment_id = ?
                """,
                (str(sample_id), assessment_id),
            ).fetchone()
            if saved != document:
                raise ReviewCaseAlreadyResolvedError
        return {**decision, "resolved_at": resolved_at}

    def intervention_counts(self, sample: QaSample) -> dict[str, int]:
        population = json.dumps(sample.population)
        with closing(sqlite3.connect(self._path)) as connection:
            prior, later, combined = connection.execute(
                """
                WITH eligible AS (SELECT value AS assessment_id FROM json_each(?)),
                prior AS (
                    SELECT assessment_id FROM eligible
                    JOIN review_cases USING (assessment_id)
                    JOIN review_resolutions USING (case_id)
                    WHERE supervisor_identity_id IS NOT NULL
                ),
                later AS (
                    SELECT assessment_id FROM eligible
                    JOIN qa_sample_reviews USING (assessment_id)
                    WHERE sample_id = ?
                )
                SELECT (SELECT count(*) FROM prior), (SELECT count(*) FROM later),
                       (SELECT count(*) FROM (
                           SELECT assessment_id FROM prior
                           UNION SELECT assessment_id FROM later
                       ))
                """,
                (population, str(sample.sample_id)),
            ).fetchone()
        return {
            "eligible": len(sample.population),
            "prior": prior,
            "retrospective": later,
            "combined": combined,
        }

    def eligible_ids(self, start: datetime, end: datetime) -> tuple[str, ...]:
        with closing(sqlite3.connect(self._path)) as connection:
            rows = connection.execute(
                """
                SELECT assessment_id FROM (
                    SELECT assessment_id,
                           CASE WHEN length(prepared_at) = 25
                                THEN replace(prepared_at, '+00:00', '.000000+00:00')
                                ELSE prepared_at END AS timestamp
                    FROM qa_deliveries
                )
                WHERE timestamp >= ? AND timestamp < ?
                  AND (? = 0 OR assessment_id IN (
                      SELECT assessment_id FROM qa_delivery_content
                  ))
                ORDER BY assessment_id
                """,
                (
                    start.astimezone(UTC).isoformat(timespec="microseconds"),
                    end.astimezone(UTC).isoformat(timespec="microseconds"),
                    self._retain_content,
                ),
            )
            return tuple(row[0] for row in rows)

    def save(self, sample: QaSample) -> None:
        document = asdict(sample)
        document["sample_id"] = str(sample.sample_id)
        document["start"] = sample.start.isoformat()
        document["end"] = sample.end.isoformat()
        with closing(sqlite3.connect(self._path)) as connection, connection:
            connection.execute(
                "INSERT INTO qa_samples VALUES (?, ?)",
                (str(sample.sample_id), json.dumps(document)),
            )

    def get(self, sample_id: UUID) -> QaSample:
        with closing(sqlite3.connect(self._path)) as connection:
            row = connection.execute(
                "SELECT snapshot_json FROM qa_samples WHERE sample_id = ?",
                (str(sample_id),),
            ).fetchone()
        if row is None:
            raise QaSampleNotFoundError
        document = json.loads(row[0])
        return QaSample(
            sample_id,
            datetime.fromisoformat(document["start"]),
            datetime.fromisoformat(document["end"]),
            document["seed"],
            QaSamplingPolicy(**document["policy"]),
            document["algorithm"],
            tuple(document["population"]),
            tuple(document["selected"]),
        )


class SqliteReviewCaseRepository:
    """Protected in-process reads of retained content, separate from audit queries.

    HTTP access goes only through the supervisor-protected review routes.
    shortcut: no encryption at rest; any process with file access can read it.
    Upgrade with encryption and the PostgreSQL store before real pilot data.
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
        parsed = _read_content(content)
        if isinstance(parsed, RecommendationSelection):
            raise ReviewCaseIntegrityError(
                "Stored case has selection instead of content"
            )
        return ReviewCase(
            case_id,
            assessment_id,
            datetime.fromisoformat(created_at),
            parsed,
        )


class SqliteReviewResolutionRepository:
    """Final review outcomes; the UNIQUE case_id enforces one per case.

    shortcut: modified content and justifications are stored in clear like the
    cases; upgrade with encryption at rest and the PostgreSQL store.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        _initialize(path)

    def save(self, resolution: ReviewResolution) -> None:
        supervisor = resolution.supervisor
        content = (
            _content_json(resolution.modified_content)
            if resolution.modified_content is not None
            else None
        )
        try:
            with closing(sqlite3.connect(self._path)) as connection, connection:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute(
                    "INSERT INTO review_resolutions VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        resolution.resolution_id,
                        resolution.case_id,
                        resolution.outcome.value,
                        resolution.resolved_at.isoformat(),
                        resolution.expires_at.isoformat(),
                        supervisor.identity_id if supervisor else None,
                        (
                            json.dumps({"display_name": supervisor.display_name})
                            if supervisor
                            else None
                        ),
                        resolution.justification,
                        content,
                        _sha256(content) if content is not None else None,
                    ),
                )
        except sqlite3.IntegrityError as error:
            if "review_resolutions.case_id" in str(error):
                raise ReviewCaseAlreadyResolvedError("Case already resolved") from error
            raise

    def get(self, case_id: str) -> ReviewResolution | None:
        with closing(sqlite3.connect(self._path)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM review_resolutions WHERE case_id = ?", (case_id,)
            ).fetchone()
        if row is None:
            return None
        content = row["modified_content_json"]
        if content is not None and _sha256(content) != row["modified_content_sha256"]:
            raise ReviewCaseIntegrityError("Stored resolution digest mismatch")
        modified = _read_content(content) if content is not None else None
        if isinstance(modified, RetainedRecommendation):
            raise ReviewCaseIntegrityError("Stored resolution has full recommendation")
        supervisor = None
        if row["supervisor_identity_id"] is not None:
            snapshot = json.loads(row["supervisor_snapshot_json"])
            supervisor = Supervisor(
                row["supervisor_identity_id"], snapshot["display_name"]
            )
        return ReviewResolution(
            row["resolution_id"],
            row["case_id"],
            ReviewOutcome(row["outcome"]),
            datetime.fromisoformat(row["resolved_at"]),
            datetime.fromisoformat(row["expires_at"]),
            supervisor,
            row["justification"],
            modified,
        )

    def pending_case_ids(self, created_before: datetime) -> tuple[str, ...]:
        with closing(sqlite3.connect(self._path)) as connection:
            rows = connection.execute(
                "SELECT c.case_id, c.created_at FROM review_cases c "
                "LEFT JOIN review_resolutions r ON r.case_id = c.case_id "
                "WHERE r.case_id IS NULL ORDER BY c.created_at"
            ).fetchall()
        # Compared as datetimes: stored ISO strings may differ in offset format.
        return tuple(
            case_id
            for case_id, created_at in rows
            if datetime.fromisoformat(created_at) < created_before
        )
