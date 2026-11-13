import httpx

from app.domain.supervision import (
    AdminTokenRejectedError,
    AdminVerifierUnavailableError,
)

VALIDATE_ADMIN_PATH = "/internal/auth/validate-admin-token/"


class SearchAdminTokenValidator:
    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def validate(self, token: str) -> None:
        try:
            request = httpx.Request(
                "GET",
                self._client.base_url.join(VALIDATE_ADMIN_PATH),
                headers={"X-Admin-Token": token},
            )
            response = self._client.send(request)
        except httpx.HTTPError:
            raise AdminVerifierUnavailableError("Admin verifier unreachable") from None
        if response.status_code == 403:
            raise AdminTokenRejectedError("Administrator token rejected")
        if response.status_code != 204:
            raise AdminVerifierUnavailableError("Unexpected admin verifier response")


def create_admin_client(base_url: str, timeout_seconds: float) -> httpx.Client:
    return httpx.Client(
        base_url=base_url,
        timeout=timeout_seconds,
        follow_redirects=False,
        trust_env=False,
    )
