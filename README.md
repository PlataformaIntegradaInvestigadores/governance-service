# Centinela Governance Service

Service bootstrap for Component E, with offline Q&A assessment, versioned
experimental policies, transactional local decision storage and protected review
cases for suspended output. The API exposes
runtime health, service metadata and OpenAPI. Interception, approved policies,
human review, PostgreSQL forensic audit and explainability are pending;
governance is not enabled for live traffic.

## Local Q&A assessment

`app.infrastructure.qa_response.assess_qa_response` validates a supplied
`search-service/generate-qa` response, then calls the application policy
evaluator. The domain contains immutable observations, policy identity and
decisions, without HTTP, persistence or framework dependencies.

```python
from app.domain.qa import QaPolicy
from app.infrastructure.qa_response import assess_qa_response

policy = QaPolicy("alexander-qa-review", "draft-1", expected_pairs=3)
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
body. There is no automatic allow path in this increment.

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
    Path("policies/alexander-qa-review.draft-1.json"), database
)
record = assessor.execute({"status": "error", "qa_pairs": []})
assert SqliteAssessmentRepository(database).get(record.assessment_id) == record
```

The policy file is experimental. Its contract and intended meaning are documented
in [the Q&A contract](docs/contracts/alexander-generate-qa.v1.md). Missing,
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

Limits of this prototype: there is no authentication, authorization or
encryption at rest, so any process that can open the file can read the content.
For that reason no HTTP route exposes cases. Reviewer decisions, expiry and
release are not implemented; a case stays pending. Decisions stored before this
change have `review_case_id = None`. See
[ADR 0001](docs/adr/0001-protected-review-cases-in-local-store.md).

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

The API retains its bootstrap behavior. Domain models and repository protocols
are independent of frameworks and storage. Application use cases evaluate and
record decisions through those protocols. Infrastructure supplies strict JSON
policy loading, response validation and SQLite storage; `main` composes them for
the offline workflow. The application does not import Pydantic, HTTP or SQLite.
There is no gateway interception, review endpoint or automatic content release.
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
Next: authenticated reviewers (AB#248), reviewer decisions and expiry
(AB#244/245), the agreed audit guarantees, then the PEP. The separate
rag-service /ask integration remains conditional on contract, data and scope.

## Publication

Keep development commits local on feature branches. Push, branch/PR publication,
remote merges and deployment require the owner's explicit instruction or
approval for that step. Local thesis documents and AGENTS.md stay outside Git.
