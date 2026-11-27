import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from uuid import UUID, uuid4

import psycopg
from psycopg import errors

from app.core.tracing import current_trace_id
from app.domain.audit import GENESIS_HASH, AuditEvent, canonical_json, chain_hash
from app.domain.qa import (
    Assessment,
    PolicyVersionConflictError,
    QaPolicy,
    RecordedAssessment,
    Verdict,
)
from app.domain.qa_sampling import QaSample, QaSampleNotFoundError, QaSamplingPolicy
from app.domain.resolution import (
    ReviewCaseAlreadyResolvedError,
    ReviewOutcome,
    ReviewResolution,
)
from app.domain.review import (
    ReviewCase,
    ReviewCaseIntegrityError,
    require_review_case_link,
)
from app.domain.supervision import Supervisor
from app.infrastructure.postgres_migrations import require_current_schema
from app.infrastructure.sqlite_assessment_repository import (
    _content_json,
    _read_content,
    _sha256,
)

_AUDIT_LOCK = 7_301_202_602
APPEND_ONLY_TABLES = (
    "policies",
    "assessments",
    "review_cases",
    "review_resolutions",
    "qa_deliveries",
    "qa_delivery_content",
    "qa_samples",
    "qa_sample_reviews",
    "audit_events",
)


class InsecureDatabaseRoleError(Exception):
    """The runtime role could rewrite or erase audit records."""


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


class _AuditTrail:
    """Appends chained events inside the caller's transaction."""

    def __init__(self, connection: psycopg.Connection) -> None:
        self.connection = connection
        self._head: tuple[int, str] | None = None

    def append(self, event_type: str, data: dict[str, object]) -> None:
        if self._head is None:
            row = self.connection.execute(
                "SELECT seq, hash FROM audit_events ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            self._head = (row[0], row[1]) if row else (0, GENESIS_HASH)
        seq, prev_hash = self._head[0] + 1, self._head[1]
        body = canonical_json(
            {
                "seq": seq,
                "event_id": str(uuid4()),
                "type": event_type,
                "recorded_at": datetime.now(UTC).isoformat(),
                "trace_id": current_trace_id.get(),
                "data": data,
            }
        )
        digest = chain_hash(prev_hash, body)
        self.connection.execute(
            "INSERT INTO audit_events VALUES (%s, %s, %s, %s)",
            (seq, body, prev_hash, digest),
        )
        self._head = (seq, digest)


class PostgresDatabase:
    """Connection settings plus the checks the runtime makes before serving."""

    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    def connect(self) -> psycopg.Connection:
        return psycopg.connect(self._conninfo)

    def require_runtime_role(self) -> None:
        with self.connect() as connection:
            require_current_schema(connection)
            (writable,) = connection.execute(
                "SELECT coalesce(array_agg(name ORDER BY name), '{}') "
                "FROM unnest(%s::text[]) AS name WHERE has_table_privilege("
                "current_user, name, 'UPDATE, DELETE, TRUNCATE')",
                (list(APPEND_ONLY_TABLES),),
            ).fetchone()
        if writable:
            raise InsecureDatabaseRoleError(
                f"Runtime role can modify append-only tables: {writable}"
            )

    @contextmanager
    def audited(self) -> Iterator[_AuditTrail]:
        """One transaction; writers are serialized so the chain stays linear."""
        with self.connect() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (_AUDIT_LOCK,))
            yield _AuditTrail(connection)


def _policy_snapshot(policy: QaPolicy) -> str:
    fields = asdict(policy)
    if not policy.allow_complete:
        # Same bytes as the SQLite store for review-only policies.
        del fields["allow_complete"]
    return json.dumps(fields, sort_keys=True, separators=(",", ":"))


