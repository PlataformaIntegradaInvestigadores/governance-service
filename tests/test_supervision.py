import logging
import sqlite3
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.application.supervision import AuthorizeSupervisor, ManageSupervisors
from app.core.config import Settings
from app.domain.supervision import (
    AuthenticationError,
    IdentityUnavailableError,
    Supervisor,
    SupervisorChange,
    SupervisorNotEnabledError,
)
from app.infrastructure.identity_token_validator import (
    VALIDATE_TOKEN_PATH,
    IdentityTokenValidator,
    create_identity_client,
)
from app.infrastructure.sqlite_supervisor_directory import SqliteSupervisorDirectory
from app.main import create_app, create_supervisor_authorizer

TOKEN = "synthetic-access-token"  # noqa: S105 - test value, never a real credential
NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


class FakeTokens:
    def __init__(self, identity_id="U000000001", error=None):
        self.identity_id = identity_id
        self.error = error
        self.calls = []

    def validate(self, token):
        self.calls.append(token)
        if self.error is not None:
            raise self.error
        return self.identity_id


@pytest.fixture
def directory(tmp_path):
    return SqliteSupervisorDirectory(tmp_path / "supervisors.sqlite3")


@pytest.fixture
def roster(directory):
    return ManageSupervisors(directory, clock=lambda: NOW)


def identity_responding(status_code, headers=None, requests=None):
    def handler(request):
        if requests is not None:
            requests.append(request)
        return httpx.Response(status_code, headers=headers)

    client = httpx.Client(
        base_url="http://identity.test", transport=httpx.MockTransport(handler)
    )
    return IdentityTokenValidator(client)


ACCEPTED = {
    "X-Authenticated-User-Id": "U000000001",
    "X-Authenticated-Token-Type": "access",
}


# Domain


@pytest.mark.parametrize(("identity_id", "display_name"), [(" ", "Ana"), ("U1", "")])
def test_supervisor_requires_identity_and_name(identity_id, display_name):
    with pytest.raises(ValueError, match="must not be blank"):
        Supervisor(identity_id, display_name)


def test_supervisor_change_requires_timezone():
    naive = datetime(2026, 11, 8)  # noqa: DTZ001 - the invalid input under test
    with pytest.raises(ValueError, match="timezone-aware"):
        SupervisorChange(Supervisor("U1", "Ana"), True, naive)


# Authorization use case


@pytest.mark.parametrize("token", [None, "", "   "])
def test_missing_token_is_rejected_without_calling_identity(directory, token):
    tokens = FakeTokens()
    with pytest.raises(AuthenticationError):
        AuthorizeSupervisor(tokens, directory).execute(token)
    assert tokens.calls == []


@pytest.mark.parametrize(
    "error", [AuthenticationError("rejected"), IdentityUnavailableError("down")]
)
def test_identity_failures_deny_access_without_fallback(directory, roster, error):
    roster.enable("U000000001", "Ana Reviewer")
    with pytest.raises(type(error)):
        AuthorizeSupervisor(FakeTokens(error=error), directory).execute(TOKEN)


def test_authenticated_user_outside_the_list_is_denied(directory, roster):
    roster.enable("U000000002", "Other Reviewer")
    with pytest.raises(SupervisorNotEnabledError):
        AuthorizeSupervisor(FakeTokens(), directory).execute(TOKEN)


def test_enabled_supervisor_is_returned_with_snapshot(directory, roster):
    roster.enable(" U000000001 ", " Ana Reviewer ")
    tokens = FakeTokens()

    supervisor = AuthorizeSupervisor(tokens, directory).execute(TOKEN)

    assert supervisor == Supervisor("U000000001", "Ana Reviewer")
    assert tokens.calls == [TOKEN]


def test_disabled_supervisor_is_denied(directory, roster):
    roster.enable("U000000001", "Ana Reviewer")
    roster.disable("U000000001")
    with pytest.raises(SupervisorNotEnabledError):
        AuthorizeSupervisor(FakeTokens(), directory).execute(TOKEN)


