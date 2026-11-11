"""Repeatable SQL migration runner.

A migration is applied at most once per database. Re-running on an empty or
already-migrated database is a no-op that still verifies the stored checksum,
so an edited migration is reported instead of silently diverging.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .db import connect, schema_digest

MIGRATION_DIR = Path(__file__).with_name("migrations")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def migration_files() -> list[tuple[int, Path]]:
    files: list[tuple[int, Path]] = []
    for path in sorted(MIGRATION_DIR.glob("[0-9][0-9][0-9]_*.sql")):
        try:
            version = int(path.name.split("_", 1)[0])
        except ValueError as exc:  # pragma: no cover - defensive
            raise RuntimeError(f"invalid migration filename: {path.name}") from exc
        files.append((version, path))
    if not files:
        raise RuntimeError(f"no migrations found in {MIGRATION_DIR}")
    versions = [version for version, _ in files]
    if len(versions) != len(set(versions)):
        raise RuntimeError("duplicate migration versions")
    return files


def migrate(db_path: str | Path) -> dict:
    """Apply pending migrations and return a machine-checkable summary."""

    conn = connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version    INTEGER PRIMARY KEY,
                name       TEXT NOT NULL,
                checksum   TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
        applied_rows = conn.execute(
            "SELECT version, name, checksum, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()
        applied = {row["version"]: dict(row) for row in applied_rows}
        applied_now: list[dict] = []

        for version, path in migration_files():
            sql = path.read_text(encoding="utf-8")
            checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            if version in applied:
                if applied[version]["checksum"] != checksum:
                    raise RuntimeError(
                        f"migration {version} ({path.name}) checksum changed after being applied"
                    )
                continue
            now = _now()
            script = (
                "BEGIN IMMEDIATE;\n"
                + sql
                + "\nINSERT INTO schema_migrations (version, name, checksum, applied_at) VALUES ("
                + f"{version}, {_literal(path.name)}, {_literal(checksum)}, {_literal(now)}"
                + ");\nCOMMIT;"
            )
            try:
                conn.executescript(script)
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            applied_now.append(
                {
                    "version": version,
                    "name": path.name,
                    "checksum": checksum,
                    "applied_at": now,
                }
            )

        total = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        return {
            "database": str(db_path),
            "appliedNow": applied_now,
            "appliedTotal": total,
            "schemaDigest": schema_digest(conn),
        }
    finally:
        conn.close()


def migration_status(db_path: str | Path) -> list[dict]:
    conn = connect(db_path)
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        if not exists:
            return []
        return [
            dict(row)
            for row in conn.execute(
                "SELECT version, name, checksum, applied_at FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
    finally:
        conn.close()