class PostgresAssessmentRepository:
    """Decision metadata and the linked case ID, never retained content."""

    def __init__(self, database: PostgresDatabase) -> None:
        self._database = database

    def save(self, record: RecordedAssessment, review_case: ReviewCase | None) -> None:
        require_review_case_link(record, review_case)
        policy = record.policy
        assessment = record.assessment
        if (assessment.policy_id, assessment.policy_version) != (
            policy.policy_id,
            policy.version,
        ):
            raise ValueError("Assessment and policy identities must match")
        snapshot = _policy_snapshot(policy)
        digest = _sha256(snapshot)
        with self._database.audited() as trail:
            inserted = trail.connection.execute(
                "INSERT INTO policies VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (policy.policy_id, policy.version, snapshot, digest),
            ).rowcount
            stored = trail.connection.execute(
                "SELECT snapshot_json, sha256 FROM policies "
                "WHERE policy_id = %s AND version = %s",
                (policy.policy_id, policy.version),
            ).fetchone()
            if stored != (snapshot, digest):
                raise PolicyVersionConflictError(
                    "Policy content changed without a new version"
                )
            if inserted:
                trail.append(
                    "policy.registered",
                    {
                        "policy_id": policy.policy_id,
                        "version": policy.version,
                        "sha256": digest,
                    },
                )
            trail.connection.execute(
                "INSERT INTO assessments VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    record.assessment_id,
                    record.created_at,
                    policy.policy_id,
                    policy.version,
                    assessment.verdict.value,
                    json.dumps(assessment.reason_codes),
                ),
            )
            trail.append(
                "assessment.recorded",
                {
                    "assessment_id": record.assessment_id,
                    "created_at": record.created_at.isoformat(),
                    "policy_id": policy.policy_id,
                    "policy_version": policy.version,
                    "policy_sha256": digest,
                    "verdict": assessment.verdict.value,
                    "reason_codes": list(assessment.reason_codes),
                    "review_case_id": record.review_case_id,
                },
            )
            if review_case is not None:
                self._open_case(trail, review_case)

    @staticmethod
    def _open_case(trail: _AuditTrail, review_case: ReviewCase) -> None:
        content = _content_json(review_case.content)
        content_digest = _sha256(content)
        trail.connection.execute(
            "INSERT INTO review_cases VALUES (%s, %s, %s, %s, %s)",
            (
                review_case.case_id,
                review_case.assessment_id,
                review_case.created_at,
                content,
                content_digest,
            ),
        )
        trail.append(
            "review_case.opened",
            {
                "case_id": review_case.case_id,
                "assessment_id": review_case.assessment_id,
                "created_at": review_case.created_at.isoformat(),
                "content_sha256": content_digest,
            },
        )

    def get(self, assessment_id: str) -> RecordedAssessment | None:
        with self._database.connect() as connection:
            row = connection.execute(
                """SELECT a.assessment_id, a.created_at, a.policy_id,
                          a.policy_version, a.verdict, a.reason_codes_json,
                          p.snapshot_json, p.sha256, r.case_id
                   FROM assessments a JOIN policies p
                   ON a.policy_id = p.policy_id AND a.policy_version = p.version
                   LEFT JOIN review_cases r ON r.assessment_id = a.assessment_id
                   WHERE a.assessment_id = %s""",
                (assessment_id,),
            ).fetchone()
        if row is None:
            return None
        identifier, created_at, policy_id, version, verdict, reasons = row[:6]
        snapshot, digest, case_id = row[6:]
        if _sha256(snapshot) != digest:
            raise ValueError("Stored policy snapshot digest mismatch")
        return RecordedAssessment(
            identifier,
            _utc(created_at),
            QaPolicy(**json.loads(snapshot)),
            Assessment(
                Verdict(verdict), policy_id, version, tuple(json.loads(reasons))
            ),
            case_id,
        )


class PostgresReviewCaseRepository:
    """Protected in-process reads of retained content, separate from audit queries.

    shortcut: content is stored in clear until AB#321 adds encryption at rest.
    """

    def __init__(self, database: PostgresDatabase) -> None:
        self._database = database

    def get(self, case_id: str) -> ReviewCase | None:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT case_id, assessment_id, created_at, content_json, "
                "content_sha256 FROM review_cases WHERE case_id = %s",
                (case_id,),
            ).fetchone()
        if row is None:
            return None
        case_id, assessment_id, created_at, content, digest = row
        if _sha256(content) != digest:
            raise ReviewCaseIntegrityError("Stored review case digest mismatch")
        return ReviewCase(
            case_id, assessment_id, _utc(created_at), _read_content(content)
        )


def _optional_sha256(text: str | None) -> str | None:
    return _sha256(text) if text is not None else None


