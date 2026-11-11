# ADR 0005: Review supervision through the administrator account

## Status

Accepted for the local prototype on 2026-11-11. Supersedes ADR 0002 (AB#248).

## Context

The shared Centinela administrator account supervises review cases. The
admin panel already uses an administrator token issued by search-service,
which owns token signing, expiry and credential rotation.

## Decision

Governance verifies `X-Admin-Token` on each request to `/supervisors/me`
and both `/review-cases` operations. The internal verifier is
`GET /internal/auth/validate-admin-token/` on search-service. It returns
an empty 204 for an accepted token and an empty 403 for an absent or rejected
token. It is intended for the service network. Gateway exposure must be checked
in the deployment environment.

Governance sends only the admin credential to the configured service origin,
with a bounded timeout and redirects and environment proxies disabled.
A missing header yields 401. A rejected token yields 403. An unconfigured
verifier, network failure or unexpected verifier response yields 503.
Only 204 authorizes the operation. Protected responses use
`Cache-Control: no-store`. Credentials are excluded from logs and errors.
The requester's Q&A review credential retains its separate purpose.

New human decisions record the fixed role `admin` in the existing actor
fields, alongside the timestamp and applicable justification. The SQLite
schema and existing resolutions remain intact. Expiries have no human actor.

## Alternatives

Local verification would require the Django signing key and a copy of the
issuer's expiry and rotation rules. Validating the username and password in
governance would duplicate custody of the password and change the panel flow.
Individual identity and roster authorization would require each reviewer to
authenticate separately from the shared account.

## Consequences

Each protected call depends on search-service availability. The shared
account supports role attribution for new decisions. Historical resolutions
retain their recorded identity and snapshot. A new administrator action cannot
be attributed to a particular person.
