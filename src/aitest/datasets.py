"""T08: dataset and versioned sample management.

The mutable ``sample`` table is the working set. Every mutation publishes a new
immutable ``dataset_version`` with a full sample snapshot and a content digest.
Runs bind a dataset version through ``spec_version.dataset_version``, so an old
Run keeps displaying the exact samples it was scored against.

Only the Python standard library is used, matching the T01 skeleton.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
from collections import Counter
from typing import Any

from .service import ApiError, new_id, now, require_project, require_write

PRIORITIES = ("P0", "P1")
FORMATS = ("jsonl", "csv")
MODES = ("abort", "skip")
MAX_IMPORT_SAMPLES = 10000
MAX_LINE_BYTES = 64 * 1024
MAX_TAGS = 50
MAX_TAG_LEN = 100
SAMPLE_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")

IMPORT_FIELDS = {"sample_id", "input", "expected", "context", "context_ref", "priority", "tags"}
UPDATE_FIELDS = {"input", "expected", "context", "context_ref", "priority", "tags"}
CSV_COLUMNS = ("sample_id", "input", "expected", "context", "context_ref", "priority", "tags")
CSV_ALIASES = {
    "sample_id": "sample_id",
    "id": "sample_id",
    "key": "sample_id",
    "input": "input",
    "expected": "expected",
    "context": "context",
    "context_ref": "context_ref",
    "priority": "priority",
    "tags": "tags",
}


# ---------------------------------------------------------------------------
# Small validation helpers
# ---------------------------------------------------------------------------


def _json_value(text: str | None) -> Any:
    return None if text is None else json.loads(text)


def _dump_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical_tags(tags: list[str]) -> str:
    return json.dumps(tags, ensure_ascii=False, separators=(",", ":"))


def _require_text(payload: dict, field: str, *, max_len: int | None = None) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ApiError(400, "validation_error", f"'{field}' is required and must be a non-empty string")
    value = value.strip()
    if max_len is not None and len(value) > max_len:
        raise ApiError(400, "validation_error", f"'{field}' must be at most {max_len} characters")
    return value


def _optional_text(payload: dict, field: str, *, max_len: int | None = None) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ApiError(400, "validation_error", f"'{field}' must be a string")
    value = value.strip()
    if max_len is not None and len(value) > max_len:
        raise ApiError(400, "validation_error", f"'{field}' must be at most {max_len} characters")
    return value or None


def _normalize_priority(value: Any, *, default: str = "P1") -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ApiError(400, "validation_error", "'priority' must be P0 or P1")
    normalized = value.strip().upper()
    if normalized not in PRIORITIES:
        raise ApiError(400, "validation_error", f"'priority' must be P0 or P1 (got {value!r})")
    return normalized


def _normalize_tags(value: Any, *, csv_cell: bool = False) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if csv_cell and text.startswith("["):
            try:
                value = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ApiError(400, "validation_error", f"'tags' is not a valid JSON array: {exc.msg}") from exc
        else:
            parts = re.split(r"[,;|]", text) if csv_cell else text.split(",")
            value = [part.strip() for part in parts if part.strip()]
    if not isinstance(value, list):
        raise ApiError(400, "validation_error", "'tags' must be an array of strings")
    if len(value) > MAX_TAGS:
        raise ApiError(400, "validation_error", f"'tags' may contain at most {MAX_TAGS} items")
    result: list[str] = []
    for tag in value:
        if not isinstance(tag, str) or not tag.strip():
            raise ApiError(400, "validation_error", "'tags' must contain non-empty strings")
        tag = tag.strip()
        if len(tag) > MAX_TAG_LEN:
            raise ApiError(400, "validation_error", f"each tag must be at most {MAX_TAG_LEN} characters")
        if tag not in result:
            result.append(tag)
    return result


def _normalize_priorities(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [value.strip() for value in values.split(",") if value.strip()]
    if not isinstance(values, (list, tuple)):
        raise ApiError(400, "validation_error", "'priority' filter must be a list or comma-separated string")
    result: list[str] = []
    for value in values:
        priority = _normalize_priority(value)
        if priority not in result:
            result.append(priority)
    return result


def _normalize_key(value: Any) -> str:
    if not isinstance(value, str) or not SAMPLE_KEY_RE.match(value.strip()):
        raise ApiError(
            400,
            "validation_error",
            "'sample_id' must be 1-200 characters of letters, digits, '.', '_', ':' or '-' and start with a letter/digit",
        )
    return value.strip()


def parse_filters(
    query: dict[str, list[str]] | None, tags: Any = None, priorities: Any = None
) -> tuple[list[str], list[str], bool]:
    """Normalize HTTP/CLI filter inputs."""

    tag_values: list[str] = []
    if query:
        for value in (query.get("tag") or []) + (query.get("tags") or []):
            tag_values.extend(part.strip() for part in value.split(",") if part.strip())
    if tags:
        tag_values.extend(tags)
    priority_values: list[str] = []
    if query:
        for value in (query.get("priority") or []) + (query.get("priorities") or []):
            priority_values.extend(part.strip() for part in value.split(",") if part.strip())
    if priorities:
        priority_values.extend(
            value for value in priorities if isinstance(value, str) and value.strip()
        )
    all_tags = False
    if query:
        all_tags = (query.get("all_tags") or ["false"])[0].strip().lower() in ("1", "true", "yes", "on")
    tag_list: list[str] = []
    for tag in tag_values:
        if tag and tag not in tag_list:
            tag_list.append(tag)
    return tag_list, _normalize_priorities(priority_values), all_tags


def _err(errors: list[dict], line: int | None, field: str | None, reason: str) -> None:
    item: dict[str, Any] = {"reason": reason}
    if line is not None:
        item["line"] = line
    if field is not None:
        item["field"] = field
    errors.append(item)


# ---------------------------------------------------------------------------
# Sample payload / digest helpers
# ---------------------------------------------------------------------------


def _payload_from_values(
    sample_key: str,
    input_value: Any,
    expected_value: Any,
    context_value: Any,
    context_ref: str | None,
    priority: str,
    tags: list[str],
) -> dict:
    return {
        "sample_id": sample_key,
        "input": input_value,
        "expected": expected_value,
        "context": context_value,
        "context_ref": context_ref,
        "priority": priority,
        "tags": tags,
    }


def _payload_from_row(row: sqlite3.Row | dict) -> dict:
    return _payload_from_values(
        row["sample_key"],
        _json_value(row["input_json"]),
        _json_value(row["expected_json"]),
        _json_value(row["context_json"]),
        row["context_ref"],
        row["priority"],
        _json_value(row["tags_json"]),
    )


def _sample_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["sample_id"] = data.pop("sample_key")
    data["input"] = _json_value(data.pop("input_json"))
    data["expected"] = _json_value(data.pop("expected_json"))
    data["context"] = _json_value(data.pop("context_json"))
    data["tags"] = _json_value(data.pop("tags_json"))
    return data


def _digest_payloads(payloads: list[dict]) -> str:
    ordered = sorted(payloads, key=lambda item: item["sample_id"])
    text = _dump_value(ordered)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _filter_payloads(
    payloads: list[dict], tags: list[str], priorities: list[str], all_tags: bool
) -> list[dict]:
    result: list[dict] = []
    for payload in payloads:
        if priorities and payload["priority"] not in priorities:
            continue
        if tags:
            sample_tags = set(payload["tags"])
            if all_tags:
                if not all(tag in sample_tags for tag in tags):
                    continue
            elif not any(tag in sample_tags for tag in tags):
                continue
        result.append(payload)
    return result


# ---------------------------------------------------------------------------
# Dataset CRUD
# ---------------------------------------------------------------------------


def create_dataset(conn: sqlite3.Connection, actor: dict, project_id: str, payload: dict) -> dict:
    require_project(actor, project_id)
    require_write(actor)
    name = _require_text(payload, "name", max_len=200)
    description = _optional_text(payload, "description", max_len=2000)
    if conn.execute(
        "SELECT 1 FROM dataset WHERE project_id = ? AND name = ?", (project_id, name)
    ).fetchone():
        raise ApiError(409, "conflict", f"dataset name already exists: {name}")
    dataset_id = new_id("dst")
    try:
        conn.execute(
            "INSERT INTO dataset (id, project_id, name, description, created_at) VALUES (?, ?, ?, ?, ?)",
            (dataset_id, project_id, name, description, now()),
        )
    except sqlite3.IntegrityError as exc:
        raise ApiError(409, "conflict", f"could not create dataset: {exc}") from exc
    return get_dataset(conn, actor, project_id, dataset_id)


def _version_summary(row: sqlite3.Row, dataset_name: str | None = None) -> dict:
    data = dict(row)
    data["import"] = _json_value(data.pop("import_json"))
    if dataset_name is not None:
        data["dataset_name"] = dataset_name
    return data


def list_datasets(conn: sqlite3.Connection, actor: dict, project_id: str) -> list[dict]:
    require_project(actor, project_id)
    rows = conn.execute(
        "SELECT id FROM dataset WHERE project_id = ? ORDER BY name", (project_id,)
    ).fetchall()
    return [
        get_dataset(conn, actor, project_id, row["id"], include_samples=False)
        for row in rows
    ]


def get_dataset(
    conn: sqlite3.Connection,
    actor: dict,
    project_id: str,
    dataset_id: str,
    *,
    include_samples: bool = True,
) -> dict:
    require_project(actor, project_id)
    row = conn.execute(
        "SELECT * FROM dataset WHERE id = ? AND project_id = ?", (dataset_id, project_id)
    ).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "dataset not found")
    dataset = dict(row)
    versions = [
        _version_summary(version)
        for version in conn.execute(
            "SELECT * FROM dataset_version WHERE dataset_id = ? AND project_id = ? ORDER BY version DESC",
            (dataset_id, project_id),
        ).fetchall()
    ]
    dataset["latest_version"] = versions[0] if versions else None
    dataset["versions"] = versions
    dataset["sample_count"] = conn.execute(
        "SELECT COUNT(*) FROM sample WHERE dataset_id = ? AND project_id = ?",
        (dataset_id, project_id),
    ).fetchone()[0]
    if include_samples:
        dataset["samples"] = [
            _sample_dict(sample)
            for sample in conn.execute(
                "SELECT * FROM sample WHERE dataset_id = ? AND project_id = ? ORDER BY sample_key",
                (dataset_id, project_id),
            ).fetchall()
        ]
    return dataset


def _require_dataset(conn: sqlite3.Connection, project_id: str, dataset_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM dataset WHERE id = ? AND project_id = ?", (dataset_id, project_id)
    ).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "dataset not found")
    return row


def list_dataset_versions(
    conn: sqlite3.Connection, actor: dict, project_id: str, dataset_id: str
) -> list[dict]:
    require_project(actor, project_id)
    _require_dataset(conn, project_id, dataset_id)
    rows = conn.execute(
        "SELECT * FROM dataset_version WHERE dataset_id = ? AND project_id = ? ORDER BY version",
        (dataset_id, project_id),
    ).fetchall()
    return [_version_summary(row) for row in rows]


def _version_snapshot(conn: sqlite3.Connection, dataset_version_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM dataset_version_sample WHERE dataset_version_id = ? ORDER BY sample_key",
        (dataset_version_id,),
    ).fetchall()
    return [_payload_from_row(row) for row in rows]


def get_dataset_version(
    conn: sqlite3.Connection,
    actor: dict,
    project_id: str,
    dataset_id: str,
    version: int,
) -> dict:
    require_project(actor, project_id)
    dataset = _require_dataset(conn, project_id, dataset_id)
    row = conn.execute(
        "SELECT * FROM dataset_version WHERE dataset_id = ? AND project_id = ? AND version = ?",
        (dataset_id, project_id, version),
    ).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "dataset version not found")
    data = _version_summary(row, dataset["name"])
    data["samples"] = _version_snapshot(conn, row["id"])
    return data


def resolve_version_ref(conn: sqlite3.Connection, project_id: str, ref: Any) -> dict | None:
    if not isinstance(ref, str) or not ref.startswith("dsv_"):
        return None
    row = conn.execute(
        """
        SELECT dv.*, d.name AS dataset_name
        FROM dataset_version dv JOIN dataset d ON d.id = dv.dataset_id
        WHERE dv.id = ? AND dv.project_id = ? AND d.project_id = ?
        """,
        (ref, project_id, project_id),
    ).fetchone()
    if row is None:
        return None
    return _version_summary(row, row["dataset_name"])


def dataset_version_exists(conn: sqlite3.Connection, project_id: str, ref: str) -> bool:
    return resolve_version_ref(conn, project_id, ref) is not None


# ---------------------------------------------------------------------------
# Sample CRUD
# ---------------------------------------------------------------------------


def _next_sample_key(conn: sqlite3.Connection, dataset_id: str, used: set[str]) -> str:
    count = conn.execute(
        "SELECT COUNT(*) FROM sample WHERE dataset_id = ?", (dataset_id,)
    ).fetchone()[0]
    counter = count
    while True:
        counter += 1
        key = f"smp_{counter:06d}"
        if key not in used:
            return key


def _insert_sample(
    conn: sqlite3.Connection, dataset_id: str, project_id: str, record: dict, timestamp: str
) -> str:
    sample_id = new_id("smp")
    conn.execute(
        """
        INSERT INTO sample
          (id, dataset_id, project_id, sample_key, input_json, expected_json,
           context_json, context_ref, priority, tags_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sample_id,
            dataset_id,
            project_id,
            record["sample_key"],
            record["input_json"],
            record["expected_json"],
            record["context_json"],
            record["context_ref"],
            record["priority"],
            record["tags_json"],
            timestamp,
            timestamp,
        ),
    )
    return sample_id


