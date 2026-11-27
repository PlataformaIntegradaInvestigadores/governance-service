import json
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ConfigDict, ValidationError

from app.domain.qa import InvalidPolicyError, QaPolicy
from app.domain.rag import RagPolicy


class _VersionedPolicyDocument(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    policy_id: str
    version: str
    allow_complete: bool = False


class _PolicyDocument(_VersionedPolicyDocument):
    expected_pairs: int


_Policy = TypeVar("_Policy", QaPolicy, RagPolicy)


def _load_policy(
    path: Path,
    document_type: type[_VersionedPolicyDocument],
    policy_type: type[_Policy],
) -> _Policy:
    text = path.read_text(encoding="utf-8")
    try:
        payload = json.loads(text, object_pairs_hook=_unique_object)
        document = document_type.model_validate(payload)
        return policy_type(**document.model_dump())
    except (ValidationError, ValueError, RecursionError) as error:
        raise InvalidPolicyError(f"Invalid policy file: {path.name}") from error


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
        return _load_policy(self._path, _PolicyDocument, QaPolicy)


class JsonRagPolicyRepository:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> RagPolicy:
        return _load_policy(self._path, _VersionedPolicyDocument, RagPolicy)
