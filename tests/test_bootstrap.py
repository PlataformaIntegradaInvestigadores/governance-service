from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.main import create_app


def make_client(**overrides) -> TestClient:
    return TestClient(create_app(Settings(_env_file=None, **overrides)))


def test_health_reports_only_runtime():
    response = make_client().get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "server_name": "governance-service",
        "ip_address": "unknown",
        "global_status": "Online",
        "groups": [
            {
                "group_name": "Service runtime",
                "group_status": "Operativo",
                "services": [{"name": "api", "status": "ok"}],
            }
        ],
    }


def test_status_does_not_claim_governance():
    response = make_client().get("/api/v1/status")
    assert response.status_code == 200
    assert response.json()["stage"] == "bootstrap"
    assert response.json()["governance_enabled"] is False


def test_openapi_is_cached_and_excludes_health():
    client = make_client(public_base_path="/api/governance/")
    schema = client.get("/openapi.json").json()
    assert schema["servers"] == [{"url": "/api/governance"}]
    assert list(schema["paths"]) == ["/status"]
    assert client.app.openapi() is client.app.openapi()
    assert "/api/v1/status" not in schema["paths"]


def test_local_openapi_uses_executable_path():
    client = make_client()
    schema = client.get("/openapi.json").json()
    url = schema["servers"][0]["url"] + next(iter(schema["paths"]))
    assert client.get(url).status_code == 200
    assert client.get("/docs").status_code == 200


def test_unknown_routes_are_not_forwarded_and_get_trace_id():
    response = make_client().get("/api/v1/governed")
    assert response.status_code == 404
    UUID(response.headers["X-Trace-ID"])


def test_trace_ids_are_generated_instead_of_trusting_input():
    client = make_client()
    first = client.get("/health", headers={"X-Trace-ID": "untrusted"})
    second = client.get("/health")
    UUID(first.headers["X-Trace-ID"])
    assert first.headers["X-Trace-ID"] != second.headers["X-Trace-ID"]


@pytest.mark.parametrize(
    "path", ["relative", "//host", "/x?q=1", "/x#f", "/x y", "/{id}", "/x\\y"]
)
def test_invalid_public_paths_are_rejected(path):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, public_base_path=path)


@pytest.mark.parametrize("port", [0, 1023, 65536])
def test_invalid_ports_are_rejected(port):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, port=port)


def test_environment_configuration_is_validated(monkeypatch):
    monkeypatch.setenv("GOVERNANCE_PORT", "8015")
    assert Settings(_env_file=None).port == 8015
    monkeypatch.setenv("GOVERNANCE_ENVIRONMENT", "invalid")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_root_public_path_is_supported():
    assert Settings(_env_file=None, public_base_path="/").public_base_path == "/"


def test_default_factory_loads_settings():
    assert create_app().state.settings.environment in ("local", "staging", "production")
