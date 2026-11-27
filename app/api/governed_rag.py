import hmac
import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.application.record_rag_assessment import RecordRagAssessment
from app.domain.qa import Verdict
from app.domain.rag import RetainedRagContent
from app.domain.resolution import RELEASING_OUTCOMES, ReviewCaseNotFoundError
from app.infrastructure.rag_upstream import RagUpstream

router = APIRouter(prefix="/governed/rag", tags=["Governed RAG"])
_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class RagRouteDependencies:
    upstream: RagUpstream
    assessor: RecordRagAssessment
    release_key: bytes = field(repr=False)


class AskRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)

    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    topk: Annotated[int, Field(ge=1)] | None = None
    stream: bool = False

    @field_validator("query")
    @classmethod
    def validate_encoding(cls, value: str) -> str:
        value.encode("utf-8")
        return value


def _response(status: int, body: dict[str, object]) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})


def _denied(request: Request) -> JSONResponse:
    return _response(403, {"code": "RAG_DENIED", "trace_id": request.state.trace_id})


def _ticket(key: bytes, review_id: UUID) -> str:
    return hmac.digest(key, b"governed-rag-release:" + review_id.bytes, "sha256").hex()


def _deliver(content: RetainedRagContent) -> JSONResponse:
    cited, _ = content.citations()
    blocks = []
    for block in content.blocks:
        metadata = asdict(block)
        del metadata["text"]
        blocks.append(metadata)
    return _response(
        200,
        {
            "query": content.query,
            "answer_text": content.answer_text,
            "blocks": blocks,
            "used_refs_report": "\n".join(
                f"[{position}] {content.blocks[position - 1].title}"
                for position in cited
            ),
            "timing_ms": asdict(content.timing_ms),
        },
    )


@router.post("/ask")
def ask(request: Request, payload: AskRequest) -> JSONResponse:
    if payload.stream:
        return _response(
            422,
            {"code": "RAG_STREAMING_UNSUPPORTED", "trace_id": request.state.trace_id},
        )
    rag: RagRouteDependencies | None = request.app.state.rag
    if rag is None:
        return _denied(request)
    content = rag.upstream.ask(
        payload.model_dump(exclude={"stream"}, exclude_unset=True)
    )
    try:
        record = rag.assessor.execute(content)
    except (OSError, ValueError, sqlite3.Error):
        return _denied(request)
    if content is not None and record.assessment.verdict is Verdict.ALLOW:
        return _deliver(content)
    if record.review_case_id is None:
        return _denied(request)
    return _response(
        202,
        {
            "review_id": record.review_case_id,
            "review_token": _ticket(rag.release_key, UUID(record.review_case_id)),
        },
    )


@router.get("/reviews/{review_id}")
def review_status(
    request: Request,
    review_id: UUID,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> JSONResponse:
    state = request.app.state
    rag: RagRouteDependencies | None = state.rag
    if (
        rag is None
        or state.review_cases is None
        or credentials is None
        or not hmac.compare_digest(
            credentials.credentials.encode(),
            _ticket(rag.release_key, review_id).encode(),
        )
    ):
        return _denied(request)
    try:
        view = state.review_cases.view(str(review_id))
    except (OSError, ReviewCaseNotFoundError, ValueError, sqlite3.Error):
        return _denied(request)
    if view.expired or not isinstance(view.case.content, RetainedRagContent):
        return _denied(request)
    if view.resolution is None:
        return _response(202, {"review_id": str(review_id)})
    if view.resolution.outcome not in RELEASING_OUTCOMES:
        return _denied(request)
    content = view.resolution.modified_content or view.case.content
    if not isinstance(content, RetainedRagContent):
        return _denied(request)
    return _deliver(content)
