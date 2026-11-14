import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.recommender import (
    Recommendation,
    RecommendationList,
    RecommenderAssessment,
    RecommenderPolicy,
    assess_recommendations,
)


class _Schema(BaseModel):
    model_config = ConfigDict(strict=True, allow_inf_nan=False)


class _Policy(_Schema):
    version: str = Field(min_length=1)
    weight: float = Field(alias="lambda", ge=0, le=1)
    max_per_institution: int = Field(gt=0)
    min_low_visibility_share: float = Field(ge=0, le=1)
    relevance_floor: float = Field(ge=0, le=1)
    mode: Literal["prefix", "fair"]
    baseline: Literal["retain", "permit_with_notice"]
    protected_attribute: str = Field(min_length=1)
    unknown_institution: str = Field(min_length=1)
    require_rejection_justification: bool


def load_recommender_policy(path: Path) -> RecommenderPolicy:
    policy = _Policy.model_validate_json(path.read_bytes())
    return RecommenderPolicy(
        policy.version,
        policy.weight,
        policy.max_per_institution,
        policy.min_low_visibility_share,
        policy.relevance_floor,
        policy.mode,
        policy.baseline,
        policy.protected_attribute,
        policy.unknown_institution,
        policy.require_rejection_justification,
    )


class _Author(_Schema):
    author_id: str = Field(min_length=1)


class _Profile(_Author):
    name: str | None
    institution: str = Field(min_length=1)
    low_visibility: bool
    rank: int = Field(gt=0)
    baseline_rank: int = Field(gt=0)
    relevance_score: float
    relevance_norm: float
    institution_novelty: float
    final_score: float
    selection_reason: Literal["score", "quota", "cap_relaxed"]
    model_config = ConfigDict(extra="allow")


class _Diagnostics(_Schema):
    quota_picks: int = Field(ge=0)
    cap_relaxations: int = Field(ge=0)
    quota_shortfalls: int = Field(ge=0)
    candidates_retrieved: int = Field(ge=0)


class _Fairness(_Schema):
    low_visibility_share: float = Field(ge=0, le=1)
    pool_low_visibility_share: float = Field(ge=0, le=1)


class _Rerank(_Schema):
    lambda_: float = Field(ge=0, le=1)
    max_per_institution: int | None = Field(gt=0)
    min_protected_share: float = Field(ge=0, le=1)
    relevance_floor: float = Field(ge=0, le=1)
    fairness_mode: Literal["none", "prefix", "fair"]


class _Parameters(_Schema):
    top_k: int = Field(gt=0)
    rerank: _Rerank


class _Response(_Schema):
    model: object
    team: list[_Author]
    recommendations: list[_Profile]
    fairness_metrics: _Fairness
    diagnostics: _Diagnostics
    parameters: _Parameters


def assess_recommender_response(
    status: int, body: bytes, policy: RecommenderPolicy
) -> RecommenderAssessment:
    if status in (404, 422):
        return RecommenderAssessment("client_error")
    if status != 200:
        return RecommenderAssessment("deny", ("UPSTREAM_ERROR",))
    try:
        response = _Response.model_validate(json.loads(body))
        profiles: list[Recommendation] = []
        for profile in response.recommendations:
            protected = profile.model_dump().get(policy.protected_attribute)
            if not isinstance(protected, bool):
                return RecommenderAssessment("deny", ("UPSTREAM_CONTRACT_INVALID",))
            profiles.append(
                Recommendation(
                    author_id=profile.author_id,
                    name=profile.name,
                    institution=profile.institution,
                    protected=protected,
                    rank=profile.rank,
                    relevance_norm=profile.relevance_norm,
                    institution_novelty=profile.institution_novelty,
                    final_score=profile.final_score,
                    selection_reason=profile.selection_reason,
                )
            )
    except (UnicodeError, ValueError):
        return RecommenderAssessment("deny", ("UPSTREAM_CONTRACT_INVALID",))
    rerank = response.parameters.rerank
    diag = response.diagnostics
    metrics = response.fairness_metrics
    result = RecommendationList(
        response.parameters.top_k,
        frozenset(author.author_id for author in response.team),
        tuple(profiles),
        {
            "lambda_": rerank.lambda_,
            "max_per_institution": rerank.max_per_institution,
            "min_protected_share": rerank.min_protected_share,
            "relevance_floor": rerank.relevance_floor,
            "fairness_mode": rerank.fairness_mode,
        },
        diag.quota_picks,
        diag.cap_relaxations,
        diag.quota_shortfalls,
        diag.candidates_retrieved,
        metrics.low_visibility_share,
        metrics.pool_low_visibility_share,
    )
    return assess_recommendations(result, policy)
