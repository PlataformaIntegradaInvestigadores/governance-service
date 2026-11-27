"""PostgreSQL store, roles and hash chain against a real server.

Set GOVERNANCE_TEST_POSTGRES_URL to a role that can create roles and schemas.
Each test gets its own schema; the roles are created once per session.
"""

import json
import os
import secrets
import shutil
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import errors, sql
from psycopg.conninfo import make_conninfo
from pydantic import ValidationError

from app import cli
from app.core.config import Settings
from app.core.tracing import current_trace_id
from app.domain.audit import AuditCheckpoint, verify_chain
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
    RetainedQaContent,
    RetainedQaPair,
    ReviewCase,
    ReviewCaseIntegrityError,
)
from app.domain.supervision import Supervisor
from app.infrastructure import postgres_store
from app.infrastructure.file_checkpoints import FileCheckpointRepository
from app.infrastructure.postgres_migrations import (
    MIGRATIONS,
    MigrationError,
    apply_migrations,
    require_current_schema,
)
from app.infrastructure.postgres_store import (
    APPEND_ONLY_TABLES,
    InsecureDatabaseRoleError,
    PostgresAssessmentRepository,
    PostgresAuditEventRepository,
    PostgresDatabase,
    PostgresQaDeliveryRepository,
    PostgresReviewCaseRepository,
    PostgresReviewResolutionRepository,
)
from app.main import create_app, create_review_cases, create_storage

ADMIN_URL = os.environ.get("GOVERNANCE_TEST_POSTGRES_URL")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(ADMIN_URL is None, reason="GOVERNANCE_TEST_POSTGRES_URL unset"),
]

_NOW = datetime(2026, 11, 27, 12, tzinfo=UTC)
_POLICY = QaPolicy("qa", "1", 1)
_CONTENT = RetainedQaContent((RetainedQaPair("Synthetic q", "Synthetic a"),), ("7",))


class Server:
    """Session roles plus a fresh schema per test, as centinela-ops grants them."""

    def __init__(self, admin_url):
        self.admin_url = admin_url
        prefix = f"gov_t{secrets.token_hex(3)}"
        self.passwords = {}
        self.roles = {}
        for kind in ("migrator", "app", "audit"):
            self.roles[kind] = f"{prefix}_{kind}"
            self.passwords[kind] = secrets.token_urlsafe(18)
        self.schemas = []

    def admin(self):
        return psycopg.connect(self.admin_url, autocommit=True)

    def create_roles(self):
        with self.admin() as connection:
            for kind, role in self.roles.items():
                connection.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER").format(
                        sql.Identifier(role), sql.Literal(self.passwords[kind])
                    )
                )

    def create_schema(self):
        schema = f"s{uuid4().hex[:12]}"
        role = sql.Identifier
        migrator, app, audit = (
            role(self.roles[k]) for k in ("migrator", "app", "audit")
        )
        statements = [
            sql.SQL("CREATE SCHEMA {} AUTHORIZATION {}").format(role(schema), migrator),
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}, {}").format(
                role(schema), app, audit
            ),
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} "
                "GRANT SELECT, INSERT ON TABLES TO {}"
            ).format(migrator, role(schema), app),
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} "
                "GRANT SELECT ON TABLES TO {}"
            ).format(migrator, role(schema), audit),
        ]
        with self.admin() as connection:
            for statement in statements:
                connection.execute(statement)
        self.schemas.append(schema)
        return schema

    def url(self, kind, schema):
        return make_conninfo(
            self.admin_url,
            user=self.roles[kind],
            password=self.passwords[kind],
            options=f"-c search_path={schema}",
        )

    def drop(self):
        with self.admin() as connection:
            for schema in self.schemas:
                connection.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                        sql.Identifier(schema)
                    )
                )
            for role in self.roles.values():
                connection.execute(
                    sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role))
                )
                connection.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


class Store:
    def __init__(self, server, schema):
        self.server = server
        self.schema = schema
        self.migrator_url = server.url("migrator", schema)
        self.app_url = server.url("app", schema)
        self.audit_url = server.url("audit", schema)
        self.database = PostgresDatabase(self.app_url)

    def admin_execute(self, statement, params=()):
        with self.server.admin() as connection:
            connection.execute(
                sql.SQL("SET search_path TO {}").format(sql.Identifier(self.schema))
            )
            cursor = connection.execute(statement, params)
            return cursor.fetchall() if cursor.description else None

    def reopen(self):
        """Fresh adapters on a fresh connection: nothing comes from memory."""
        return PostgresDatabase(self.app_url)

    def count(self, table):
        return self.admin_execute(
            sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
        )[0][0]

    def events(self):
        rows = self.admin_execute("SELECT body FROM audit_events ORDER BY seq")
        return [json.loads(body) for (body,) in rows]


