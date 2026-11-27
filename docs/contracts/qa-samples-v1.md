# Q&A sample API v1

The administrator token required by the supervisor API also protects sample
creation and retrieval. Responses carry `Cache-Control: no-store`.

- `POST /api/v1/qa-samples/` accepts `start` and `end` as timezone-aware
  timestamps and `seed` as an unsigned 64-bit integer. The interval includes
  its start and excludes its end. A successful request returns 201 with the
  generated sample ID, interval, seed, policy snapshot, algorithm, ordered
  population and selected assessment IDs.
- `GET /api/v1/qa-samples/{sample_id}` returns the saved snapshot. It returns
  404 for an unknown sample ID.

Policy `qa-retrospective-sampling.v1.json` selects 10% rounded up with a
minimum of 10. A smaller population returns 409
(`INSUFFICIENT_QA_DELIVERIES`). Each assessment appears once. The algorithm
orders IDs by SHA-256 of the decimal seed, a colon and the assessment ID,
then by ID to break ties. Identical inputs produce identical selections.
A new POST creates a new execution. GET retrieves a previous execution even
after the population grows.

The snapshot contains identifiers and sampling metadata. Local synthetic runs
can enable `GOVERNANCE_QA_RETAIN_SYNTHETIC_DELIVERIES=true` before generating
deliveries. These runs sample only deliveries with a retained body. The flag
defaults to false and is unavailable in staging and production. Existing
delivery marks without retained bodies cannot be reviewed retrospectively.

- `GET /api/v1/qa-samples/{sample_id}/deliveries/{assessment_id}` returns the
  original body after verifying its digest.
- `PUT /api/v1/qa-samples/{sample_id}/deliveries/{assessment_id}/resolution`
  records `confirm`, `flag` or `correct` as the `outcome`. Flag and correct
  require `justification`. Correct also requires replacement `qa_pairs`.
  Identical retries return the saved decision. A different decision conflicts.
  A correction is an audit record and does not change or notify the recipient.
- `GET /api/v1/qa-samples/{sample_id}/interventions` reports prior,
  retrospective and combined counts and rates over the frozen delivery
  population. Combined uses the union of assessment IDs.

Content and decisions require the administrator token. The local SQLite
content store is unencrypted and intended only for synthetic inputs.
