"""The thin repository base.

Thin is the requirement, not an aspiration (Implementation_Plan §3). No ORM, no
query builder, parameterised SQL only — because Stage 13 must swap SQLite for
PostgreSQL as a backend change, and every abstraction that leaks dialect into
agent code is a future rewrite.

Three conventions live here rather than in the schema, because SQLite cannot
express them portably:

* **Timestamps are stamped in Python.** `DEFAULT (datetime('now'))` is
  SQLite-only; doing it here means every row agrees on ISO-8601 UTC.
* **`uid` is minted on insert.** Backend-Schema §0 gives every table a UUID for
  cross-system reference.
* **JSON columns are encoded and decoded** at this boundary, so callers pass and
  receive Python objects, and the `TEXT`/`JSONB` difference stays invisible.

There is deliberately **no delete method**. "Nothing is hard-deleted. Use status
transitions and `archived_at`" (Backend-Schema §0) — an audit trail with holes
in it is not an audit trail.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

Row = dict[str, Any]


def utcnow_iso() -> str:
    """The one timestamp format in the database: ISO-8601, UTC, always."""
    return datetime.now(tz=UTC).isoformat()


def new_uid() -> str:
    return str(uuid.uuid4())


class Repository:
    """CRUD over one table. Subclass to add queries that table actually needs."""

    table: str
    json_columns: frozenset[str] = frozenset()
    has_uid: bool = True
    created_column: str | None = "created_at"
    updated_column: str | None = None

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # -- encoding boundary -----------------------------------------------------

    def _encode(self, fields: dict[str, Any]) -> dict[str, Any]:
        encoded: dict[str, Any] = {}
        for key, value in fields.items():
            if key in self.json_columns and value is not None and not isinstance(value, str):
                encoded[key] = json.dumps(value, sort_keys=True, separators=(",", ":"))
            elif isinstance(value, bool):
                encoded[key] = int(value)  # {{BOOL}} is INTEGER on SQLite
            else:
                encoded[key] = value
        return encoded

    def _decode(self, row: sqlite3.Row | None) -> Row | None:
        if row is None:
            return None
        decoded = dict(row)
        for key in self.json_columns:
            raw = decoded.get(key)
            if isinstance(raw, str):
                try:
                    decoded[key] = json.loads(raw)
                except json.JSONDecodeError:
                    pass  # leave malformed JSON visible rather than silently dropping it
        return decoded

    # -- CRUD ------------------------------------------------------------------

    def insert(self, **fields: Any) -> int:
        if self.has_uid:
            fields.setdefault("uid", new_uid())
        now = utcnow_iso()
        if self.created_column:
            fields.setdefault(self.created_column, now)
        if self.updated_column:
            fields.setdefault(self.updated_column, now)

        encoded = self._encode(fields)
        columns = ", ".join(encoded)
        placeholders = ", ".join("?" for _ in encoded)
        cursor = self.conn.execute(
            f"INSERT INTO {self.table} ({columns}) VALUES ({placeholders})", list(encoded.values())
        )
        return int(cursor.lastrowid)

    def get(self, row_id: int) -> Row | None:
        return self._decode(
            self.conn.execute(f"SELECT * FROM {self.table} WHERE id = ?", (row_id,)).fetchone()
        )

    def get_by_uid(self, uid: str) -> Row | None:
        return self._decode(
            self.conn.execute(f"SELECT * FROM {self.table} WHERE uid = ?", (uid,)).fetchone()
        )

    def update(self, row_id: int, **fields: Any) -> None:
        if not fields:
            return
        if self.updated_column:
            fields.setdefault(self.updated_column, utcnow_iso())
        encoded = self._encode(fields)
        assignments = ", ".join(f"{column} = ?" for column in encoded)
        self.conn.execute(
            f"UPDATE {self.table} SET {assignments} WHERE id = ?", [*encoded.values(), row_id]
        )

    def find(
        self,
        order_by: str | None = None,
        limit: int | None = None,
        **equals: Any,
    ) -> list[Row]:
        """Equality filters only. Anything richer belongs in a named method on a
        subclass, where it can be read, indexed and tested."""
        sql = f"SELECT * FROM {self.table}"
        params: list[Any] = []
        if equals:
            encoded = self._encode(equals)
            clauses = []
            for column, value in encoded.items():
                if value is None:
                    clauses.append(f"{column} IS NULL")
                else:
                    clauses.append(f"{column} = ?")
                    params.append(value)
            sql += " WHERE " + " AND ".join(clauses)
        if order_by:
            sql += f" ORDER BY {order_by}"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [self._decode(row) for row in self.conn.execute(sql, params)]  # type: ignore[misc]

    def count(self, **equals: Any) -> int:
        sql = f"SELECT COUNT(*) AS n FROM {self.table}"
        params: list[Any] = []
        if equals:
            encoded = self._encode(equals)
            sql += " WHERE " + " AND ".join(f"{column} = ?" for column in encoded)
            params = list(encoded.values())
        return int(self.conn.execute(sql, params).fetchone()["n"])
