from datetime import datetime
from uuid import uuid4

from app.domain.qa_sampling import QaSample, QaSamplingPolicy
from app.domain.repositories import QaSampleRepository


class SampleQaDeliveries:
    def __init__(self, samples: QaSampleRepository, policy: QaSamplingPolicy) -> None:
        self._samples = samples
        self._policy = policy

    def execute(self, start: datetime, end: datetime, seed: int) -> QaSample:
        population = self._samples.eligible_ids(start, end)
        sample = QaSample(
            uuid4(),
            start,
            end,
            seed,
            self._policy,
            "sha256-seed-colon-id-v1",
            population,
            self._policy.select(population, seed),
        )
        self._samples.save(sample)
        return sample
