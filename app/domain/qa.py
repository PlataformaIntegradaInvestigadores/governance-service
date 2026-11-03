from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class GenerationStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    ERROR = "error"


class Verdict(StrEnum):
    SUSPEND = "suspend"
    DENY = "deny"


@dataclass(frozen=True)
class QaPolicy:
    policy_id: str
    version: str
    expected_pairs: int

    def __post_init__(self) -> None:
        if not self.policy_id.strip() or not self.version.strip():
            raise ValueError("Policy identity and version must not be blank")
        if type(self.expected_pairs) is not int or self.expected_pairs < 1:
            raise ValueError("expected_pairs must be a positive integer")


@dataclass(frozen=True)
class GenerationObservation:
    status: GenerationStatus
    pair_count: int
    sources_complete: bool
    has_error: bool


@dataclass(frozen=True)
class Assessment:
    verdict: Verdict
    policy_id: str
    policy_version: str
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class RecordedAssessment:
    assessment_id: str
    created_at: datetime
    policy: QaPolicy
    assessment: Assessment


class PolicyVersionConflict(ValueError):
    pass
