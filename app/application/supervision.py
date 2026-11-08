from collections.abc import Callable
from datetime import UTC, datetime

from app.domain.repositories import SupervisorDirectory, TokenValidator
from app.domain.supervision import (
    AuthenticationError,
    Supervisor,
    SupervisorChange,
    SupervisorNotEnabledError,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class AuthorizeSupervisor:
    """Allow a caller only when identity confirms the token and the list enables it.

    Every other outcome raises: there is no default supervisor and no fallback when
    identity is unavailable.
    """

    def __init__(
        self, tokens: TokenValidator, supervisors: SupervisorDirectory
    ) -> None:
        self._tokens = tokens
        self._supervisors = supervisors

    def execute(self, token: str | None) -> Supervisor:
        if token is None or not token.strip():
            raise AuthenticationError("Missing access token")
        identity_id = self._tokens.validate(token)
        supervisor = self._supervisors.current(identity_id)
        if supervisor is None:
            raise SupervisorNotEnabledError("User is not an enabled supervisor")
        return supervisor


class ManageSupervisors:
    """Enable or disable supervisors by appending changes to the list."""

    def __init__(
        self,
        supervisors: SupervisorDirectory,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._supervisors = supervisors
        self._clock = clock

    def enable(self, identity_id: str, display_name: str) -> Supervisor:
        supervisor = Supervisor(identity_id.strip(), display_name.strip())
        self._supervisors.record(SupervisorChange(supervisor, True, self._clock()))
        return supervisor

    def disable(self, identity_id: str) -> None:
        supervisor = self._supervisors.current(identity_id)
        if supervisor is None:
            raise SupervisorNotEnabledError("User is not an enabled supervisor")
        self._supervisors.record(SupervisorChange(supervisor, False, self._clock()))