def _publish_dataset_version(
    conn: sqlite3.Connection,
    actor: dict,
    dataset_id: str,
    project_id: str,
    *,
    source: str,
    import_meta: dict | None = None,
) -> dict:
    rows = conn.execute(
        "SELECT * FROM sample WHERE dataset_id = ? AND project_id = ? ORDER BY sample_key",
        (dataset_id, project_id),
    ).fetchall()
    payloads = [_payload_from_row(row) for row in rows]
    digest = _digest_payloads(payloads)
    version = conn.execute(
        "SELECT COALESCE(MAX(version), 0) + 1 FROM dataset_version WHERE dataset_id = ?",
        (dataset_id,),
    ).fetchone()[0]
    version_id = new_id("dsv")
    timestamp = now()
    conn.execute(
        """
        INSERT INTO dataset_version
          (id, dataset_id, project_id, version, sample_count, content_digest,
           source, import_json, created_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            version_id,
            dataset_id,
            project_id,
            version,
            len(payloads),
            digest,
            source,
            _dump_value(import_meta or {}),
            actor["id"],
            timestamp,
        ),
    )
    for row in rows:
        conn.execute(
            """
            INSERT INTO dataset_version_sample
              (id, dataset_version_id, sample_key, input_json, expected_json,
               context_json, context_ref, priority, tags_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("dvs"),
                version_id,
                row["sample_key"],
                row["input_json"],
                row["expected_json"],
                row["context_json"],
                row["context_ref"],
                row["priority"],
                row["tags_json"],
                row["created_at"],
                row["updated_at"],
            ),
        )
    row = conn.execute(
        "SELECT * FROM dataset_version WHERE id = ?", (version_id,)
    ).fetchone()
    return _version_summary(row)


