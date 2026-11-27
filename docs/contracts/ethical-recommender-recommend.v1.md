# Ethical recommender contract v1

Upstream `POST /api/v1/recommend` on the configured internal recommender origin
follows `ethical-recommender-service` revision `d502b65`. Governance sends the
request object without translating its options. It evaluates
`model`, `team`, `recommendations`, `fairness_metrics`, `diagnostics` and
`parameters`. In particular, `parameters.rerank.lambda_` and
`min_protected_share` are the applied values. The request uses `lambda` and
`min_low_visibility_share`.

## Requester endpoints

- `POST /api/v1/governed/recommender/recommend` returns the upstream 200 on
  permit. Informational codes are available in `X-Governance-Notices`.
- A retained response returns 202 with `review_id` and `review_token`.
  `GET /api/v1/governed/recommender/reviews/{review_id}` takes the review token
  as a bearer credential. It returns 202 while pending, 200 after approval or
  modification, and a generic 403 otherwise.
- Upstream 404 and 422 preserve the status and body. Transport errors,
  unexpected statuses, invalid bodies and unavailable policy/storage return
  403 with `RECOMMENDATION_DENIED` and a trace ID.
- Responses are served with `Cache-Control: no-store`.

## Supervisor endpoints

Use the existing `/api/v1/review-cases/{case_id}` and
`/api/v1/review-cases/{case_id}/resolution` endpoints with
`X-Admin-Token`. The protected response is under `recommendation` and excludes
names. The case includes a suggested `review_plan`. Send `outcome`,
`justification` and, on modify, a nonempty subset of retained list ranks in
original order. An administrator can choose a different outcome or subset
with a nonblank justification. The resolution records `proposed_outcome`
and `departed_from_plan`. The release preserves original ranks and reports Jaccard
divergence under `governance`. The remaining upstream fairness metrics describe
the original list. Approved retained responses also omit names.

The default policy, `reco-draft-3`, permits the exact baseline: applied mode
`none`, no institutional cap and `lambda = 1.0`. It returns
`X-Governance-Notices: BASELINE_WITHOUT_FAIRNESS` and excludes the response from
window monitoring. Other disabled configurations and policies applied below the
required strength still require review. The thresholds match `reco-draft-2`.

## Window monitoring

Set `GOVERNANCE_RECOMMENDER_MONITOR_POLICY_PATH` to enable window monitoring
in the local prototype. Invalid or unavailable monitoring configuration or
storage denies an otherwise eligible response. Only consistent upstream 200
responses with fairness enabled at the configured strength enter a window.
The monitor observes upstream selections before any supervisor edits.

The request that triggers escalation requires review. The request that
completes recovery still requires review, and subsequent requests use their
per-response verdict. The monitor can retain an otherwise permitted response
with `FAIRNESS_MONITOR_REVIEW`. Existing per-response retention and denial
decisions take precedence.

The SQLite transaction serializes observations in commit order and saves the
decision and observation together. The recommendation audit references the
observation sequence. Observations contain aggregate counts, window results and
control state. Policy snapshots are immutable. Changing
either policy version starts a fresh window and clears the streaks while
preserving the current control level. The draft reference and thresholds are
subject to approval.
