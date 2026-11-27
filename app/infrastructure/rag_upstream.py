from typing import Annotated

import httpx
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.domain.rag import RagBlock, RagTiming, RetainedRagContent
from app.infrastructure.generation_response import read_generation_response

_Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
_Milliseconds = Annotated[int, Field(ge=0)]


class _UpstreamModel(BaseModel):
    model_config = ConfigDict(strict=True, hide_input_in_errors=True)


class _Block(_UpstreamModel):
    cite_id: str
    title: str
    text: _Text
    authors_mention: str | None = None
    authors_raw: str | None = None
    year: str | None = None
    doi_raw: str | None = None


class _Timing(_UpstreamModel):
    search: _Milliseconds
    rerank: _Milliseconds
    generate: _Milliseconds
    total: _Milliseconds


class _AskResponse(_UpstreamModel):
    query: _Text
    answer_text: _Text
    blocks: list[_Block]
    used_refs_report: str
    timing_ms: _Timing


class RagUpstream:
    def __init__(self, base_url: str, timeout_seconds: float) -> None:
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds

    def ask(self, payload: dict[str, object]) -> RetainedRagContent | None:
        try:
            with (
                httpx.Client(
                    base_url=self._base_url,
                    timeout=self._timeout_seconds,
                    follow_redirects=False,
                    trust_env=False,
                ) as client,
                client.stream("POST", "/ask", json=payload) as response,
            ):
                if response.status_code != 200:
                    return None
                upstream = _AskResponse.model_validate(
                    read_generation_response(response)
                )
        except (httpx.HTTPError, ValueError, RecursionError):
            return None
        if upstream.query != payload["query"]:
            return None
        return RetainedRagContent(
            upstream.query,
            upstream.answer_text,
            tuple(RagBlock(**block.model_dump()) for block in upstream.blocks),
            RagTiming(**upstream.timing_ms.model_dump()),
        )
