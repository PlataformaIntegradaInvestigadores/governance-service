from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from uuid import UUID


class InsufficientDeliveriesError(ValueError):
    pass


class QaSampleNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class QaSamplingPolicy:
    version: str
    percent: int
    minimum: int

    def __post_init__(self) -> None:
        if (
            not self.version.strip()
            or type(self.percent) is not int
            or not 1 <= self.percent <= 100
            or type(self.minimum) is not int
            or self.minimum < 1
        ):
            raise ValueError("Invalid sampling policy")

    def select(self, population: tuple[str, ...], seed: int) -> tuple[str, ...]:
        if len(population) < self.minimum:
            raise InsufficientDeliveriesError
        size = max(self.minimum, (len(population) * self.percent + 99) // 100)
        prefix = f"{seed}:".encode("ascii")
        ranked = sorted(
            population, key=lambda item: (sha256(prefix + item.encode()).digest(), item)
        )
        return tuple(ranked[:size])


@dataclass(frozen=True)
class QaSample:
    sample_id: UUID
    start: datetime
    end: datetime
    seed: int
    policy: QaSamplingPolicy
    algorithm: str
    population: tuple[str, ...]
    selected: tuple[str, ...]
