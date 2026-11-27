# ADR 0009: Encrypted protected content with retention

## Status

Accepted on 2026-11-27 for the PostgreSQL store (AB#321). Builds on ADR 0008.
Replaces the "stored in clear" limit of ADR 0001 for PostgreSQL; the SQLite
prototype keeps it and stays for synthetic local tests.

## Context

Review cases, justifications, modified content, retained deliveries and
retrospective sample decisions can contain generated text and reviewer notes.
They must not be readable from a database copy, must disappear after a
retention period, and their removal must not alter the audit records or break
the hash chain. Real data needs this first.

## Decision

- Protected content lives in one table, `protected_content`, encrypted with
  AES-256-GCM (`cryptography`). Each row holds its key ID, a random 96-bit
  nonce, the ciphertext, the plaintext SHA-256, its creation time and its
  retention deadline. The content ID and key ID are authenticated data, so a
  ciphertext moved to another row fails to open.
- Domain tables keep only the SHA-256 (`content_sha256`, `justification_sha256`,
  `modified_content_sha256`, `response_sha256`, `decision_sha256`). Reads open
  the ciphertext and compare it with that digest. Audit events already carry
  the same digests (ADR 0008).
- Keys come from `GOVERNANCE_CONTENT_KEYS` (`id:base64key[,...]`, 32-byte keys),
  outside the repository and the database. The first key seals; every listed
  key opens, which allows rotation. The service refuses PostgreSQL storage
  without keys. Keys never appear in errors or `repr`.
- `GOVERNANCE_CONTENT_RETENTION_DAYS` (default 30, 1 to 3650) sets the deadline
  and must outlast the review window. `python -m app.cli purge-expired`, with
  the runtime role, deletes expired rows and appends one
  `protected_content.purged` event per row, with the content ID and digest, in
  the same transaction.
- The runtime role may `DELETE` only on `protected_content`. A trigger refuses
  rows before their deadline; `UPDATE` and `TRUNCATE` stay forbidden there and
  everywhere else, and the service refuses a role that has them.
- Content that was purged reads as `ProtectedContentUnavailableError`: the
  review poll is denied (403) and other routes answer 410 `CONTENT_EXPIRED`.
- Migration `0002_protected_content` drops the clear-text columns. It stops if
  any protected row exists, because clear text cannot be encrypted without the
  key inside a SQL migration.

## Alternatives

- `pgcrypto` in the database: the key would travel to the database server and
  appear in statements and logs.
- Keep encrypted columns in each domain table: retention would need `UPDATE`
  on audited tables.
- Anonymize in place instead of deleting: also needs `UPDATE`; deleting the
  ciphertext and keeping the digest achieves the same with fewer privileges.
- A key management service: not available in the platform; the key ID column
  allows moving to one later.

## Consequences

- New dependency `cryptography` 50.0.2 (Apache-2.0 or BSD).
- Losing every key makes retained content unreadable; the audited records and
  digests survive. Rotation needs the old key listed until its content expires.
- Whoever holds both a database copy and the key can read unexpired content.
- A superuser can still disable the deadline trigger; each purge leaves an
  event, but a deletion outside the service does not (the digest stays and the
  read reports the content as unavailable).
- The purge job must be scheduled; without it content stays until it runs.