@pytest.fixture(scope="session")
def server():
    instance = Server(ADMIN_URL)
    instance.create_roles()
    yield instance
    instance.drop()


@pytest.fixture
def empty_store(server):
    return Store(server, server.create_schema())


@pytest.fixture
def store(empty_store):
    apply_migrations(empty_store.migrator_url)
    return empty_store


def _record(verdict=Verdict.SUSPEND, policy=_POLICY):
    case_id = str(uuid4()) if verdict is Verdict.SUSPEND else None
    return RecordedAssessment(
        str(uuid4()),
        _NOW,
        policy,
        Assessment(verdict, policy.policy_id, policy.version, ("synthetic",)),
        case_id,
    )


def _case(record):
    return ReviewCase(record.review_case_id, record.assessment_id, _NOW, _CONTENT)


def _save_suspended(database):
    record = _record()
    PostgresAssessmentRepository(database).save(record, _case(record))
    return record


# Migrations


def test_migrations_apply_once_and_are_recorded(empty_store):
    assert apply_migrations(empty_store.migrator_url) == ("0001_audit_store",)
    assert apply_migrations(empty_store.migrator_url) == ()
    with psycopg.connect(empty_store.app_url) as connection:
        require_current_schema(connection)


def test_edited_applied_migration_is_refused(empty_store, tmp_path):
    apply_migrations(empty_store.migrator_url)
    shutil.copy(MIGRATIONS / "0001_audit_store.sql", tmp_path)
    (tmp_path / "0001_audit_store.sql").write_text("-- edited\n", encoding="utf-8")

    with pytest.raises(MigrationError, match="were edited"):
        apply_migrations(empty_store.migrator_url, tmp_path)


def test_database_ahead_of_the_code_is_refused(empty_store, tmp_path):
    apply_migrations(empty_store.migrator_url)

    with pytest.raises(MigrationError, match="unknown to the code"):
        apply_migrations(empty_store.migrator_url, tmp_path)


def test_failed_migration_leaves_no_partial_schema(empty_store, tmp_path):
    shutil.copy(MIGRATIONS / "0001_audit_store.sql", tmp_path)
    (tmp_path / "0002_broken.sql").write_text("CREATE TABLE x (", encoding="utf-8")

    with pytest.raises(errors.SyntaxError):
        apply_migrations(empty_store.migrator_url, tmp_path)

    assert empty_store.admin_execute(
        "SELECT count(*) FROM pg_tables WHERE schemaname = %s", (empty_store.schema,)
    ) == [(0,)]


def test_runtime_refuses_a_schema_without_migrations(empty_store):
    with pytest.raises(MigrationError, match="no applied migrations"):
        empty_store.database.require_runtime_role()


def test_runtime_refuses_a_schema_missing_migrations(store, tmp_path):
    shutil.copy(MIGRATIONS / "0001_audit_store.sql", tmp_path)
    (tmp_path / "0002_next.sql").write_text("SELECT 1;", encoding="utf-8")

    with (
        psycopg.connect(store.app_url) as connection,
        pytest.raises(MigrationError, match="missing migrations"),
    ):
        require_current_schema(connection, tmp_path)


# Roles and append-only enforcement


def test_runtime_role_has_only_select_and_insert(store):
    store.database.require_runtime_role()


def test_owner_role_is_refused_as_runtime_role(store):
    with pytest.raises(InsecureDatabaseRoleError, match="audit_events"):
        PostgresDatabase(store.migrator_url).require_runtime_role()


@pytest.mark.parametrize("table", APPEND_ONLY_TABLES)
@pytest.mark.parametrize(
    "statement", ["UPDATE {} SET {} = {}", "DELETE FROM {}", "TRUNCATE {}"]
)
def test_runtime_role_cannot_change_or_erase_records(store, table, statement):
    _save_suspended(store.database)
    with psycopg.connect(store.app_url) as connection:
        column = connection.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = %s ORDER BY ordinal_position LIMIT 1",
            (table,),
        ).fetchone()[0]
        query = sql.SQL(statement).format(
            sql.Identifier(table), sql.Identifier(column), sql.Identifier(column)
        )
        with pytest.raises(errors.InsufficientPrivilege, match="permission denied"):
            connection.execute(query)


