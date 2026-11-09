# Centinela Governance Service

Service bootstrap for Component E, with offline Q&A assessment, versioned
experimental policies, transactional local decision storage and protected review
cases for suspended output. The API exposes
runtime health, service metadata, OpenAPI and supervisor-only review routes.
The optional Q&A proxy governs requests sent to its own route. Gateway and
frontend integration, approved policies and PostgreSQL forensic audit are pending.

## Governed Q&A

Configure supervision, then set `GOVERNANCE_QA_UPSTREAM_BASE_URL` to the
search-service origin and `GOVERNANCE_QA_RELEASE_KEY` to a stable random
secret of at least 32 characters. Use the same key on every instance.
`GOVERNANCE_QA_POLICY_PATH` defaults to the bundled review-only policy.
`GOVERNANCE_QA_TIMEOUT_SECONDS` sets the httpx phase timeouts.

Send the assistant's JSON request to `POST /api/v1/governed/qa/generate`.
A suspension returns 202 with `review_id` and `review_token`. Poll
`GET /api/v1/governed/qa/reviews/{review_id}` with the review token as the
Bearer credential. Pending reviews return 202. Approval or modification
returns 200 with the authorized pairs and source IDs. Rejection, expiry
or invalid credentials return 403. Keep the token out of logs and URLs.
Use TLS at the public boundary.

A policy version with `allow_complete: true` can allow a complete response
directly. Upstream failures and invalid responses return 403. See the
[Q&A contract](docs/contracts/search-generate-qa.v1.md) and
[ADR 0004](docs/adr/0004-governed-qa-policy-enforcement.md).

## Local Q&A assessment

`app.infrastructure.qa_response.assess_qa_response` validates a supplied
`search-service/generate-qa` response, then calls the application policy
evaluator. The domain contains immutable observations, policy identity and
decisions, without HTTP, persistence or framework dependencies.

```python
from app.domain.qa import QaPolicy
from app.infrastructure.qa_response import assess_qa_response

policy = QaPolicy("search-qa-review", "draft-1", expected_pairs=3)
result = assess_qa_response({"status": "error", "qa_pairs": []}, policy)
assert result.verdict == "deny"
assert result.reason_codes == ("UPSTREAM_GENERATION_FAILED",)
```

Malformed bodies, generation errors and unusable pair counts are denied.
Nonempty responses within the expected count are suspended for human review,
with additional reasons for partial generation or incomplete source identifiers.
Every assessment identifies its policy and version and excludes response text.
Complete output is also suspended: source identifiers do not verify the actual
context, factual correctness or claim-to-source support. The example policy is
an experimental local policy, not an approved institutional threshold.

This function is not an HTTP gate or a persisted review. Calling it neither
releases content nor creates an audit event. HTTP status alone must not be
used to interpret generation success; the upstream can return errors in a 200
body.

## Versioned local assessments

The composition root also exposes an offline workflow that loads a strict JSON
policy, evaluates a supplied response and saves a decision before returning it:

```python
from pathlib import Path

from app.main import create_qa_assessor
from app.infrastructure.sqlite_assessment_repository import SqliteAssessmentRepository

Path("local").mkdir(exist_ok=True)
database = Path("local/assessments.sqlite3")
assessor = create_qa_assessor(
    Path("policies/search-qa-review.draft-1.json"), database
)
record = assessor.execute({"status": "error", "qa_pairs": []})
assert SqliteAssessmentRepository(database).get(record.assessment_id) == record
```

The policy file is experimental. Its contract and intended meaning are documented
in [the Q&A contract](docs/contracts/search-generate-qa.v1.md). Missing,
malformed, duplicate-key or invalid policies fail without a hardcoded fallback.
Changing a policy requires a new version: reusing the same ID/version for another
snapshot raises `PolicyVersionConflictError` without saving another decision.

SQLite stores the policy snapshot and SHA-256 together with each decision's ID,
UTC timestamp, verdict and reason codes. Policy insertion and decision insertion
share one transaction. An insertion or commit error propagates to the caller;
the workflow does not return a record as saved. Reopening the repository recovers
the persisted record. Denied responses store no generated text.

## Protected review cases

A suspended assessment also opens a review case in the same transaction. The
case keeps what a reviewer needs and nothing else: the question/answer pairs
(surrounding whitespace removed) and the source article identifiers present in
the response. Raw output and other upstream fields are discarded. The decision
record carries `review_case_id`; the case carries `assessment_id`, and both are
unique, so each case belongs to exactly one suspended decision.

```python
from app.infrastructure.sqlite_assessment_repository import SqliteReviewCaseRepository

case = SqliteReviewCaseRepository(database).get(record.review_case_id)
```

