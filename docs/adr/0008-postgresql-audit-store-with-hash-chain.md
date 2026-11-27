# ADR 0008: PostgreSQL audit store with a hash chain

## Status

Accepted on 2026-11-27 for staging (AB#258, AB#259, AB#260). Keeps the SQLite
store of ADR 0001 for local tests. Encryption and retention of retained content
remain pending (AB#321).

## Context

Decisions, review cases, resolutions, deliveries and retrospective samples are
accountability records. The SQLite prototype blocks updates with triggers and
checks a SHA-256 per policy and per content, but any process that can write the
file can rewrite it, and nothing shows whether rows were removed. The design
asks for PostgreSQL, a runtime role without `UPDATE` or `DELETE`, a hash chain
per event, a way to verify it and a checkpoint that does not live in the same
database.

## Decision

- `app.infrastructure.postgres_store` implements the existing ports with
  psycopg 3. `GOVERNANCE_DATABASE_URL` selects it; `GOVERNANCE_DATABASE`
  keeps SQLite. Both at once are rejected.
- Every write appends events to `audit_events` in the same transaction:
  `policy.registered`, `assessment.recorded`, `review_case.opened`,
  `review.resolved`, `qa.delivery.prepared`, `qa.sample.created` and
  `qa.sample.reviewed`. Each event holds its sequence, a UUID, the UTC time,
  the request trace ID and metadata. Retained content, justifications and
  sample decisions appear only as SHA-256 digests, so retention can remove
  them later without breaking the chain.
- `hash = sha256(prev_hash || body)`, where `body` is canonical JSON (sorted
  keys, no spaces) stored as `text`. `jsonb` would normalize the bytes that were
  hashed. The first event links to 64 zeros. Sequences are gapless.
- Writers take a transaction-scoped advisory lock before their first insert,
  so the chain stays linear under concurrency. The primary key on `seq` makes a
  writer that skips the lock fail instead of forking the chain.
- Migrations are SQL files under `app/infrastructure/migrations/`, applied in
  one transaction by `python -m app.cli migrate` with the migrator role and
  recorded with their SHA-256. An edited applied migration, or a database ahead
  of the code, stops the run. The runtime never migrates: it refuses to start
  if migrations are missing or if its role can `UPDATE`, `DELETE` or `TRUNCATE`
  any table.
- Roles come from `centinela-ops/production/governance/`: the runtime role has
  `SELECT, INSERT`; the migrator owns the tables; the audit role reads.
  Triggers also reject `UPDATE`, `DELETE` and `TRUNCATE` from the owner.
- `python -m app.cli verify-audit` recomputes the chain and reports the first
  broken sequence. `checkpoint-audit` verifies the chain and appends its head to
  a JSON lines file (`GOVERNANCE_AUDIT_CHECKPOINT_PATH`). Both run with the
  audit role. The file must live where the service cannot write.

## Threat model

| Change | Detected by |
|---|---|
| Runtime role updates, deletes or truncates | Refused by privileges and triggers |
| Owner role updates, deletes or truncates | Refused by triggers |
| Superuser edits an event | Hash mismatch at that sequence |
| Superuser removes an event in the middle | Sequence gap |
| Superuser recomputes the chain after an edit | Checkpoint mismatch, up to the last checkpoint |
| Superuser removes the tail | Checkpoint ahead of the head, up to the last checkpoint |
| Edits after the last checkpoint and before the next | Not detected |
| Edit of a domain table without its event | Digest checks on read; the event keeps the original digest |

A superuser can disable triggers and change privileges. The chain does not
prevent that; it makes it visible against a checkpoint the attacker cannot
also change. Checkpoints are as frequent as the job that writes them.

## Alternatives

- Keep SQLite with checksums: no role separation and no detection of removed
  rows.
- Store the chain in `jsonb`: smaller queries, but the hashed bytes would not
  be the stored bytes.
- One chain per table: simpler locks, but a removed related record would not
  break any chain.
- Alembic or an ORM for migrations: a new dependency for one schema; plain SQL
  files with checksums are enough and keep the SQL reviewable.
- Signed checkpoints (HMAC): adds a key to manage; separation of storage is
  required either way and is the control documented here.

## Consequences

- PostgreSQL 16 or later and psycopg 3.3.9 (LGPL-3.0, used as a library).
- One connection per operation and one global writer lock: enough for the
  evaluation load, not tuned for high throughput.
  shortcut: no connection pool; add psycopg_pool if latency measurements show
  connection set-up in the p95.
- Integration tests need `GOVERNANCE_TEST_POSTGRES_URL`; CI provides a
  PostgreSQL service. Without it, those tests are skipped locally.
- The recommender storage on `feature/recommender-fairness-monitor` must be
  ported to this store when it reaches `develop`.
