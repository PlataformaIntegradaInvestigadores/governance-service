import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from app.domain.qa import InvalidPolicyError, QaPolicy


class _PolicyDocument(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    policy_id: str
    version: str
    expected_pairs: int
    allow_complete: bool = False


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate policy field: {key}")
        result[key] = value
    return result


class JsonPolicyRepository:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> QaPolicy:
        text = self._path.read_text(encoding="utf-8")
        try:
            payload = json.loads(text, object_pairs_hook=_unique_object)
            document = _PolicyDocument.model_validate(payload)
            return QaPolicy(**document.model_dump())
        except (ValidationError, ValueError) as error:
            raise InvalidPolicyError(
                f"Invalid policy file: {self._path.name}"
            ) from error
