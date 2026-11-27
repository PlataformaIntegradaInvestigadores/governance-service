import sqlite3
from dataclasses import asdict
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response

from app.api.supervisors import current_supervisor
from app.domain.explanation import Explainer, ExplanationUnavailableError

router = APIRouter(prefix="/assessments", tags=["Decision explanations"])


@router.get("/{assessment_id}/explanation", dependencies=[Depends(current_supervisor)])
def explain_decision(
    assessment_id: UUID, request: Request, response: Response
) -> dict[str, object]:
    explainer: Explainer | None = request.app.state.decision_explainer
    if explainer is None:
        raise ExplanationUnavailableError("Explanation store is unavailable")
    try:
        result = explainer.execute(str(assessment_id))
    except (OSError, ValueError, sqlite3.Error):
        raise ExplanationUnavailableError("Explanation store is unavailable") from None
    record = result.assessment
    policy = record.policy
    response.headers["Cache-Control"] = "no-store"
    return {
        "assessment_id": record.assessment_id,
        "review_case_id": record.review_case_id,
        "created_at": record.created_at.isoformat(),
        "case_type": "qa",
        "verdict": record.assessment.verdict.value,
        "reason_codes": list(record.assessment.reason_codes),
        "policy": {
            "policy_id": policy.policy_id,
            "version": policy.version,
            "parameters": {
                "expected_pairs": policy.expected_pairs,
                "allow_complete": policy.allow_complete,
            },
        },
        "applied_parameters": None,
        "evidence": asdict(result.evidence) if result.evidence is not None else None,
    }