class PostgresReviewResolutionRepository:
    """Final review outcomes; one per case, enforced by a unique constraint."""

    def __init__(self, database: PostgresDatabase) -> None:
        self._database = database

    def save(self, resolution: ReviewResolution) -> None:
        supervisor = resolution.supervisor
        content = (
            _content_json(resolution.modified_content)
            if resolution.modified_content is not None
            else None
        )
        try:
            with self._database.audited() as trail:
                trail.connection.execute(
                    "INSERT INTO review_resolutions VALUES "
                    "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        resolution.resolution_id,
                        resolution.case_id,
                        resolution.outcome.value,
                        resolution.resolved_at,
                        resolution.expires_at,
                        supervisor.identity_id if supervisor else None,
                        (
                            json.dumps({"display_name": supervisor.display_name})
                            if supervisor
                            else None
                        ),
                        resolution.justification,
                        content,
                        _optional_sha256(content),
                    ),
                )
                trail.append(
                    "review.resolved",
                    {
                        "resolution_id": resolution.resolution_id,
                        "case_id": resolution.case_id,
                        "outcome": resolution.outcome.value,
                        "resolved_at": resolution.resolved_at.isoformat(),
                        "expires_at": resolution.expires_at.isoformat(),
                        "supervisor_identity_id": (
                            supervisor.identity_id if supervisor else None
                        ),
                        "justification_sha256": _optional_sha256(
                            resolution.justification
                        ),
                        "modified_content_sha256": _optional_sha256(content),
                    },
                )
        except errors.UniqueViolation as error:
            if error.diag.constraint_name == "review_resolutions_one_per_case":
                raise ReviewCaseAlreadyResolvedError("Case already resolved") from error
            raise

    def get(self, case_id: str) -> ReviewResolution | None:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT resolution_id, case_id, outcome, resolved_at, expires_at, "
                "supervisor_identity_id, supervisor_snapshot_json, justification, "
                "modified_content_json, modified_content_sha256 "
                "FROM review_resolutions WHERE case_id = %s",
                (case_id,),
            ).fetchone()
        if row is None:
            return None
        identifier, case, outcome, resolved_at, expires_at = row[:5]
        identity_id, snapshot, justification, content, digest = row[5:]
        if content is not None and _sha256(content) != digest:
            raise ReviewCaseIntegrityError("Stored resolution digest mismatch")
        supervisor = (
            Supervisor(identity_id, json.loads(snapshot)["display_name"])
            if identity_id is not None
            else None
        )
        return ReviewResolution(
            identifier,
            case,
            ReviewOutcome(outcome),
            _utc(resolved_at),
            _utc(expires_at),
            supervisor,
            justification,
            _read_content(content) if content is not None else None,
        )

    def pending_case_ids(self, created_before: datetime) -> tuple[str, ...]:
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT c.case_id FROM review_cases c "
                "LEFT JOIN review_resolutions r ON r.case_id = c.case_id "
                "WHERE r.case_id IS NULL AND c.created_at < %s "
                "ORDER BY c.created_at",
                (created_before,),
            ).fetchall()
        return tuple(row[0] for row in rows)