@pytest.mark.parametrize("statement", ["UPDATE {} SET seq = seq", "DELETE FROM {}"])
def test_triggers_block_the_owner_too(store, statement):
    _save_suspended(store.database)
    with (
        psycopg.connect(store.migrator_url) as connection,
        pytest.raises(errors.InsufficientPrivilege, match="append-only"),
    ):
        connection.execute(sql.SQL(statement).format(sql.Identifier("audit_events")))


def test_truncate_trigger_blocks_the_owner(store):
    with (
        psycopg.connect(store.migrator_url) as connection,
        pytest.raises(errors.InsufficientPrivilege, match="append-only"),
    ):
        connection.execute("TRUNCATE assessments CASCADE")


def test_audit_role_can_read_but_not_write(store):
    _save_suspended(store.database)
    with psycopg.connect(store.audit_url) as connection:
        assert connection.execute("SELECT count(*) FROM audit_events").fetchone() == (
            3,
        )
        with pytest.raises(errors.InsufficientPrivilege, match="permission denied"):
            connection.execute("INSERT INTO qa_samples VALUES ('x', '{}')")


# Assessments, cases and resolutions


def test_suspended_assessment_and_case_survive_reopening(store):
    record = _save_suspended(store.database)

    database = store.reopen()
    loaded = PostgresAssessmentRepository(database).get(record.assessment_id)
    case = PostgresReviewCaseRepository(database).get(record.review_case_id)

    assert loaded == record
    assert loaded.created_at.tzinfo is UTC
    assert case == _case(record)
    assert [event["type"] for event in store.events()] == [
        "policy.registered",
        "assessment.recorded",
        "review_case.opened",
    ]


def test_events_hold_metadata_and_digests_but_no_retained_content(store):
    _save_suspended(store.database)

    bodies = json.dumps(store.events())

    assert "Synthetic a" not in bodies
    assert "Synthetic q" not in bodies
    assert store.events()[2]["data"]["content_sha256"]


def test_unknown_ids_return_none(store):
    assert PostgresAssessmentRepository(store.database).get("missing") is None
    assert PostgresReviewCaseRepository(store.database).get("missing") is None
    assert PostgresReviewResolutionRepository(store.database).get("missing") is None


def test_policy_is_registered_once_and_conflicts_leave_nothing(store):
    repository = PostgresAssessmentRepository(store.database)
    repository.save(_record(Verdict.DENY), None)
    repository.save(_record(Verdict.ALLOW), None)
    changed = replace(_POLICY, expected_pairs=2)

    with pytest.raises(PolicyVersionConflictError, match="new version"):
        repository.save(_record(Verdict.DENY, changed), None)

    assert store.count("assessments") == 2
    assert [event["type"] for event in store.events()].count("policy.registered") == 1
    assert verify_chain(PostgresAuditEventRepository(store.reopen()).events()).valid


def test_allow_complete_policy_is_stored_and_read_back(store):
    policy = QaPolicy("qa", "2", 1, allow_complete=True)
    record = _record(Verdict.ALLOW, policy)
    PostgresAssessmentRepository(store.database).save(record, None)

    loaded = PostgresAssessmentRepository(store.reopen()).get(record.assessment_id)

    assert loaded.policy.allow_complete


def test_mismatched_policy_identity_is_rejected(store):
    record = _record(Verdict.DENY)
    record = replace(record, assessment=replace(record.assessment, policy_version="9"))

    with pytest.raises(ValueError, match="identities must match"):
        PostgresAssessmentRepository(store.database).save(record, None)


