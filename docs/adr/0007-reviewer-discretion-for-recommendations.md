# ADR 0007 - Reviewer discretion for recommendations

## Status

Accepted for the local AB#230 prototype on 2026-11-23 (Notion 17).

## Context

ADR 0006 treated the proposed recommendation outcome and selected ranks as
mandatory. The academic director authorized the reviewer to depart from them
with a justification. The proposal still helps the administrator decide.

## Decision

An authorized administrator can approve, reject, or modify a retained list.
A change in outcome or in the proposed ranks requires a nonblank justification.
A modification selects a nonempty subset of the original ranks in original
order. Candidates cannot be added or reordered. This amends the review plan
rule of ADR 0006. The active policy requires justification for all outcomes.

Persist the proposed outcome and whether the reviewer departed from the plan
as metadata on the final resolution. Historical rows have unknown metadata
and are excluded from the denominator of the override rate. The protected
case endpoint exposes the proposal and recorded resolution metadata. The
general audit keeps its pseudonymous fields and excludes the retained list,
candidate names and justification text.

## Alternatives

A binding plan prevents a justified human override. Inferring overrides from
current code or policy would make the measurement depend on replaying old
inputs. Storing the full proposal in general audit would collect excess data.

## Consequences

A release authorized by a recorded human approval or modification is a
controlled delivery. The leakage criterion counts a held output delivered
without a valid resolution. An identical replay returns the recorded
resolution. On a resolved case, a conflict with the original proposal keeps
the recommendation-specific 422.
A conflicting request that follows the proposal keeps the finality code 409.
Names in reviewed releases continue to follow ADR 0006.