def _record_from_mapping(data: dict, line: int | None, errors: list[dict]) -> dict:
    """Validate one import/manual sample object and collect field-level errors."""

    record: dict[str, Any] = {
        "line": line,
        "sample_key": None,
        "input_json": None,
        "expected_json": None,
        "context_json": None,
        "context_ref": None,
        "priority": "P1",
        "tags_json": "[]",
    }
    if not isinstance(data, dict):
        _err(errors, line, None, "line must be a JSON object")
        return record
    before = len(errors)
    unknown = sorted(set(data) - IMPORT_FIELDS)
    if unknown:
        _err(errors, line, unknown[0], f"unknown field(s): {', '.join(unknown)}")

    if "sample_id" in data:
        try:
            record["sample_key"] = _normalize_key(data["sample_id"])
        except ApiError as exc:
            _err(errors, line, "sample_id", exc.message)

    if "input" not in data:
        _err(errors, line, "input", "'input' is required")
    elif data["input"] is None:
        _err(errors, line, "input", "'input' must not be null")
    else:
        record["input_json"] = _dump_value(data["input"])

    if "expected" in data and data["expected"] is not None:
        record["expected_json"] = _dump_value(data["expected"])
    if "context" in data and data["context"] is not None:
        record["context_json"] = _dump_value(data["context"])

    if "context_ref" in data and data["context_ref"] is not None:
        value = data["context_ref"]
        if not isinstance(value, str):
            _err(errors, line, "context_ref", "'context_ref' must be a string")
        elif len(value.strip()) > 1000:
            _err(errors, line, "context_ref", "'context_ref' must be at most 1000 characters")
        else:
            record["context_ref"] = value.strip() or None

    if "priority" in data:
        try:
            record["priority"] = _normalize_priority(data["priority"])
        except ApiError as exc:
            _err(errors, line, "priority", exc.message)

    if "tags" in data:
        try:
            record["tags_json"] = _canonical_tags(_normalize_tags(data["tags"]))
        except ApiError as exc:
            _err(errors, line, "tags", exc.message)

    record["valid"] = len(errors) == before
    return record


