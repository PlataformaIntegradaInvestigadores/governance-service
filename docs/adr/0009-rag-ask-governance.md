# 0009. Govern document RAG before releasing `/ask` responses

## Status

Implementation decision for AB#320, following the November 13, 2026 decision
on protected fragments, unresolved citations, upstream errors and streaming.

## Context

`rag-service` `6a04b09` returns full source fragments and an answer with numbered
citations. It numbers prompt blocks by position and can omit invalid citations
from `used_refs_report`. The report may also disclose internal failures with a
200 response. The text passed to the model can be truncated.

## Decision

Use the existing assessment and asynchronous review workflow for the RAG
case. Persist the exact versioned policy and decision before delivery. Keep
the original answer and ordered source fragments in the protected case when
review is required. Persist an authorized correction as a separate resolution
using the same source blocks. Use caller-specific review tokens and validate
the administrator through the existing supervisor authorization boundary.

Resolve citations against block positions. Record unresolved citations with
their own reason code. Construct the public reference report from resolved
positions and respond with complete JSON. Deny when assessment or persistence
cannot complete. Omit upstream error bodies and fragment text from the public
response. See the [contract](../contracts/rag-ask.v1.md) for the wire format.

## Alternatives

- Use `cite_id` as the citation number. The upstream assigns these different
  roles, so a matching identifier can point to the wrong document.
- Use the upstream's reference report for enforcement. Its filtering hides
  missing positions and its failure path can expose exception details.
- Stream output and review afterward. A pending case would already have
  delivered text, bypassing the pre-release control.

## Consequences

Structural correspondence can be tested independently. Human review remains
responsible for judging whether a fragment supports a claim. The storage
adapter must encode the RAG policy, protected case and correction while reading
existing Q&A and recommendation records. Production content depends on the
retention and encryption controls in AB#321. Deployment and public route
compatibility are verified in the separate gateway increment.
