import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from app.application.audit_chain import RecordAuditCheckpoint, VerifyAuditChain
from app.domain.audit import (
    GENESIS_HASH,
    AuditChainBrokenError,
    AuditCheckpoint,
    AuditEvent,
    canonical_json,
    chain_hash,
    verify_chain,
)
from app.infrastructure.file_checkpoints import FileCheckpointRepository

_NOW = datetime(2026, 11, 27, 12, tzinfo=UTC)


def _chain(length):
    events, prev = [], GENESIS_HASH
    for seq in range(1, length + 1):
        body = canonical_json({"seq": seq, "type": "synthetic", "data": {"n": seq}})
        digest = chain_hash(prev, body)
        events.append(AuditEvent(seq, body, prev, digest))
        prev = digest
    return events


class Events:
    def __init__(self, events):
        self.items = events

    def events(self):
        return iter(self.items)


class Checkpoints:
    def __init__(self, stored=()):
        self.stored = list(stored)

    def load(self):
        return tuple(self.stored)

    def append(self, checkpoint):
        self.stored.append(checkpoint)


def test_canonical_json_sorts_keys_and_keeps_unicode():
    assert canonical_json({"b": 1, "a": "ñ"}) == '{"a":"ñ","b":1}'


def test_intact_chain_reports_its_head():
    events = _chain(3)

    report = verify_chain(events)

    assert report.valid
    assert (report.events, report.head_seq, report.head_hash) == (
        3,
        3,
        events[-1].hash,
    )


def test_empty_chain_is_valid_with_genesis_head():
    report = verify_chain([])

    assert report.valid
    assert (report.head_seq, report.head_hash) == (0, GENESIS_HASH)


def test_edited_body_is_reported_at_its_sequence():
    events = _chain(3)
    events[1] = replace(events[1], body=events[1].body.replace('"n":2', '"n":9'))

    report = verify_chain(events)

    assert (report.valid, report.failed_seq, report.events) == (False, 2, 1)
    assert report.failure == "event hash does not match its content"


def test_recomputed_hash_breaks_the_next_link():
    events = _chain(3)
    body = events[1].body.replace('"n":2', '"n":9')
    events[1] = replace(
        events[1], body=body, hash=chain_hash(events[1].prev_hash, body)
    )

    report = verify_chain(events)

    assert (report.failed_seq, report.failure) == (3, "previous hash does not match")


def test_deleted_event_is_a_sequence_gap():
    events = _chain(3)
    del events[1]

    assert verify_chain(events).failure == "sequence gap or reordering"


def test_renumbered_row_does_not_match_its_body():
    first = _chain(1)[0]
    body = canonical_json({"seq": 2})
    tampered = AuditEvent(1, body, GENESIS_HASH, chain_hash(GENESIS_HASH, body))

    assert verify_chain([tampered]).failure == "event body does not match its sequence"
    assert verify_chain([replace(first, body="not json")]).failed_seq == 1
    assert verify_chain([replace(first, body="[1]")]).failed_seq == 1


def test_truncated_tail_is_only_detected_with_a_checkpoint():
    events = _chain(4)
    checkpoint = AuditCheckpoint(4, events[3].hash, _NOW)

    assert verify_chain(events[:2]).valid
    report = verify_chain(events[:2], [checkpoint])

    assert (report.valid, report.failed_seq) == (False, 4)
    assert report.failure == "truncated before a checkpoint"


def test_consistent_rewrite_is_detected_by_the_checkpoint():
    original = _chain(2)
    rewritten, prev = [], GENESIS_HASH
    for seq in (1, 2):
        body = canonical_json({"seq": seq, "type": "forged"})
        digest = chain_hash(prev, body)
        rewritten.append(AuditEvent(seq, body, prev, digest))
        prev = digest

    assert verify_chain(rewritten).valid
    report = verify_chain(rewritten, [AuditCheckpoint(2, original[1].hash, _NOW)])

    assert (report.failed_seq, report.failure) == (
        2,
        "differs from an external checkpoint",
    )


@pytest.mark.parametrize(
    ("seq", "digest", "recorded_at"),
    [
        (0, "a" * 64, _NOW),
        (1, "short", _NOW),
        (1, "a" * 64, datetime(2026, 11, 27)),  # noqa: DTZ001 - naive on purpose
    ],
)
def test_checkpoint_rejects_invalid_values(seq, digest, recorded_at):
    with pytest.raises(ValueError, match="Checkpoint"):
        AuditCheckpoint(seq, digest, recorded_at)


def test_verify_use_case_reads_events_and_checkpoints():
    events = _chain(2)
    report = VerifyAuditChain(
        Events(events), Checkpoints([AuditCheckpoint(1, events[0].hash, _NOW)])
    ).execute()

    assert report.valid


def test_checkpoint_use_case_records_new_heads_only():
    events = _chain(2)
    checkpoints = Checkpoints()
    use_case = RecordAuditCheckpoint(Events(events), checkpoints, lambda: _NOW)

    first = use_case.execute()
    again = use_case.execute()

    assert first == AuditCheckpoint(2, events[1].hash, _NOW)
    assert again is None
    assert checkpoints.stored == [first]


def test_checkpoint_use_case_skips_an_empty_chain():
    checkpoints = Checkpoints()

    assert (
        RecordAuditCheckpoint(Events([]), checkpoints, lambda: _NOW).execute() is None
    )
    assert checkpoints.stored == []


def test_checkpoint_use_case_refuses_a_broken_chain():
    events = _chain(2)
    events[0] = replace(events[0], body=canonical_json({"seq": 1, "forged": True}))
    checkpoints = Checkpoints()

    with pytest.raises(AuditChainBrokenError, match="broken at 1") as raised:
        RecordAuditCheckpoint(Events(events), checkpoints, lambda: _NOW).execute()

    assert raised.value.report.failed_seq == 1
    assert checkpoints.stored == []


def test_checkpoint_file_round_trips_and_survives_reopening(tmp_path):
    path = tmp_path / "checkpoints.jsonl"
    first = AuditCheckpoint(1, "a" * 64, _NOW)
    second = AuditCheckpoint(5, "b" * 64, _NOW)

    assert FileCheckpointRepository(path).load() == ()
    FileCheckpointRepository(path).append(first)
    FileCheckpointRepository(path).append(second)

    assert FileCheckpointRepository(path).load() == (first, second)
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[0])["seq"] == 1


@pytest.mark.parametrize("order", [(2, 1), (1, 1)])
def test_checkpoint_file_rejects_disordered_entries(tmp_path, order):
    path = tmp_path / "checkpoints.jsonl"
    repository = FileCheckpointRepository(path)
    for seq in order:
        repository.append(AuditCheckpoint(seq, "a" * 64, _NOW))

    with pytest.raises(ValueError, match="strictly increasing"):
        repository.load()


def test_checkpoint_file_rejects_corrupt_lines(tmp_path):
    path = tmp_path / "checkpoints.jsonl"
    path.write_text("{not json\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Expecting property name"):
        FileCheckpointRepository(path).load()
