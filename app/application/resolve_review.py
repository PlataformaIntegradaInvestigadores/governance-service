from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.domain.qa import RecordedAssessment
from app.domain.repositories import (
    AssessmentRepository,
    ReviewCaseRepository,
    ReviewResolutionRepository,
)
from app.domain.resolution import (
    InvalidRecommendationResolutionError,
    JustificationRequiredError,
    ReviewCaseAlreadyResolvedError,
    ReviewCaseExpiredError,
    ReviewCaseNotFoundError,
    ReviewOutcome,
    ReviewResolution,
    justification_required,
)
from app.domain.review import (
    RecommendationSelection,
    RetainedQaContent,
    RetainedQaPair,
    RetainedRecommendation,
    ReviewCase,
)
from app.domain.supervision import Supervisor


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class CaseView:
    """What a supervisor sees: the case, why it was suspended and its state."""

    case: ReviewCase
    record: RecordedAssessment
    expires_at: datetime
    resolution: ReviewResolution | None
    expired: bool


@dataclass(frozen=True)
class ResolutionResult:
    resolution: ReviewResolution
    created: bool


class ReviewCases:
    """Supervisor-facing review workflow: view, resolve and expire cases.

    Callers must pass an authorized supervisor; this class does not authenticate.
    A case gets at most one resolution. Once the window closes, the case is
    denied as expired and can no longer be released.
    """

    def __init__(
        self,
        assessments: AssessmentRepository,
        cases: ReviewCaseRepository,
        resolutions: ReviewResolutionRepository,
        window: timedelta,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if window <= timedelta(0):
            raise ValueError("Review window must be positive")
        self._assessments = assessments
        self._cases = cases
        self._resolutions = resolutions
        self._window = window
        self._clock = clock

    def view(self, case_id: str) -> CaseView:
        case = self._case(case_id)
        record = self._record(case)
        resolution = self._resolutions.get(case_id)
        expires_at = case.created_at + self._window
        expired = (
            resolution.outcome is ReviewOutcome.EXPIRED
            if resolution is not None
            else self._clock() >= expires_at
        )
        return CaseView(case, record, expires_at, resolution, expired)

    def resolve(
        self,
        case_id: str,
        supervisor: Supervisor,
        outcome: ReviewOutcome,
        justification: str | None = None,
        modified_pairs: tuple[RetainedQaPair, ...] | None = None,
        retained_ranks: tuple[int, ...] | None = None,
    ) -> ResolutionResult:
        if outcome is ReviewOutcome.EXPIRED:
            raise ValueError("Expiry is recorded by the service, not a supervisor")
        case = self._case(case_id)
        content = self._modified_content(
            case, outcome, justification, modified_pairs, retained_ranks
        )
        requested = ReviewResolution(
            str(uuid4()),
            case_id,
            outcome,
            self._clock(),
            case.created_at + self._window,
            supervisor,
            (justification.strip() or None) if justification else None,
            content,
        )
        existing = self._resolutions.get(case_id)
        if existing is not None:
            return self._replay(existing, requested)
        if requested.resolved_at >= requested.expires_at:
            self._expire(case)
            raise ReviewCaseExpiredError("Review window closed")
        reasons = self._record(case).assessment.reason_codes
        if justification_required(outcome, reasons) and not requested.justification:
            raise JustificationRequiredError("This outcome needs a justification")
        try:
            self._resolutions.save(requested)
        except ReviewCaseAlreadyResolvedError:
            return self._replay(self._resolutions.get(case_id), requested)
        return ResolutionResult(requested, created=True)

    def _modified_content(
        self,
        case: ReviewCase,
        outcome: ReviewOutcome,
        justification: str | None,
        pairs: tuple[RetainedQaPair, ...] | None,
        ranks: tuple[int, ...] | None,
    ) -> RetainedQaContent | RecommendationSelection | None:
        if isinstance(case.content, RetainedRecommendation):
            original = case.content
            expected = (
                original.allowed_ranks if outcome is ReviewOutcome.MODIFY else None
            )
            if (
                pairs is not None
                or outcome.value != original.permitted_outcome
                or ranks != expected
            ):
                raise InvalidRecommendationResolutionError
            if (
                outcome is not ReviewOutcome.REJECT
                or original.require_rejection_justification
            ) and not (justification or "").strip():
                raise JustificationRequiredError("This outcome needs a justification")
            return (
                RecommendationSelection(original.allowed_ranks)
                if outcome is ReviewOutcome.MODIFY
                else None
            )
        if ranks is not None:
            raise InvalidRecommendationResolutionError
        if pairs is None:
            return None
        return RetainedQaContent(pairs, case.content.source_article_ids)

    def expire_overdue(self) -> int:
        """Record an expiry for every pending case whose window has closed."""
        expired = 0
        cutoff = self._clock() - self._window
        for case_id in self._resolutions.pending_case_ids(cutoff):
            if self._expire(self._case(case_id)):
                expired += 1
        return expired

    def _expire(self, case: ReviewCase) -> bool:
        resolution = ReviewResolution(
            str(uuid4()),
            case.case_id,
            ReviewOutcome.EXPIRED,
            self._clock(),
            case.created_at + self._window,
            None,
        )
        try:
            self._resolutions.save(resolution)
        except ReviewCaseAlreadyResolvedError:
            return False
        return True

    def _replay(
        self, existing: ReviewResolution | None, requested: ReviewResolution
    ) -> ResolutionResult:
        if existing is None:
            raise ReviewCaseAlreadyResolvedError("Resolution changed concurrently")
        if existing.outcome is ReviewOutcome.EXPIRED:
            raise ReviewCaseExpiredError("Review window closed")
        if not existing.matches(requested):
            raise ReviewCaseAlreadyResolvedError("Case already resolved")
        return ResolutionResult(existing, created=False)

    def _case(self, case_id: str) -> ReviewCase:
        case = self._cases.get(case_id)
        if case is None:
            raise ReviewCaseNotFoundError("Unknown review case")
        return case

    def _record(self, case: ReviewCase) -> RecordedAssessment:
        record = self._assessments.get(case.assessment_id)
        if record is None:
            raise ReviewCaseNotFoundError("Review case has no assessment")
        return record
