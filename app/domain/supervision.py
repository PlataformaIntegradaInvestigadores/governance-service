from dataclasses import dataclass
from datetime import datetime


class AuthenticationError(Exception):
    """The caller presented no valid access token."""


class IdentityUnavailableError(Exception):
    """Identity could not confirm a token; access is denied, not assumed."""


class SupervisorNotEnabledError(Exception):
    """The authenticated user is not an enabled supervisor."""


def _require_text(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} must not be blank")


@dataclass(frozen=True)
class Supervisor:
    """An enabled reviewer: identity reference plus the name recorded for it."""

    identity_id: str
    display_name: str

    def __post_init__(self) -> None:
        _require_text(self.identity_id, "identity_id")
        _require_text(self.display_name, "display_name")


@dataclass(frozen=True)
class SupervisorChange:
    """One append-only entry of the supervisor list."""

    supervisor: Supervisor
    enabled: bool
    recorded_at: datetime

    def __post_init__(self) -> None:
        if self.recorded_at.utcoffset() is None:
            raise ValueError("recorded_at must be timezone-aware")
