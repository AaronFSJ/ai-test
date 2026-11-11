"""Evidence ingestion, metadata search, drill-down and retention cleanup.

Content is redacted before it is written to the object store. The object key
is partitioned by project and run, while the ``evidence`` table keeps the
searchable metadata (run, item, kind, digest, retention tombstone).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import redaction, service, storage

EVIDENCE_KINDS = ("log", "dom", "screenshot", "request", "model_io")
DEFAULT_RETENTION_DAYS = {"P0": 90, "P1": 30}
MAX_EVIDENCE_BYTES = 5 * 1024 * 1024
DEFAULT_CONTENT_TYPES = {
    "log": "text/plain",
    "dom": "text/html",
    "screenshot": "image/png",
    "request": "application/json",
    "model_io": "application/json",
}
EXTENSION_CONTENT_TYPES = {
    ".json": "application/json",
    ".log": "text/plain",
    ".txt": "text/plain",
    ".html": "text/html",
    ".htm": "text/html",
    ".xml": "application/xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".har": "application/json",
    ".csv": "text/csv",
}
DEFAULT_EXTENSIONS = {
    "log": ".log",
    "dom": ".html",
    "screenshot": ".png",
    "request": ".json",
    "model_io": ".json",
}
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_CONTENT_TYPE = re.compile(r"^[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*$")
_ID = re.compile(r"^[A-Za-z0-9_]+$")


def _iso_after(created_at: str, days: int) -> str:
    parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    return (parsed + timedelta(days=days)).astimezone(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def _safe_filename(value: str | None, kind: str, evidence_id: str) -> str:
    name = Path(value or "").name.strip()
    name = _SAFE_NAME.sub("_", name).strip("._")
    if not name:
        name = f"evidence-{evidence_id}{DEFAULT_EXTENSIONS[kind]}"
    if len(name) > 120:
        suffix = Path(name).suffix[:16]
        name = name[: 120 - len(suffix)] + suffix
    return name


def _content_type(kind: str, filename: str, explicit: str | None) -> str:
    if explicit:
        media_type = explicit.split(";", 1)[0].strip().lower()
        if not _CONTENT_TYPE.match(media_type):
            raise service.ApiError(400, "validation_error", "'content_type' must look like type/subtype")
        return media_type
    return EXTENSION_CONTENT_TYPES.get(Path(filename).suffix.lower(), DEFAULT_CONTENT_TYPES[kind])


def _policy_rows(conn: sqlite3.Connection, project_id: str) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT case_priority, retain_days, updated_by, updated_at FROM retention_policy WHERE project_id = ?",
        (project_id,),
    ).fetchall()
    configured = {row["case_priority"]: dict(row) for row in rows}
    result: dict[str, dict] = {}
    for priority in ("P0", "P1"):
        if priority in configured:
            row = configured[priority]
            result[priority] = {
                "case_priority": priority,
                "retain_days": row["retain_days"],
                "source": "configured",
                "updated_by": row["updated_by"],
                "updated_at": row["updated_at"],
            }
        else:
            result[priority] = {
                "case_priority": priority,
                "retain_days": DEFAULT_RETENTION_DAYS[priority],
                "source": "default",
                "updated_by": None,
                "updated_at": None,
            }
    return result


def get_retention_policy(conn: sqlite3.Connection, actor: dict, project_id: str) -> dict:
    service.require_project(actor, project_id)
    if conn.execute("SELECT 1 FROM project WHERE id = ?", (project_id,)).fetchone() is None:
        raise service.ApiError(404, "not_found", "project not found")
    return {
        "project_id": project_id,
        "defaults": dict(DEFAULT_RETENTION_DAYS),
        "policies": _policy_rows(conn, project_id),
    }


def set_retention_policy(
    conn: sqlite3.Connection, actor: dict, project_id: str, payload: dict
) -> dict:
    service.require_project(actor, project_id)
    service.require_write(actor)
    if conn.execute("SELECT 1 FROM project WHERE id = ?", (project_id,)).fetchone() is None:
        raise service.ApiError(404, "not_found", "project not found")

    updates: dict[str, int] = {}
    if "policies" in payload:
        source = payload["policies"]
        if not isinstance(source, dict):
            raise service.ApiError(400, "validation_error", "'policies' must be an object")
        updates = source  # type: ignore[assignment]
    elif "case_priority" in payload or "retain_days" in payload:
        updates = {payload.get("case_priority"): payload.get("retain_days")}  # type: ignore[dict-item]
    else:
        updates = payload  # type: ignore[assignment]

    validated: dict[str, int] = {}
    for priority, days in updates.items():
        if priority not in DEFAULT_RETENTION_DAYS:
            raise service.ApiError(400, "validation_error", f"unknown case priority: {priority}")
        if isinstance(days, bool) or not isinstance(days, int) or days < 0 or days > 3650:
            raise service.ApiError(400, "validation_error", "'retain_days' must be an integer between 0 and 3650")
        validated[priority] = days
    if not validated:
        raise service.ApiError(400, "validation_error", "no retention policy values supplied")

    for priority, days in validated.items():
        conn.execute(
            """
            INSERT INTO retention_policy (project_id, case_priority, retain_days, updated_by, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(project_id, case_priority)
            DO UPDATE SET retain_days = excluded.retain_days,
                          updated_by = excluded.updated_by,
                          updated_at = excluded.updated_at
            """,
            (project_id, priority, days, actor["id"], service.now()),
        )
    return get_retention_policy(conn, actor, project_id)


def _item_context(
    conn: sqlite3.Connection, project_id: str, run_id: str, item_id: str
) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT ri.*, r.project_id AS run_project_id
        FROM run_item ri JOIN run r ON r.id = ri.run_id
        WHERE ri.id = ? AND ri.run_id = ? AND r.project_id = ?
        """,
        (item_id, run_id, project_id),
    ).fetchone()
    if row is None:
        raise service.ApiError(404, "not_found", "run item not found")
    return row


