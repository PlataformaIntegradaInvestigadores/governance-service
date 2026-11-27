# Search Q&A assistant response contract — local revision 1

This document describes the observed contract used by the local assessor, not an
approved institutional policy or a deployed governance route.

## Source and route

Observed upstream: search-service, develop commit
`385c7fb55a085e5092b7b3f46a6d048e3e69a9fd`,
`apps/search_engine/infrastructure/api/v1/views/llm_search_views.py`,
`LLMSearchViewSet.generate_qa`. Its generator is `QAGenerationPipeline` in
`apps/search_engine/application/services/qa_generator_service.py`.

The current frontend calls `POST /api/search/v1/llm-search/generate-qa/` through
the gateway. The internal upstream route is
`POST /api-se/v1/llm-search/generate-qa/`. The optional PEP calls that internal
route for requests sent to `POST /api/v1/governed/qa/generate`. It forwards the
JSON object with `query` and `documents` (or legacy `data`). Gateway and
frontend integration remain pending.

## Response fields consumed

| Field | Accepted shape | Meaning and limitation |
|---|---|---|
| `status` | `success`, `partial`/`partial: ...`, `error`/`error: ...` | Generation state, not an approval or factual quality score |
| `qa_pairs` | List of objects with nonblank string `question` and `answer` | Structural output; zero or excess pairs are denied |
| `source_docs` | Optional list of objects with string/integer/null `article_id` | Missing/blank identifiers require review; populated identifiers do not verify context or claim support |
| `error` | Optional string or null | A nonempty value causes denial even with success status |

Malformed types or unknown states are denied. Other upstream fields are ignored
by this structural assessor. The generated text is not copied into the decision.
For suspended responses only, the pairs and present source identifiers are
retained in a protected review case (see below). A 200 transport response can contain an upstream error and
must not be treated as an allow decision.

## Experimental policy

`policies/search-qa-review.draft-1.json` records ID, version and expected pair
count. The expected count of three reflects the observed assistant contract;
it is not a threshold for factual quality or fairness. Invalid, incomplete or
duplicate-key policy documents fail before a decision is saved.

The bundled policy suspends usable output for human review and denies errors
and unusable responses. A distinct policy version can set `allow_complete`
to `true` to allow success with the expected pair count and complete source
identifiers. Complete identifiers do not make client-supplied context
authoritative.

## Governed HTTP responses

| Operation | Status | Body |
|---|---|---|
| Generate, allowed | 200 | `status`, normalized `qa_pairs` and `source_docs` with article IDs |
| Generate, suspended | 202 | `review_id` and `review_token` |
| Poll, pending | 202 | `review_id` |
| Poll, approved or modified | 200 | Same shape as allowed output, with authorized pairs |
| Generate or poll, denied | 403 | `code` and `trace_id` |

Poll `GET /api/v1/governed/qa/reviews/{review_id}` with the returned
`review_token` as a Bearer credential. The token grants access to that
review only. Rejection, expiry and invalid credentials yield 403. The PEP
excludes raw output and sends `Cache-Control: no-store` with these responses.

## Local storage boundary

The offline workflow returns only after persisting the decision and exact policy
snapshot transactionally. A reused ID/version with different policy content is
rejected. Policy changes require a new version. The store retains both versions
and their decisions; it does not overwrite prior rows.

Persisted metadata includes a UUID, UTC timestamp, policy snapshot/checksum,
verdict, reason codes and, for suspensions, the review case ID. A suspended
response also stores a review case with its `qa_pairs` text (whitespace-trimmed)
and nonblank `source_docs[].article_id` values, in the same transaction. The
original request, raw output and other fields are not stored. Failed writes
propagate without a successful saved result and leave no partial rows.

SQLite triggers and a policy checksum are limited local safeguards. A database
owner can rewrite the file or regenerate checksums. PostgreSQL roles, a hash
chain, independent checkpoints and encryption at rest remain pending.
Supervisor authorization and resolution follow ADR 0002 and ADR 0003.
