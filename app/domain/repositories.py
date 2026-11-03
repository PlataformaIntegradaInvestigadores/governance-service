from typing import Protocol

from app.domain.qa import QaPolicy, RecordedAssessment


class PolicyRepository(Protocol):
    def load(self) -> QaPolicy: ...


class AssessmentRepository(Protocol):
    def save(self, record: RecordedAssessment) -> None: ...

    def get(self, assessment_id: str) -> RecordedAssessment | None: ...