def add_sample(
    conn: sqlite3.Connection, actor: dict, project_id: str, dataset_id: str, payload: dict
) -> dict:
    require_project(actor, project_id)
    require_write(actor)
    _require_dataset(conn, project_id, dataset_id)
    errors: list[dict] = []
    record = _record_from_mapping(payload, None, errors)
    if errors:
        raise ApiError(400, "validation_error", errors[0]["reason"], details={"errors": errors})
    conn.execute("BEGIN IMMEDIATE")
    try:
        used = {
            row["sample_key"]
            for row in conn.execute(
                "SELECT sample_key FROM sample WHERE dataset_id = ?", (dataset_id,)
            ).fetchall()
        }
        if record["sample_key"] is None:
            record["sample_key"] = _next_sample_key(conn, dataset_id, used)
        if record["sample_key"] in used:
            raise ApiError(409, "conflict", f"sample_id already exists: {record['sample_key']}")
        _insert_sample(conn, dataset_id, project_id, record, now())
        version = _publish_dataset_version(conn, actor, dataset_id, project_id, source="api")
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    sample = conn.execute(
        "SELECT * FROM sample WHERE dataset_id = ? AND sample_key = ?",
        (dataset_id, record["sample_key"]),
    ).fetchone()
    return {"sample": _sample_dict(sample), "dataset_version": version, "changed": True}


