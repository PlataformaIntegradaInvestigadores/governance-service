# ADR 0003: Final review resolutions with expiry

Status: Accepted for the local prototype, 2026-11-08. It replaces the "no HTTP
route exposes cases" decision of ADR 0001 now that ADR 0002 authenticates
supervisors; the rest of ADR 0001 stands.

## Context

A suspended answer waits in a review case (ADR 0001). The supervisor decision
PBI (AB#243) asks that a supervisor can authorize, modify or reject it, that a
decision contradicting the evaluator carries a justification, and that a case
nobody decides in time is denied. Supervisors are authenticated through
identity and an own list (ADR 0002). The evaluator never allows output: usable
output is suspended with `HUMAN_REVIEW_REQUIRED`, plus `PARTIAL_GENERATION` or
`SOURCE_EVIDENCE_INCOMPLETE` when it found a defect. No enforcement point
delivers output to the requester yet.

## Decision

- Each case has at most one resolution, in an append-only
  `review_resolutions` table with a unique `case_id`. Outcomes: `approve`,
  `modify`, `reject` and the service-recorded `expired`. CHECK constraints tie
  `expired` to having no supervisor and `modify` to modified content.
- "Contradicts the verdict" is read as overriding the evaluator: approving
  output that carries a defect code, or modifying any output, needs a
  justification. Rejecting agrees with the cautious default and needs none.
- Modified pairs are stored with the case's original source IDs and a SHA-256
  digest. The supervisor is stored as `identity_id` plus a display-name
  snapshot.
- Idempotency: the same supervisor repeating the same outcome, justification
  and content gets the stored resolution (200). Any other request gets 409.
  A concurrent insert that loses the unique constraint is re-read and treated
  the same way.
- Expiry: a case expires a fixed window after it was opened. A resolution
  attempt at or after that time records `expired` and is refused. `GET` shows an
  overdue case as expired without writing. `expire_overdue()` records expiries
  in bulk. An expired case is never released.
- Routes `GET /api/v1/review-cases/{case_id}` and
  `POST /api/v1/review-cases/{case_id}/resolution` require an enabled
  supervisor. Authentication runs before the case lookup and body validation,
  so unauthenticated callers learn nothing about case IDs. Content responses
  send `Cache-Control: no-store`. Validation errors return a code without
  echoing the input.

## Alternatives

- Several decisions per case with the latest winning: allows correcting a
  decision, but makes "what was released" ambiguous. Corrections need an
  explicit, audited flow if they are ever required.
- Require a justification for every decision: simple, but it makes the reason
  for overriding the evaluator indistinguishable from routine agreement.
- An idempotency key header: more general, but the request content already
  identifies a repeated decision, and a key would need its own storage.
- A background job for expiry: needs a scheduler the deployment does not have.
  Lazy expiry plus a sweep keeps the denial safe without one.
- The window in the versioned policy: the right long-term place, but it would
  change the policy schema before the YAML policy work (AB#236). It is a
  setting for now.

## Consequences

- Output can be approved but is not delivered: release needs the PEP and the
  202 response.
- A resolution is permanent; a wrong decision cannot be corrected in this
  version.
- Changing the window setting changes when existing pending cases expire. The
  applied `expires_at` is stored with each resolution.
- Case views are not audited and there is no case list (part of AB#242).
- Justifications and modified content are stored in clear, like the cases.