def test_disabling_someone_not_enabled_records_nothing(directory, roster, tmp_path):
    with pytest.raises(SupervisorNotEnabledError):
        roster.disable("U000000001")
    with sqlite3.connect(tmp_path / "supervisors.sqlite3") as connection:
        count = connection.execute("SELECT COUNT(*) FROM supervisor_changes")
        assert count.fetchone()[0] == 0


def test_default_clock_records_utc(directory):
    ManageSupervisors(directory).enable("U000000001", "Ana Reviewer")
    with sqlite3.connect(directory._path) as connection:
        recorded_at = connection.execute(
            "SELECT recorded_at FROM supervisor_changes"
        ).fetchone()[0]
    assert datetime.fromisoformat(recorded_at).utcoffset().total_seconds() == 0


# SQLite supervisor list


def test_list_survives_reopening_and_latest_change_wins(tmp_path, roster):
    roster.enable("U000000001", "Ana Reviewer")
    roster.disable("U000000001")
    roster.enable("U000000001", "Ana M. Reviewer")

    reopened = SqliteSupervisorDirectory(tmp_path / "supervisors.sqlite3")

    assert reopened.current("U000000001") == Supervisor("U000000001", "Ana M. Reviewer")
    assert reopened.current("unknown") is None
    with sqlite3.connect(tmp_path / "supervisors.sqlite3") as connection:
        history = connection.execute(
            "SELECT enabled FROM supervisor_changes ORDER BY sequence"
        ).fetchall()
    assert history == [(1,), (0,), (1,)]


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE supervisor_changes SET enabled = 1",
        "DELETE FROM supervisor_changes",
    ],
)
def test_supervisor_changes_are_append_only(tmp_path, roster, statement):
    roster.enable("U000000001", "Ana Reviewer")
    with (
        sqlite3.connect(tmp_path / "supervisors.sqlite3") as connection,
        pytest.raises(sqlite3.IntegrityError, match="append-only"),
    ):
        connection.execute(statement)


@pytest.mark.parametrize("values", [(" ", "Ana", 1), ("U1", " ", 1), ("U1", "Ana", 2)])
def test_database_rejects_invalid_changes(tmp_path, directory, values):
    with (
        sqlite3.connect(tmp_path / "supervisors.sqlite3") as connection,
        pytest.raises(sqlite3.IntegrityError, match="CHECK"),
    ):
        connection.execute(
            "INSERT INTO supervisor_changes "
            "(identity_id, display_name, enabled, recorded_at) VALUES (?, ?, ?, ?)",
            (*values, NOW.isoformat()),
        )


# Identity adapter


