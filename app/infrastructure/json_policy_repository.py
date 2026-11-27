from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from app.domain.qa import InvalidPolicyError, QaPolicy
from app.infrastructure.policy_files import read_policy_data


class _PolicyDocument(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    policy_id: str
    version: str
    expected_pairs: int
    allow_complete: bool = False


class PolicyFileRepository:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> QaPolicy:
        payload = read_policy_data(self._path)
        try:
            document = _PolicyDocument.model_validate(payload)
            return QaPolicy(**document.model_dump())
        except (ValidationError, ValueError) as error:
            raise InvalidPolicyError(
                f"Invalid policy file: {self._path.name}"
            ) from error


JsonPolicyRepository = PolicyFileRepository
