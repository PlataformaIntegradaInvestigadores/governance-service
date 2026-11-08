from datetime import datetime
from typing import Protocol

from app.domain.qa import QaPolicy, RecordedAssessment
from app.domain.resolution import ReviewResolution
from app.domain.review import ReviewCase
from app.domain.supervision import Supervisor, SupervisorChange


class PolicyRepository(Protocol):
    def load(self) -> QaPolicy: ...


class AssessmentRepository(Protocol):
    """General decision queries; never returns retained content."""

    def save(
        self, record: RecordedAssessment, review_case: ReviewCase | None
    ) -> None: ...

    def get(self, assessment_id: str) -> RecordedAssessment | None: ...


class ReviewCaseRepository(Protocol):
    """Protected reads of retained content for reviewers."""

    def get(self, case_id: str) -> ReviewCase | None: ...


class TokenValidator(Protocol):
    """Confirms an access token with the identity service."""

    def validate(self, token: str) -> str:
        """Return the identity ID or raise an authentication or availability error."""
        ...


class SupervisorDirectory(Protocol):
    """Governance-owned list of who may review; changes are append-only."""

    def record(self, change: SupervisorChange) -> None: ...

    def current(self, identity_id: str) -> Supervisor | None:
        """Return the supervisor only while their latest change enables them."""
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