class PostgresQaDeliveryRepository:
    def __init__(
        self, database: PostgresDatabase, retain_content: bool = False
    ) -> None:
        self._database = database
        self._retain_content = retain_content

    def record(self, assessment_id: str, response_body: bytes) -> None:
        prepared_at = datetime.now(UTC)
        digest = hashlib.sha256(response_body).hexdigest()
        with self._database.audited() as trail:
            inserted = trail.connection.execute(
                "INSERT INTO qa_deliveries VALUES (%s, %s, %s) "
                "ON CONFLICT (assessment_id) DO NOTHING",
                (assessment_id, prepared_at, digest),
            ).rowcount
            if not inserted:
                return
            if self._retain_content:
                trail.connection.execute(
                    "INSERT INTO qa_delivery_content VALUES (%s, %s)",
                    (assessment_id, response_body),
                )
            trail.append(
                "qa.delivery.prepared",
                {
                    "assessment_id": assessment_id,
                    "prepared_at": prepared_at.isoformat(),
                    "response_sha256": digest,
                    "content_retained": self._retain_content,
                },
            )

    def review_content(self, assessment_id: str) -> dict:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT response_body, response_sha256 FROM qa_delivery_content "
                "JOIN qa_deliveries USING (assessment_id) WHERE assessment_id = %s",
                (assessment_id,),
            ).fetchone()
        if row is None:
            raise QaSampleNotFoundError
        body = bytes(row[0])
        if hashlib.sha256(body).hexdigest() != row[1]:
            raise ReviewCaseIntegrityError("Delivery content integrity check failed")
        return json.loads(body)

    def save_review(self, sample_id: UUID, assessment_id: str, decision: dict) -> dict:
        document = json.dumps(decision, sort_keys=True)
        with self._database.audited() as trail:
            inserted = trail.connection.execute(
                "INSERT INTO qa_sample_reviews VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (sample_id, assessment_id) DO NOTHING",
                (str(sample_id), assessment_id, document, datetime.now(UTC)),
            ).rowcount
            saved, resolved_at = trail.connection.execute(
                "SELECT decision_json, resolved_at FROM qa_sample_reviews "
                "WHERE sample_id = %s AND assessment_id = %s",
                (str(sample_id), assessment_id),
            ).fetchone()
            if saved != document:
                raise ReviewCaseAlreadyResolvedError
            if inserted:
                trail.append(
                    "qa.sample.reviewed",
                    {
                        "sample_id": str(sample_id),
                        "assessment_id": assessment_id,
                        "outcome": decision.get("outcome"),
                        "supervisor_identity_id": decision.get(
                            "supervisor_identity_id"
                        ),
                        "decision_sha256": _sha256(document),
                        "resolved_at": _utc(resolved_at).isoformat(),
                    },
                )
        return {**decision, "resolved_at": _utc(resolved_at).isoformat()}

    def intervention_counts(self, sample: QaSample) -> dict[str, int]:
        with self._database.connect() as connection:
            prior, later, combined = connection.execute(
                """
                WITH eligible AS (SELECT unnest(%s::text[]) AS assessment_id),
                prior AS (
                    SELECT assessment_id FROM eligible
                    JOIN review_cases USING (assessment_id)
                    JOIN review_resolutions USING (case_id)
                    WHERE supervisor_identity_id IS NOT NULL
                ),
                later AS (
                    SELECT assessment_id FROM eligible
                    JOIN qa_sample_reviews USING (assessment_id)
                    WHERE sample_id = %s
                )
                SELECT (SELECT count(*) FROM prior), (SELECT count(*) FROM later),
                       (SELECT count(*) FROM (
                           SELECT assessment_id FROM prior
                           UNION SELECT assessment_id FROM later
                       ) AS combined)
                """,
                (list(sample.population), str(sample.sample_id)),
            ).fetchone()
        return {
            "eligible": len(sample.population),
            "prior": prior,
            "retrospective": later,
            "combined": combined,
        }

    def eligible_ids(self, start: datetime, end: datetime) -> tuple[str, ...]:
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT assessment_id FROM qa_deliveries "
                "WHERE prepared_at >= %s AND prepared_at < %s AND (NOT %s OR "
                "assessment_id IN (SELECT assessment_id FROM qa_delivery_content)) "
                "ORDER BY assessment_id",
                (start, end, self._retain_content),
            ).fetchall()
        return tuple(row[0] for row in rows)

    def save(self, sample: QaSample) -> None:
        document = asdict(sample)
        document["sample_id"] = str(sample.sample_id)
        document["start"] = sample.start.isoformat()
        document["end"] = sample.end.isoformat()
        snapshot = json.dumps(document)
        with self._database.audited() as trail:
            trail.connection.execute(
                "INSERT INTO qa_samples VALUES (%s, %s)",
                (str(sample.sample_id), snapshot),
            )
            trail.append(
                "qa.sample.created",
                {
                    "sample_id": str(sample.sample_id),
                    "snapshot_sha256": _sha256(snapshot),
                    "population": len(sample.population),
                    "selected": list(sample.selected),
                },
            )

    def get(self, sample_id: UUID) -> QaSample:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT snapshot_json FROM qa_samples WHERE sample_id = %s",
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


class PostgresAuditEventRepository:
    def __init__(self, database: PostgresDatabase) -> None:
        self._database = database

    def events(self) -> Iterator[AuditEvent]:
        with (
            self._database.connect() as connection,
            connection.cursor(name="audit_events") as cursor,
        ):
            cursor.execute(
                "SELECT seq, body, prev_hash, hash FROM audit_events ORDER BY seq"
            )
            for seq, body, prev_hash, digest in cursor:
                yield AuditEvent(seq, body, prev_hash, digest)
