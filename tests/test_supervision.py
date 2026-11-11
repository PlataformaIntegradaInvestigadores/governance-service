import logging

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.domain.supervision import Supervisor
from app.infrastructure.admin_token_validator import create_admin_client
from app.main import create_app

ME = "/api/v1/supervisors/me"
VERIFIER = "http://search.test/internal/auth/validate-admin-token/"
TOKEN = "synthetic-admin-token"  # noqa: S105 - synthetic fixture
AUTH = {"X-Admin-Token": TOKEN}


@pytest.mark.parametrize(("identity_id", "display_name"), [(" ", "Ana"), ("U1", "")])
def test_historical_actor_requires_identity_and_name(identity_id, display_name):
    with pytest.raises(ValueError, match="must not be blank"):
        Supervisor(identity_id, display_name)


@pytest.fixture
def client():
    return TestClient(
        create_app(
            Settings(_env_file=None, admin_verifier_base_url="http://search.test")
        )
    )


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer synthetic-user-token"},
        {"Authorization": "Basic synthetic-basic"},
        {"X-Admin-Token": ""},
        [("X-Admin-Token", TOKEN), ("X-Admin-Token", TOKEN)],
    ],
)
def test_route_requires_one_admin_header(client, headers):
    response = client.get(ME, headers=headers)
    assert response.status_code == 401
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["code"] == "AUTHENTICATION_REQUIRED"


@pytest.mark.parametrize("value", [b"invalid value", b"bad\x7f", b"bad\xff"])
def test_malformed_admin_header_is_rejected(client, value):
    response = client.get(ME, headers=[(b"X-Admin-Token", value)])
    assert response.status_code == 403
    assert response.json()["code"] == "ADMIN_TOKEN_REJECTED"


def test_route_returns_admin_role_and_forwards_only_admin_credential(
    client, respx_mock, caplog
):
    verifier = respx_mock.get(VERIFIER).respond(
        204, headers={"Set-Cookie": "upstream=ignore"}
    )
    with caplog.at_level(logging.DEBUG):
        for _ in range(2):
            response = client.get(
                ME,
                headers={**AUTH, "Authorization": "Bearer other", "Cookie": "test=1"},
            )
            assert response.status_code == 200
            assert response.headers["Cache-Control"] == "no-store"
            assert response.json() == {"role": "admin"}
    assert TOKEN not in caplog.text
    assert verifier.call_count == 2
    for call in verifier.calls:
        assert call.request.headers["X-Admin-Token"] == TOKEN
        assert "Authorization" not in call.request.headers
        assert "Cookie" not in call.request.headers


def test_unconfigured_verifier_denies_admin_access(tmp_path):
    settings = Settings(_env_file=None, database=tmp_path / "governance.sqlite3")
    response = TestClient(create_app(settings)).get(ME, headers=AUTH)
    assert response.status_code == 503
    assert response.json()["code"] == "SUPERVISION_NOT_CONFIGURED"


def test_admin_client_ignores_proxies_and_redirects(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.test")
    with create_admin_client("http://search.test", 1.5) as client:
        assert client.trust_env is False
        assert client.follow_redirects is False
        assert client.timeout.read == 1.5


@pytest.mark.parametrize(
    "origin", ["http://search.test/path", "http://user@search.test"]
)
def test_admin_verifier_requires_a_service_origin(origin):
    with pytest.raises(ValidationError, match="without credentials or path"):
        Settings(_env_file=None, admin_verifier_base_url=origin)


@pytest.mark.parametrize("timeout", [0, 31])
def test_admin_verifier_timeout_is_bounded(timeout):
    with pytest.raises(ValidationError, match="admin_verifier_timeout_seconds"):
        Settings(_env_file=None, admin_verifier_timeout_seconds=timeout)