def update_sample(
    conn: sqlite3.Connection,
    actor: dict,
    project_id: str,
    dataset_id: str,
    sample_id: str,
    payload: dict,
) -> dict:
    require_project(actor, project_id)
    require_write(actor)
    _require_dataset(conn, project_id, dataset_id)
    if "sample_id" in payload:
        raise ApiError(
            400, "validation_error", "'sample_id' cannot be changed; delete and add a new sample"
        )
    unknown = sorted(set(payload) - UPDATE_FIELDS)
    if unknown:
        raise ApiError(400, "validation_error", f"unknown field(s): {', '.join(unknown)}")
    if not any(field in payload for field in UPDATE_FIELDS):
        raise ApiError(400, "validation_error", "at least one sample field is required")

    errors: list[dict] = []
    changes: dict[str, Any] = {}
    if "input" in payload:
        if payload["input"] is None:
            _err(errors, None, "input", "'input' must not be null")
        else:
            changes["input_json"] = _dump_value(payload["input"])
    if "expected" in payload:
        changes["expected_json"] = (
            None if payload["expected"] is None else _dump_value(payload["expected"])
        )
    if "context" in payload:
        changes["context_json"] = (
            None if payload["context"] is None else _dump_value(payload["context"])
        )
    if "context_ref" in payload:
        value = payload["context_ref"]
        if value is None or (isinstance(value, str) and not value.strip()):
            changes["context_ref"] = None
        elif not isinstance(value, str):
            _err(errors, None, "context_ref", "'context_ref' must be a string")
        elif len(value.strip()) > 1000:
            _err(errors, None, "context_ref", "'context_ref' must be at most 1000 characters")
        else:
            changes["context_ref"] = value.strip()
    if "priority" in payload:
        try:
            changes["priority"] = _normalize_priority(payload["priority"])
        except ApiError as exc:
            _err(errors, None, "priority", exc.message)
    if "tags" in payload:
        try:
            changes["tags_json"] = _canonical_tags(_normalize_tags(payload["tags"]))
        except ApiError as exc:
            _err(errors, None, "tags", exc.message)
    if errors:
        raise ApiError(400, "validation_error", errors[0]["reason"], details={"errors": errors})

    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM sample WHERE id = ? AND dataset_id = ? AND project_id = ?",
            (sample_id, dataset_id, project_id),
        ).fetchone()
        if row is None:
            raise ApiError(404, "not_found", "sample not found")
        changed = any(row[column] != value for column, value in changes.items())
        if not changed:
            conn.execute("COMMIT")
            return {"sample": _sample_dict(row), "dataset_version": None, "changed": False}
        assignments = ", ".join(f"{column} = ?" for column in changes)
        conn.execute(
            f"UPDATE sample SET {assignments}, updated_at = ? WHERE id = ?",
            (*changes.values(), now(), sample_id),
        )
        version = _publish_dataset_version(conn, actor, dataset_id, project_id, source="api")
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    updated = conn.execute("SELECT * FROM sample WHERE id = ?", (sample_id,)).fetchone()
    return {"sample": _sample_dict(updated), "dataset_version": version, "changed": True}


def delete_sample(
    conn: sqlite3.Connection, actor: dict, project_id: str, dataset_id: str, sample_id: str
) -> dict:
    require_project(actor, project_id)
    require_write(actor)
    _require_dataset(conn, project_id, dataset_id)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM sample WHERE id = ? AND dataset_id = ? AND project_id = ?",
            (sample_id, dataset_id, project_id),
        ).fetchone()
        if row is None:
            raise ApiError(404, "not_found", "sample not found")
        conn.execute("DELETE FROM sample WHERE id = ?", (sample_id,))
        version = _publish_dataset_version(conn, actor, dataset_id, project_id, source="api")
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return {
        "deleted": True,
        "sample_id": row["sample_key"],
        "sample": _sample_dict(row),
        "dataset_version": version,
    }


def list_samples(
    conn: sqlite3.Connection,
    actor: dict,
    project_id: str,
    dataset_id: str,
    *,
    tags: list[str] | None = None,
    priorities: list[str] | None = None,
    all_tags: bool = False,
) -> list[dict]:
    require_project(actor, project_id)
    _require_dataset(conn, project_id, dataset_id)
    rows = conn.execute(
        "SELECT * FROM sample WHERE dataset_id = ? AND project_id = ? ORDER BY sample_key",
        (dataset_id, project_id),
    ).fetchall()
    payloads = [_payload_from_row(row) for row in rows]
    filtered = _filter_payloads(payloads, tags or [], priorities or [], all_tags)
    selected = {item["sample_id"] for item in filtered}
    return [_sample_dict(row) for row in rows if row["sample_key"] in selected]


