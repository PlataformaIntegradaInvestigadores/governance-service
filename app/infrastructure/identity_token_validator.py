import httpx

from app.domain.supervision import AuthenticationError, IdentityUnavailableError

VALIDATE_TOKEN_PATH = "/internal/auth/validate-token/"  # noqa: S105 - URL path
_USER_HEADER = "X-Authenticated-User-Id"
_TOKEN_TYPE_HEADER = "X-Authenticated-Token-Type"  # noqa: S105 - header name


class IdentityTokenValidator:
    """Delegates token checks to identity-service; never verifies JWTs locally.

    Identity signs tokens with a shared HS256 key, so holding that key here would
    let this service forge tokens. Errors never include the token.
    """

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def validate(self, token: str) -> str:
        try:
            response = self._client.get(
                VALIDATE_TOKEN_PATH, headers={"Authorization": f"Bearer {token}"}
            )
        except httpx.HTTPError as error:
            raise IdentityUnavailableError("Identity service unreachable") from error
        if response.status_code in (401, 403):
            raise AuthenticationError("Token rejected by identity service")
        identity_id = response.headers.get(_USER_HEADER, "").strip()
        if response.status_code != 204 or not identity_id:
            raise IdentityUnavailableError("Unexpected identity service response")
        if response.headers.get(_TOKEN_TYPE_HEADER) != "access":
            raise AuthenticationError("Only access tokens are accepted")
        return identity_id


def create_identity_client(base_url: str, timeout_seconds: float) -> httpx.Client:
    # trust_env=False: proxy variables must not route bearer tokens elsewhere.
    return httpx.Client(
        base_url=base_url,
        timeout=timeout_seconds,
        follow_redirects=False,
        trust_env=False,
    )
