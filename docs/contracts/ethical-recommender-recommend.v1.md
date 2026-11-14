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
names. Send `outcome`, `justification` and, on modify, `retained_ranks` in
original order. The release preserves original ranks and reports Jaccard
divergence under `governance`. The remaining upstream fairness metrics describe
the original list. Approved retained responses also omit names.

The draft policy defaults to retaining requests whose applied configuration
disables fairness. An alternate baseline configuration permits the exact
disabled signature and returns an informational notice. The integration
makes no decision about fairness over windows.
