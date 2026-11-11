from dataclasses import dataclass


class AuthenticationError(Exception):
    """The caller presented no valid access token."""


class AdminVerifierUnavailableError(Exception):
    """The administrator token verifier could not confirm a token."""


class AdminTokenRejectedError(Exception):
    """The administrator token verifier rejected the token."""


def _require_text(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} must not be blank")


@dataclass(frozen=True)
class Supervisor:
    """The recorded actor of a review resolution."""

    identity_id: str
    display_name: str

    def __post_init__(self) -> None:
        _require_text(self.identity_id, "identity_id")
        _require_text(self.display_name, "display_name")


ADMIN_SUPERVISOR = Supervisor("admin", "admin")
