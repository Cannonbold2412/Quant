"""Migrations from day one (Implementation_Plan §3).

Numbered `.sql` files, applied in order, each inside one transaction, recorded
in `schema_migrations`. Re-running is a no-op.

The one non-obvious rule is **checksum drift detection**. An already-applied
migration whose file has since been edited raises rather than being silently
ignored: the database no longer matches the source that claims to describe it,
and the whole point of `experiments.comparable` (TRD §6.6) is that we can trust
what the stored record means. Fix drift by writing a *new* migration, never by
editing an applied one.

Checksums are taken over the raw file text, before dialect substitution, so the
same migration has the same identity on SQLite and PostgreSQL.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..hashing import hash_bytes
from .connection import Dialect, connect, render_sql, split_statements, transaction

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_FILENAME = re.compile(r"^(\d{4})_(\w+)\.sql$")

_BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    checksum   TEXT NOT NULL
)
"""


class MigrationError(RuntimeError):
    """A migration could not be applied, or the record disagrees with the files."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path
    sql: str

    @property
    def checksum(self) -> str:
        return hash_bytes(self.sql.encode("utf-8"))


def discover(directory: Path | None = None) -> list[Migration]:
    """All migration files, ordered by version."""
    directory = directory or MIGRATIONS_DIR
    migrations: list[Migration] = []
    seen: dict[int, str] = {}

    for path in sorted(directory.glob("*.sql")):
        match = _FILENAME.match(path.name)
        if not match:
            raise MigrationError(f"migration filename must be NNNN_name.sql: {path.name}")
        version = int(match.group(1))
        if version in seen:
            raise MigrationError(f"duplicate migration version {version}: {seen[version]} and {path.name}")
        seen[version] = path.name
        migrations.append(
            Migration(version=version, name=match.group(2), path=path, sql=path.read_text(encoding="utf-8"))
        )
    return migrations


def applied(conn: sqlite3.Connection) -> dict[int, sqlite3.Row]:
    conn.execute(_BOOTSTRAP)
    rows = conn.execute("SELECT version, name, applied_at, checksum FROM schema_migrations").fetchall()
    return {row["version"]: row for row in rows}


def _verify_no_drift(migrations: list[Migration], history: dict[int, sqlite3.Row]) -> None:
    for migration in migrations:
        record = history.get(migration.version)
        if record is not None and record["checksum"] != migration.checksum:
            raise MigrationError(
                f"migration {migration.path.name} changed after being applied "
                f"(recorded {record['checksum'][:12]}, file {migration.checksum[:12]}). "
                "Applied migrations are immutable — add a new migration instead."
            )
    unknown = set(history) - {m.version for m in migrations}
    if unknown:
        raise MigrationError(
            f"database records migrations with no matching file: {sorted(unknown)}. "
            "The database is newer than this checkout."
        )


def pending(conn: sqlite3.Connection, directory: Path | None = None) -> list[Migration]:
    migrations = discover(directory)
    history = applied(conn)
    _verify_no_drift(migrations, history)
    return [m for m in migrations if m.version not in history]


def migrate(
    conn: sqlite3.Connection | None = None,
    directory: Path | None = None,
    dialect: Dialect = "sqlite",
    target: int | None = None,
) -> list[Migration]:
    """Apply pending migrations up to `target` (default: all). Returns those applied."""
    owned = conn is None
    conn = conn or connect()
    try:
        to_apply = [m for m in pending(conn, directory) if target is None or m.version <= target]
        for migration in to_apply:
            statements = split_statements(render_sql(migration.sql, dialect))
            with transaction(conn):
                for statement in statements:
                    try:
                        conn.execute(statement)
                    except sqlite3.Error as exc:
                        head = " ".join(statement.split())[:120]
                        raise MigrationError(f"{migration.path.name}: {exc}\n  in: {head}") from exc
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at, checksum) VALUES (?, ?, ?, ?)",
                    (
                        migration.version,
                        migration.name,
                        datetime.now(tz=UTC).isoformat(),
                        migration.checksum,
                    ),
                )
        return to_apply
    finally:
        if owned:
            conn.close()


def status(conn: sqlite3.Connection, directory: Path | None = None) -> dict[str, object]:
    """A summary for `aqrl db status`."""
    migrations = discover(directory)
    history = applied(conn)
    _verify_no_drift(migrations, history)
    tables = [
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    return {
        "applied": sorted(history),
        "pending": [m.version for m in migrations if m.version not in history],
        "tables": tables,
    }