def get_sample(
    conn: sqlite3.Connection,
    actor: dict,
    project_id: str,
    dataset_id: str,
    sample_id: str,
) -> dict:
    require_project(actor, project_id)
    _require_dataset(conn, project_id, dataset_id)
    row = conn.execute(
        "SELECT * FROM sample WHERE id = ? AND dataset_id = ? AND project_id = ?",
        (sample_id, dataset_id, project_id),
    ).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "sample not found")
    return _sample_dict(row)


def dataset_stats(
    conn: sqlite3.Connection, actor: dict, project_id: str, dataset_id: str
) -> dict:
    require_project(actor, project_id)
    _require_dataset(conn, project_id, dataset_id)
    rows = conn.execute(
        "SELECT priority, tags_json FROM sample WHERE dataset_id = ? AND project_id = ?",
        (dataset_id, project_id),
    ).fetchall()
    by_priority: Counter[str] = Counter()
    by_tag: Counter[str] = Counter()
    per_priority: dict[str, Counter[str]] = {priority: Counter() for priority in PRIORITIES}
    for row in rows:
        by_priority[row["priority"]] += 1
        for tag in _json_value(row["tags_json"]):
            by_tag[tag] += 1
            per_priority[row["priority"]][tag] += 1
    return {
        "dataset_id": dataset_id,
        "total": len(rows),
        "by_priority": {priority: by_priority.get(priority, 0) for priority in PRIORITIES},
        "by_tag": dict(sorted(by_tag.items())),
        "by_priority_and_tag": {
            priority: dict(sorted(counter.items()))
            for priority, counter in per_priority.items()
        },
    }


# ---------------------------------------------------------------------------
# JSONL / CSV import
# ---------------------------------------------------------------------------


def _cell_value(cell: str) -> Any:
    text = cell.strip()
    if text == "":
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return cell


def _parse_jsonl(content: str, errors: list[dict]) -> tuple[list[dict], int]:
    records: list[dict] = []
    total = 0
    for line_no, raw in enumerate(content.splitlines(), 1):
        if line_no == 1:
            raw = raw.lstrip("\ufeff")
        if not raw.strip():
            continue
        total += 1
        if len(raw.encode("utf-8")) > MAX_LINE_BYTES:
            _err(errors, line_no, None, f"line exceeds {MAX_LINE_BYTES} bytes")
            records.append({"line": line_no, "valid": False})
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            _err(errors, line_no, None, f"invalid JSON: {exc.msg} at column {exc.colno}")
            records.append({"line": line_no, "valid": False})
            continue
        if not isinstance(data, dict):
            _err(errors, line_no, None, "line must be a JSON object")
            records.append({"line": line_no, "valid": False})
            continue
        records.append(_record_from_mapping(data, line_no, errors))
    return records, total


def _parse_csv(content: str, errors: list[dict]) -> tuple[list[dict], int]:
    reader = csv.reader(io.StringIO(content, newline=""))
    header: list[str] | None = None
    records: list[dict] = []
    total = 0
    for row in reader:
        if not any(cell.strip() for cell in row):
            continue
        if header is None:
            header = _csv_header(row)
            continue
        total += 1
        line_no = reader.line_num
        if len(row) != len(header):
            _err(
                errors,
                line_no,
                None,
                f"row has {len(row)} cells but header has {len(header)}",
            )
            records.append({"line": line_no, "valid": False})
            continue
        data: dict[str, Any] = {}
        cell_map = dict(zip(header, row))
        if cell_map.get("sample_id", "").strip():
            data["sample_id"] = cell_map["sample_id"].strip()
        if "input" in cell_map:
            data["input"] = _cell_value(cell_map["input"])
        if "expected" in cell_map:
            data["expected"] = _cell_value(cell_map["expected"])
        if "context" in cell_map:
            data["context"] = _cell_value(cell_map["context"])
        if "context_ref" in cell_map:
            data["context_ref"] = cell_map["context_ref"].strip() or None
        if "priority" in cell_map and cell_map["priority"].strip():
            data["priority"] = cell_map["priority"].strip()
        if "tags" in cell_map:
            try:
                data["tags"] = _normalize_tags(cell_map["tags"], csv_cell=True)
            except ApiError as exc:
                _err(errors, line_no, "tags", exc.message)
                data["tags"] = []
        records.append(_record_from_mapping(data, line_no, errors))
    if header is None:
        raise ApiError(
            422,
            "import_validation_failed",
            "CSV content is empty",
            details={
                "format": "csv",
                "totalLines": 0,
                "validLines": 0,
                "invalidLines": 0,
                "errors": [],
            },
        )
    return records, total


