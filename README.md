# Centinela Governance Service

Initial service bootstrap for Component E. This increment exposes runtime
health, service metadata and OpenAPI documentation. Governance policies,
interception, human review, audit persistence and explainability are pending.

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

The present code contains the API adapter, configuration and composition
root. Domain policies and application use cases will be introduced with the
corresponding backlog tasks, preserving inward dependencies. No empty
repositories or policy engines are presented as implemented.

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
Next: confirm GRS contracts and design decisions, then implement the PDP/PEP
and policy storage under their existing backlog tasks.
