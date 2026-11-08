from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from app.domain.qa import Assessment, QaPolicy, RecordedAssessment, Verdict
from app.domain.repositories import AssessmentRepository, PolicyRepository
from app.domain.review import RetainedQaContent, ReviewCase


class RecordQaAssessment:
    def __init__(
        self,
        policies: PolicyRepository,
        assessments: AssessmentRepository,
        assessor: Callable[[object, QaPolicy], Assessment],
        retain: Callable[[object], RetainedQaContent],
    ) -> None:
        self._policies = policies
        self._assessments = assessments
        self._assessor = assessor
        self._retain = retain

    def execute(self, payload: object) -> RecordedAssessment:
        policy = self._policies.load()
        assessment = self._assessor(payload, policy)
        record = RecordedAssessment(str(uuid4()), datetime.now(UTC), policy, assessment)
        case = None
        if assessment.verdict is Verdict.SUSPEND:
            case = ReviewCase(
                str(uuid4()),
                record.assessment_id,
                record.created_at,
                self._retain(payload),
            )
            record = replace(record, review_case_id=case.case_id)
        self._assessments.save(record, case)
        return record
