import re
from dataclasses import dataclass, replace
from unicodedata import decimal

_CITATION = re.compile(r"\[(\d+)\]")

CITATION_WITHOUT_BLOCK = "RAG_CITATION_WITHOUT_BLOCK"
CITATIONS_MISSING = "RAG_CITATIONS_MISSING"


def _digits(value: str) -> str:
    return value if value.isascii() else "".join(str(decimal(char)) for char in value)


class InvalidRagCorrectionError(ValueError):
    pass


@dataclass(frozen=True)
class RagPolicy:
    policy_id: str
    version: str
    allow_complete: bool = False

    def __post_init__(self) -> None:
        if not self.policy_id.strip() or not self.version.strip():
            raise ValueError("Policy identity and version must not be blank")
        if type(self.allow_complete) is not bool:
            raise ValueError("allow_complete must be a boolean")


@dataclass(frozen=True, repr=False)
class RagBlock:
    cite_id: str
    title: str
    text: str
    authors_mention: str | None = None
    authors_raw: str | None = None
    year: str | None = None
    doi_raw: str | None = None


@dataclass(frozen=True)
class RagTiming:
    search: int
    rerank: int
    generate: int
    total: int


@dataclass(frozen=True, repr=False)
class RetainedRagContent:
    query: str
    answer_text: str
    blocks: tuple[RagBlock, ...]
    timing_ms: RagTiming

    def citations(self) -> tuple[tuple[int, ...], tuple[str, ...]]:
        size = len(self.blocks)
        max_digits = len(str(size))
        resolved: list[int] = []
        unresolved: list[str] = []
        positions = dict.fromkeys(
            _digits(match[1]).lstrip("0") or "0"
            for match in _CITATION.finditer(self.answer_text)
        )
        for position in positions:
            # An out-of-range citation may exceed Python's conversion limit.
            number = int(position) if len(position) <= max_digits else 0
            if 1 <= number <= size:
                resolved.append(number)
            else:
                unresolved.append(position)
        return tuple(resolved), tuple(unresolved)

    def correct(self, answer_text: str) -> "RetainedRagContent":
        result = replace(self, answer_text=answer_text)
        resolved, unresolved = result.citations()
        if not resolved or unresolved:
            raise InvalidRagCorrectionError("Corrected citations must resolve")
        return result