def test_failure_inside_the_transaction_leaves_no_partial_record(store, monkeypatch):
    original = postgres_store._AuditTrail.append
    calls = []

    def failing(self, event_type, data):
        calls.append(event_type)
        if event_type == "review_case.opened":
            raise OSError("Simulated write failure")
        original(self, event_type, data)

    monkeypatch.setattr(postgres_store._AuditTrail, "append", failing)
    record = _record()

    with pytest.raises(OSError, match="Simulated write failure"):
        PostgresAssessmentRepository(store.database).save(record, _case(record))

    database = store.reopen()
    assert PostgresAssessmentRepository(database).get(record.assessment_id) is None
    assert [store.count(t) for t in ("policies", "assessments", "review_cases")] == [
        0,
        0,
        0,
    ]
    assert store.count("audit_events") == 0
    assert calls[-1] == "review_case.opened"


def test_case_needs_a_suspended_assessment_in_the_database(store):
    record = _record(Verdict.DENY)
    PostgresAssessmentRepository(store.database).save(record, None)

    with pytest.raises(errors.CheckViolation, match="suspended assessment"):
        store.admin_execute(
            "INSERT INTO review_cases VALUES (%s, %s, now(), '{}', %s)",
            (str(uuid4()), record.assessment_id, "a" * 64),
        )


def test_tampered_case_content_is_detected(store):
    record = _save_suspended(store.database)
    store.admin_execute("ALTER TABLE review_cases DISABLE TRIGGER USER")
    store.admin_execute(
        "UPDATE review_cases SET content_json = replace(content_json, 'a\"', 'b\"')"
    )

    with pytest.raises(ReviewCaseIntegrityError, match="digest mismatch"):
        PostgresReviewCaseRepository(store.database).get(record.review_case_id)


def test_tampered_policy_snapshot_is_detected(store):
    record = _save_suspended(store.database)
    store.admin_execute("ALTER TABLE policies DISABLE TRIGGER USER")
    store.admin_execute("UPDATE policies SET snapshot_json = snapshot_json || ' '")

    with pytest.raises(ValueError, match="snapshot digest mismatch"):
        PostgresAssessmentRepository(store.database).get(record.assessment_id)


def _resolution(record, outcome=ReviewOutcome.MODIFY):
    supervisor = None
    if outcome is not ReviewOutcome.EXPIRED:
        supervisor = Supervisor("sup-1", "Synthetic supervisor")
    return ReviewResolution(
        str(uuid4()),
        record.review_case_id,
        outcome,
        _NOW,
        _NOW + timedelta(hours=1),
        supervisor,
        "Synthetic reason" if supervisor else None,
        _CONTENT if outcome is ReviewOutcome.MODIFY else None,
    )


def test_resolution_survives_reopening_and_is_audited(store):
    record = _save_suspended(store.database)
    resolution = _resolution(record)
    PostgresReviewResolutionRepository(store.database).save(resolution)

    loaded = PostgresReviewResolutionRepository(store.reopen()).get(
        record.review_case_id
    )

    assert loaded == resolution
    event = store.events()[-1]
    assert event["type"] == "review.resolved"
    assert event["data"]["supervisor_identity_id"] == "sup-1"
    assert event["data"]["justification_sha256"]
    assert "Synthetic reason" not in json.dumps(event)


def test_expired_resolution_has_no_supervisor(store):
    record = _save_suspended(store.database)
    resolution = _resolution(record, ReviewOutcome.EXPIRED)
    PostgresReviewResolutionRepository(store.database).save(resolution)

    assert (
        PostgresReviewResolutionRepository(store.reopen()).get(record.review_case_id)
        == resolution
    )


def test_second_resolution_is_refused_and_not_audited(store):
    record = _save_suspended(store.database)
    repository = PostgresReviewResolutionRepository(store.database)
    repository.save(_resolution(record))
    events = store.count("audit_events")

    with pytest.raises(ReviewCaseAlreadyResolvedError, match="already resolved"):
        repository.save(_resolution(record, ReviewOutcome.REJECT))

    assert store.count("audit_events") == events


def test_other_unique_violations_propagate(store):
    record = _save_suspended(store.database)
    resolution = _resolution(record)
    PostgresReviewResolutionRepository(store.database).save(resolution)
    second = _save_suspended(store.database)

    with pytest.raises(errors.UniqueViolation, match="review_resolutions_pkey"):
        PostgresReviewResolutionRepository(store.database).save(
            replace(_resolution(second), resolution_id=resolution.resolution_id)
        )


