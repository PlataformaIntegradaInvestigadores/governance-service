import json

import httpx

_PATH = "/api-se/v1/llm-search/generate-qa/"
_MAX_RESPONSE_BYTES = 1024 * 1024


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
                body = bytearray()
                for chunk in response.iter_bytes(chunk_size=64 * 1024):
                    if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
                        return None
                    body.extend(chunk)
            parsed = json.loads(body.decode("utf-8"))
            # Reject non-finite numbers and escaped unpaired surrogates.
            json.dumps(parsed, ensure_ascii=False, allow_nan=False).encode("utf-8")
            return parsed
        except httpx.HTTPError:
            return {"status": "error", "qa_pairs": []}
        except (ValueError, RecursionError):
            return None
