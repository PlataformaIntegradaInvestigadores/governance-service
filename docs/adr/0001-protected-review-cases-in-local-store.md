# ADR 0001: Protected review cases in the local store

Status: Accepted for the local prototype, 2026-11-05.

## Context

Suspended Q&A output must stay withheld until a reviewer decides on it. The
reviewer needs the withheld content, linked to the exact decision and policy
version that suspended it. Until now the store kept decision metadata only, so
a suspended answer could not be reviewed later. The planned production store is
PostgreSQL with separated roles (AB#241, AB#258/259); it is not available in the
current environment. Authentication against identity-service and the reviewer
list (AB#248) do not exist yet.

## Decision

- A suspended assessment opens a `ReviewCase` in the same SQLite transaction as
  the decision and policy snapshot. A failure in any insert stores nothing.
- The case keeps only question/answer pairs and present source article
  identifiers. Raw output and other upstream fields are discarded.
- Cases live in their own append-only table, `review_cases`, with a unique
  `assessment_id` and a SHA-256 digest of the canonical content, checked on read.
- Reads are split into two ports: `AssessmentRepository` for general decision
  queries, which returns only the case ID, and `ReviewCaseRepository` for
  content. Retained content has a redacted `repr`, and nothing logs it.
- Without authentication or authorization, no HTTP route exposes cases.

## Alternatives

- Store the content in the `assessments` row: simpler, but every general query
  would carry protected content.
- Store only a hash of the content: protects confidentiality but leaves the
  reviewer nothing to review; a hash cannot reconstruct deleted evidence.
- Encrypt the content now: no symmetric cipher in the standard library or
  installed dependencies, and key management is undefined. Deferred.
- Wait for PostgreSQL: blocks the review flow on infrastructure that is not
  available; the ports keep the adapter replaceable.

## Consequences

- Each suspended decision is reviewable and traceable to its policy version.
- The SQLite file holds plaintext generated content. Anyone with file access
  can read it; the owner can also drop triggers and recompute digests.
- Authentication, reviewer authorization, encryption at rest, retention limits,
  decisions, expiry and release remain pending and must exist before any API
  route serves review cases.
- The original client request is not stored; the PEP increment must add it if
  the reviewer needs it, as AB#240 requires.
