from typing import Protocol

from app.domain.qa import QaPolicy, RecordedAssessment
from app.domain.review import ReviewCase


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