Content is kept apart from general queries. `SqliteAssessmentRepository.get`
returns decision metadata and the case ID only; only `SqliteReviewCaseRepository`
reads the content. `repr` of retained content shows counts, not text, and the
service writes no logs about it. A failed write of the case rolls back the
decision and policy snapshot too. A case is append-only, SQLite rejects one for
a non-suspended decision, and a SHA-256 digest detects altered content on read
(`ReviewCaseIntegrityError`, whose message has no content).

Limits of this prototype: there is no encryption at rest, so any process that
can open the file can read the content. Over HTTP, cases are reachable only
through the supervisor-protected review routes described below. Decisions stored
before this change have `review_case_id = None`. See
[ADR 0001](docs/adr/0001-protected-review-cases-in-local-store.md).

## Supervisor authentication

Only enabled supervisors may act on review cases. `GET /api/v1/supervisors/me`
checks the caller and returns their supervisor entry; it returns no review
content. The service sends the bearer token to identity-service
(`/internal/auth/validate-token/`) on every request and never verifies JWTs
itself: identity signs them with a shared key that this service must not hold.

| Result | Status | `code` |
|---|---|---|
| Missing, malformed or rejected token, or a refresh token | 401 | `AUTHENTICATION_REQUIRED` |
| Valid user who is not an enabled supervisor | 403 | `SUPERVISOR_NOT_ENABLED` |
| Identity unreachable, timed out or answering unexpectedly | 503 | `IDENTITY_UNAVAILABLE` |
| Supervision not configured | 503 | `SUPERVISION_NOT_CONFIGURED` |

Error bodies hold only `code` and `trace_id`. The token is not stored, logged or
included in error messages.

The supervisor list belongs to governance. Each change appends a row with the
identity ID, the display name recorded as its snapshot, the enabled flag and
the UTC time; the latest change per identity decides. Manage it in process:

```python
from pathlib import Path

from app.application.supervision import ManageSupervisors
from app.infrastructure.sqlite_supervisor_directory import SqliteSupervisorDirectory

roster = ManageSupervisors(SqliteSupervisorDirectory(Path("local/governance.sqlite3")))
roster.enable("<identity user ID>", "<display name>")
roster.disable("<identity user ID>")
```

Set `GOVERNANCE_IDENTITY_BASE_URL` and `GOVERNANCE_DATABASE` (the local SQLite
file shared with decisions and cases) together; setting only one fails at
startup, and setting neither leaves the supervisor and review routes at 503. `GOVERNANCE_IDENTITY_TIMEOUT_SECONDS` bounds each identity call
(default 3, maximum 30).

Limits: there is no administrator route, and list changes do not record who
made them. A token stays valid until identity rejects it. Deployed environments
do not enable supervision yet; the read-only container needs a writable volume
for the list first. If identity restricts `ALLOWED_HOSTS`, it must accept the
internal host name. See
[ADR 0002](docs/adr/0002-supervisor-authentication-through-identity.md).

## Review resolutions

An enabled supervisor views a case and resolves it once. Both routes need a
bearer token accepted by identity and an enabled supervisor (previous section);
authentication is checked before the case ID or the body.

- `GET /api/v1/review-cases/{case_id}` returns the retained pairs, source IDs,
  policy version, reason codes, `expires_at`, `status` (`pending`, `expired`,
  `approve`, `modify` or `reject`) and the resolution, with
  `Cache-Control: no-store`.
- `POST /api/v1/review-cases/{case_id}/resolution` takes `outcome`
  (`approve`, `modify` or `reject`), an optional `justification` (at most 2000
  characters) and, only for `modify`, the edited `qa_pairs` (1 to 50). Edited
  pairs keep the case's source IDs. It answers 201 for a new resolution and 200
  when the same supervisor repeats the same request.

Rules:

- A justification is required to modify, and to approve output flagged
  `PARTIAL_GENERATION` or `SOURCE_EVIDENCE_INCOMPLETE`: both override the
  evaluator. Rejecting needs none.
- A case has one final resolution. Another outcome, or another supervisor,
  gets 409 `CASE_ALREADY_RESOLVED`.
- A case expires `GOVERNANCE_REVIEW_WINDOW_MINUTES` after it was opened
  (default 1440, maximum 10080). A late resolution records the expiry and gets
  409 `CASE_EXPIRED`; expired output is denied and never released. `GET` reports
  an overdue case as `expired` without writing. `ReviewCases.expire_overdue()`
  records expiries for all overdue cases, for a scheduler to call.
- Resolutions are append-only, with the supervisor stored as `identity_id` plus
  a display-name snapshot. Modified content carries a SHA-256 digest checked on
  read.
