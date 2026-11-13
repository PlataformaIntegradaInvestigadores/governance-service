from datetime import datetime
from typing import Protocol

from app.domain.qa import QaPolicy, RecordedAssessment
from app.domain.qa_sampling import QaSample
from app.domain.resolution import ReviewResolution
from app.domain.review import ReviewCase


class PolicyRepository(Protocol):
    def load(self) -> QaPolicy: ...


class QaSampleRepository(Protocol):
    def eligible_ids(self, start: datetime, end: datetime) -> tuple[str, ...]: ...
    def save(self, sample: QaSample) -> None: ...


class AssessmentRepository(Protocol):
    """General decision queries; never returns retained content."""

    def save(
        self, record: RecordedAssessment, review_case: ReviewCase | None
    ) -> None: ...

    def get(self, assessment_id: str) -> RecordedAssessment | None: ...


class ReviewCaseRepository(Protocol):
    """Protected reads of retained content for reviewers."""

    def get(self, case_id: str) -> ReviewCase | None: ...


class AdminTokenValidator(Protocol):
    def validate(self, token: str) -> None:
        """Confirm the admin token or raise a rejection or availability error."""
        ...


class ReviewResolutionRepository(Protocol):
    """Final outcomes of review cases; one per case, append-only."""

    def save(self, resolution: ReviewResolution) -> None:
        """Store it or raise ReviewCaseAlreadyResolvedError if the case has one."""
        ...

    def get(self, case_id: str) -> ReviewResolution | None: ...

    def pending_case_ids(self, created_before: datetime) -> tuple[str, ...]:
        """Cases without a resolution that were opened before the given time."""
        ...
