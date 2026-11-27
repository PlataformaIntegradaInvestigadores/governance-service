from collections.abc import Callable
from datetime import datetime

from app.domain.audit import (
    AuditChainBrokenError,
    AuditCheckpoint,
    ChainReport,
    verify_chain,
)
from app.domain.repositories import AuditCheckpointRepository, AuditEventRepository


class VerifyAuditChain:
    def __init__(
        self, events: AuditEventRepository, checkpoints: AuditCheckpointRepository
    ) -> None:
        self._events = events
        self._checkpoints = checkpoints

    def execute(self) -> ChainReport:
        return verify_chain(self._events.events(), self._checkpoints.load())


class RecordAuditCheckpoint:
    """Store the current head outside the database, only for a valid chain."""

    def __init__(
        self,
        events: AuditEventRepository,
        checkpoints: AuditCheckpointRepository,
        clock: Callable[[], datetime],
    ) -> None:
        self._events = events
        self._checkpoints = checkpoints
        self._clock = clock

    def execute(self) -> AuditCheckpoint | None:
        stored = self._checkpoints.load()
        report = verify_chain(self._events.events(), stored)
        if not report.valid:
            raise AuditChainBrokenError(report)
        if report.head_seq == 0 or (stored and stored[-1].seq == report.head_seq):
            return None
        checkpoint = AuditCheckpoint(report.head_seq, report.head_hash, self._clock())
        self._checkpoints.append(checkpoint)
        return checkpoint
