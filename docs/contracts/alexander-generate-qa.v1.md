# Alexander assistant response contract — local revision 1

This document describes the observed contract used by the local assessor, not an
approved institutional policy or a deployed governance route.

## Source and route

Observed upstream: search-service, develop commit
`055c32ab355df03f402303fcc622223818b98288`,
`apps/search_engine/infrastructure/api/v1/views/llm_search_views.py`,
`LLMSearchViewSet.generate_qa`. Its generator is `QAGenerationPipeline` in
`apps/search_engine/application/services/qa_generator_service.py`.

The current frontend calls `POST /api/search/v1/llm-search/generate-qa/` through
the gateway. The internal upstream route is
`POST /api-se/v1/llm-search/generate-qa/`. This increment does not intercept either
route or send requests to it.

## Response fields consumed

| Field | Accepted shape | Meaning and limitation |
|---|---|---|
| `status` | `success`, `partial`/`partial: ...`, `error`/`error: ...` | Generation state, not an approval or factual quality score |
| `qa_pairs` | List of objects with nonblank string `question` and `answer` | Structural output; zero or excess pairs are denied |
| `source_docs` | Optional list of objects with string/integer/null `article_id` | Missing/blank identifiers require review; populated identifiers do not verify context or claim support |
| `error` | Optional string or null | A nonempty value causes denial even with success status |

Malformed types or unknown states are denied. Other upstream fields are ignored
by this structural assessor. The generated text is not copied into the decision
or local database. A 200 transport response can contain an upstream error and
must not be treated as an allow decision.

## Experimental policy

`policies/alexander-qa-review.draft-1.json` records ID, version and expected pair
count. The expected count of three reflects the observed assistant contract;
it is not a threshold for factual quality or fairness. Invalid, incomplete or
duplicate-key policy documents fail before a decision is saved.

All usable output is suspended for human review; errors and unusable responses
are denied. There is no allow path. Complete source identifiers do not make an
untrusted client-supplied context authoritative.

## Local storage boundary

The offline workflow returns only after persisting the decision and exact policy
snapshot transactionally. A reused ID/version with different policy content is
rejected. Policy changes require a new version. The store retains both versions
and their decisions; it does not overwrite prior rows.

Persisted metadata includes a UUID, UTC timestamp, policy snapshot/checksum,
verdict and reason codes. It excludes the original request, generated text and
retained output. Failed writes propagate without a successful saved result.

SQLite triggers and a policy checksum are limited local safeguards. A database
owner can rewrite the file or regenerate checksums. PostgreSQL roles, a hash
chain and independent checkpoints, authenticated review and protected retained
evidence remain pending. This contract does not authorize live content delivery.
