"""SQLite connection helpers shared by the API, CLI and tests."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection with FK enforcement enabled.

    ``isolation_level=None`` keeps the driver in autocommit mode so that
    explicit ``BEGIN IMMEDIATE`` transactions in the service layer are not
    silently wrapped in an implicit transaction.
    """

    path = str(db_path)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    if fk != 1:
        conn.close()
        raise RuntimeError("SQLite foreign key enforcement could not be enabled")
    return conn


def schema_digest(conn: sqlite3.Connection) -> str:
    """Stable digest of the database schema (not data)."""

    rows = conn.execute(
        """
        SELECT type, name, tbl_name, sql
        FROM sqlite_master
        WHERE name NOT LIKE 'sqlite_%'
        ORDER BY type, name
        """
    ).fetchall()
    payload = "\n".join(
        f"{row['type']}|{row['name']}|{row['tbl_name']}|{row['sql']}" for row in rows
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def schema_sql(conn: sqlite3.Connection) -> str:
    """Human-checkable DDL dump of the current database."""

    rows = conn.execute(
        """
        SELECT type, name, tbl_name, sql
        FROM sqlite_master
        WHERE name NOT LIKE 'sqlite_%'
        ORDER BY type, name
        """
    ).fetchall()
    parts = []
    for row in rows:
        parts.append(f"-- {row['type']}: {row['name']} (table {row['tbl_name']})")
        parts.append(row["sql"] + ";")
        parts.append("")
    return "\n".join(parts)


def foreign_keys(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """Return PRAGMA foreign_key_list for every user table."""

    tables = [
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]
    result: dict[str, list[dict]] = {}
    for table in tables:
        rows = conn.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
        result[table] = [dict(row) for row in rows]
    return result
