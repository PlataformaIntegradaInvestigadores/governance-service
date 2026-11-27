from dataclasses import asdict
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.api.errors import SupervisionNotConfiguredError
from app.api.supervisors import current_supervisor
from app.application.resolve_review import ReviewCases
from app.domain.rag import RetainedRagContent
from app.domain.resolution import (
    MAX_JUSTIFICATION_LENGTH,
    ReviewOutcome,
    ReviewResolution,
)
from app.domain.review import RetainedQaContent, RetainedQaPair
from app.domain.supervision import Supervisor

router = APIRouter(prefix="/review-cases", tags=["Review cases"])

_Text = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=10000)
]
_NO_STORE = {"Cache-Control": "no-store"}


class _Pair(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: _Text
    answer: _Text


class ResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Literal["approve", "modify", "reject"]
    justification: (
        Annotated[
            str,
            StringConstraints(
                strip_whitespace=True,
                max_length=MAX_JUSTIFICATION_LENGTH,
            ),
        ]
        | None
    ) = None
    qa_pairs: list[_Pair] | None = Field(default=None, min_length=1, max_length=50)
    answer_text: _Text | None = None

    @field_validator("answer_text")
    @classmethod
    def validate_answer_encoding(cls, value: str | None) -> str | None:
        if value is not None:
            value.encode("utf-8")
        return value

    @model_validator(mode="after")
    def content_only_for_modify(self) -> "ResolutionRequest":
        supplied = sum(value is not None for value in (self.qa_pairs, self.answer_text))
        expected = 1 if self.outcome == "modify" else 0
        if supplied != expected:
            raise ValueError("Modify requires exactly one correction")
        return self


def _reviews(request: Request) -> ReviewCases:
    reviews = request.app.state.review_cases
    if reviews is None:
        raise SupervisionNotConfiguredError
    return reviews


_Supervisor = Annotated[Supervisor, Depends(current_supervisor)]
_Reviews = Annotated[ReviewCases, Depends(_reviews)]


def _resolution(resolution: ReviewResolution) -> dict:
    supervisor = resolution.supervisor
    return {
        "resolution_id": resolution.resolution_id,
        "outcome": resolution.outcome.value,
        "resolved_at": resolution.resolved_at.isoformat(),
        "supervisor": (
            {
                "identity_id": supervisor.identity_id,
                "display_name": supervisor.display_name,
            }
            if supervisor is not None
            else None
        ),
        "justification": resolution.justification,
    }


def _review_content(case_content: RetainedRagContent | RetainedQaContent) -> dict:
    if isinstance(case_content, RetainedRagContent):
        cited, unresolved = case_content.citations()
        return {
            "rag": {
                "query": case_content.query,
                "answer_text": case_content.answer_text,
                "blocks": [asdict(block) for block in case_content.blocks],
                "cited_positions": list(cited),
                "unresolved_positions": list(unresolved),
            }
        }
    return {
        "qa_pairs": [asdict(pair) for pair in case_content.qa_pairs],
        "source_article_ids": list(case_content.source_article_ids),
    }


@router.get("/{case_id}", dependencies=[Depends(current_supervisor)])
def view_case(case_id: str, reviews: _Reviews, response: Response) -> dict:
    view = reviews.view(case_id)
    response.headers.update(_NO_STORE)
    if view.resolution is not None:
        status = view.resolution.outcome.value
    else:
        status = "expired" if view.expired else "pending"
    return {
        "case_id": view.case.case_id,
        "assessment_id": view.case.assessment_id,
        "created_at": view.case.created_at.isoformat(),
        "expires_at": view.expires_at.isoformat(),
        "status": status,
        "policy": {
            "policy_id": view.record.policy.policy_id,
            "version": view.record.policy.version,
        },
        "reason_codes": list(view.record.assessment.reason_codes),
        **_review_content(view.case.content),
        "resolution": (
            _resolution(view.resolution) if view.resolution is not None else None
        ),
    }


@router.post("/{case_id}/resolution")
def resolve_case(
    case_id: str,
    body: ResolutionRequest,
    admin: _Supervisor,
    reviews: _Reviews,
    response: Response,
) -> dict:
    pairs = (
        tuple(RetainedQaPair(pair.question, pair.answer) for pair in body.qa_pairs)
        if body.qa_pairs is not None
        else None
    )
    result = reviews.resolve(
        case_id,
        admin,
        ReviewOutcome(body.outcome),
        body.justification,
        pairs,
        modified_rag_answer=body.answer_text,
    )
    response.status_code = 201 if result.created else 200
    response.headers.update(_NO_STORE)
    return {"case_id": case_id, **_resolution(result.resolution)}