def _csv_header(row: list[str]) -> list[str]:
    raw_header = [cell.strip().lstrip("\ufeff").lower() for cell in row]
    header: list[str] = []
    seen: set[str] = set()
    header_errors: list[dict] = []
    for name in raw_header:
        mapped = CSV_ALIASES.get(name)
        if mapped is None:
            _err(header_errors, 1, name or None, f"unknown CSV column: {name!r}")
            continue
        if mapped in seen:
            _err(header_errors, 1, mapped, f"duplicate CSV column: {mapped!r}")
            continue
        seen.add(mapped)
        header.append(mapped)
    if "input" not in header:
        _err(header_errors, 1, "input", "CSV header must include 'input'")
    if header_errors:
        raise ApiError(
            422,
            "import_validation_failed",
            "CSV header is invalid",
            details={
                "format": "csv",
                "totalLines": 0,
                "validLines": 0,
                "invalidLines": 0,
                "errors": header_errors,
            },
        )
    return header


def _assign_keys(
    conn: sqlite3.Connection, dataset_id: str, records: list[dict], errors: list[dict]
) -> None:
    existing = {
        row["sample_key"]
        for row in conn.execute(
            "SELECT sample_key FROM sample WHERE dataset_id = ?", (dataset_id,)
        ).fetchall()
    }
    seen: dict[str, int | None] = {}
    for record in records:
        key = record.get("sample_key")
        if not key:
            continue
        if key in seen:
            _err(
                errors,
                record.get("line"),
                "sample_id",
                f"duplicate sample_id {key!r} (first seen on line {seen[key]})",
            )
            record["valid"] = False
            continue
        seen[key] = record.get("line")
        if key in existing:
            _err(
                errors,
                record.get("line"),
                "sample_id",
                f"sample_id already exists in this dataset: {key!r}",
            )
            record["valid"] = False
    used = set(existing) | set(seen)
    counter = len(existing)
    for record in records:
        if not record.get("valid") or record.get("sample_key"):
            continue
        while True:
            counter += 1
            key = f"smp_{counter:06d}"
            if key not in used:
                break
        record["sample_key"] = key
        used.add(key)


def import_samples(
    conn: sqlite3.Connection, actor: dict, project_id: str, dataset_id: str, payload: dict
) -> dict:
    require_project(actor, project_id)
    require_write(actor)
    _require_dataset(conn, project_id, dataset_id)
    fmt = payload.get("format") or "jsonl"
    if not isinstance(fmt, str) or fmt.lower() not in FORMATS:
        raise ApiError(400, "validation_error", f"'format' must be one of {list(FORMATS)}")
    fmt = fmt.lower()
    mode = payload.get("mode") or "abort"
    if not isinstance(mode, str) or mode.lower() not in MODES:
        raise ApiError(400, "validation_error", f"'mode' must be one of {list(MODES)}")
    mode = mode.lower()
    content = payload.get("content")
    if not isinstance(content, str):
        raise ApiError(400, "validation_error", "'content' is required and must be a string")
    if not content.strip():
        raise ApiError(400, "validation_error", "'content' must not be empty")

    errors: list[dict] = []
    if fmt == "jsonl":
        records, total = _parse_jsonl(content, errors)
    else:
        records, total = _parse_csv(content, errors)
    if total > MAX_IMPORT_SAMPLES:
        raise ApiError(
            400,
            "validation_error",
            f"import contains {total} samples; the limit is {MAX_IMPORT_SAMPLES}",
        )
    _assign_keys(conn, dataset_id, records, errors)
    errors.sort(key=lambda item: (item.get("line") or 0, item.get("field") or ""))
    valid_records = [
        record for record in records if record.get("valid") and record.get("sample_key")
    ]
    valid_lines = len(valid_records)
    invalid_lines = total - valid_lines
    details = {
        "format": fmt,
        "mode": mode,
        "totalLines": total,
        "validLines": valid_lines,
        "invalidLines": invalid_lines,
        "errors": errors[:200],
        "errorsTruncated": len(errors) > 200,
        "errorCount": len(errors),
    }
    if errors and (mode == "abort" or not valid_records):
        raise ApiError(
            422,
            "import_validation_failed",
            f"{invalid_lines} of {total} lines failed validation; no samples were imported",
            details=details,
        )

    conn.execute("BEGIN IMMEDIATE")
    try:
        timestamp = now()
        for record in valid_records:
            _insert_sample(conn, dataset_id, project_id, record, timestamp)
        version = _publish_dataset_version(
            conn,
            actor,
            dataset_id,
            project_id,
            source="import",
            import_meta={
                "format": fmt,
                "mode": mode,
                "totalLines": total,
                "imported": valid_lines,
                "skipped": invalid_lines,
                "errorCount": len(errors),
            },
        )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise

    return {
        "dataset_id": dataset_id,
        "format": fmt,
        "mode": mode,
        "totalLines": total,
        "imported": valid_lines,
        "skipped": invalid_lines,
        "errors": errors[:200],
        "errorCount": len(errors),
        "dataset_version": version,
    }


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _render_jsonl(payloads: list[dict]) -> str:
    if not payloads:
        return ""
    return (
        "\n".join(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            for payload in payloads
        )
        + "\n"
    )


