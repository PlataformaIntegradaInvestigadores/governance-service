# Governed RAG `/ask` contract v1

Implementation target for AB#320. This contract is published ahead of the route
so the review panel and the evaluation harness can depend on the wire format.

## Upstream

`rag-service` revision `6a04b097434af2179082e6ad6a65294ce1630ac0` provides
`POST /ask`. `AskRequest` has `query` and optional `topk`. `AskResponse` has
`query`, `answer_text`, `blocks`, `used_refs_report` and `timing_ms`. The blocks
carry document metadata and fragment text. The generator uses complete JSON.

Governance evaluates the answer and blocks from a valid upstream 200. The
upstream's `used_refs_report` is untrusted since it can omit unresolved citations
or contain exception details even on a 200. Governance generates its own report.
Transport failures, unexpected statuses and invalid bodies yield a generic
denial. Upstream error bodies are discarded.

## Requester endpoints

`POST /api/v1/governed/rag/ask` accepts a JSON object with a nonblank string
`query`, an optional positive integer or null `topk`, and an optional boolean
`stream` (default `false`). Governance trims the query and sends the upstream
only `query` and, when supplied, `topk`. The route produces complete JSON.

| Result | Status | JSON body |
| --- | --- | --- |
| Invalid request | 422 | `code: INVALID_REQUEST`, `trace_id` |
| `stream: true` | 422 | `code: RAG_STREAMING_UNSUPPORTED`, `trace_id` |
| Allowed answer or released review | 200 | Response below |
| Retained answer | 202 | `review_id`, `review_token` |
| Review still pending | 202 | `review_id` |
| Denied, rejected, expired, invalid credential or unavailable policy/storage | 403 | `code: RAG_DENIED`, `trace_id` |

`GET /api/v1/governed/rag/reviews/{review_id}` requires the corresponding
`review_token` as a Bearer credential. An approved original or authorized
correction uses the same response schema as an immediate 200. Responses use
`Cache-Control: no-store`. The 202 responses carry neither answer nor source
text. Release follows successful persistence of the governing decision.

The 200 body contains `query`, `answer_text`, `blocks`, `used_refs_report`
and `timing_ms`. `blocks` preserves the original order and contains `cite_id`,
`title`, `authors_mention`, `authors_raw`, `year` and `doi_raw` (the last four
allow null). Full fragment `text` stays on the protected side. `used_refs_report`
is a string listing the resolved positions with their titles in order of first
appearance. `timing_ms` contains integer `search`, `rerank`, `generate` and
`total` durations reported by the upstream.

## Policy and citations

`[n]` points to `blocks[n - 1]`. `cite_id` identifies a document, vector or row.
A distinct position is counted once. Position zero or a position above the
number of blocks has no matching block, even if its number matches a `cite_id`.
The check establishes structural correspondence. The upstream can truncate the
prompt, and the reviewer determines whether a fragment supports a claim.

| Condition | Result | Reason code |
| --- | --- | --- |
| Invalid or empty generator output | Deny | `UPSTREAM_CONTRACT_INVALID` |
| Nonempty answer cites a position without a block | Retain | `RAG_CITATION_WITHOUT_BLOCK` |
| No blocks and no citations | Deny | `RAG_NO_SOURCES` |
| Blocks present, answer has no citations | Retain | `RAG_CITATIONS_MISSING` |
| Valid structural correspondence, review required by policy | Retain | `RAG_REVIEW_REQUIRED` |
| Valid structural correspondence, policy permits complete output | Allow | `RAG_STRUCTURALLY_COMPLETE` |

The bundled draft policy requires review for structurally complete output.
A distinct policy version may enable allowance for complete output. The
policy snapshot and its checksum are immutable per policy ID and version.

## Supervisor endpoints

Use the existing `GET /api/v1/review-cases/{case_id}` and
`POST /api/v1/review-cases/{case_id}/resolution` with `X-Admin-Token`.
The RAG detail extends the common case metadata with `rag`, an object with
`query`, `answer_text`, `blocks`, `cited_positions` and `unresolved_positions`.
Each block in `rag.blocks` includes the metadata above and its full `text`.
Both position arrays list distinct citations in order of first appearance
within their respective groups. `cited_positions` contains integers.
`unresolved_positions` contains decimal strings so an arbitrarily large
citation position remains representable.

Send `outcome` (`approve`, `modify` or `reject`) and `justification` when
required. A `modify` request also carries `answer_text` with the corrected
answer. The correction must have at least one citation and all its positions
must resolve against the case's original blocks. A correction that fails
this condition returns 422 `INVALID_RAG_CORRECTION`. Rejecting, modifying or
approving with a citation defect requires a nonblank justification. Approving
with unresolved citations is a documented human override. The regenerated
report still lists only resolved positions. The case keeps the original
protected answer and the resolution records the correction separately.

General decision records store the decision and case IDs, UTC timestamp,
policy identity and checksum, verdict and reason codes. The query, answer,
fragment text and any correction belong to protected content. Saving a
retained decision and its case is one transaction. Real data in the protected
store requires encryption and retention controls (AB#321). Gateway routing
is tracked in C4. The protocol tracks retrospective sampling separately from
the pre-release review flow described here.
