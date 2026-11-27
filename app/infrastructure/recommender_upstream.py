import httpx

_PATH = "/api/v1/recommend"
_MAX_RESPONSE_BYTES = 1024 * 1024


class RecommenderUpstream:
    def __init__(self, base_url: str, timeout_seconds: float) -> None:
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds

    def recommend(self, payload: dict[str, object]) -> tuple[int, bytes, str]:
        try:
            with (
                httpx.Client(
                    base_url=self._base_url,
                    timeout=self._timeout_seconds,
                    follow_redirects=False,
                    trust_env=False,
                ) as client,
                client.stream("POST", _PATH, json=payload) as response,
            ):
                if response.status_code not in (200, 404, 422):
                    return response.status_code, b"", "application/json"
                body = bytearray()
                for chunk in response.iter_bytes(chunk_size=64 * 1024):
                    if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
                        return 502, b"", "application/json"
                    body.extend(chunk)
                return response.status_code, bytes(body), "application/json"
        except (httpx.HTTPError, ValueError, RecursionError):
            return 502, b"", "application/json"
