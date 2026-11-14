import hmac
import json
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Body, Depends, Request, Response
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.domain.qa import Assessment, RecordedAssessment, Verdict
from app.domain.recommender import RecommendationList, quota_minimum
from app.domain.resolution import RELEASING_OUTCOMES, ReviewCaseNotFoundError
from app.domain.review import (
    RecommendationSelection,
    RetainedRecommendation,
    ReviewCase,
)
from app.infrastructure.recommender_response import (
    evaluate_recommender_response,
    load_recommender_policy,
    retain_recommendation,
)

router = APIRouter(prefix="/governed/recommender", tags=["Governed recommendations"])
_bearer = HTTPBearer(auto_error=False)
_NO_STORE = {"Cache-Control": "no-store"}


def _denied(request: Request) -> JSONResponse:
    return JSONResponse(
        {"code": "RECOMMENDATION_DENIED", "trace_id": request.state.trace_id},
        status_code=403,
        headers=_NO_STORE,
    )


def _ticket(key: bytes, review_id: UUID) -> str:
    return hmac.digest(key, b"recommender-release:" + review_id.bytes, "sha256").hex()


def _ids(values: object, key: bytes) -> list[str]:
    if not isinstance(values, (list, frozenset)):
        return []
    return sorted(
        {
            hmac.digest(key, b"recommender-author:" + value.encode(), "sha256").hex()
            for value in values
            if isinstance(value, str)
            and not any(0xD800 <= ord(c) <= 0xDFFF for c in value)
        }
    )


def _unknown_ids(body: bytes) -> object:
    try:
        detail = json.loads(body)["detail"]
        return detail.get("unknown_author_ids") if isinstance(detail, dict) else None
    except (ValueError, TypeError, KeyError, RecursionError):
        return None


def _audit(
    status: int,
    body: bytes,
    key: bytes,
    request_ids: object,
    result: RecommendationList | None,
    unknown_institution: str,
) -> dict[str, object]:
    evidence: dict[str, object] = {
        "status": status,
        "team_hmac": _ids(request_ids, key),
    }
    if status == 404:
        evidence["unknown_author_hmac"] = _ids(_unknown_ids(body), key)
    if result is not None:
        profiles = result.profiles
        counts = Counter(
            p["institution"]
            for p in profiles
            if p["institution"] != unknown_institution
        )
        evidence.update(
            team_hmac=_ids(result.team_ids, key),
            recommended_hmac=_ids([p["author_id"] for p in profiles], key),
            max_per_institution_observed=max(counts.values(), default=0),
            rerank=result.rerank,
            pool_size=result.candidates_retrieved,
            pool_protected=round(
                result.pool_low_visibility_share * result.candidates_retrieved
            ),
            list_size=len(profiles),
            list_protected=sum(p["protected"] for p in profiles),
            applied_quota_minimum=quota_minimum(
                result.rerank["min_protected_share"], len(profiles)
            ),
            quota_shortfalls=result.quota_shortfalls,
            cap_relaxations=result.cap_relaxations,
        )
    return evidence


@router.post("/recommend")
def recommend(
    request: Request, payload: Annotated[dict[str, object], Body()]
) -> Response:
    state = request.app.state
    if state.recommender_upstream is None:
        return _denied(request)
    try:
        policy = load_recommender_policy(state.settings.recommender_policy_path)
        status, body, media_type = state.recommender_upstream.recommend(payload)
        assessment, result = evaluate_recommender_response(status, body, policy)
        now = datetime.now(UTC)
        assessment_id = str(uuid4())
        case = None
        if assessment.verdict == "retain" and result is not None:
            case = ReviewCase(
                str(uuid4()),
                assessment_id,
                now,
                retain_recommendation(body, result, policy, assessment.reasons),
            )
        verdict = {
            "permit": Verdict.ALLOW,
            "retain": Verdict.SUSPEND,
            "deny": Verdict.DENY,
            "client_error": Verdict.DENY,
        }[assessment.verdict]
        reasons = assessment.reasons
        if assessment.verdict == "client_error":
            reasons = ("UPSTREAM_CLIENT_ERROR",)
        record = RecordedAssessment(
            assessment_id,
            now,
            policy,
            Assessment(verdict, policy.policy_id, policy.version, reasons),
            case.case_id if case is not None else None,
        )
        key = state.recommender_key
        state.recommender_assessments.save(
            record,
            case,
            _audit(
                status,
                body,
                key,
                payload.get("author_ids"),
                result,
                policy.unknown_institution,
            ),
        )
    except (OSError, RecursionError, ValueError, sqlite3.Error):
        return _denied(request)
    if assessment.verdict == "client_error":
        return Response(
            body, status_code=status, media_type=media_type, headers=_NO_STORE
        )
    if assessment.verdict == "permit":
        headers = dict(_NO_STORE)
        if assessment.reasons:
            headers["X-Governance-Notices"] = ",".join(assessment.reasons)
        return Response(body, media_type=media_type, headers=headers)
    if case is None:
        return _denied(request)
    return JSONResponse(
        {"review_id": case.case_id, "review_token": _ticket(key, UUID(case.case_id))},
        status_code=202,
        headers=_NO_STORE,
    )


@router.get("/reviews/{review_id}")
def review_status(
    request: Request,
    review_id: UUID,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> JSONResponse:
    state = request.app.state
    if (
        state.recommender_upstream is None
        or credentials is None
        or not hmac.compare_digest(
            credentials.credentials.encode(),
            _ticket(state.recommender_key, review_id).encode(),
        )
    ):
        return _denied(request)
    try:
        view = state.review_cases.view(str(review_id))
        content = view.case.content
        if view.expired or not isinstance(content, RetainedRecommendation):
            return _denied(request)
        resolution = view.resolution
        if resolution is None:
            return JSONResponse(
                {"review_id": str(review_id)}, status_code=202, headers=_NO_STORE
            )
        if resolution.outcome not in RELEASING_OUTCOMES:
            return _denied(request)
        document = json.loads(content.response_json)
        if isinstance(resolution.modified_content, RecommendationSelection):
            ranks = set(resolution.modified_content.ranks)
            original = document["recommendations"]
            document["recommendations"] = [p for p in original if p["rank"] in ranks]
            document["governance"] = {
                "modified": True,
                "jaccard_divergence": round(1 - len(ranks) / len(original), 6),
                "metrics_describe_original": True,
            }
        return JSONResponse(document, headers=_NO_STORE)
    except (ReviewCaseNotFoundError, ValueError, sqlite3.Error):
        return _denied(request)
