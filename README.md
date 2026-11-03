# Centinela Governance Service

Service bootstrap for Component E, with an offline Q&A response assessment
increment. The API exposes runtime health, service metadata and OpenAPI.
Interception, approved policies, human review, audit persistence and
explainability are pending; governance is not enabled for live traffic.

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

Coverage includes branches and must reach 90%. The CI workflow targets
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

The API adapter, configuration and composition root retain their bootstrap
behavior. The local Q&A assessment adds domain models, an application evaluator
and an infrastructure response adapter. The adapter depends on the application
and domain; the application depends only on the domain. No persistence,
gateway interception or review endpoint is introduced by this increment.

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
Next: confirm the governed assistant contract, persist decisions and reviews,
then connect the PEP under the existing tasks. The separate rag-service /ask
integration remains conditional on its contract, data and approved scope.

## Publication

Keep development commits local on feature branches. Push, branch/PR publication,
remote merges and deployment require the owner's explicit instruction or
approval for that step. Local thesis documents and AGENTS.md stay outside Git.
