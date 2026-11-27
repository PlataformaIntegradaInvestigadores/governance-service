"""Operations commands for the PostgreSQL audit store.

    python -m app.cli migrate            # migrator role
    python -m app.cli verify-audit       # audit (read-only) role
    python -m app.cli checkpoint-audit   # audit role; file outside the database
    python -m app.cli purge-expired      # runtime role; removes expired content

GOVERNANCE_DATABASE_URL selects the role; GOVERNANCE_AUDIT_CHECKPOINT_PATH the
checkpoint file. Exit codes: 0 valid, 1 broken chain, 2 configuration error.
"""

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime

from app.application.audit_chain import RecordAuditCheckpoint, VerifyAuditChain
from app.core.config import Settings
from app.domain.audit import AuditChainBrokenError, ChainReport
from app.infrastructure.file_checkpoints import FileCheckpointRepository
from app.infrastructure.postgres_migrations import apply_migrations
from app.infrastructure.postgres_store import (
    PostgresAuditEventRepository,
    PostgresDatabase,
)


def _write(document: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(document, sort_keys=True, default=str) + "\n")


def _report(report: ChainReport) -> int:
    _write({"valid": report.valid, **asdict(report)})
    return 0 if report.valid else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    parser.add_argument(
        "command",
        choices=("migrate", "verify-audit", "checkpoint-audit", "purge-expired"),
    )
    command = parser.parse_args(argv).command
    settings = Settings()
    if settings.database_url is None:
        _write({"error": "GOVERNANCE_DATABASE_URL is not set"})
        return 2
    conninfo = settings.database_url.get_secret_value()
    if command == "migrate":
        _write({"applied": list(apply_migrations(conninfo))})
        return 0
    if command == "purge-expired":
        purged = PostgresDatabase(conninfo).purge_expired()
        _write({"purged": len(purged)})
        return 0
    if settings.audit_checkpoint_path is None:
        _write({"error": "GOVERNANCE_AUDIT_CHECKPOINT_PATH is not set"})
        return 2
    events = PostgresAuditEventRepository(PostgresDatabase(conninfo))
    checkpoints = FileCheckpointRepository(settings.audit_checkpoint_path)
    if command == "verify-audit":
        return _report(VerifyAuditChain(events, checkpoints).execute())
    try:
        checkpoint = RecordAuditCheckpoint(
            events, checkpoints, lambda: datetime.now(UTC)
        ).execute()
    except AuditChainBrokenError as error:
        return _report(error.report)
    _write({"checkpoint": asdict(checkpoint) if checkpoint else None})
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point; main() is tested
    sys.exit(main())
