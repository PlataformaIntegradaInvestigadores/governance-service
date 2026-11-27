# Supervisor decision explanation contract v1

Implementation target for AB#263–265. The panel uses this contract to explain a
governance decision using the policy and evidence recorded when it was made.

## Endpoint

`GET /api/v1/assessments/{assessment_id}/explanation` requires `X-Admin-Token`
validated through the existing supervisor authorization flow. Use the
`assessment_id` from the review case detail. The gateway prefix for the panel
is `/api/governance`, followed by `/assessments/{assessment_id}/explanation`.
The endpoint also accepts a recorded decision without a review case.

Responses set `Cache-Control: no-store`. The success body has these fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `assessment_id` | UUID string | Recorded governance decision |
| `review_case_id` | UUID string or null | Linked protected case when one was created |
| `created_at` | ISO 8601 UTC string | Time of the decision |
| `case_type` | `qa`, `recommendation` or `rag` | Governed case |
| `verdict` | `allow`, `suspend` or `deny` | Original verdict before any review outcome |
| `reason_codes` | Array of strings | Triggered rules and applicable notices in recorded order |
| `policy` | Object | `policy_id`, `version` and `parameters` from the stored snapshot |
| `applied_parameters` | Object or null | Effective parameters recorded for this output, where available |
| `evidence` | Object or null | Case-specific evidence below, when still retained |

The panel can display `reason_codes` through its existing reason labels. Policy
parameters describe the thresholds and options in the snapshot. Applied
parameters describe the values actually observed for the output. Parameter
objects contain the allowlisted scalars described below. Unknown or unrecorded
values remain absent or null. A later policy change does not alter a historical
explanation. The original verdict remains visible after approval, correction
or rejection. The review case detail carries the resolution.

### Q&A

`policy.parameters` contains `expected_pairs` and `allow_complete`.
`applied_parameters` is null. `evidence`, when available, contains `pair_count`
and `source_article_ids` from the retained case. The source IDs are pooled for
the response. The records do not attribute each pair to a specific article.

### Recommendation

`policy.parameters` contains `mode`, `baseline`, `max_weight`,
`max_per_institution`, `min_protected_share`, `max_relevance_floor`,
`protected_attribute` and `require_rejection_justification`. Where recorded,
`applied_parameters` contains `fairness_mode`, `lambda_`, `max_per_institution`,
`min_protected_share` and `relevance_floor`. The `evidence.ranking` list uses
rank as the row key and exposes `baseline_rank`, `relevance_norm`,
`institution_novelty`, `final_score` and `selection_reason` if retained. These
values describe the upstream list before the reviewer changed it. Profile
names, identity identifiers and institution names are excluded.

### RAG

`policy.parameters` contains `allow_complete`. `applied_parameters` is null
because the request's retrieval options are not stored with the decision.
`evidence` contains `cited_positions`, `unresolved_positions` and `sources`.
Each source contains `position`, `cite_id` and `title`. Positions use the
ordered blocks in the protected review case, and unresolved positions use
decimal strings as defined in the [RAG contract](https://github.com/PlataformaIntegradaInvestigadores/governance-service/blob/e53f754f3ac071d465838ccc3c3d199cdfb43ca3/docs/contracts/rag-ask.v1.md). The case detail
carries the block text. The explanation exposes the structural correspondence
available for review. The reviewer judges whether a fragment supports a claim.

## Error responses

Errors use `code` and `trace_id`. Authentication failures use the common
`AUTHENTICATION_REQUIRED`, `ADMIN_TOKEN_REJECTED` or
`ADMIN_VERIFIER_UNAVAILABLE` codes. A malformed ID returns 422 `INVALID_REQUEST`.
A missing decision returns 404 `ASSESSMENT_NOT_FOUND`. An unavailable or
inconsistent decision store returns 503 `EXPLANATION_UNAVAILABLE`.

The endpoint requires admin authorization even for decisions without a case.
It does not expose prompts, generated answers, full source fragments or raw
audit events. If protected evidence was removed under the retention policy,
the endpoint can still explain the decision from the immutable metadata and
returns null in `evidence`.
