import json

import httpx

_MAX_RESPONSE_BYTES = 1024 * 1024


def read_generation_response(response: httpx.Response) -> object:
    body = bytearray()
    for chunk in response.iter_bytes(chunk_size=64 * 1024):
        if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
            return None
        body.extend(chunk)
    parsed = json.loads(body.decode("utf-8"))
    # Reject non-finite numbers and escaped unpaired surrogates.
    json.dumps(parsed, ensure_ascii=False, allow_nan=False).encode("utf-8")
    return parsed
