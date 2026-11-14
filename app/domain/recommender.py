import math
from collections import Counter
from dataclasses import dataclass
from typing import Literal, TypedDict

RecommenderVerdict = Literal["permit", "retain", "deny", "client_error"]


@dataclass(frozen=True)
class RecommenderPolicy:
    version: str
    max_weight: float
    max_per_institution: int
    min_protected_share: float
    max_relevance_floor: float
    mode: str
    baseline: Literal["retain", "permit_with_notice"]
    protected_attribute: str
    unknown_institution: str
    require_rejection_justification: bool


class AppliedRerank(TypedDict):
    lambda_: float
    max_per_institution: int | None
    min_protected_share: float
    relevance_floor: float
    fairness_mode: str


class Recommendation(TypedDict):
    author_id: str
    name: str | None
    institution: str
    protected: bool
    rank: int
    relevance_norm: float
    institution_novelty: float
    final_score: float
    selection_reason: str


@dataclass(frozen=True, repr=False)
class RecommendationList:
    top_k: int
    team_ids: frozenset[str]
    profiles: tuple[Recommendation, ...]
    rerank: AppliedRerank
    quota_picks: int
    cap_relaxations: int
    quota_shortfalls: int
    candidates_retrieved: int
    low_visibility_share: float
    pool_low_visibility_share: float


@dataclass(frozen=True)
class RecommenderAssessment:
    verdict: RecommenderVerdict
    reasons: tuple[str, ...] = ()


def quota_minimum(share: float, size: int) -> int:
    return math.floor(share * size + 1e-9)


def consistent(result: RecommendationList) -> bool:
    profiles = result.profiles
    size = len(profiles)
    ids = {p["author_id"] for p in profiles}
    if (
        [p["rank"] for p in profiles] != list(range(1, size + 1))
        or size > result.top_k
        or len(ids) != size
        or ids & result.team_ids
    ):
        return False
    quota = [p for p in profiles if p["selection_reason"] == "quota"]
    if (
        len(quota) != result.quota_picks
        or not all(p["protected"] for p in quota)
        or result.cap_relaxations
        < sum(p["selection_reason"] == "cap_relaxed" for p in profiles)
    ):
        return False
    q = sum(p["protected"] for p in profiles)
    n = result.candidates_retrieved
    pool = result.pool_low_visibility_share * n
    if (
        abs(result.low_visibility_share * size - q) > 1e-4
        or abs(pool - round(pool)) > 0.5e-6 * n + 1e-9
    ):
        return False
    applied = result.rerank
    if (
        applied["fairness_mode"] == "prefix"
        and result.quota_shortfalls == 0
        and q < quota_minimum(applied["min_protected_share"], size)
    ):
        return False
    weight = applied["lambda_"]
    return all(
        abs(
            weight * p["relevance_norm"]
            + (1 - weight) * p["institution_novelty"]
            - p["final_score"]
        )
        <= 2e-6
        for p in profiles
    )


def assess_recommendations(
    result: RecommendationList, policy: RecommenderPolicy
) -> RecommenderAssessment:
    if not consistent(result):
        return RecommenderAssessment("deny", ("UPSTREAM_INCONSISTENT",))
    applied = result.rerank
    cap = applied["max_per_institution"]
    mode = applied["fairness_mode"]
    retention: list[str] = []
    reasons: list[str] = []
    if mode == "none" or cap is None:
        if (
            mode == "none"
            and cap is None
            and applied["lambda_"] == 1.0
            and policy.baseline == "permit_with_notice"
        ):
            reasons.append("BASELINE_WITHOUT_FAIRNESS")
        else:
            retention.append("FAIRNESS_DISABLED")
            reasons.append("FAIRNESS_DISABLED")
    else:
        weakened = (
            applied["lambda_"] > policy.max_weight
            or cap > policy.max_per_institution
            or applied["min_protected_share"] < policy.min_protected_share
            or applied["relevance_floor"] > policy.max_relevance_floor
            or mode != policy.mode
        )
        if weakened:
            retention.append("FAIRNESS_WEAKENED")
            reasons.append("FAIRNESS_WEAKENED")
        counts = Counter(
            p["institution"]
            for p in result.profiles
            if p["institution"] != policy.unknown_institution
        )
        limit = min(cap, policy.max_per_institution)
        excess = sum(max(0, count - limit) for count in counts.values())
        if excess:
            if excess <= result.cap_relaxations and not weakened:
                reasons.append("INSTITUTION_CAP_RELAXED_REPORTED")
            else:
                retention.append("INSTITUTION_CONCENTRATION")
                reasons.append("INSTITUTION_CONCENTRATION")
        share = max(policy.min_protected_share, applied["min_protected_share"])
        if sum(p["protected"] for p in result.profiles) < quota_minimum(
            share, len(result.profiles)
        ):
            if result.quota_shortfalls > 0 and not weakened:
                reasons.append("LOW_VISIBILITY_SHORTFALL_REPORTED")
            else:
                retention.append("LOW_VISIBILITY_QUOTA_UNMET")
                reasons.append("LOW_VISIBILITY_QUOTA_UNMET")
    if any(
        p["name"] is None and p["institution"] == policy.unknown_institution
        for p in result.profiles
    ):
        retention.append("PROTECTED_ATTRIBUTE_UNKNOWN")
        reasons.append("PROTECTED_ATTRIBUTE_UNKNOWN")
    return RecommenderAssessment("retain" if retention else "permit", tuple(reasons))
