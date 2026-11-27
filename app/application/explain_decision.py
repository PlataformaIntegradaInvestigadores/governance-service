from app.domain.explanation import (
    AssessmentNotFoundError,
    DecisionExplanation,
    ExplanationUnavailableError,
    QaExplanationEvidence,
)
from app.domain.repositories import AssessmentRepository, ReviewCaseRepository


class ExplainDecision:
    def __init__(
        self, assessments: AssessmentRepository, cases: ReviewCaseRepository
    ) -> None:
        self._assessments = assessments
        self._cases = cases

    def execute(self, assessment_id: str) -> DecisionExplanation:
        record = self._assessments.get(assessment_id)
        if record is None:
            raise AssessmentNotFoundError("Unknown assessment")
        if record.assessment_id != assessment_id:
            raise ExplanationUnavailableError("Assessment identity mismatch")
        if record.review_case_id is None:
            return DecisionExplanation(record, None)
        case = self._cases.get(record.review_case_id)
        if case is None:
            # Retention can remove the protected evidence while keeping the decision.
            return DecisionExplanation(record, None)
        if case.assessment_id != assessment_id or case.case_id != record.review_case_id:
            raise ExplanationUnavailableError("Review case identity mismatch")
        evidence = QaExplanationEvidence(
            len(case.content.qa_pairs), case.content.source_article_ids
        )
        return DecisionExplanation(record, evidence)
