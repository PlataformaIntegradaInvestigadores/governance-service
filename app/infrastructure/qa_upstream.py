import httpx

from app.infrastructure.generation_response import read_generation_response

_PATH = "/api-se/v1/llm-search/generate-qa/"


class QaUpstream:
    def __init__(self, base_url: str, timeout_seconds: float) -> None:
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds

    def generate(self, payload: dict[str, object]) -> object:
        try:
            # A fresh client prevents upstream cookies crossing requester boundaries.
            with (
                httpx.Client(
                    base_url=self._base_url,
                    timeout=self._timeout_seconds,
                    follow_redirects=False,
                    trust_env=False,
                ) as client,
                client.stream("POST", _PATH, json=payload) as response,
            ):
                if response.status_code != 200:
                    return {"status": "error", "qa_pairs": []}
                return read_generation_response(response)
        except httpx.HTTPError:
            return {"status": "error", "qa_pairs": []}
        except (ValueError, RecursionError):
            return None
