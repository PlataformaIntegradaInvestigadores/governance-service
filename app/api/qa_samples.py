import json
import sqlite3
from pathlib import Path
from typing import Annotated, Literal, Self
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from app.api.errors import SupervisionNotConfiguredError
from app.api.review_cases import _Pair
from app.api.supervisors import current_supervisor
from app.application.sample_qa_deliveries import SampleQaDeliveries
from app.domain.qa_sampling import (
    InsufficientDeliveriesError,
    QaSample,
    QaSampleNotFoundError,
    QaSamplingPolicy,
)
from app.domain.resolution import MAX_JUSTIFICATION_LENGTH, JustificationRequiredError
from app.domain.supervision import Supervisor
from app.infrastructure.sqlite_assessment_repository import SqliteQaDeliveryRepository

router = APIRouter(
    prefix="/qa-samples",
    tags=["Q&A samples"],
    dependencies=[Depends(current_supervisor)],
)


class SampleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: AwareDatetime
    end: AwareDatetime
    seed: Annotated[int, Field(strict=True, ge=0, le=2**64 - 1)]

    @model_validator(mode="after")
    def validate_interval(self) -> Self:
        if self.start >= self.end:
            raise ValueError("Sampling interval must have positive duration")
        return self


class SampleDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal["confirm", "flag", "correct"]
    justification: (
        Annotated[
            str,
            StringConstraints(
                strip_whitespace=True, min_length=1, max_length=MAX_JUSTIFICATION_LENGTH
            ),
        ]
        | None
    ) = None
    qa_pairs: list[_Pair] | None = Field(default=None, min_length=1, max_length=50)

    @model_validator(mode="after")
    def validate_correction(self) -> Self:
        if (self.outcome == "correct") != (self.qa_pairs is not None):
            raise ValueError("Only a correction requires replacement pairs")
        return self


def _repository(request: Request) -> SqliteQaDeliveryRepository:
    repository = request.app.state.qa_deliveries
    if repository is None:
        raise SupervisionNotConfiguredError
    return repository


@router.post("/", status_code=201)
def create_sample(
    body: SampleRequest, request: Request, response: Response
) -> QaSample:
    repository = _repository(request)
    try:
        document = Path("policies/qa-retrospective-sampling.v1.json").read_text(
            encoding="utf-8"
        )
        policy = QaSamplingPolicy(**json.loads(document))
        sample = SampleQaDeliveries(repository, policy).execute(
            body.start, body.end, body.seed
        )
    except InsufficientDeliveriesError:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError) as error:
        raise SupervisionNotConfiguredError from error
    response.headers["Cache-Control"] = "no-store"
    return sample


@router.get("/{sample_id}")
def get_sample(sample_id: UUID, request: Request, response: Response) -> QaSample:
    try:
        sample = _repository(request).get(sample_id)
    except (sqlite3.Error, ValueError) as error:
        raise SupervisionNotConfiguredError from error
    response.headers["Cache-Control"] = "no-store"
    return sample


def _selected(
    repository: SqliteQaDeliveryRepository, sample_id: UUID, assessment_id: UUID
) -> str:
    identifier = str(assessment_id)
    if identifier not in repository.get(sample_id).selected:
        raise QaSampleNotFoundError
    return identifier


@router.get("/{sample_id}/deliveries/{assessment_id}")
def read_delivery(
    sample_id: UUID, assessment_id: UUID, request: Request, response: Response
) -> dict:
    repository = _repository(request)
    try:
        content = repository.review_content(
            _selected(repository, sample_id, assessment_id)
        )
    except (sqlite3.Error, ValueError) as error:
        raise SupervisionNotConfiguredError from error
    response.headers["Cache-Control"] = "no-store"
    return content


@router.put("/{sample_id}/deliveries/{assessment_id}/resolution")
def resolve_delivery(
    sample_id: UUID,
    assessment_id: UUID,
    body: SampleDecision,
    request: Request,
    response: Response,
    supervisor: Annotated[Supervisor, Depends(current_supervisor)],
) -> dict:
    if body.outcome != "confirm" and body.justification is None:
        raise JustificationRequiredError
    repository = _repository(request)
    try:
        identifier = _selected(repository, sample_id, assessment_id)
        repository.review_content(identifier)
        result = repository.save_review(
            sample_id,
            identifier,
            {
                **body.model_dump(),
                "supervisor_identity_id": supervisor.identity_id,
                "supervisor_snapshot": {"display_name": supervisor.display_name},
            },
        )
    except (sqlite3.Error, ValueError) as error:
        raise SupervisionNotConfiguredError from error
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("/{sample_id}/interventions")
def interventions(sample_id: UUID, request: Request, response: Response) -> dict:
    repository = _repository(request)
    try:
        counts = repository.intervention_counts(repository.get(sample_id))
    except (sqlite3.Error, ValueError) as error:
        raise SupervisionNotConfiguredError from error
    response.headers["Cache-Control"] = "no-store"
    return {
        "counts": counts,
        "rates": {
            key: counts[key] / counts["eligible"]
            for key in ("prior", "retrospective", "combined")
        },
    }