def test_tampered_resolution_content_is_detected(store):
    record = _save_suspended(store.database)
    PostgresReviewResolutionRepository(store.database).save(_resolution(record))
    store.admin_execute("ALTER TABLE review_resolutions DISABLE TRIGGER USER")
    store.admin_execute(
        "UPDATE review_resolutions SET modified_content_json = "
        "replace(modified_content_json, '7', '8')"
    )

    with pytest.raises(ReviewCaseIntegrityError, match="digest mismatch"):
        PostgresReviewResolutionRepository(store.database).get(record.review_case_id)


def test_pending_cases_are_listed_until_resolved(store):
    first = _save_suspended(store.database)
    second = _save_suspended(store.database)
    repository = PostgresReviewResolutionRepository(store.database)
    repository.save(_resolution(first))

    assert repository.pending_case_ids(_NOW + timedelta(seconds=1)) == (
        second.review_case_id,
    )
    assert repository.pending_case_ids(_NOW) == ()


# Deliveries and retrospective samples


def test_delivery_is_recorded_once_with_its_digest(store):
    record = _record(Verdict.ALLOW)
    PostgresAssessmentRepository(store.database).save(record, None)
    deliveries = PostgresQaDeliveryRepository(store.database)

    deliveries.record(record.assessment_id, b'{"status":"success"}')
    deliveries.record(record.assessment_id, b'{"status":"other"}')

    assert store.count("qa_deliveries") == 1
    assert store.count("qa_delivery_content") == 0
    assert [e["type"] for e in store.events()].count("qa.delivery.prepared") == 1
    with pytest.raises(QaSampleNotFoundError):
        deliveries.review_content(record.assessment_id)


def _delivered(store, count=2):
    deliveries = PostgresQaDeliveryRepository(store.database, retain_content=True)
    identifiers = []
    for _ in range(count):
        record = _record(Verdict.ALLOW)
        PostgresAssessmentRepository(store.database).save(record, None)
        deliveries.record(record.assessment_id, b'{"qa_pairs":[]}')
        identifiers.append(record.assessment_id)
    return deliveries, sorted(identifiers)


def test_retained_delivery_is_readable_and_sampled(store):
    deliveries, identifiers = _delivered(store)
    start = datetime.now(UTC) - timedelta(minutes=1)
    end = datetime.now(UTC) + timedelta(minutes=1)

    assert deliveries.review_content(identifiers[0]) == {"qa_pairs": []}
    assert deliveries.eligible_ids(start, end) == tuple(identifiers)
    assert deliveries.eligible_ids(end, end + timedelta(minutes=1)) == ()
    unretained = PostgresQaDeliveryRepository(store.database)
    assert unretained.eligible_ids(start, end) == tuple(identifiers)


def test_tampered_delivery_content_is_detected(store):
    deliveries, identifiers = _delivered(store, 1)
    store.admin_execute("ALTER TABLE qa_delivery_content DISABLE TRIGGER USER")
    store.admin_execute("UPDATE qa_delivery_content SET response_body = '\\x7b7d'")

    with pytest.raises(ReviewCaseIntegrityError, match="integrity check"):
        deliveries.review_content(identifiers[0])


def _sample(identifiers):
    policy = QaSamplingPolicy("1", 10, 1)
    return QaSample(
        uuid4(),
        _NOW,
        _NOW + timedelta(days=1),
        42,
        policy,
        "sha256-seed-colon-id-v1",
        tuple(identifiers),
        tuple(identifiers[:1]),
    )


def test_sample_and_its_reviews_survive_reopening(store):
    deliveries, identifiers = _delivered(store)
    sample = _sample(identifiers)
    deliveries.save(sample)
    decision = {"outcome": "confirm", "supervisor_identity_id": "sup-1"}

    saved = deliveries.save_review(sample.sample_id, identifiers[0], decision)
    again = deliveries.save_review(sample.sample_id, identifiers[0], decision)

    reopened = PostgresQaDeliveryRepository(store.reopen(), retain_content=True)
    assert reopened.get(sample.sample_id) == sample
    assert saved == again
    assert datetime.fromisoformat(saved["resolved_at"]).tzinfo is not None
    assert reopened.intervention_counts(sample) == {
        "eligible": 2,
        "prior": 0,
        "retrospective": 1,
        "combined": 1,
    }
    types = [event["type"] for event in store.events()]
    assert types.count("qa.sample.created") == 1
    assert types.count("qa.sample.reviewed") == 1
    with pytest.raises(ReviewCaseAlreadyResolvedError):
        deliveries.save_review(
            sample.sample_id, identifiers[0], {**decision, "outcome": "correct"}
        )