def test_identity_accepts_access_token_and_returns_user_id():
    requests = []
    validator = identity_responding(204, ACCEPTED, requests)

    assert validator.validate(TOKEN) == "U000000001"
    assert requests[0].method == "GET"
    assert requests[0].url.path == VALIDATE_TOKEN_PATH
    assert requests[0].headers["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize("status_code", [401, 403])
def test_identity_rejection_is_an_authentication_error(status_code):
    with pytest.raises(AuthenticationError):
        identity_responding(status_code).validate(TOKEN)


def test_refresh_tokens_are_rejected():
    headers = {**ACCEPTED, "X-Authenticated-Token-Type": "refresh"}
    with pytest.raises(AuthenticationError):
        identity_responding(204, headers).validate(TOKEN)


@pytest.mark.parametrize(
    ("status_code", "headers"),
    [
        (200, ACCEPTED),
        (302, {"Location": "http://elsewhere.test/"}),
        (500, None),
        (204, {"X-Authenticated-Token-Type": "access"}),
        (204, {**ACCEPTED, "X-Authenticated-User-Id": " "}),
    ],
)
def test_unexpected_identity_responses_deny_access(status_code, headers):
    with pytest.raises(IdentityUnavailableError):
        identity_responding(status_code, headers).validate(TOKEN)


def test_unreachable_identity_denies_access_without_leaking_token():
    def handler(request):
        raise httpx.ConnectTimeout("timed out", request=request)

    validator = IdentityTokenValidator(
        httpx.Client(
            base_url="http://identity.test", transport=httpx.MockTransport(handler)
        )
    )
    with pytest.raises(IdentityUnavailableError) as raised:
        validator.validate(TOKEN)
    assert TOKEN not in str(raised.value)
    assert TOKEN not in str(raised.value.__cause__)


def test_identity_client_ignores_proxies_and_redirects(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.test:3128")
    client = create_identity_client("http://identity.test", 1.5)
    assert client.follow_redirects is False
    assert client.trust_env is False
    assert client.timeout.read == 1.5


# HTTP route


@pytest.fixture
def api(directory):
    tokens = FakeTokens()
    application = create_app(Settings(_env_file=None))
    application.state.authorize_supervisor = AuthorizeSupervisor(tokens, directory)
    return TestClient(application), tokens


def assert_error(response, status_code, code):
    assert response.status_code == status_code
    assert response.json() == {
        "code": code,
        "trace_id": response.headers["X-Trace-ID"],
    }


@pytest.mark.parametrize(
    "headers", [{}, {"Authorization": f"Basic {TOKEN}"}, {"Authorization": "Bearer"}]
)
def test_route_requires_a_bearer_token(api, headers):
    client, tokens = api
    response = client.get("/api/v1/supervisors/me", headers=headers)
    assert_error(response, 401, "AUTHENTICATION_REQUIRED")
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert tokens.calls == []


def test_route_denies_authenticated_user_not_enabled(api):
    client, _ = api
    response = client.get(
        "/api/v1/supervisors/me", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert_error(response, 403, "SUPERVISOR_NOT_ENABLED")


def test_route_denies_when_identity_is_unavailable(api, roster):
    client, tokens = api
    roster.enable("U000000001", "Ana Reviewer")
    tokens.error = IdentityUnavailableError("down")
    response = client.get(
        "/api/v1/supervisors/me", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert_error(response, 503, "IDENTITY_UNAVAILABLE")


def test_route_returns_enabled_supervisor_without_logging_token(api, roster, caplog):
    client, _ = api
    roster.enable("U000000001", "Ana Reviewer")
    with caplog.at_level(logging.DEBUG):
        response = client.get(
            "/api/v1/supervisors/me", headers={"Authorization": f"Bearer {TOKEN}"}
        )
    assert response.status_code == 200
    assert response.json() == {
        "identity_id": "U000000001",
        "display_name": "Ana Reviewer",
    }
    assert TOKEN not in caplog.text


def test_route_is_unavailable_until_supervision_is_configured():
    client = TestClient(create_app(Settings(_env_file=None)))
    response = client.get(
        "/api/v1/supervisors/me", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert_error(response, 503, "SUPERVISION_NOT_CONFIGURED")


# Composition and settings


def test_complete_settings_compose_the_authorizer(tmp_path):
    settings = Settings(
        _env_file=None,
        identity_base_url="http://identity-service:8002",
        database=tmp_path / "supervisors.sqlite3",
    )
    assert isinstance(create_supervisor_authorizer(settings), AuthorizeSupervisor)
    assert (tmp_path / "supervisors.sqlite3").exists()


@pytest.mark.parametrize(
    "overrides",
    [
        {"identity_base_url": "http://identity-service:8002"},
        {"database": "local/supervisors.sqlite3"},
    ],
)
def test_partial_supervision_settings_are_rejected(overrides):
    with pytest.raises(ValidationError, match="together"):
        Settings(_env_file=None, **overrides)


@pytest.mark.parametrize("timeout", [0, 31])
def test_identity_timeout_is_bounded(timeout):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, identity_timeout_seconds=timeout)
