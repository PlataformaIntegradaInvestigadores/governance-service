from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from app.application.assess_generation import assess_generation
from app.domain.qa import (
    Assessment,
    GenerationObservation,
    GenerationStatus,
    QaPolicy,
    Verdict,
)
from app.domain.review import RetainedQaContent, RetainedQaPair

NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class _QaPair(BaseModel):
    model_config = ConfigDict(strict=True)

    question: NonEmptyText
    answer: NonEmptyText


class _SourceDocument(BaseModel):
    model_config = ConfigDict(strict=True)

    article_id: str | int | None = None


class _QaResponse(BaseModel):
    model_config = ConfigDict(strict=True)

    status: NonEmptyText
    qa_pairs: list[_QaPair]
    source_docs: list[_SourceDocument] = Field(default_factory=list)
    error: str | None = None


def assess_qa_response(payload: object, policy: QaPolicy) -> Assessment:
    """Assess caller-supplied evidence without releasing or persisting its content."""
    try:
        response = _QaResponse.model_validate(payload)
        if response.status == "success":
            status = GenerationStatus.SUCCESS
        elif response.status == "partial" or response.status.startswith("partial:"):
            status = GenerationStatus.PARTIAL
        elif response.status == "error" or response.status.startswith("error:"):
            status = GenerationStatus.ERROR
        else:
            raise ValueError("Unknown generation status")
    except (ValidationError, ValueError):
        return Assessment(
            Verdict.DENY,
            policy.policy_id,
            policy.version,
            ("INVALID_UPSTREAM_RESPONSE",),
        )

    observation = GenerationObservation(
        status=status,
        pair_count=len(response.qa_pairs),
        sources_complete=bool(response.source_docs)
        and all(
            source.article_id is not None and str(source.article_id).strip()
            for source in response.source_docs
        ),
        has_error=bool(response.error),
    )
    return assess_generation(observation, policy)


def retain_qa_content(payload: object) -> RetainedQaContent:
    """Keep only the pairs and source identifiers a reviewer needs."""
    response = _QaResponse.model_validate(payload)
    return RetainedQaContent(
        tuple(RetainedQaPair(pair.question, pair.answer) for pair in response.qa_pairs),
        tuple(
            str(source.article_id).strip()
            for source in response.source_docs
            if source.article_id is not None and str(source.article_id).strip()
        ),
    )
