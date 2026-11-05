from dataclasses import dataclass, field
from datetime import datetime

from app.domain.qa import RecordedAssessment, Verdict


class ReviewCaseIntegrityError(ValueError):
    pass


@dataclass(frozen=True)
class RetainedQaPair:
    question: str = field(repr=False)
    answer: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.question.strip() or not self.answer.strip():
            raise ValueError("Retained question and answer must not be blank")


@dataclass(frozen=True, repr=False)
class RetainedQaContent:
    """Generated output withheld from users until a reviewer decides on it."""

    qa_pairs: tuple[RetainedQaPair, ...]
    source_article_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.qa_pairs:
            raise ValueError("Retained content needs at least one pair")
        if any(not article_id.strip() for article_id in self.source_article_ids):
            raise ValueError("Source article identifiers must not be blank")

    def __repr__(self) -> str:
        return (
            f"RetainedQaContent(pairs={len(self.qa_pairs)}, "
            f"sources={len(self.source_article_ids)})"
        )


@dataclass(frozen=True)
class ReviewCase:
    case_id: str
    assessment_id: str
    created_at: datetime
    content: RetainedQaContent


def require_review_case_link(
    record: RecordedAssessment, case: ReviewCase | None
) -> None:
    """A suspended assessment has exactly one linked case; a denial has none."""
    if (record.assessment.verdict is Verdict.SUSPEND) != (case is not None):
        raise ValueError("Only suspended assessments have exactly one review case")
    if case is not None and (
        case.assessment_id != record.assessment_id
        or case.case_id != record.review_case_id
    ):
        raise ValueError("Review case is not linked to this assessment")
