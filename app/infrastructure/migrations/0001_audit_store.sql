-- Governance store for PostgreSQL 16+. Applied by the migrator role, which owns
-- the objects. The runtime role receives SELECT and INSERT only (centinela-ops).

CREATE FUNCTION governance_reject_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE TABLE policies (
    policy_id text NOT NULL,
    version text NOT NULL,
    snapshot_json text NOT NULL,
    sha256 char(64) NOT NULL,
    PRIMARY KEY (policy_id, version)
);

CREATE TABLE assessments (
    assessment_id text PRIMARY KEY,
    created_at timestamptz NOT NULL,
    policy_id text NOT NULL,
    policy_version text NOT NULL,
    verdict text NOT NULL CHECK (verdict IN ('deny', 'suspend', 'allow')),
    reason_codes_json text NOT NULL,
    FOREIGN KEY (policy_id, policy_version) REFERENCES policies (policy_id, version)
);

CREATE TABLE review_cases (
    case_id text PRIMARY KEY,
    assessment_id text NOT NULL UNIQUE REFERENCES assessments (assessment_id),
    created_at timestamptz NOT NULL,
    content_json text NOT NULL,
    content_sha256 char(64) NOT NULL
);

CREATE FUNCTION governance_require_suspension() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (SELECT verdict FROM assessments WHERE assessment_id = NEW.assessment_id)
        IS DISTINCT FROM 'suspend' THEN
        RAISE EXCEPTION 'Review cases require a suspended assessment'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER review_cases_need_suspension BEFORE INSERT ON review_cases
FOR EACH ROW EXECUTE FUNCTION governance_require_suspension();

CREATE TABLE review_resolutions (
    resolution_id text PRIMARY KEY,
    case_id text NOT NULL REFERENCES review_cases (case_id),
    outcome text NOT NULL CHECK (outcome IN ('approve', 'modify', 'reject', 'expired')),
    resolved_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    supervisor_identity_id text,
    supervisor_snapshot_json text,
    justification text,
    modified_content_json text,
    modified_content_sha256 char(64),
    CONSTRAINT review_resolutions_one_per_case UNIQUE (case_id),
    CHECK ((outcome = 'expired') = (supervisor_identity_id IS NULL)),
    CHECK ((supervisor_identity_id IS NULL) = (supervisor_snapshot_json IS NULL)),
    CHECK ((outcome = 'modify') = (modified_content_json IS NOT NULL)),
    CHECK ((modified_content_json IS NULL) = (modified_content_sha256 IS NULL))
);

CREATE TABLE qa_deliveries (
    assessment_id text PRIMARY KEY REFERENCES assessments (assessment_id),
    prepared_at timestamptz NOT NULL,
    response_sha256 char(64) NOT NULL
);

CREATE TABLE qa_delivery_content (
    assessment_id text PRIMARY KEY REFERENCES qa_deliveries (assessment_id),
    response_body bytea NOT NULL
);

CREATE TABLE qa_samples (
    sample_id text PRIMARY KEY,
    snapshot_json text NOT NULL
);

CREATE TABLE qa_sample_reviews (
    sample_id text NOT NULL REFERENCES qa_samples (sample_id),
    assessment_id text NOT NULL REFERENCES qa_delivery_content (assessment_id),
    decision_json text NOT NULL,
    resolved_at timestamptz NOT NULL,
    PRIMARY KEY (sample_id, assessment_id)
);

-- hash = sha256(prev_hash || body); body is canonical JSON kept as exact text
-- (jsonb would reorder keys and change the hashed bytes).
CREATE TABLE audit_events (
    seq bigint PRIMARY KEY CHECK (seq > 0),
    body text NOT NULL,
    prev_hash char(64) NOT NULL CHECK (prev_hash ~ '^[0-9a-f]{64}$'),
    hash char(64) NOT NULL UNIQUE CHECK (hash ~ '^[0-9a-f]{64}$')
);

DO $$
DECLARE
    name text;
BEGIN
    FOREACH name IN ARRAY ARRAY[
        'policies', 'assessments', 'review_cases', 'review_resolutions',
        'qa_deliveries', 'qa_delivery_content', 'qa_samples', 'qa_sample_reviews',
        'audit_events'
    ] LOOP
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I '
            'FOR EACH ROW EXECUTE FUNCTION governance_reject_change()',
            name || '_append_only', name
        );
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE TRUNCATE ON %I '
            'FOR EACH STATEMENT EXECUTE FUNCTION governance_reject_change()',
            name || '_no_truncate', name
        );
    END LOOP;
END;
$$;
