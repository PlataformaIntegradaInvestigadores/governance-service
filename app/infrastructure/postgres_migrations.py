import hashlib
from pathlib import Path

import psycopg

MIGRATIONS = Path(__file__).resolve().parent / "migrations"
_LOCK = 7_301_202_601


class MigrationError(Exception):
    """Applied migrations and the code disagree; nothing was changed."""


def migration_files(directory: Path = MIGRATIONS) -> tuple[tuple[str, str], ...]:
    return tuple(
        (path.stem, path.read_text(encoding="utf-8"))
        for path in sorted(directory.glob("*.sql"))
    )


def _digest(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def _applied(connection: psycopg.Connection) -> dict[str, str]:
    rows = connection.execute("SELECT version, sha256 FROM schema_migrations")
    return dict(rows.fetchall())


def _check(applied: dict[str, str], files: tuple[tuple[str, str], ...]) -> None:
    known = {version: _digest(sql) for version, sql in files}
    unknown = sorted(set(applied) - set(known))
    if unknown:
        raise MigrationError(f"Database has migrations unknown to the code: {unknown}")
    changed = sorted(v for v, digest in applied.items() if known[v] != digest)
    if changed:
        raise MigrationError(f"Applied migrations were edited: {changed}")


def apply_migrations(conninfo: str, directory: Path = MIGRATIONS) -> tuple[str, ...]:
    """Apply pending migrations in one transaction, as the migrator role."""
    files = migration_files(directory)
    with psycopg.connect(conninfo) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK,))
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version text PRIMARY KEY, sha256 char(64) NOT NULL, "
            "applied_at timestamptz NOT NULL DEFAULT now())"
        )
        applied = _applied(connection)
        _check(applied, files)
        pending = tuple((v, sql) for v, sql in files if v not in applied)
        for version, sql in pending:
            connection.execute(sql)
            connection.execute(
                "INSERT INTO schema_migrations (version, sha256) VALUES (%s, %s)",
                (version, _digest(sql)),
            )
    return tuple(version for version, _ in pending)


def require_current_schema(
    connection: psycopg.Connection, directory: Path = MIGRATIONS
) -> None:
    """The runtime never migrates: it refuses a schema that is behind or ahead."""
    files = migration_files(directory)
    try:
        applied = _applied(connection)
    except psycopg.errors.UndefinedTable as error:
        raise MigrationError("Database has no applied migrations") from error
    _check(applied, files)
    missing = sorted({version for version, _ in files} - set(applied))
    if missing:
        raise MigrationError(f"Database is missing migrations: {missing}")
