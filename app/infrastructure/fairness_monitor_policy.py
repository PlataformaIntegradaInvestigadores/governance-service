import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.fairness_monitor import MonitorPolicy
from app.infrastructure.json_policy_repository import unique_policy_fields


class _PolicyDocument(BaseModel):
    model_config = ConfigDict(strict=True, allow_inf_nan=False, extra="forbid")

    version: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    window: int
    min_selected_per_group: int
    di_min: float
    spd_range: tuple[float, float] = Field(strict=False)
    reference: Literal["pool", "population"]
    population_share: float
    escalate_after: int
    return_after: int


def load_monitor_policy(path: Path) -> tuple[str, MonitorPolicy]:
    payload = json.loads(path.read_bytes(), object_pairs_hook=unique_policy_fields)
    document = _PolicyDocument.model_validate(payload)
    return document.version, MonitorPolicy(**document.model_dump(exclude={"version"}))
