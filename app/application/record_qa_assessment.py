from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from app.domain.qa import Assessment, QaPolicy, RecordedAssessment
from app.domain.repositories import AssessmentRepository, PolicyRepository


class RecordQaAssessment:
    def __init__(
        self,
        policies: PolicyRepository,
        assessments: AssessmentRepository,
        assessor: Callable[[object, QaPolicy], Assessment],
    ) -> None:
        self._policies = policies
        self._assessments = assessments
        self._assessor = assessor

    def execute(self, payload: object) -> RecordedAssessment:
        policy = self._policies.load()
        assessment = self._assessor(payload, policy)
        record = RecordedAssessment(str(uuid4()), datetime.now(UTC), policy, assessment)
        self._assessments.save(record)
        return record
