from app.domain.repositories import AdminTokenValidator
from app.domain.supervision import ADMIN_SUPERVISOR, AdminTokenRejectedError, Supervisor


class AuthorizeSupervisor:
    def __init__(self, tokens: AdminTokenValidator) -> None:
        self._tokens = tokens

    def execute(self, token: str) -> Supervisor:
        if (
            not token.isascii()
            or not token.isprintable()
            or any(character.isspace() for character in token)
        ):
            raise AdminTokenRejectedError("Invalid administrator token header")
        self._tokens.validate(token)
        return ADMIN_SUPERVISOR
