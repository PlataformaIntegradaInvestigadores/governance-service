from dataclasses import dataclass
from typing import Protocol

from app.domain.qa import RecordedAssessment


class AssessmentNotFoundError(LookupError):
    pass


class ExplanationUnavailableError(Exception):
    pass


@dataclass(frozen=True, repr=False)
class QaExplanationEvidence:
    pair_count: int
    source_article_ids: tuple[str, ...]


@dataclass(frozen=True, repr=False)
class DecisionExplanation:
    assessment: RecordedAssessment
    evidence: QaExplanationEvidence | None


class Explainer(Protocol):
    def execute(self, assessment_id: str) -> DecisionExplanation: ...
