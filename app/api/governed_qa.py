import hmac
import sqlite3
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.domain.qa import Verdict
from app.domain.resolution import RELEASING_OUTCOMES, ReviewCaseNotFoundError
from app.domain.review import RetainedQaContent
from app.infrastructure.qa_response import retain_qa_content

router = APIRouter(prefix="/governed/qa", tags=["Governed Q&A"])
_bearer = HTTPBearer(auto_error=False)


def _response(status: int, body: dict[str, object]) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})


def _denied(request: Request) -> JSONResponse:
    return _response(403, {"code": "QA_DENIED", "trace_id": request.state.trace_id})


def _ticket(key: bytes, review_id: UUID) -> str:
    return hmac.digest(key, b"governed-qa-release:" + review_id.bytes, "sha256").hex()


def _output(content: RetainedQaContent) -> JSONResponse:
    return _response(
        200,
        {
            "status": "success",
            "qa_pairs": [
                {"question": pair.question, "answer": pair.answer}
                for pair in content.qa_pairs
            ],
            "source_docs": [
                {"article_id": article_id} for article_id in content.source_article_ids
            ],
        },
    )


@router.post("/generate")
def generate(
    request: Request, payload: Annotated[dict[str, object], Body()]
) -> JSONResponse:
    state = request.app.state
    if state.qa_upstream is None:
        return _denied(request)
    upstream = state.qa_upstream.generate(payload)
    try:
        record = state.qa_assessor.execute(upstream)
        if record.assessment.verdict is Verdict.ALLOW:
            return _output(retain_qa_content(upstream))
    except (OSError, ValueError, sqlite3.Error):
        return _denied(request)
    if record.review_case_id is None:
        return _denied(request)
    return _response(
        202,
        {
            "review_id": record.review_case_id,
            "review_token": _ticket(state.qa_release_key, UUID(record.review_case_id)),
        },
    )


@router.get("/reviews/{review_id}")
def review_status(
    request: Request,
    review_id: UUID,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> JSONResponse:
    state = request.app.state
    key = state.qa_release_key
    if (
        key is None
        or credentials is None
        or not hmac.compare_digest(
            credentials.credentials.encode(), _ticket(key, review_id).encode()
        )
    ):
        return _denied(request)
    try:
        view = state.review_cases.view(str(review_id))
    except (ReviewCaseNotFoundError, ValueError, sqlite3.Error):
        return _denied(request)
    if view.expired:
        return _denied(request)
    if view.resolution is None:
        return _response(202, {"review_id": str(review_id)})
    if view.resolution.outcome not in RELEASING_OUTCOMES:
        return _denied(request)
    try:
        return _output(view.resolution.modified_content or view.case.content)
    except UnicodeError:
        return _denied(request)
