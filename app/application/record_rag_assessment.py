from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from app.domain.qa import Assessment, RecordedAssessment, Verdict
from app.domain.rag import CITATION_WITHOUT_BLOCK, CITATIONS_MISSING, RetainedRagContent
from app.domain.repositories import AssessmentRepository, RagPolicyRepository
from app.domain.review import ReviewCase


def _decision(
    content: RetainedRagContent | None, allow_complete: bool
) -> tuple[Verdict, str]:
    if content is None:
        return Verdict.DENY, "UPSTREAM_CONTRACT_INVALID"
    cited, unresolved = content.citations()
    if unresolved:
        return Verdict.SUSPEND, CITATION_WITHOUT_BLOCK
    if not content.blocks:
        return Verdict.DENY, "RAG_NO_SOURCES"
    if not cited:
        return Verdict.SUSPEND, CITATIONS_MISSING
    if not allow_complete:
        return Verdict.SUSPEND, "RAG_REVIEW_REQUIRED"
    return Verdict.ALLOW, "RAG_STRUCTURALLY_COMPLETE"


class RecordRagAssessment:
    def __init__(
        self, policies: RagPolicyRepository, assessments: AssessmentRepository
    ) -> None:
        self._policies = policies
        self._assessments = assessments

    def execute(self, content: RetainedRagContent | None) -> RecordedAssessment:
        policy = self._policies.load()
        verdict, reason = _decision(content, policy.allow_complete)
        assessment = Assessment(verdict, policy.policy_id, policy.version, (reason,))
        record = RecordedAssessment(str(uuid4()), datetime.now(UTC), policy, assessment)
        case = None
        if content is not None and verdict is Verdict.SUSPEND:
            case = ReviewCase(
                str(uuid4()), record.assessment_id, record.created_at, content
            )
            record = replace(record, review_case_id=case.case_id)
        self._assessments.save(record, case)
        return record