def test_prior_supervision_counts_towards_interventions(store):
    record = _save_suspended(store.database)
    PostgresReviewResolutionRepository(store.database).save(_resolution(record))
    deliveries = PostgresQaDeliveryRepository(store.database, retain_content=True)
    deliveries.record(record.assessment_id, b"{}")
    sample = _sample([record.assessment_id])
    deliveries.save(sample)

    assert deliveries.intervention_counts(sample)["prior"] == 1


def test_missing_sample_is_reported(store):
    with pytest.raises(QaSampleNotFoundError):
        PostgresQaDeliveryRepository(store.database).get(uuid4())


# The hash chain


def test_chain_verifies_and_detects_tampering_by_a_superuser(store):
    for _ in range(3):
        _save_suspended(store.database)
    events = PostgresAuditEventRepository(PostgresDatabase(store.audit_url))
    assert verify_chain(events.events()).valid
    store.admin_execute("ALTER TABLE audit_events DISABLE TRIGGER USER")
    store.admin_execute(
        "UPDATE audit_events SET body = replace(body, 'suspend', 'allow') "
        "WHERE seq = 2"
    )

    report = verify_chain(events.events())

    assert (report.valid, report.failed_seq) == (False, 2)


def test_deleted_middle_event_is_a_gap(store):
    for _ in range(2):
        _save_suspended(store.database)
    store.admin_execute("ALTER TABLE audit_events DISABLE TRIGGER USER")
    store.admin_execute("DELETE FROM audit_events WHERE seq = 3")

    report = verify_chain(PostgresAuditEventRepository(store.database).events())

    assert (report.failed_seq, report.failure) == (4, "sequence gap or reordering")


def test_checkpoint_outside_the_database_detects_a_truncated_tail(store, tmp_path):
    for _ in range(2):
        _save_suspended(store.database)
    events = PostgresAuditEventRepository(store.database)
    head = verify_chain(events.events())
    checkpoint = AuditCheckpoint(head.head_seq, head.head_hash, _NOW)
    store.admin_execute("ALTER TABLE audit_events DISABLE TRIGGER USER")
    store.admin_execute("DELETE FROM audit_events WHERE seq > 3")

    assert verify_chain(events.events()).valid
    report = verify_chain(events.events(), [checkpoint])

    assert report.failure == "truncated before a checkpoint"


def test_concurrent_writers_keep_one_linear_chain(store):
    errors_seen = []

    def write():
        try:
            for _ in range(5):
                _save_suspended(PostgresDatabase(store.app_url))
        except psycopg.Error as error:  # pragma: no cover - reported below
            errors_seen.append(error)

    threads = [threading.Thread(target=write) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    report = verify_chain(PostgresAuditEventRepository(store.database).events())
    assert errors_seen == []
    assert report.valid
    assert report.events == 1 + 6 * 5 * 2
    assert store.admin_execute("SELECT max(seq) FROM audit_events") == [
        (report.events,)
    ]


def test_events_carry_the_current_trace_id(store):
    token = current_trace_id.set("trace-synthetic")
    try:
        _save_suspended(store.database)
    finally:
        current_trace_id.reset(token)
    _save_suspended(store.database)

    traces = [event["trace_id"] for event in store.events()]

    assert traces == ["trace-synthetic"] * 3 + [None, None]


# Composition, HTTP and operations commands


def _settings(store, tmp_path, **overrides):
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps({"policy_id": "qa", "version": "1", "expected_pairs": 1})
    )
    values = {
        "_env_file": None,
        "admin_verifier_base_url": "http://search.test",
        "database_url": store.app_url,
        "qa_upstream_base_url": "http://search.test",
        "qa_policy_path": policy,
        "qa_release_key": "synthetic-test-material-of-at-least-32-bytes",
    }
    return Settings(**(values | overrides))


def test_settings_refuse_two_stores(store, tmp_path):
    with pytest.raises(ValidationError, match="either a SQLite database or"):
        _settings(store, tmp_path, database=tmp_path / "x.sqlite3")


def test_settings_refuse_an_empty_database_url():
    with pytest.raises(ValidationError, match="at least 1 item"):
        Settings(_env_file=None, database_url="")


