from app.domain.qa import (
    Assessment,
    GenerationObservation,
    GenerationStatus,
    QaPolicy,
    Verdict,
)


def assess_generation(
    observation: GenerationObservation, policy: QaPolicy
) -> Assessment:
    if observation.status == GenerationStatus.ERROR or observation.has_error:
        verdict, reasons = Verdict.DENY, ("UPSTREAM_GENERATION_FAILED",)
    elif observation.pair_count == 0:
        verdict, reasons = Verdict.DENY, ("NO_VALID_PAIRS",)
    elif observation.pair_count > policy.expected_pairs:
        verdict, reasons = Verdict.DENY, ("UNEXPECTED_PAIR_COUNT",)
    else:
        verdict = Verdict.SUSPEND
        reasons = ("HUMAN_REVIEW_REQUIRED",)
        if (
            observation.status == GenerationStatus.PARTIAL
            or observation.pair_count < policy.expected_pairs
        ):
            reasons += ("PARTIAL_GENERATION",)
        if not observation.sources_complete:
            reasons += ("SOURCE_EVIDENCE_INCOMPLETE",)
        if policy.allow_complete and reasons == ("HUMAN_REVIEW_REQUIRED",):
            verdict, reasons = Verdict.ALLOW, ("COMPLETE_GENERATION_ALLOWED",)

    return Assessment(verdict, policy.policy_id, policy.version, reasons)
