import json
import sqlite3
from pathlib import Path
from typing import Annotated, Self
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.api.errors import SupervisionNotConfiguredError
from app.api.supervisors import current_supervisor
from app.application.sample_qa_deliveries import SampleQaDeliveries
from app.domain.qa_sampling import (
    InsufficientDeliveriesError,
    QaSample,
    QaSamplingPolicy,
)
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
