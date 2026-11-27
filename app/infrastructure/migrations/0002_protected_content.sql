-- Protected content moves to one encrypted table with a retention deadline
-- (AB#321). Domain tables keep only the SHA-256 that ties each record to its
-- content, so purging content never touches an audited record or the chain.

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM review_cases)
        OR EXISTS (SELECT 1 FROM review_resolutions)
        OR EXISTS (SELECT 1 FROM qa_delivery_content)
        OR EXISTS (SELECT 1 FROM qa_sample_reviews) THEN
        RAISE EXCEPTION 'Clear-text protected content exists; it must be '
            'encrypted with the content key before this migration';
    END IF;
END;
$$;

CREATE TABLE protected_content (
    content_id text PRIMARY KEY,
    key_id text NOT NULL,
    nonce bytea NOT NULL CHECK (length(nonce) = 12),
    ciphertext bytea NOT NULL,
    sha256 char(64) NOT NULL,
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL CHECK (expires_at >= created_at)
);

CREATE FUNCTION governance_purge_only_expired() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.expires_at > now() THEN
        RAISE EXCEPTION 'protected_content % is within its retention period',
            OLD.content_id USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN OLD;
END;
$$;

CREATE TRIGGER protected_content_no_update BEFORE UPDATE ON protected_content
FOR EACH ROW EXECUTE FUNCTION governance_reject_change();
CREATE TRIGGER protected_content_purge_expired BEFORE DELETE ON protected_content
FOR EACH ROW EXECUTE FUNCTION governance_purge_only_expired();
CREATE TRIGGER protected_content_no_truncate BEFORE TRUNCATE ON protected_content
FOR EACH STATEMENT EXECUTE FUNCTION governance_reject_change();

ALTER TABLE review_cases DROP COLUMN content_json;

-- Dropping the clear-text columns also drops the CHECKs that used them.
ALTER TABLE review_resolutions
    DROP COLUMN justification,
    DROP COLUMN modified_content_json,
    ADD COLUMN justification_sha256 char(64),
    ADD CONSTRAINT review_resolutions_modify_has_content
        CHECK ((outcome = 'modify') = (modified_content_sha256 IS NOT NULL));

ALTER TABLE qa_delivery_content DROP COLUMN response_body;

ALTER TABLE qa_sample_reviews
    DROP COLUMN decision_json,
    ADD COLUMN outcome text NOT NULL,
    ADD COLUMN decision_sha256 char(64) NOT NULL;