def test_composition_refuses_the_owner_role(store, tmp_path):
    with pytest.raises(InsecureDatabaseRoleError):
        create_storage(_settings(store, tmp_path, database_url=store.migrator_url))


def test_composition_without_a_store_has_no_review_cases(tmp_path):
    settings = Settings(_env_file=None)

    assert create_storage(settings) is None
    assert create_review_cases(settings) is None


def test_governed_qa_over_postgres_audits_with_the_request_trace(
    store, tmp_path, respx_mock
):
    client = TestClient(create_app(_settings(store, tmp_path)))
    respx_mock.post("http://search.test/api-se/v1/llm-search/generate-qa/").respond(
        json={
            "status": "success",
            "qa_pairs": [{"question": "Synthetic q", "answer": "Synthetic a"}],
            "source_docs": [{"article_id": 1}],
        }
    )

    response = client.post("/api/v1/governed/qa/generate", json={"query": "q"})

    assert response.status_code == 202
    traces = {event["trace_id"] for event in store.events()}
    assert traces == {response.headers["X-Trace-ID"]}
    assert client.get("/api/v1/status").json()["governance_enabled"]


def test_governed_qa_is_denied_when_postgres_fails(store, tmp_path, respx_mock):
    application = create_app(_settings(store, tmp_path))
    respx_mock.post("http://search.test/api-se/v1/llm-search/generate-qa/").respond(
        json={
            "status": "success",
            "qa_pairs": [{"question": "Synthetic q", "answer": "Synthetic a"}],
            "source_docs": [{"article_id": 1}],
        }
    )
    application.state.qa_assessor._assessments._database = PostgresDatabase(
        make_conninfo(store.app_url, dbname="governance_missing")
    )

    response = TestClient(application).post(
        "/api/v1/governed/qa/generate", json={"query": "q"}
    )

    assert response.status_code == 403
    assert response.json()["code"] == "QA_DENIED"


def _run_cli(monkeypatch, url, checkpoints, command):
    monkeypatch.setenv("GOVERNANCE_DATABASE_URL", url)
    if checkpoints is None:
        monkeypatch.delenv("GOVERNANCE_AUDIT_CHECKPOINT_PATH", raising=False)
    else:
        monkeypatch.setenv("GOVERNANCE_AUDIT_CHECKPOINT_PATH", str(checkpoints))
    return cli.main([command])


def test_cli_migrates_verifies_and_checkpoints(
    empty_store, tmp_path, monkeypatch, capsys
):
    checkpoints = tmp_path / "checkpoints.jsonl"
    url = empty_store.migrator_url

    assert _run_cli(monkeypatch, url, None, "migrate") == 0
    assert json.loads(capsys.readouterr().out) == {"applied": ["0001_audit_store"]}
    _save_suspended(empty_store.database)
    audit = empty_store.audit_url
    assert _run_cli(monkeypatch, audit, checkpoints, "checkpoint-audit") == 0
    assert json.loads(capsys.readouterr().out)["checkpoint"]["seq"] == 3
    assert _run_cli(monkeypatch, audit, checkpoints, "checkpoint-audit") == 0
    assert json.loads(capsys.readouterr().out) == {"checkpoint": None}
    assert _run_cli(monkeypatch, audit, checkpoints, "verify-audit") == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True

    empty_store.admin_execute("ALTER TABLE audit_events DISABLE TRIGGER USER")
    empty_store.admin_execute("DELETE FROM audit_events WHERE seq = 3")

    assert _run_cli(monkeypatch, audit, checkpoints, "verify-audit") == 1
    assert json.loads(capsys.readouterr().out)["failed_seq"] == 3
    assert _run_cli(monkeypatch, audit, checkpoints, "checkpoint-audit") == 1
    assert json.loads(capsys.readouterr().out)["valid"] is False
    assert len(FileCheckpointRepository(checkpoints).load()) == 1


def test_cli_reports_missing_configuration(store, monkeypatch, capsys):
    monkeypatch.delenv("GOVERNANCE_DATABASE_URL", raising=False)
    assert cli.main(["verify-audit"]) == 2
    assert "GOVERNANCE_DATABASE_URL" in capsys.readouterr().out

    assert _run_cli(monkeypatch, store.audit_url, None, "verify-audit") == 2
    assert "GOVERNANCE_AUDIT_CHECKPOINT_PATH" in capsys.readouterr().out
