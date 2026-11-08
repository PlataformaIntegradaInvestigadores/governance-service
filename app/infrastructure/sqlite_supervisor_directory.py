import sqlite3
from contextlib import closing
from pathlib import Path

from app.domain.supervision import Supervisor, SupervisorChange

_SCHEMA = """
CREATE TABLE IF NOT EXISTS supervisor_changes (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    identity_id TEXT NOT NULL CHECK (length(trim(identity_id)) > 0),
    display_name TEXT NOT NULL CHECK (length(trim(display_name)) > 0),
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS supervisor_changes_by_identity
ON supervisor_changes (identity_id, sequence);
CREATE TRIGGER IF NOT EXISTS supervisor_changes_no_update
BEFORE UPDATE ON supervisor_changes
BEGIN SELECT RAISE(ABORT, 'Supervisor changes are append-only'); END;
CREATE TRIGGER IF NOT EXISTS supervisor_changes_no_delete
BEFORE DELETE ON supervisor_changes
BEGIN SELECT RAISE(ABORT, 'Supervisor changes are append-only'); END;
"""


class SqliteSupervisorDirectory:
    """Local append-only supervisor list; the latest change per identity wins.

    shortcut: changes record no operator and no management route exists; upgrade
    with an authenticated administrator flow and the PostgreSQL store.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        with closing(sqlite3.connect(path)) as connection:
            connection.executescript(_SCHEMA)

    def record(self, change: SupervisorChange) -> None:
        with closing(sqlite3.connect(self._path)) as connection, connection:
            connection.execute(
                "INSERT INTO supervisor_changes "
                "(identity_id, display_name, enabled, recorded_at) VALUES (?, ?, ?, ?)",
                (
                    change.supervisor.identity_id,
                    change.supervisor.display_name,
                    int(change.enabled),
                    change.recorded_at.isoformat(),
                ),
            )

    def current(self, identity_id: str) -> Supervisor | None:
        with closing(sqlite3.connect(self._path)) as connection:
            row = connection.execute(
                "SELECT display_name, enabled FROM supervisor_changes "
                "WHERE identity_id = ? ORDER BY sequence DESC LIMIT 1",
                (identity_id,),
            ).fetchone()
        if row is None or row[1] != 1:
            return None
        return Supervisor(identity_id, row[0])
