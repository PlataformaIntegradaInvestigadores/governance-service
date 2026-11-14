# ADR 0006 — Governed recommendations

## Status

Accepted for the local implementation of AB#230. The versioned policy is a draft
pending academic review.

## Context

The ethical recommender applies its own ranking configuration. Request options
can differ from the settings that produced the returned list. Its response
contract is frozen at upstream revision `d502b65`.

## Decision

Inspect the applied `parameters.rerank` and verify the consistency of the
returned profiles with the scores, ranks and aggregate diagnostics. Evaluate
the output against the versioned R1 reference policy. Fail closed when the
service cannot load the policy, assess the response or persist the decision.

Use the existing assessment, protected review and resolution workflow. Store a
versioned policy snapshot and append the audit metadata in the same transaction
as the decision and any protected case. Namespace HMACs for author identifiers
separately from requester review credentials. Audit only selected applied
parameters, aggregate counts, HTTP status and pseudonymous IDs. Keep names out
of storage. Forward upstream 404 and 422 bodies to the requester and exclude
them from audit and logs.

For recommendation reviews, the policy can require rejection justification
while D6 remains open. Its draft default requires it. Approval and modification
also require justification. ADR 0003 continues to govern Q&A. A modification
uses the permitted subsequence of original ranks. It cannot add candidates or
reorder them.

## Alternatives

Auditing request options alone would miss options ignored by the upstream.
Copying upstream bodies into the general audit would expose identifiers and
error inputs. A separate review workflow would duplicate authorization,
expiration and idempotency.

## Consequences

Local tests use synthetic data and SQLite. Real retained content requires
AB#321 and the staging measurement requires a separate authorized integration.
Monitoring across windows, the gateway and frontend are subsequent work.