def _render_csv(payloads: list[dict]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for payload in payloads:
        writer.writerow(
            [
                payload["sample_id"],
                "" if payload["input"] is None else _dump_value(payload["input"]),
                "" if payload["expected"] is None else _dump_value(payload["expected"]),
                "" if payload["context"] is None else _dump_value(payload["context"]),
                payload["context_ref"] or "",
                payload["priority"],
                _canonical_tags(payload["tags"]),
            ]
        )
    return buffer.getvalue()


def _safe_filename(name: str, version: int, fmt: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-._") or "dataset"
    extension = "jsonl" if fmt == "jsonl" else "csv"
    return f"{safe}-v{version}.{extension}"


def export_version(
    conn: sqlite3.Connection,
    actor: dict,
    project_id: str,
    dataset_id: str,
    version: int,
    *,
    fmt: str = "jsonl",
    tags: list[str] | None = None,
    priorities: list[str] | None = None,
    all_tags: bool = False,
) -> dict:
    require_project(actor, project_id)
    if fmt not in FORMATS:
        raise ApiError(400, "validation_error", f"'format' must be one of {list(FORMATS)}")
    row = conn.execute(
        """
        SELECT dv.*, d.name AS dataset_name
        FROM dataset_version dv JOIN dataset d ON d.id = dv.dataset_id
        WHERE dv.dataset_id = ? AND dv.project_id = ? AND dv.version = ? AND d.project_id = ?
        """,
        (dataset_id, project_id, version, project_id),
    ).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "dataset version not found")
    payloads = _version_snapshot(conn, row["id"])
    filtered = _filter_payloads(payloads, tags or [], priorities or [], all_tags)
    digest = _digest_payloads(filtered)
    content = _render_jsonl(filtered) if fmt == "jsonl" else _render_csv(filtered)
    media_type = (
        "application/x-ndjson; charset=utf-8"
        if fmt == "jsonl"
        else "text/csv; charset=utf-8"
    )
    return {
        "content": content,
        "contentType": media_type,
        "format": fmt,
        "filename": _safe_filename(row["dataset_name"], version, fmt),
        "dataset_id": dataset_id,
        "dataset_name": row["dataset_name"],
        "dataset_version_id": row["id"],
        "version": version,
        "sampleCount": len(filtered),
        "sampleDigest": digest,
        "contentSha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "filters": {
            "tags": tags or [],
            "priorities": priorities or [],
            "allTags": all_tags,
        },
    }


def export_latest(
    conn: sqlite3.Connection,
    actor: dict,
    project_id: str,
    dataset_id: str,
    *,
    fmt: str = "jsonl",
    tags: list[str] | None = None,
    priorities: list[str] | None = None,
    all_tags: bool = False,
) -> dict:
    require_project(actor, project_id)
    row = conn.execute(
        "SELECT version FROM dataset_version WHERE dataset_id = ? AND project_id = ? ORDER BY version DESC LIMIT 1",
        (dataset_id, project_id),
    ).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "dataset has no published versions")
    return export_version(
        conn,
        actor,
        project_id,
        dataset_id,
        row["version"],
        fmt=fmt,
        tags=tags,
        priorities=priorities,
        all_tags=all_tags,
    )


# ---------------------------------------------------------------------------
# Run binding helpers
# ---------------------------------------------------------------------------


def attach_dataset_to_run(conn: sqlite3.Connection, run_data: dict) -> None:
    """Attach the exact dataset snapshot bound by a B-channel Run."""

    if run_data.get("channel") != "B":
        return
    spec_version = run_data.get("spec_version") or {}
    summary = resolve_version_ref(
        conn, run_data["project_id"], spec_version.get("dataset_version")
    )
    if summary is None:
        return
    run_data["dataset_version_detail"] = summary
    run_data["dataset_samples"] = _version_snapshot(conn, summary["id"])


def run_dataset_samples(
    conn: sqlite3.Connection, actor: dict, project_id: str, run_id: str
) -> dict:
    require_project(actor, project_id)
    row = conn.execute(
        "SELECT id, channel, spec_version_id FROM run WHERE id = ? AND project_id = ?",
        (run_id, project_id),
    ).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "run not found")
    if row["channel"] != "B":
        raise ApiError(404, "not_found", "run is not bound to a dataset version")
    spec_version = conn.execute(
        "SELECT dataset_version FROM spec_version WHERE id = ?", (row["spec_version_id"],)
    ).fetchone()
    ref = spec_version["dataset_version"] if spec_version is not None else None
    summary = resolve_version_ref(conn, project_id, ref)
    if summary is None:
        raise ApiError(404, "not_found", "run does not reference a managed dataset version")
    return {
        "run_id": run_id,
        "dataset_version": summary,
        "samples": _version_snapshot(conn, summary["id"]),
    }