- Errors: 404 `CASE_NOT_FOUND`, 422 `JUSTIFICATION_REQUIRED` or
  `INVALID_REQUEST`. Bodies hold only `code` and `trace_id` and never echo input.

The governed polling route delivers the authorized output to the holder of
the review credential. Views are not audited, there is
no case list, and no scheduler calls the expiry sweep. The window is a setting,
not part of the versioned policy. See
[ADR 0003](docs/adr/0003-final-review-resolutions-with-expiry.md).

## Local audit limits

Local triggers reject updates and deletes; a policy snapshot checksum detects
inconsistent content when read. A database file owner can replace records or
disable/recreate triggers and checksums. This is not a hash chain, independently
anchored audit log, authorization boundary or production database. Read access
is local to Python callers, with no HTTP query endpoint. Store local databases
under the ignored `local/` directory and use synthetic data for demonstrations.

JSON and SQLite use available dependencies and Python's standard library for
this local increment. The planned YAML policy store and PostgreSQL audit adapter,
roles, migrations and integrity model remain pending. No production deployment,
automatic schema migration or approval of institutional policy is implied.

## Run locally

Python 3.11 is the deployment and CI target.
Resolved dependency versions are pinned in constraints.txt from the initial
Python 3.11 environment. CI must confirm the Linux build separately.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8005
```

Use GET /health, GET /api/v1/status and /docs.
The health response describes the API process only. The address field is
unknown until deployment has an agreed reporting mechanism. A healthy
bootstrap does not indicate that governance controls are available.

Settings use the GOVERNANCE_ prefix and optional .env. Example files have no
credentials. The port is provisional pending agreement with the maintainer.
GOVERNANCE_PORT configures Compose/container startup; direct Uvicorn startup
uses its --port argument. Invalid settings fail during application creation.

## Quality

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m black --check .
.\.venv\Scripts\python.exe -m pytest --cov=app --cov-report=term-missing --cov-report=xml --junitxml=report.xml
```

Coverage includes branches and must reach 90%. Coding, testing, security and
review rules are in [the engineering standards](docs/engineering-standards.md);
Ruff and `tests/test_architecture.py` enforce part of them. The CI workflow targets
Python 3.11, validates Compose and builds the Docker image. It does not deploy.
Organization deployment workflows require agreed server paths, runners and
permissions before configuration.

## Container

```powershell
docker network create centinela-net
docker compose up -d --build
```

Create the external network only if it is absent. Compose binds to loopback,
uses a non-root runtime, drops capabilities and makes the filesystem read
only. No database or upstream connection is needed for this increment.

## Architecture

The default configuration leaves governed Q&A disabled. Domain models and repository protocols
are independent of frameworks and storage. Application use cases evaluate and
record decisions through those protocols. Infrastructure supplies strict JSON
policy loading, response validation and SQLite storage; `main` composes them for
the offline workflow. The application does not import Pydantic, HTTP or SQLite.
`app.api.errors` maps domain errors to coded responses. There is no gateway
interception or automatic content release.
Decisions are recorded under `docs/adr/`.

OpenAPI follows the existing search-bff pattern: internal /api/v1 paths are
rendered relative to a configured server. Local server: /api/v1; planned
gateway server: /api/governance, with the gateway rewriting to /api/v1.
The latter is a proposed integration and requires a separate gateway PR.
See [FastAPI metadata](https://fastapi.tiangolo.com/tutorial/metadata/) and
[proxy documentation](https://fastapi.tiangolo.com/advanced/behind-a-proxy/).

## Traceability

Existing Azure work items: AB#221 (PBI), AB#222 (bootstrap),
AB#223 (workflows/templates) and AB#224 (container configuration).
Local thesis context and evidence are maintained outside this repository.
The local Q&A assessment advances AB#230/231 (policy evaluation and tests).
Local policy loading and decision snapshots provide partial groundwork for
AB#217/218/236/237 and AB#258/260. They do not complete the YAML or PostgreSQL
tasks. Protected review cases advance AB#240/241 locally; the PostgreSQL
repository, 202 response, request evidence and explanation remain pending.
Supervisor authentication and the supervisor list implement AB#248 locally.
Review resolutions and expiry advance AB#244/245 and the case view advances
AB#242. Next: release through the PEP and the 202 response, the agreed audit
guarantees and PostgreSQL. The separate
rag-service /ask integration remains conditional on contract, data and scope.

## Publication

Sync before starting and push feature branches after their checks pass. Opening
pull requests, merging and deployment require the owner's approval, because a
merge to `develop` deploys to staging automatically. See section 9 of
[the engineering standards](docs/engineering-standards.md). Local thesis
documents and AGENTS.md stay outside Git.
