# ADR 0002: Supervisor authentication through identity-service

Status: Accepted for the local prototype, 2026-11-08.

## Context

Review cases (ADR 0001) may only reach people the governance component trusts
to review AI output. Platform users authenticate with identity-service, which
issues JWT access tokens signed with a shared HS256 key and exposes
`GET /internal/auth/validate-token/` on the internal network. That endpoint
answers 204 with `X-Authenticated-User-Id` and `X-Authenticated-Token-Type`, or
401/403. The gateway routes `/api/governance/` to this service but does not
apply `auth_request` to it, so any route added here is public unless this
service checks the caller itself. Identity has no reviewer role.

## Decision

- The service delegates token validation to identity-service for every request
  (`IdentityTokenValidator`). It does not verify JWTs locally.
- Only 204 with a non-blank user ID and token type `access` authenticates. 401
  and 403 are authentication errors. Any other status, a redirect, a timeout or
  a transport error denies access as `IDENTITY_UNAVAILABLE`; there is no
  fallback and no cached allow.
- The identity client ignores proxy environment variables and does not follow
  redirects, so the bearer token goes only to the configured identity URL.
- Governance keeps its own supervisor list: an append-only table of changes
  (`identity_id`, `display_name`, enabled flag, UTC time). The latest change per
  identity decides. The display name is the snapshot recorded with the
  identity reference, following the identity-decoupled pattern.
- `AuthorizeSupervisor` combines both checks. `GET /api/v1/supervisors/me` is
  the first protected route; it returns the caller's supervisor entry and no
  review content. Errors return a code and the trace ID: 401
  `AUTHENTICATION_REQUIRED`, 403 `SUPERVISOR_NOT_ENABLED`, 503
  `IDENTITY_UNAVAILABLE` or `SUPERVISION_NOT_CONFIGURED`.
- The route is unavailable until both `GOVERNANCE_IDENTITY_BASE_URL` and
  `GOVERNANCE_SUPERVISORS_DATABASE` are set. Setting only one fails at startup.

## Alternatives

- Verify JWTs locally with the shared key: no network call, but this service
  would hold a key that can forge tokens for the whole platform, and it would
  miss identity's checks such as inactive users.
- Rely on the gateway's `auth_request`: the gateway does not apply it to
  governance today, and in-service checks still need the user ID and the
  supervisor list.
- Use an identity role or group as the supervisor list: identity has no such
  role, and enabling reviewers is a governance decision that must keep its own
  history.
- A mutable `supervisors` table: simpler, but enabling and disabling reviewers
  would leave no history.

## Consequences

- Each protected request calls identity once. If identity is down, supervisors
  cannot act; output stays suspended, which is the safe failure.
- Revocation is as fast as identity applies it: an access token stays valid
  until identity rejects it (15 minutes by default).
- If identity restricts `ALLOWED_HOSTS`, it must accept the internal host name
  used by this service.
- The list is managed in-process (`ManageSupervisors`) with no administrator
  route, and changes do not record who made them. Both are pending.
- Staging does not configure supervision yet: it needs both settings and a
  writable volume for the database, because the container filesystem is read
  only.
- The SQLite list is a local prototype, like the other local tables.