def add_evidence(
    conn: sqlite3.Connection,
    store: storage.ObjectStore,
    actor: dict,
    project_id: str,
    run_id: str,
    item_id: str,
    payload: dict,
) -> dict:
    """Redact, store and index one evidence object for a run item."""

    service.require_project(actor, project_id)
    service.require_write(actor)
    item = _item_context(conn, project_id, run_id, item_id)
    kind = payload.get("kind")
    if kind not in EVIDENCE_KINDS:
        raise service.ApiError(400, "validation_error", f"'kind' must be one of {list(EVIDENCE_KINDS)}")

    if payload.get("content_base64") is not None:
        try:
            raw = base64.b64decode(str(payload["content_base64"]), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise service.ApiError(400, "validation_error", "content_base64 is not valid base64") from exc
    elif payload.get("content") is not None:
        if not isinstance(payload["content"], str):
            raise service.ApiError(400, "validation_error", "'content' must be a string")
        raw = payload["content"].encode("utf-8")
    else:
        raise service.ApiError(400, "validation_error", "one of 'content' or 'content_base64' is required")
    if not raw:
        raise service.ApiError(400, "validation_error", "evidence content must not be empty")
    if len(raw) > MAX_EVIDENCE_BYTES:
        raise service.ApiError(413, "payload_too_large", f"evidence must be at most {MAX_EVIDENCE_BYTES} bytes")

    evidence_id = service.new_id("evd")
    filename = _safe_filename(payload.get("filename"), kind, evidence_id)
    content_type = _content_type(kind, filename, payload.get("content_type"))
    stored_bytes, redaction_report = redaction.redact_bytes(raw, content_type)
    digest = hashlib.sha256(stored_bytes).hexdigest()

    safe_project = project_id if _ID.match(project_id) else "invalid-project"
    safe_run = run_id if _ID.match(run_id) else "invalid-run"
    storage_key = f"projects/{safe_project}/runs/{safe_run}/{evidence_id}-{filename}"
    created = service.now()
    priority = item["priority"] or "P1"
    policy = _policy_rows(conn, project_id)[priority]
    expires_at = _iso_after(created, policy["retain_days"])
    put = store.put(storage_key, stored_bytes)
    try:
        conn.execute(
            """
            INSERT INTO evidence
              (id, run_item_id, kind, uri, bytes, created_at, project_id, run_id,
               filename, storage_key, content_sha256, content_type, bytes_original,
               redacted, redaction_json, retention_class, retention_expires_at, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                evidence_id,
                item_id,
                kind,
                put["uri"],
                put["bytes"],
                created,
                project_id,
                run_id,
                filename,
                storage_key,
                digest,
                content_type,
                len(raw),
                1 if redaction_report.get("redacted") else 0,
                json.dumps(redaction_report, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                priority,
                expires_at,
                actor["id"],
            ),
        )
    except Exception:
        store.delete(storage_key)
        raise
    return get_evidence(conn, actor, project_id, run_id, item_id, evidence_id)


def _evidence_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["redaction"] = json.loads(data.pop("redaction_json") or "{}")
    data["redacted"] = bool(data["redacted"])
    data["deleted"] = data["deleted_at"] is not None
    return data


def _get_row(
    conn: sqlite3.Connection, project_id: str, run_id: str, item_id: str, evidence_id: str
) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT e.* FROM evidence e
        JOIN run_item ri ON ri.id = e.run_item_id
        JOIN run r ON r.id = ri.run_id
        WHERE e.id = ? AND e.run_item_id = ? AND e.run_id = ? AND e.project_id = ?
          AND ri.run_id = ? AND r.project_id = ?
        """,
        (evidence_id, item_id, run_id, project_id, run_id, project_id),
    ).fetchone()
    if row is None:
        raise service.ApiError(404, "not_found", "evidence not found")
    return row


def get_evidence(
    conn: sqlite3.Connection,
    actor: dict,
    project_id: str,
    run_id: str,
    item_id: str,
    evidence_id: str,
) -> dict:
    service.require_project(actor, project_id)
    return _evidence_dict(_get_row(conn, project_id, run_id, item_id, evidence_id))


def list_evidence(
    conn: sqlite3.Connection,
    actor: dict,
    project_id: str,
    *,
    run_id: str | None = None,
    item_id: str | None = None,
    kind: str | None = None,
    filename_like: str | None = None,
    include_deleted: bool = False,
    limit: int = 200,
) -> list[dict]:
    service.require_project(actor, project_id)
    if kind is not None and kind not in EVIDENCE_KINDS:
        raise service.ApiError(400, "validation_error", f"'kind' must be one of {list(EVIDENCE_KINDS)}")
    if limit < 1 or limit > 1000:
        raise service.ApiError(400, "validation_error", "'limit' must be between 1 and 1000")

    clauses = ["e.project_id = ?"]
    params: list[object] = [project_id]
    if run_id is not None:
        if conn.execute(
            "SELECT 1 FROM run WHERE id = ? AND project_id = ?", (run_id, project_id)
        ).fetchone() is None:
            raise service.ApiError(404, "not_found", "run not found")
        clauses.append("e.run_id = ?")
        params.append(run_id)
    if item_id is not None:
        if conn.execute(
            """
            SELECT 1 FROM run_item ri JOIN run r ON r.id = ri.run_id
            WHERE ri.id = ? AND r.project_id = ? AND (? IS NULL OR ri.run_id = ?)
            """,
            (item_id, project_id, run_id, run_id),
        ).fetchone() is None:
            raise service.ApiError(404, "not_found", "run item not found")
        clauses.append("e.run_item_id = ?")
        params.append(item_id)
    if kind is not None:
        clauses.append("e.kind = ?")
        params.append(kind)
    if filename_like:
        clauses.append("e.filename LIKE ?")
        params.append(f"%{filename_like}%")
    if not include_deleted:
        clauses.append("e.deleted_at IS NULL")
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT e.* FROM evidence e
        WHERE {' AND '.join(clauses)}
        ORDER BY e.created_at, e.id
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [_evidence_dict(row) for row in rows]


def read_evidence_content(
    conn: sqlite3.Connection,
    store: storage.ObjectStore,
    actor: dict,
    project_id: str,
    run_id: str,
    item_id: str,
    evidence_id: str,
) -> dict:
    service.require_project(actor, project_id)
    row = _get_row(conn, project_id, run_id, item_id, evidence_id)
    if row["deleted_at"] is not None:
        raise service.ApiError(
            410, "evidence_expired", "evidence content was removed by the retention policy"
        )
    try:
        content = store.get(row["storage_key"])
    except storage.StorageError as exc:
        raise service.ApiError(500, "evidence_corrupt", "stored evidence object is missing") from exc
    digest = hashlib.sha256(content).hexdigest()
    if digest != row["content_sha256"]:
        raise service.ApiError(500, "evidence_corrupt", "stored evidence digest does not match metadata")
    return {
        "evidence_id": row["id"],
        "filename": row["filename"],
        "content_type": row["content_type"],
        "content": content,
        "bytes": len(content),
        "content_sha256": digest,
        "redacted": bool(row["redacted"]),
    }


def drilldown(
    conn: sqlite3.Connection, actor: dict, project_id: str, run_id: str, item_id: str
) -> dict:
    """Everything reachable from one verdict: item, verdict, evidence and attribution."""

    service.require_project(actor, project_id)
    item = _item_context(conn, project_id, run_id, item_id)
    verdict_row = conn.execute(
        "SELECT * FROM verdict WHERE run_item_id = ?", (item_id,)
    ).fetchone()
    verdict = service._verdict_dict(verdict_row) if verdict_row is not None else None
    evidence = list_evidence(
        conn, actor, project_id, run_id=run_id, item_id=item_id, include_deleted=True
    )
    attribution_rows = conn.execute(
        "SELECT * FROM attribution WHERE run_item_id = ? ORDER BY created_at", (item_id,)
    ).fetchall()
    attributions = []
    for row in attribution_rows:
        record = dict(row)
        record["evidence_refs"] = json.loads(record.pop("evidence_refs_json") or "[]")
        attributions.append(record)
    by_kind: dict[str, int] = {}
    for record in evidence:
        by_kind[record["kind"]] = by_kind.get(record["kind"], 0) + 1
    return {
        "item": service._run_item_dict(item),
        "verdict": verdict,
        "evidence": evidence,
        "attributions": attributions,
        "counts": {
            "evidence": len(evidence),
            "availableEvidence": sum(1 for record in evidence if not record["deleted"]),
            "byKind": by_kind,
        },
    }


def cleanup_expired_evidence(
    conn: sqlite3.Connection,
    store: storage.ObjectStore,
    actor: dict,
    project_id: str,
    *,
    cutoff: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Remove expired objects and record every action in ``evidence_cleanup_log``.

    Metadata is retained as a tombstone (``deleted_at``); only the object bytes
    disappear. ``cutoff`` defaults to now and exists so an operator can audit
    or rehearse a future cleanup deterministically. A dry run reports the same
    candidates without deleting or writing anything.
    """

    service.require_project(actor, project_id)
    service.require_write(actor)
    cutoff_value = cutoff or service.now()
    try:
        datetime.fromisoformat(cutoff_value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise service.ApiError(400, "validation_error", "'cutoff' must be an ISO-8601 timestamp") from exc

    rows = conn.execute(
        """
        SELECT * FROM evidence
        WHERE project_id = ? AND deleted_at IS NULL
          AND retention_expires_at IS NOT NULL AND retention_expires_at <= ?
        ORDER BY retention_expires_at, id
        """,
        (project_id, cutoff_value),
    ).fetchall()
    records = []
    deleted = 0
    for row in rows:
        record = {
            "evidence_id": row["id"],
            "run_id": row["run_id"],
            "item_id": row["run_item_id"],
            "filename": row["filename"],
            "kind": row["kind"],
            "bytes": row["bytes"],
            "retention_class": row["retention_class"],
            "retention_expires_at": row["retention_expires_at"],
        }
        if dry_run:
            records.append(record)
            continue
        object_present = store.delete(row["storage_key"])
        conn.execute(
            "UPDATE evidence SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
            (cutoff_value, row["id"]),
        )
        conn.execute(
            """
            INSERT INTO evidence_cleanup_log
              (id, project_id, evidence_id, run_id, storage_key, retention_expires_at,
               deleted_at, reason, actor, bytes)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'expired', ?, ?)
            """,
            (
                service.new_id("cln"),
                project_id,
                row["id"],
                row["run_id"],
                row["storage_key"],
                row["retention_expires_at"],
                cutoff_value,
                actor["id"],
                row["bytes"],
            ),
        )
        record["object_removed"] = object_present
        records.append(record)
        deleted += 1
    return {
        "project_id": project_id,
        "cutoff": cutoff_value,
        "dry_run": dry_run,
        "matched": len(rows),
        "deleted": deleted,
        "policy": _policy_rows(conn, project_id),
        "records": records,
    }


def list_cleanup_log(
    conn: sqlite3.Connection, actor: dict, project_id: str, limit: int = 100
) -> list[dict]:
    service.require_project(actor, project_id)
    if limit < 1 or limit > 1000:
        raise service.ApiError(400, "validation_error", "'limit' must be between 1 and 1000")
    rows = conn.execute(
        """
        SELECT * FROM evidence_cleanup_log
        WHERE project_id = ?
        ORDER BY deleted_at DESC, id
        LIMIT ?
        """,
        (project_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def evidence_stats(conn: sqlite3.Connection, actor: dict, project_id: str) -> dict:
    service.require_project(actor, project_id)
    rows = conn.execute(
        """
        SELECT kind, COUNT(*) AS count, COALESCE(SUM(bytes), 0) AS bytes,
               SUM(CASE WHEN deleted_at IS NULL THEN 1 ELSE 0 END) AS available
        FROM evidence WHERE project_id = ? GROUP BY kind ORDER BY kind
        """,
        (project_id,),
    ).fetchall()
    return {
        "project_id": project_id,
        "byKind": [dict(row) for row in rows],
        "total": sum(row["count"] for row in rows),
        "available": sum(row["available"] or 0 for row in rows),
    }
