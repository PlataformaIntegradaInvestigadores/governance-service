from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from app.domain.review import RecommendationSelection, RetainedQaContent
from app.domain.supervision import Supervisor


class ReviewOutcome(StrEnum):
    APPROVE = "approve"
    MODIFY = "modify"
    REJECT = "reject"
    EXPIRED = "expired"


RELEASING_OUTCOMES = frozenset({ReviewOutcome.APPROVE, ReviewOutcome.MODIFY})
# Reason codes that flag a defect; approving output that carries one overrides
# the evaluator and therefore needs a justification.
RISK_REASON_CODES = frozenset({"PARTIAL_GENERATION", "SOURCE_EVIDENCE_INCOMPLETE"})
MAX_JUSTIFICATION_LENGTH = 2000


class ReviewCaseNotFoundError(LookupError):
    pass


class ReviewCaseExpiredError(Exception):
    """The review window closed; the output is denied and cannot be released."""


class ReviewCaseAlreadyResolvedError(Exception):
    """The case has a different resolution; resolutions are final."""


class JustificationRequiredError(ValueError):
    pass


class InvalidRecommendationResolutionError(ValueError):
    pass


def _identity_of(supervisor: Supervisor | None) -> str | None:
    return supervisor.identity_id if supervisor is not None else None


def justification_required(
    outcome: ReviewOutcome, reason_codes: tuple[str, ...]
) -> bool:
    if outcome is ReviewOutcome.MODIFY:
        return True
    return outcome is ReviewOutcome.APPROVE and bool(
        RISK_REASON_CODES.intersection(reason_codes)
    )


@dataclass(frozen=True)
class ReviewResolution:
    """The single, final outcome of a review case."""

    resolution_id: str
    case_id: str
    outcome: ReviewOutcome
    resolved_at: datetime
    expires_at: datetime
    supervisor: Supervisor | None
    justification: str | None = field(default=None, repr=False)
    modified_content: RetainedQaContent | RecommendationSelection | None = None

    def __post_init__(self) -> None:
        if self.resolved_at.utcoffset() is None or self.expires_at.utcoffset() is None:
            raise ValueError("Resolution times must be timezone-aware")
        if (self.outcome is ReviewOutcome.EXPIRED) != (self.supervisor is None):
            raise ValueError("Only an expiry is recorded without a supervisor")
        if (self.outcome is ReviewOutcome.MODIFY) != (
            self.modified_content is not None
        ):
            raise ValueError("Modified content is required only for modify")
        if self.justification is not None and (
            not self.justification.strip()
            or len(self.justification) > MAX_JUSTIFICATION_LENGTH
        ):
            raise ValueError("Justification must be nonblank and at most 2000 chars")

    def matches(self, other: "ReviewResolution") -> bool:
        """True when a repeated request asks for this same resolution."""
        return (
            self.outcome is other.outcome
            and _identity_of(self.supervisor) == _identity_of(other.supervisor)
            and self.justification == other.justification
            and self.modified_content == other.modified_content
        )
