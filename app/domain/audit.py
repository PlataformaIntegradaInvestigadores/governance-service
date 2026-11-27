import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

GENESIS_HASH = "0" * 64


def canonical_json(document: object) -> str:
    """Deterministic JSON: sorted keys, no spaces, UTF-8 text kept as is."""
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def chain_hash(prev_hash: str, body: str) -> str:
    return hashlib.sha256((prev_hash + body).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AuditEvent:
    """One link of the chain: hash = sha256(prev_hash || canonical body)."""

    seq: int
    body: str
    prev_hash: str
    hash: str


@dataclass(frozen=True)
class AuditCheckpoint:
    """Chain head recorded outside the database, used as trusted reference."""

    seq: int
    hash: str
    recorded_at: datetime

    def __post_init__(self) -> None:
        if self.seq < 1 or len(self.hash) != 64:
            raise ValueError("Checkpoints need a positive sequence and a SHA-256")
        if self.recorded_at.utcoffset() is None:
            raise ValueError("Checkpoint time must be timezone-aware")


@dataclass(frozen=True)
class ChainReport:
    events: int
    head_seq: int
    head_hash: str
    failed_seq: int | None = None
    failure: str | None = None

    @property
    def valid(self) -> bool:
        return self.failure is None


class AuditChainBrokenError(Exception):
    def __init__(self, report: ChainReport) -> None:
        super().__init__(f"Audit chain broken at {report.failed_seq}")
        self.report = report


def _body_seq(body: str) -> object:
    try:
        document = json.loads(body)
    except ValueError:
        return None
    return document.get("seq") if isinstance(document, dict) else None


def _event_failure(event: AuditEvent, expected_seq: int, prev_hash: str) -> str | None:
    if event.seq != expected_seq:
        return "sequence gap or reordering"
    if event.prev_hash != prev_hash:
        return "previous hash does not match"
    if _body_seq(event.body) != event.seq:
        return "event body does not match its sequence"
    if chain_hash(event.prev_hash, event.body) != event.hash:
        return "event hash does not match its content"
    return None


def verify_chain(
    events: Iterable[AuditEvent], checkpoints: Sequence[AuditCheckpoint] = ()
) -> ChainReport:
    """Recompute the chain in order and compare it with external checkpoints.

    Reports the first break. A chain alone cannot reveal a complete rewrite or a
    truncation of its tail; the checkpoints can, up to the last one recorded.
    """
    expected = {checkpoint.seq: checkpoint.hash for checkpoint in checkpoints}
    head_seq, head_hash, count = 0, GENESIS_HASH, 0
    for event in events:
        failure = _event_failure(event, head_seq + 1, head_hash)
        if failure is None and expected.get(event.seq, event.hash) != event.hash:
            failure = "differs from an external checkpoint"
        if failure is not None:
            return ChainReport(count, head_seq, head_hash, event.seq, failure)
        head_seq, head_hash, count = event.seq, event.hash, count + 1
    missing = [seq for seq in expected if seq > head_seq]
    if missing:
        return ChainReport(
            count, head_seq, head_hash, min(missing), "truncated before a checkpoint"
        )
    return ChainReport(count, head_seq, head_hash)
