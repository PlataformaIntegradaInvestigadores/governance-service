import json
from datetime import datetime
from pathlib import Path

from app.domain.audit import AuditCheckpoint


class FileCheckpointRepository:
    """Append-only JSON lines file, meant for storage the service cannot write."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> tuple[AuditCheckpoint, ...]:
        if not self._path.exists():
            return ()
        checkpoints = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            document = json.loads(line)
            checkpoints.append(
                AuditCheckpoint(
                    int(document["seq"]),
                    str(document["hash"]),
                    datetime.fromisoformat(document["recorded_at"]),
                )
            )
        if [c.seq for c in checkpoints] != sorted({c.seq for c in checkpoints}):
            raise ValueError("Checkpoint file is not in strictly increasing order")
        return tuple(checkpoints)

    def append(self, checkpoint: AuditCheckpoint) -> None:
        line = json.dumps(
            {
                "seq": checkpoint.seq,
                "hash": checkpoint.hash,
                "recorded_at": checkpoint.recorded_at.isoformat(),
            },
            sort_keys=True,
        )
        with self._path.open("a", encoding="utf-8", newline="\n") as file:
            file.write(line + "\n")
