"""Business rules: project isolation, Spec versioning and T02 run lifecycle.

The module owns the SQLite-backed scheduler (queue, priority, concurrency,
hard timeouts), runner registration/heartbeat and the append-only run_event
ledger. Scheduler maintenance is tick-driven so no background worker is needed.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

ROLES = ("member", "readonly")
CHANNELS = ("A", "B")
RUN_STATES = ("pass", "fail", "uncertain", "error")
TRIGGERS = ("ci", "manual", "schedule", "api")


class ApiError(Exception):
    """Error that maps directly to an HTTP/CLI failure."""

    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _row(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def _require_text(payload: dict, field: str, *, max_len: int | None = None) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ApiError(400, "validation_error", f"'{field}' is required and must be a non-empty string")
    value = value.strip()
    if max_len is not None and len(value) > max_len:
        raise ApiError(400, "validation_error", f"'{field}' must be at most {max_len} characters")
    return value


def _optional_text(payload: dict, field: str) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ApiError(400, "validation_error", f"'{field}' must be a string")
    value = value.strip()
    return value or None


def _json_text(value: Any, field: str, *, required: bool = False) -> str | None:
    """Normalize a JSON object/string to canonical JSON text."""

    if value is None:
        if required:
            raise ApiError(400, "validation_error", f"'{field}' is required")
        return None
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ApiError(400, "validation_error", f"'{field}' must be valid JSON: {exc.msg}") from exc
    else:
        parsed = value
    return json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_value(text: str | None) -> Any:
    if text is None:
        return None
    return json.loads(text)


# ---------------------------------------------------------------------------
# Bootstrap and authentication
# ---------------------------------------------------------------------------


def create_project(
    conn: sqlite3.Connection, name: str, default_branch: str = "main", quota_config: Any = None
) -> dict:
    name = (name or "").strip()
    default_branch = (default_branch or "main").strip() or "main"
    if not name:
        raise ApiError(400, "validation_error", "'name' is required")
    if conn.execute("SELECT 1 FROM project WHERE name = ?", (name,)).fetchone():
        raise ApiError(409, "conflict", f"project name already exists: {name}")
    project_id = new_id("prj")
    conn.execute(
        """
        INSERT INTO project
          (id, name, default_branch, quota_config, scheduler_config_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            project_id,
            name,
            default_branch,
            _json_text({} if quota_config is None else quota_config, "quota_config") or "{}",
            _canonical_json(SCHEDULER_DEFAULTS),
            now(),
        ),
    )
    return get_project(conn, project_id)


def get_project(conn: sqlite3.Connection, project_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM project WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        return None
    project = dict(row)
    project["quota_config"] = _json_value(project["quota_config"])
    project["scheduler_config"] = get_scheduler_settings(conn, project_id)
    project.pop("scheduler_config_json", None)
    project["counts"] = {
        "members": conn.execute("SELECT COUNT(*) FROM member WHERE project_id = ?", (project_id,)).fetchone()[0],
        "specs": conn.execute("SELECT COUNT(*) FROM spec WHERE project_id = ?", (project_id,)).fetchone()[0],
        "runs": conn.execute("SELECT COUNT(*) FROM run WHERE project_id = ?", (project_id,)).fetchone()[0],
    }
    return project


def add_member(
    conn: sqlite3.Connection,
    project_id: str,
    name: str,
    role: str,
    email: str | None = None,
) -> dict:
    name = (name or "").strip()
    if not name:
        raise ApiError(400, "validation_error", "'name' is required")
    if role not in ROLES:
        raise ApiError(400, "validation_error", f"'role' must be one of {list(ROLES)}")
    if conn.execute("SELECT 1 FROM project WHERE id = ?", (project_id,)).fetchone() is None:
        raise ApiError(404, "not_found", "project not found")
    if conn.execute("SELECT 1 FROM member WHERE project_id = ? AND name = ?", (project_id, name)).fetchone():
        raise ApiError(409, "conflict", f"member already exists in project: {name}")
    token = "at_" + secrets.token_urlsafe(32)
    member_id = new_id("mem")
    conn.execute(
        """
        INSERT INTO member (id, project_id, name, email, role, token_hash, token_prefix, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (member_id, project_id, name, email, role, hash_token(token), token[:10], now()),
    )
    member = get_member(conn, member_id)
    member["token"] = token  # returned once at creation; only the hash is stored
    return member


def get_member(conn: sqlite3.Connection, member_id: str) -> dict | None:
    row = conn.execute(
        "SELECT id, project_id, name, email, role, token_prefix, created_at FROM member WHERE id = ?",
        (member_id,),
    ).fetchone()
    return _row(row)


def authenticate(conn: sqlite3.Connection, token: str | None) -> dict:
    if not token or not token.strip():
        raise ApiError(401, "unauthorized", "missing bearer token")
    row = conn.execute(
        """
        SELECT m.id, m.project_id, m.name, m.email, m.role, m.token_prefix,
               p.name AS project_name, p.default_branch
        FROM member m JOIN project p ON p.id = m.project_id
        WHERE m.token_hash = ?
        """,
        (hash_token(token.strip()),),
    ).fetchone()
    if row is None:
        raise ApiError(401, "unauthorized", "invalid bearer token")
    return dict(row)


def require_project(actor: dict, project_id: str) -> None:
    # A member of another project gets 404 so project existence is not revealed.
    if actor["project_id"] != project_id:
        raise ApiError(404, "not_found", "project not found")


def require_write(actor: dict) -> None:
    if actor["role"] != "member":
        raise ApiError(403, "forbidden", f"role '{actor['role']}' cannot perform write operations")


# ---------------------------------------------------------------------------
# Specs and immutable versions
# ---------------------------------------------------------------------------


def create_spec(
    conn: sqlite3.Connection, actor: dict, project_id: str, payload: dict
) -> dict:
    require_project(actor, project_id)
    require_write(actor)
    channel = payload.get("channel")
    if channel not in CHANNELS:
        raise ApiError(400, "validation_error", f"'channel' must be one of {list(CHANNELS)}")
    expected_type = "script" if channel == "A" else "dataset"
    spec_type = payload.get("type") or expected_type
    if spec_type != expected_type:
        raise ApiError(
            400,
            "validation_error",
            f"channel {channel} requires spec type '{expected_type}'",
        )
    name = _require_text(payload, "name", max_len=200)
    if conn.execute(
        "SELECT 1 FROM spec WHERE project_id = ? AND name = ?", (project_id, name)
    ).fetchone():
        raise ApiError(409, "conflict", f"spec name already exists: {name}")
    spec_id = new_id("spc")
    conn.execute(
        "INSERT INTO spec (id, project_id, channel, type, name, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (spec_id, project_id, channel, spec_type, name, now()),
    )
    return get_spec(conn, project_id, spec_id)


def list_specs(conn: sqlite3.Connection, actor: dict, project_id: str) -> list[dict]:
    require_project(actor, project_id)
    rows = conn.execute(
        "SELECT id FROM spec WHERE project_id = ? ORDER BY name", (project_id,)
    ).fetchall()
    return [get_spec(conn, project_id, row["id"]) for row in rows]


def get_spec(conn: sqlite3.Connection, project_id: str, spec_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM spec WHERE id = ? AND project_id = ?", (spec_id, project_id)
    ).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "spec not found")
    spec = dict(row)
    versions = conn.execute(
        "SELECT * FROM spec_version WHERE spec_id = ? ORDER BY version", (spec_id,)
    ).fetchall()
    spec["versions"] = [_version_dict(v) for v in versions]
    spec["latest_version"] = spec["versions"][-1]["version"] if spec["versions"] else None
    return spec


def _version_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["params"] = _json_value(data.pop("params_json"))
    data["model_snapshot"] = _json_value(data.pop("model_snapshot_json"))
    return data


def add_spec_version(
    conn: sqlite3.Connection, actor: dict, project_id: str, spec_id: str, payload: dict
) -> dict:
    require_project(actor, project_id)
    require_write(actor)
    spec = conn.execute(
        "SELECT id, channel FROM spec WHERE id = ? AND project_id = ?", (spec_id, project_id)
    ).fetchone()
    if spec is None:
        raise ApiError(404, "not_found", "spec not found")

    channel = spec["channel"]
    params_json = _json_text(payload.get("params"), "params") or "{}"
    content_ref = _optional_text(payload, "content_ref")

    if channel == "A":
        repo_path = _require_text(payload, "repo_path", max_len=1000)
        repo_commit = _require_text(payload, "repo_commit", max_len=200)
        for forbidden in ("dataset_version", "scorer_version", "model_snapshot"):
            if payload.get(forbidden) not in (None, ""):
                raise ApiError(
                    400,
                    "validation_error",
                    f"A-channel spec versions do not accept '{forbidden}'",
                )
        dataset_version = scorer_version = model_snapshot_json = None
    else:
        dataset_version = _require_text(payload, "dataset_version", max_len=200)
        if dataset_version.startswith("dsv_"):
            from . import datasets as dataset_service

            if not dataset_service.dataset_version_exists(conn, project_id, dataset_version):
                raise ApiError(404, "not_found", "dataset version not found in this project")
        scorer_version = _require_text(payload, "scorer_version", max_len=200)
        model_snapshot_json = _json_text(payload.get("model_snapshot"), "model_snapshot", required=True)
        for forbidden in ("repo_path", "repo_commit"):
            if payload.get(forbidden) not in (None, ""):
                raise ApiError(
                    400,
                    "validation_error",
                    f"B-channel spec versions do not accept '{forbidden}'",
                )
        repo_path = repo_commit = None

    try:
        conn.execute("BEGIN IMMEDIATE")
        next_version = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM spec_version WHERE spec_id = ?", (spec_id,)
        ).fetchone()[0]
        version_id = new_id("spv")
        created_at = now()
        conn.execute(
            """
            INSERT INTO spec_version
              (id, spec_id, project_id, channel, version, content_ref, params_json,
               repo_path, repo_commit, dataset_version, scorer_version,
               model_snapshot_json, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version_id,
                spec_id,
                project_id,
                channel,
                next_version,
                content_ref,
                params_json,
                repo_path,
                repo_commit,
                dataset_version,
                scorer_version,
                model_snapshot_json,
                actor["id"],
                created_at,
            ),
        )
        conn.execute("COMMIT")
    except sqlite3.IntegrityError as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise ApiError(409, "conflict", f"could not create spec version: {exc}") from exc
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise

    row = conn.execute("SELECT * FROM spec_version WHERE id = ?", (version_id,)).fetchone()
    return _version_dict(row)


def resolve_spec_version(
    conn: sqlite3.Connection, project_id: str, payload: dict
) -> sqlite3.Row:
    version_id = payload.get("spec_version_id")
    spec_id = payload.get("spec_id")
    version = payload.get("version", payload.get("spec_version"))

    if version_id:
        row = conn.execute(
            "SELECT * FROM spec_version WHERE id = ? AND project_id = ?",
            (str(version_id), project_id),
        ).fetchone()
        if row is None:
            raise ApiError(404, "not_found", "spec_version not found in this project")
        return row

    if not spec_id:
        raise ApiError(
            400,
            "validation_error",
            "an explicit spec version is required: pass 'spec_version_id' or 'spec_id' + 'version'",
        )
    if version is None or version == "":
        raise ApiError(
            400,
            "validation_error",
            "an explicit spec version is required: 'version' is missing next to 'spec_id'",
        )
    try:
        version_int = int(version)
    except (TypeError, ValueError) as exc:
        raise ApiError(400, "validation_error", "'version' must be an integer") from exc
    row = conn.execute(
        """
        SELECT sv.* FROM spec_version sv
        JOIN spec s ON s.id = sv.spec_id
        WHERE sv.spec_id = ? AND sv.version = ? AND sv.project_id = ?
        """,
        (str(spec_id), version_int, project_id),
    ).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "spec version not found in this project")
    return row


# ---------------------------------------------------------------------------
# T02: scheduling settings, idempotency and the execution ledger
# ---------------------------------------------------------------------------

RUN_STATUSES = ("queued", "running", "succeeded", "failed", "cancelled", "timeout")
TERMINAL_RUN_STATUSES = ("succeeded", "failed", "cancelled", "timeout")
RUNNER_STATUSES = ("online", "offline", "unavailable", "draining")
EVENT_SOURCES = ("api", "scheduler", "runner", "timeout", "system")
SCHEDULER_DEFAULTS = {
    "max_concurrency": 2,
    "runner_stale_seconds": 45,
    "default_timeout_seconds": 300,
    "default_queue_timeout_seconds": 600,
}
RUN_TRANSITIONS = {
    None: {"queued"},
    "queued": {"running", "cancelled", "timeout"},
    "running": {"succeeded", "failed", "cancelled", "timeout"},
}


def now_ms() -> str:
    """Millisecond UTC timestamp used by scheduling fields."""

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _plus_seconds(at: str, seconds: int) -> str:
    base = _parse_ts(at) or datetime.now(timezone.utc)
    return (base + timedelta(seconds=seconds)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _seconds_since(value: str | None, at: str | None = None) -> float | None:
    then = _parse_ts(value)
    if then is None:
        return None
    ref = _parse_ts(at) or datetime.now(timezone.utc)
    return round((ref - then).total_seconds(), 3)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _int_field(payload: dict, field: str, default: int, minimum: int, maximum: int) -> int:
    value = payload.get(field)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiError(400, "validation_error", f"'{field}' must be an integer")
    if value < minimum or value > maximum:
        raise ApiError(400, "validation_error", f"'{field}' must be between {minimum} and {maximum}")
    return value


def _bool_field(payload: dict, field: str, default: bool = False) -> bool:
    value = payload.get(field, default)
    if not isinstance(value, bool):
        raise ApiError(400, "validation_error", f"'{field}' must be a boolean")
    return value


def _begin(conn: sqlite3.Connection) -> None:
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")


def _rollback(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        conn.execute("ROLLBACK")


def _validate_scheduler_settings(values: dict, *, partial: bool = False) -> dict:
    unknown = set(values) - set(SCHEDULER_DEFAULTS)
    if unknown:
        raise ApiError(
            400,
            "validation_error",
            f"unknown scheduler setting(s): {sorted(unknown)}",
        )
    limits = {
        "max_concurrency": (1, 64),
        "runner_stale_seconds": (1, 86400),
        "default_timeout_seconds": (1, 86400),
        "default_queue_timeout_seconds": (1, 86400),
    }
    result: dict[str, int] = {}
    for key, (minimum, maximum) in limits.items():
        if key not in values:
            if partial:
                continue
            result[key] = SCHEDULER_DEFAULTS[key]
            continue
        value = values[key]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ApiError(400, "validation_error", f"scheduler setting '{key}' must be an integer")
        if value < minimum or value > maximum:
            raise ApiError(
                400,
                "validation_error",
                f"scheduler setting '{key}' must be between {minimum} and {maximum}",
            )
        result[key] = value
    return result


def get_scheduler_settings(conn: sqlite3.Connection, project_id: str) -> dict:
    row = conn.execute(
        "SELECT scheduler_config_json FROM project WHERE id = ?", (project_id,)
    ).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "project not found")
    raw = _json_value(row["scheduler_config_json"]) or {}
    if not isinstance(raw, dict):
        raise ApiError(500, "invalid_config", "project scheduler_config_json must be an object")
    merged = dict(SCHEDULER_DEFAULTS)
    merged.update({key: value for key, value in raw.items() if key in SCHEDULER_DEFAULTS})
    return _validate_scheduler_settings(merged)


def update_scheduler_settings(
    conn: sqlite3.Connection, actor: dict, project_id: str, payload: dict
) -> dict:
    require_project(actor, project_id)
    require_write(actor)
    current = get_scheduler_settings(conn, project_id)
    updates = _validate_scheduler_settings(payload, partial=True)
    current.update(updates)
    conn.execute(
        "UPDATE project SET scheduler_config_json = ? WHERE id = ?",
        (_canonical_json(current), project_id),
    )
    return current


def _params_digest(
    env: str | None,
    sampling_config_json: str,
    model_snapshot_json: str | None,
    baseline_run_id: str | None,
) -> str:
    material = {
        "env": env,
        "sampling_config": _json_value(sampling_config_json),
        "model_snapshot": _json_value(model_snapshot_json),
        "baseline_run_id": baseline_run_id,
    }
    return hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()


def _derive_idempotency_key(
    project_id: str, commit_sha: str, spec_version_id: str, params_digest: str
) -> str:
    material = {
        "project_id": project_id,
        "commit_sha": commit_sha,
        "spec_version_id": spec_version_id,
        "params_digest": params_digest,
    }
    return hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()


def _event_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["detail"] = _json_value(data.pop("detail_json"))
    return data


def _append_run_event_locked(
    conn: sqlite3.Connection,
    run_id: str,
    from_status: str | None,
    to_status: str,
    *,
    source: str,
    actor: str | None,
    reason: str | None,
    detail: Any = None,
    at: str | None = None,
) -> None:
    if source not in EVENT_SOURCES:
        raise ApiError(500, "invalid_event", f"unknown run event source: {source}")
    created_at = at or now_ms()
    seq = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM run_event WHERE run_id = ?", (run_id,)
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO run_event
          (id, run_id, seq, from_status, to_status, source, actor, reason,
           detail_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            new_id("evt"),
            run_id,
            seq,
            from_status,
            to_status,
            source,
            actor,
            reason,
            _canonical_json({} if detail is None else detail),
            created_at,
        ),
    )


def _apply_run_transition_locked(
    conn: sqlite3.Connection,
    run_id: str,
    to_status: str,
    *,
    source: str,
    actor: str | None,
    reason: str | None,
    detail: Any = None,
    at: str | None = None,
    fields: dict | None = None,
) -> dict:
    at = at or now_ms()
    row = conn.execute("SELECT * FROM run WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "run not found")
    from_status = row["status"]
    if from_status == to_status:
        return dict(row)
    if to_status not in RUN_TRANSITIONS.get(from_status, set()):
        raise ApiError(
            409,
            "invalid_transition",
            f"run {run_id} cannot move from '{from_status}' to '{to_status}'",
        )
    updates = {"status": to_status, "updated_at": at}
    updates.update({key: value for key, value in (fields or {}).items() if key != "status"})
    columns = list(updates)
    sql = "UPDATE run SET " + ", ".join(f"{column} = ?" for column in columns)
    sql += " WHERE id = ? AND status = ?"
    cursor = conn.execute(sql, [updates[column] for column in columns] + [run_id, from_status])
    if cursor.rowcount != 1:
        raise ApiError(409, "conflict", "run status changed concurrently")
    _append_run_event_locked(
        conn,
        run_id,
        from_status,
        to_status,
        source=source,
        actor=actor,
        reason=reason,
        detail=detail,
        at=at,
    )
    return dict(conn.execute("SELECT * FROM run WHERE id = ?", (run_id,)).fetchone())


def _events_for_run(conn: sqlite3.Connection, run_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM run_event WHERE run_id = ? ORDER BY seq", (run_id,)
    ).fetchall()
    return [_event_dict(row) for row in rows]


def list_run_events(
    conn: sqlite3.Connection, actor: dict, project_id: str, run_id: str
) -> list[dict]:
    require_project(actor, project_id)
    if conn.execute(
        "SELECT 1 FROM run WHERE id = ? AND project_id = ?", (run_id, project_id)
    ).fetchone() is None:
        raise ApiError(404, "not_found", "run not found")
    return _events_for_run(conn, run_id)


def _queue_position(conn: sqlite3.Connection, run_row: sqlite3.Row) -> int | None:
    if run_row["status"] != "queued":
        return None
    ahead = conn.execute(
        """
        SELECT COUNT(*) FROM run
        WHERE project_id = ?
          AND status = 'queued'
          AND (priority > ?
               OR (priority = ? AND (created_at < ? OR (created_at = ? AND rowid < ?))))
        """,
        (
            run_row["project_id"],
            run_row["priority"],
            run_row["priority"],
            run_row["created_at"],
            run_row["created_at"],
            run_row["run_rowid"],
        ),
    ).fetchone()[0]
    return int(ahead) + 1


def _queue_depth(conn: sqlite3.Connection, project_id: str) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM run WHERE project_id = ? AND status = 'queued'",
            (project_id,),
        ).fetchone()[0]
    )


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


def create_run(
    conn: sqlite3.Connection, actor: dict, project_id: str, payload: dict
) -> dict:
    require_project(actor, project_id)
    require_write(actor)
    spec_version = resolve_spec_version(conn, project_id, payload)

    commit_sha = _require_text(payload, "commit_sha", max_len=200)
    branch = _optional_text(payload, "branch") or actor["default_branch"]
    trigger = payload.get("trigger") or "manual"
    if trigger not in TRIGGERS:
        raise ApiError(400, "validation_error", f"'trigger' must be one of {list(TRIGGERS)}")
    env = _optional_text(payload, "env")
    sampling_config_json = _json_text(payload.get("sampling_config"), "sampling_config") or "{}"
    model_snapshot_json = _json_text(payload.get("model_snapshot"), "model_snapshot")
    if model_snapshot_json is None:
        model_snapshot_json = spec_version["model_snapshot_json"]

    baseline_run_id = _optional_text(payload, "baseline_run_id")
    if baseline_run_id:
        baseline = conn.execute(
            "SELECT 1 FROM run WHERE id = ? AND project_id = ?", (baseline_run_id, project_id)
        ).fetchone()
        if baseline is None:
            raise ApiError(404, "not_found", "baseline run not found in this project")

    settings = get_scheduler_settings(conn, project_id)
    priority = _int_field(payload, "priority", 50, 0, 100)
    timeout_seconds = _int_field(
        payload, "timeout_seconds", settings["default_timeout_seconds"], 1, 86400
    )
    queue_timeout_seconds = _int_field(
        payload,
        "queue_timeout_seconds",
        settings["default_queue_timeout_seconds"],
        1,
        86400,
    )
    force = _bool_field(payload, "force", False)

    rerun_source_id = payload.get("_rerun_of_run_id")
    if rerun_source_id is not None:
        if not isinstance(rerun_source_id, str) or not rerun_source_id.strip():
            raise ApiError(400, "validation_error", "'_rerun_of_run_id' must be a run id")
        rerun_source_id = rerun_source_id.strip()
        if conn.execute(
            "SELECT 1 FROM run WHERE id = ? AND project_id = ?", (rerun_source_id, project_id)
        ).fetchone() is None:
            raise ApiError(404, "not_found", "source run not found in this project")
        force = True

    params_digest = _params_digest(
        env, sampling_config_json, model_snapshot_json, baseline_run_id
    )
    idempotency_key = _derive_idempotency_key(
        project_id, commit_sha, spec_version["id"], params_digest
    )
    created_at = now_ms()
    queue_deadline_at = _plus_seconds(created_at, queue_timeout_seconds)
    run_id = new_id("run")

    try:
        _begin(conn)
        # A forced rerun points the old run at the new run id before that row
        # exists, so foreign-key checks must wait for COMMIT.
        conn.execute("PRAGMA defer_foreign_keys = ON")
        existing = conn.execute(
            """
            SELECT * FROM run
            WHERE project_id = ? AND idempotency_key = ? AND superseded_by_run_id IS NULL
            ORDER BY created_at DESC, rowid DESC LIMIT 1
            """,
            (project_id, idempotency_key),
        ).fetchone()

        if existing is not None and not force:
            conn.execute("COMMIT")
            data = get_run(conn, project_id, existing["id"])
            data["reused"] = True
            data["idempotency"]["reused"] = True
            data["reuse"] = {
                "reused": True,
                "reason": "idempotency_key_match",
                "idempotency_key": idempotency_key,
                "existing_run_id": existing["id"],
            }
            return data

        superseded_id = None
        if existing is not None:
            # Mark the current active run before inserting the new one so the
            # partial unique index still sees at most one active key holder.
            conn.execute(
                "UPDATE run SET superseded_by_run_id = ?, updated_at = ? WHERE id = ?",
                (run_id, created_at, existing["id"]),
            )
            superseded_id = existing["id"]
            # A superseded queued run must never be executed: cancel it so the
            # queue cannot dispatch duplicate work for the same key.
            if existing["status"] == "queued":
                _apply_run_transition_locked(
                    conn,
                    existing["id"],
                    "cancelled",
                    source="system",
                    actor="scheduler",
                    reason="superseded_by_force_rerun",
                    detail={"superseded_by_run_id": run_id},
                    at=created_at,
                    fields={
                        "finished_at": created_at,
                        "cancel_source": "system",
                        "cancel_reason": "superseded_by_force_rerun",
                        "cancelled_by": "scheduler",
                        "cancelled_at": created_at,
                        "lease_expires_at": None,
                    },
                )

        rerun_of = rerun_source_id or superseded_id
        conn.execute(
            """
            INSERT INTO run
              (id, project_id, spec_version_id, channel, commit_sha, branch, env,
               model_snapshot_json, sampling_config_json, trigger, status,
               baseline_run_id, priority, idempotency_key, params_digest,
               timeout_seconds, queue_timeout_seconds, queue_deadline_at,
               rerun_of_run_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                project_id,
                spec_version["id"],
                spec_version["channel"],
                commit_sha,
                branch,
                env,
                model_snapshot_json,
                sampling_config_json,
                trigger,
                baseline_run_id,
                priority,
                idempotency_key,
                params_digest,
                timeout_seconds,
                queue_timeout_seconds,
                queue_deadline_at,
                rerun_of,
                created_at,
                created_at,
            ),
        )
        _append_run_event_locked(
            conn,
            run_id,
            None,
            "queued",
            source="api",
            actor=f"{actor['name']}/{actor['role']}",
            reason="forced_rerun" if force else "created",
            detail={
                "priority": priority,
                "idempotency_key": idempotency_key,
                "params_digest": params_digest,
                "rerun_of_run_id": rerun_of,
                "superseded_run_id": superseded_id,
                "timeout_seconds": timeout_seconds,
                "queue_timeout_seconds": queue_timeout_seconds,
            },
            at=created_at,
        )
        conn.execute("COMMIT")
    except sqlite3.IntegrityError as exc:
        _rollback(conn)
        if "UNIQUE constraint failed" in str(exc) and "idempotency" in str(exc):
            row = conn.execute(
                """
                SELECT id FROM run
                WHERE project_id = ? AND idempotency_key = ? AND superseded_by_run_id IS NULL
                ORDER BY created_at DESC, rowid DESC LIMIT 1
                """,
                (project_id, idempotency_key),
            ).fetchone()
            if row is not None:
                data = get_run(conn, project_id, row["id"])
                data["reused"] = True
                data["reuse"] = {
                    "reused": True,
                    "reason": "concurrent_idempotency_key_match",
                    "idempotency_key": idempotency_key,
                    "existing_run_id": row["id"],
                }
                return data
        raise ApiError(409, "conflict", f"could not create run: {exc}") from exc
    except Exception:
        _rollback(conn)
        raise

    data = get_run(conn, project_id, run_id)
    data["reused"] = False
    data["idempotency"]["reused"] = False
    data["reuse"] = {
        "reused": False,
        "reason": "forced_rerun" if force else "created",
        "idempotency_key": idempotency_key,
        "superseded_run_id": superseded_id,
    }
    return data


def get_run(conn: sqlite3.Connection, project_id: str, run_id: str) -> dict:
    row = conn.execute(
        """
        SELECT r.rowid AS run_rowid,
               r.*,
               sv.version AS sv_version,
               sv.content_ref AS sv_content_ref,
               sv.params_json AS sv_params_json,
               sv.repo_path AS sv_repo_path,
               sv.repo_commit AS sv_repo_commit,
               sv.dataset_version AS sv_dataset_version,
               sv.scorer_version AS sv_scorer_version,
               sv.model_snapshot_json AS sv_model_snapshot_json,
               sv.created_by AS sv_created_by,
               sv.created_at AS sv_created_at,
               s.id AS spec_id, s.name AS spec_name, s.type AS spec_type,
               s.channel AS spec_channel
        FROM run r
        JOIN spec_version sv ON sv.id = r.spec_version_id
        JOIN spec s ON s.id = sv.spec_id
        WHERE r.id = ? AND r.project_id = ?
        """,
        (run_id, project_id),
    ).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "run not found")
    data = dict(row)
    spec_version = {
        "id": data["spec_version_id"],
        "spec_id": data["spec_id"],
        "spec_name": data["spec_name"],
        "spec_type": data["spec_type"],
        "channel": data["channel"],
        "version": data["sv_version"],
        "content_ref": data["sv_content_ref"],
        "params": _json_value(data["sv_params_json"]),
        "repo_path": data["sv_repo_path"],
        "repo_commit": data["sv_repo_commit"],
        "dataset_version": data["sv_dataset_version"],
        "scorer_version": data["sv_scorer_version"],
        "model_snapshot": _json_value(data["sv_model_snapshot_json"]),
        "created_by": data["sv_created_by"],
        "created_at": data["sv_created_at"],
    }
    for key in [key for key in data if key.startswith("sv_")]:
        data.pop(key)
    for key in ("spec_id", "spec_name", "spec_type", "spec_channel"):
        data.pop(key, None)
    data["model_snapshot"] = _json_value(data.pop("model_snapshot_json"))
    data["sampling_config"] = _json_value(data.pop("sampling_config_json"))
    data["result"] = _json_value(data.pop("result_json"))
    data["spec_version"] = spec_version
    data["items"] = [
        _run_item_dict(item)
        for item in conn.execute(
            "SELECT * FROM run_item WHERE run_id = ? ORDER BY case_ref", (run_id,)
        ).fetchall()
    ]
    data["cancel"] = None
    if data.get("cancel_source") or data.get("cancelled_at"):
        data["cancel"] = {
            "source": data.get("cancel_source"),
            "reason": data.get("cancel_reason"),
            "by": data.get("cancelled_by"),
            "at": data.get("cancelled_at"),
        }
    data["idempotency"] = {
        "key": data.get("idempotency_key"),
        "params_digest": data.get("params_digest"),
        "active": data.get("superseded_by_run_id") is None,
        "superseded_by_run_id": data.get("superseded_by_run_id"),
        "rerun_of_run_id": data.get("rerun_of_run_id"),
        "reused": False,
    }
    data["queue_depth"] = _queue_depth(conn, project_id)
    try:
        data["queue_position"] = _queue_position(conn, row)
    except (KeyError, TypeError):
        data["queue_position"] = None
    data["events"] = _events_for_run(conn, run_id)
    data["assigned_runner"] = None
    if data.get("assigned_runner_id"):
        runner = conn.execute(
            "SELECT id, name, status, capacity, last_heartbeat_at FROM runner WHERE id = ?",
            (data["assigned_runner_id"],),
        ).fetchone()
        if runner is not None:
            data["assigned_runner"] = dict(runner)
    data.pop("run_rowid", None)
    if data["channel"] == "B":
        from . import datasets as dataset_service

        dataset_service.attach_dataset_to_run(conn, data)
    from . import costs

    data["cost"] = costs.run_cost_view(conn, project_id, run_id)
    return data


def list_runs(
    conn: sqlite3.Connection,
    actor: dict,
    project_id: str,
    status: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    require_project(actor, project_id)
    if status is not None and status not in RUN_STATUSES:
        raise ApiError(400, "validation_error", f"'status' must be one of {list(RUN_STATUSES)}")
    params: list[Any] = [project_id]
    where = "r.project_id = ?"
    if status is not None:
        where += " AND r.status = ?"
        params.append(status)
    sql = f"""
        SELECT r.rowid AS run_rowid, r.id, r.channel, r.commit_sha, r.branch, r.trigger,
               r.status, r.created_at, r.started_at, r.finished_at, r.spec_version_id,
               r.priority, r.timeout_seconds, r.queue_timeout_seconds,
               r.queue_deadline_at, r.deadline_at, r.assigned_runner_id,
               r.idempotency_key, r.rerun_of_run_id, r.superseded_by_run_id,
               r.cancel_source, r.cancel_reason, r.cancelled_at,
               sv.version AS spec_version, s.id AS spec_id, s.name AS spec_name
        FROM run r
        JOIN spec_version sv ON sv.id = r.spec_version_id
        JOIN spec s ON s.id = sv.spec_id
        WHERE {where}
        ORDER BY r.created_at DESC, r.rowid DESC
    """
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    queued = conn.execute(
        """
        SELECT id FROM run WHERE project_id = ? AND status = 'queued'
        ORDER BY priority DESC, created_at ASC, rowid ASC
        """,
        (project_id,),
    ).fetchall()
    positions = {row["id"]: index + 1 for index, row in enumerate(queued)}
    result = []
    for row in rows:
        item = dict(row)
        item.pop("run_rowid", None)
        item["queue_position"] = positions.get(item["id"])
        result.append(item)
    return result

def _run_item_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["cost"] = _json_value(data.pop("cost_json"))
    return data


def add_run_item(
    conn: sqlite3.Connection, actor: dict, project_id: str, run_id: str, payload: dict
) -> dict:
    require_project(actor, project_id)
    require_write(actor)
    if conn.execute(
        "SELECT 1 FROM run WHERE id = ? AND project_id = ?", (run_id, project_id)
    ).fetchone() is None:
        raise ApiError(404, "not_found", "run not found")
    case_ref = _require_text(payload, "case_ref", max_len=300)
    state = payload.get("state")
    if state not in RUN_STATES:
        raise ApiError(400, "validation_error", f"'state' must be one of {list(RUN_STATES)}")
    confidence = payload.get("confidence")
    if confidence is not None:
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise ApiError(400, "validation_error", "'confidence' must be between 0 and 1")
    duration_ms = payload.get("duration_ms")
    if duration_ms is not None and (not isinstance(duration_ms, int) or duration_ms < 0):
        raise ApiError(400, "validation_error", "'duration_ms' must be a non-negative integer")
    cost_json = _json_text(payload.get("cost"), "cost")
    item_id = new_id("itm")
    try:
        conn.execute(
            """
            INSERT INTO run_item
              (id, run_id, case_ref, state, confidence, duration_ms, cost_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (item_id, run_id, case_ref, state, confidence, duration_ms, cost_json, now()),
        )
    except sqlite3.IntegrityError as exc:
        raise ApiError(409, "conflict", f"could not create run item: {exc}") from exc
    row = conn.execute("SELECT * FROM run_item WHERE id = ?", (item_id,)).fetchone()
    return _run_item_dict(row)


# ---------------------------------------------------------------------------
# Read helpers used by the HTTP API, UI and CLI
# ---------------------------------------------------------------------------


def list_projects(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT id FROM project ORDER BY name").fetchall()
    return [get_project(conn, row["id"]) for row in rows]


def list_members(conn: sqlite3.Connection, actor: dict, project_id: str) -> list[dict]:
    require_project(actor, project_id)
    rows = conn.execute(
        """
        SELECT id, project_id, name, email, role, token_prefix, created_at
        FROM member WHERE project_id = ? ORDER BY name
        """,
        (project_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def list_spec_versions(conn: sqlite3.Connection, actor: dict, project_id: str, spec_id: str) -> list[dict]:
    require_project(actor, project_id)
    rows = conn.execute(
        "SELECT * FROM spec_version WHERE spec_id = ? AND project_id = ? ORDER BY version",
        (spec_id, project_id),
    ).fetchall()
    if not rows and conn.execute(
        "SELECT 1 FROM spec WHERE id = ? AND project_id = ?", (spec_id, project_id)
    ).fetchone() is None:
        raise ApiError(404, "not_found", "spec not found")
    return [_version_dict(row) for row in rows]


def get_spec_version(conn: sqlite3.Connection, actor: dict, project_id: str, spec_id: str, version: int) -> dict:
    require_project(actor, project_id)
    row = conn.execute(
        """
        SELECT * FROM spec_version
        WHERE spec_id = ? AND project_id = ? AND version = ?
        """,
        (spec_id, project_id, version),
    ).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "spec version not found")
    return _version_dict(row)


def list_run_items(conn: sqlite3.Connection, actor: dict, project_id: str, run_id: str) -> list[dict]:
    require_project(actor, project_id)
    if conn.execute(
        "SELECT 1 FROM run WHERE id = ? AND project_id = ?", (run_id, project_id)
    ).fetchone() is None:
        raise ApiError(404, "not_found", "run not found")
    rows = conn.execute(
        "SELECT * FROM run_item WHERE run_id = ? ORDER BY case_ref", (run_id,)
    ).fetchall()
    return [_run_item_dict(row) for row in rows]


def get_run_item(conn: sqlite3.Connection, actor: dict, project_id: str, run_id: str, item_id: str) -> dict:
    require_project(actor, project_id)
    row = conn.execute(
        """
        SELECT ri.* FROM run_item ri
        JOIN run r ON r.id = ri.run_id
        WHERE ri.id = ? AND ri.run_id = ? AND r.project_id = ?
        """,
        (item_id, run_id, project_id),
    ).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "run item not found")
    return _run_item_dict(row)


# ---------------------------------------------------------------------------
# T02: runners
# ---------------------------------------------------------------------------


def _runner_event_locked(
    conn: sqlite3.Connection,
    runner_id: str,
    kind: str,
    detail: Any = None,
    at: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO runner_event (id, runner_id, kind, detail_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            new_id("rev"),
            runner_id,
            kind,
            _canonical_json({} if detail is None else detail),
            at or now_ms(),
        ),
    )


def _runner_view(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    settings: dict,
    at: str | None = None,
) -> dict:
    at = at or now_ms()
    data = dict(row)
    data.pop("token_hash", None)
    age = _seconds_since(data.get("last_heartbeat_at"), at)
    stale = age is None or age > settings["runner_stale_seconds"]
    persisted = data["status"]
    effective = persisted
    available = False
    if persisted == "online":
        if stale:
            effective = "unavailable"
        else:
            available = True
    elif persisted == "draining":
        effective = "draining"
    data["effective_status"] = effective
    data["available"] = available
    data["heartbeat_age_seconds"] = age
    data["assigned_runs"] = int(
        conn.execute(
            "SELECT COUNT(*) FROM run WHERE assigned_runner_id = ? AND status = 'running'",
            (data["id"],),
        ).fetchone()[0]
    )
    data["capacity_remaining"] = max(0, int(data["capacity"]) - data["assigned_runs"])
    data["labels"] = _json_value(data.pop("labels_json")) or {}
    return data


def register_runner(
    conn: sqlite3.Connection, actor: dict, project_id: str, payload: dict
) -> dict:
    require_project(actor, project_id)
    require_write(actor)
    name = _require_text(payload, "name", max_len=120)
    capacity = _int_field(payload, "capacity", 1, 1, 64)
    labels_json = _json_text(payload.get("labels"), "labels") or "{}"
    if conn.execute(
        "SELECT 1 FROM runner WHERE project_id = ? AND name = ?", (project_id, name)
    ).fetchone():
        raise ApiError(409, "conflict", f"runner name already exists: {name}")
    token = "rt_" + secrets.token_urlsafe(32)
    runner_id = new_id("rnr")
    at = now_ms()
    try:
        _begin(conn)
        conn.execute(
            """
            INSERT INTO runner
              (id, project_id, name, token_hash, token_prefix, labels_json,
               capacity, status, last_heartbeat_at, registered_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'online', ?, ?, ?)
            """,
            (
                runner_id,
                project_id,
                name,
                hash_token(token),
                token[:10],
                labels_json,
                capacity,
                at,
                at,
                at,
            ),
        )
        _runner_event_locked(
            conn,
            runner_id,
            "registered",
            {"capacity": capacity, "actor": f"{actor['name']}/{actor['role']}"},
            at=at,
        )
        conn.execute("COMMIT")
    except sqlite3.IntegrityError as exc:
        _rollback(conn)
        raise ApiError(409, "conflict", f"could not register runner: {exc}") from exc
    except Exception:
        _rollback(conn)
        raise
    row = conn.execute("SELECT * FROM runner WHERE id = ?", (runner_id,)).fetchone()
    view = _runner_view(conn, row, get_scheduler_settings(conn, project_id), at)
    view["token"] = token  # returned once; only the hash is stored
    return view


def list_runners(
    conn: sqlite3.Connection, actor: dict, project_id: str
) -> list[dict]:
    require_project(actor, project_id)
    settings = get_scheduler_settings(conn, project_id)
    at = now_ms()
    _mark_stale_runners_locked(conn, project_id, at)
    rows = conn.execute(
        "SELECT * FROM runner WHERE project_id = ? ORDER BY name", (project_id,)
    ).fetchall()
    return [_runner_view(conn, row, settings, at) for row in rows]


def _mark_stale_runners_locked(
    conn: sqlite3.Connection, project_id: str | None, at: str | None = None
) -> list[dict]:
    at = at or now_ms()
    params: list[Any] = []
    where = "status = 'online'"
    if project_id is not None:
        where += " AND project_id = ?"
        params.append(project_id)
    rows = conn.execute(f"SELECT * FROM runner WHERE {where}", params).fetchall()
    settings_cache: dict[str, dict] = {}
    stale_rows: list[dict] = []
    for row in rows:
        project = row["project_id"]
        if project not in settings_cache:
            settings_cache[project] = get_scheduler_settings(conn, project)
        age = _seconds_since(row["last_heartbeat_at"], at)
        if age is None or age > settings_cache[project]["runner_stale_seconds"]:
            conn.execute(
                "UPDATE runner SET status = 'unavailable', updated_at = ? WHERE id = ? AND status = 'online'",
                (at, row["id"]),
            )
            _runner_event_locked(
                conn,
                row["id"],
                "went_unavailable",
                {
                    "last_heartbeat_at": row["last_heartbeat_at"],
                    "heartbeat_age_seconds": age,
                    "stale_after_seconds": settings_cache[project]["runner_stale_seconds"],
                },
                at=at,
            )
            stale_rows.append(
                {
                    "runner_id": row["id"],
                    "name": row["name"],
                    "last_heartbeat_at": row["last_heartbeat_at"],
                    "heartbeat_age_seconds": age,
                }
            )
    return stale_rows


def authenticate_runner(conn: sqlite3.Connection, token: str | None) -> dict:
    if not token or not token.strip():
        raise ApiError(401, "unauthorized", "missing runner token")
    row = conn.execute(
        "SELECT * FROM runner WHERE token_hash = ?", (hash_token(token.strip()),)
    ).fetchone()
    if row is None:
        raise ApiError(401, "unauthorized", "invalid runner token")
    return dict(row)


def runner_heartbeat(
    conn: sqlite3.Connection, token: str | None, payload: dict | None = None
) -> dict:
    payload = payload or {}
    runner = authenticate_runner(conn, token)
    if runner["status"] == "offline":
        raise ApiError(403, "runner_offline", "runner has been revoked")
    requested = payload.get("status") or "online"
    if requested not in ("online", "draining"):
        raise ApiError(400, "validation_error", "'status' must be 'online' or 'draining'")
    at = now_ms()
    previous = runner["status"]
    _begin(conn)
    try:
        conn.execute(
            "UPDATE runner SET status = ?, last_heartbeat_at = ?, updated_at = ? WHERE id = ?",
            (requested, at, at, runner["id"]),
        )
        if previous == "unavailable" and requested == "online":
            _runner_event_locked(
                conn,
                runner["id"],
                "recovered",
                {"previous_status": previous, "last_heartbeat_at": runner["last_heartbeat_at"]},
                at=at,
            )
        else:
            _runner_event_locked(
                conn,
                runner["id"],
                "heartbeat",
                {"status": requested, "previous_status": previous},
                at=at,
            )
        # A live runner heartbeat also renews the lease of every run it holds;
        # otherwise a long task with only global heartbeats would be reaped by
        # the lease-expiry guard before its hard timeout.
        settings = get_scheduler_settings(conn, runner["project_id"])
        lease_expires_at = _plus_seconds(at, max(settings["runner_stale_seconds"] * 2, 60))
        conn.execute(
            """
            UPDATE run SET lease_expires_at = ?, updated_at = ?
            WHERE assigned_runner_id = ? AND status = 'running'
            """,
            (lease_expires_at, at, runner["id"]),
        )
        conn.execute("COMMIT")
    except Exception:
        _rollback(conn)
        raise
    row = conn.execute("SELECT * FROM runner WHERE id = ?", (runner["id"],)).fetchone()
    return _runner_view(conn, row, get_scheduler_settings(conn, runner["project_id"]), at)


def revoke_runner(
    conn: sqlite3.Connection,
    actor: dict,
    project_id: str,
    runner_id: str,
    payload: dict | None = None,
) -> dict:
    require_project(actor, project_id)
    require_write(actor)
    row = conn.execute(
        "SELECT * FROM runner WHERE id = ? AND project_id = ?", (runner_id, project_id)
    ).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "runner not found")
    at = now_ms()
    reason = _optional_text(payload or {}, "reason")
    _begin(conn)
    try:
        conn.execute(
            "UPDATE runner SET status = 'offline', revoked_at = ?, updated_at = ? WHERE id = ?",
            (at, at, runner_id),
        )
        _runner_event_locked(
            conn,
            runner_id,
            "revoked",
            {"reason": reason, "actor": f"{actor['name']}/{actor['role']}"},
            at=at,
        )
        conn.execute("COMMIT")
    except Exception:
        _rollback(conn)
        raise
    row = conn.execute("SELECT * FROM runner WHERE id = ?", (runner_id,)).fetchone()
    return _runner_view(conn, row, get_scheduler_settings(conn, project_id), at)


def _runner_events_for(conn: sqlite3.Connection, runner_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT * FROM runner_event WHERE runner_id = ?
        ORDER BY created_at DESC, rowid DESC LIMIT 50
        """,
        (runner_id,),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["detail"] = _json_value(item.pop("detail_json"))
        result.append(item)
    return result


def list_runner_events(
    conn: sqlite3.Connection, actor: dict, project_id: str, runner_id: str
) -> list[dict]:
    require_project(actor, project_id)
    if conn.execute(
        "SELECT 1 FROM runner WHERE id = ? AND project_id = ?", (runner_id, project_id)
    ).fetchone() is None:
        raise ApiError(404, "not_found", "runner not found")
    return _runner_events_for(conn, runner_id)


def get_runner(
    conn: sqlite3.Connection, actor: dict, project_id: str, runner_id: str
) -> dict:
    require_project(actor, project_id)
    settings = get_scheduler_settings(conn, project_id)
    at = now_ms()
    _mark_stale_runners_locked(conn, project_id, at)
    row = conn.execute(
        "SELECT * FROM runner WHERE id = ? AND project_id = ?", (runner_id, project_id)
    ).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "runner not found")
    data = _runner_view(conn, row, settings, at)
    data["events"] = _runner_events_for(conn, runner_id)
    return data


def runner_self(conn: sqlite3.Connection, token: str | None) -> dict:
    runner = authenticate_runner(conn, token)
    row = conn.execute("SELECT * FROM runner WHERE id = ?", (runner["id"],)).fetchone()
    return _runner_view(
        conn, row, get_scheduler_settings(conn, runner["project_id"]), now_ms()
    )


# ---------------------------------------------------------------------------
# T02: scheduler maintenance, queue observation and runner claims
# ---------------------------------------------------------------------------


def _maintenance_locked(
    conn: sqlite3.Connection, project_id: str | None, at: str
) -> list[dict]:
    """Expire stale runners and overdue runs. Caller owns the transaction."""

    expired: list[dict] = []
    for stale in _mark_stale_runners_locked(conn, project_id, at):
        expired.append({"kind": "runner_unavailable", **stale})

    project_clause = ""
    params: list[Any] = []
    if project_id is not None:
        project_clause = " AND project_id = ?"
        params.append(project_id)

    queued = conn.execute(
        f"""
        SELECT * FROM run
        WHERE status = 'queued' AND queue_deadline_at IS NOT NULL
          AND queue_deadline_at <= ?{project_clause}
        ORDER BY queue_deadline_at, rowid
        """,
        [at] + params,
    ).fetchall()
    for row in queued:
        updated = _apply_run_transition_locked(
            conn,
            row["id"],
            "timeout",
            source="timeout",
            actor="scheduler",
            reason="queue_timeout",
            detail={
                "queue_deadline_at": row["queue_deadline_at"],
                "queue_wait_seconds": _seconds_since(row["created_at"], at),
                "queue_timeout_seconds": row["queue_timeout_seconds"],
            },
            at=at,
            fields={"finished_at": at},
        )
        expired.append(
            {
                "kind": "run_timeout",
                "run_id": row["id"],
                "reason": "queue_timeout",
                "status": updated["status"],
            }
        )

    hard = conn.execute(
        f"""
        SELECT * FROM run
        WHERE status = 'running' AND deadline_at IS NOT NULL AND deadline_at <= ?{project_clause}
        ORDER BY deadline_at, rowid
        """,
        [at] + params,
    ).fetchall()
    for row in hard:
        updated = _apply_run_transition_locked(
            conn,
            row["id"],
            "timeout",
            source="timeout",
            actor="scheduler",
            reason="hard_timeout",
            detail={
                "deadline_at": row["deadline_at"],
                "started_at": row["started_at"],
                "timeout_seconds": row["timeout_seconds"],
                "assigned_runner_id": row["assigned_runner_id"],
            },
            at=at,
            fields={"finished_at": at},
        )
        expired.append(
            {
                "kind": "run_timeout",
                "run_id": row["id"],
                "reason": "hard_timeout",
                "status": updated["status"],
            }
        )

    lease = conn.execute(
        f"""
        SELECT * FROM run
        WHERE status = 'running' AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?
          AND (deadline_at IS NULL OR deadline_at > ?){project_clause}
        ORDER BY lease_expires_at, rowid
        """,
        [at, at] + params,
    ).fetchall()
    for row in lease:
        updated = _apply_run_transition_locked(
            conn,
            row["id"],
            "timeout",
            source="timeout",
            actor="scheduler",
            reason="runner_lease_expired",
            detail={
                "lease_expires_at": row["lease_expires_at"],
                "assigned_runner_id": row["assigned_runner_id"],
            },
            at=at,
            fields={"finished_at": at},
        )
        expired.append(
            {
                "kind": "run_timeout",
                "run_id": row["id"],
                "reason": "runner_lease_expired",
                "status": updated["status"],
            }
        )
    return expired


def scheduler_maintenance(conn: sqlite3.Connection, project_id: str | None = None) -> dict:
    at = now_ms()
    _begin(conn)
    try:
        expired = _maintenance_locked(conn, project_id, at)
        conn.execute("COMMIT")
    except Exception:
        _rollback(conn)
        raise
    return {"checkedAt": at, "expired": expired}


def _scheduler_view_locked(conn: sqlite3.Connection, project_id: str, at: str) -> dict:
    settings = get_scheduler_settings(conn, project_id)
    queued_rows = conn.execute(
        """
        SELECT id, priority, created_at, queue_deadline_at, timeout_seconds,
               queue_timeout_seconds, trigger, commit_sha, spec_version_id
        FROM run WHERE project_id = ? AND status = 'queued'
        ORDER BY priority DESC, created_at ASC, rowid ASC
        """,
        (project_id,),
    ).fetchall()
    running_rows = conn.execute(
        """
        SELECT id, priority, started_at, deadline_at, lease_expires_at,
               assigned_runner_id, trigger, commit_sha, spec_version_id
        FROM run WHERE project_id = ? AND status = 'running'
        ORDER BY started_at, rowid
        """,
        (project_id,),
    ).fetchall()
    runner_rows = conn.execute(
        "SELECT * FROM runner WHERE project_id = ? ORDER BY name", (project_id,)
    ).fetchall()
    runners = [_runner_view(conn, row, settings, at) for row in runner_rows]
    queue = [
        {
            "run_id": row["id"],
            "position": index + 1,
            "priority": row["priority"],
            "created_at": row["created_at"],
            "queue_deadline_at": row["queue_deadline_at"],
            "spec_version_id": row["spec_version_id"],
            "trigger": row["trigger"],
        }
        for index, row in enumerate(queued_rows)
    ]
    running = [
        {
            "run_id": row["id"],
            "priority": row["priority"],
            "started_at": row["started_at"],
            "deadline_at": row["deadline_at"],
            "lease_expires_at": row["lease_expires_at"],
            "runner_id": row["assigned_runner_id"],
        }
        for row in running_rows
    ]
    running_count = len(running)
    available_runners = sum(1 for runner in runners if runner["available"])
    return {
        "project_id": project_id,
        "checked_at": at,
        "settings": settings,
        "max_concurrency": settings["max_concurrency"],
        "running_count": running_count,
        "queued_count": len(queue),
        "queue_depth": len(queue),
        "available_capacity": max(0, settings["max_concurrency"] - running_count),
        "priority_order": queue,
        "queue": queue,
        "running": running,
        "runners": {
            "total": len(runners),
            "available": available_runners,
            "unavailable": len(runners) - available_runners,
            "items": runners,
        },
    }


def scheduler_status(conn: sqlite3.Connection, actor: dict, project_id: str) -> dict:
    require_project(actor, project_id)
    scheduler_maintenance(conn, project_id)
    return _scheduler_view_locked(conn, project_id, now_ms())


def scheduler_tick(conn: sqlite3.Connection, actor: dict, project_id: str) -> dict:
    require_project(actor, project_id)
    require_write(actor)
    maintenance = scheduler_maintenance(conn, project_id)
    return {
        "maintenance": maintenance,
        "scheduler": _scheduler_view_locked(conn, project_id, now_ms()),
    }


def claim_run(
    conn: sqlite3.Connection, token: str | None, payload: dict | None = None
) -> dict:
    payload = payload or {}
    runner = authenticate_runner(conn, token)
    if runner["status"] == "offline":
        raise ApiError(403, "runner_offline", "runner has been revoked")
    if runner["status"] == "draining":
        raise ApiError(409, "runner_draining", "runner is draining and does not accept new runs")
    project_id = runner["project_id"]
    settings = get_scheduler_settings(conn, project_id)
    at = now_ms()
    claimed_id = None
    reason = "no_work"
    expired: list[dict] = []
    _begin(conn)
    try:
        previous = runner["status"]
        conn.execute(
            "UPDATE runner SET status = 'online', last_heartbeat_at = ?, updated_at = ? WHERE id = ?",
            (at, at, runner["id"]),
        )
        if previous != "online":
            _runner_event_locked(
                conn,
                runner["id"],
                "recovered" if previous == "unavailable" else "heartbeat",
                {"previous_status": previous, "reason": "claim"},
                at=at,
            )
        expired = _maintenance_locked(conn, project_id, at)

        assigned = int(
            conn.execute(
                "SELECT COUNT(*) FROM run WHERE assigned_runner_id = ? AND status = 'running'",
                (runner["id"],),
            ).fetchone()[0]
        )
        running_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM run WHERE project_id = ? AND status = 'running'",
                (project_id,),
            ).fetchone()[0]
        )
        if assigned >= int(runner["capacity"]):
            reason = "runner_capacity_full"
        elif running_count >= settings["max_concurrency"]:
            reason = "concurrency_limit_reached"
        else:
            candidates = conn.execute(
                """
                SELECT * FROM run WHERE project_id = ? AND status = 'queued'
                ORDER BY priority DESC, created_at ASC, rowid ASC
                """,
                (project_id,),
            ).fetchall()
            lease_seconds = max(settings["runner_stale_seconds"] * 2, 60)
            for candidate in candidates:
                deadline_at = _plus_seconds(at, candidate["timeout_seconds"])
                lease_expires_at = _plus_seconds(at, lease_seconds)
                cursor = conn.execute(
                    """
                    UPDATE run
                    SET status = 'running', assigned_runner_id = ?, started_at = ?,
                        deadline_at = ?, lease_expires_at = ?, updated_at = ?
                    WHERE id = ? AND status = 'queued'
                    """,
                    (runner["id"], at, deadline_at, lease_expires_at, at, candidate["id"]),
                )
                if cursor.rowcount != 1:
                    continue
                _append_run_event_locked(
                    conn,
                    candidate["id"],
                    "queued",
                    "running",
                    source="scheduler",
                    actor=runner["name"],
                    reason="claimed",
                    detail={
                        "runner_id": runner["id"],
                        "runner_name": runner["name"],
                        "priority": candidate["priority"],
                        "queue_wait_seconds": _seconds_since(candidate["created_at"], at),
                        "deadline_at": deadline_at,
                        "lease_expires_at": lease_expires_at,
                    },
                    at=at,
                )
                claimed_id = candidate["id"]
                reason = "claimed"
                break
            if claimed_id is None:
                reason = "no_work"

        scheduler = _scheduler_view_locked(conn, project_id, at)
        conn.execute("COMMIT")
    except Exception:
        _rollback(conn)
        raise

    data = None
    if claimed_id is not None:
        data = get_run(conn, project_id, claimed_id)
        data["claim"] = {
            "runner_id": runner["id"],
            "runner_name": runner["name"],
            "claimed_at": at,
            "lease_expires_at": data.get("lease_expires_at"),
        }
    return {
        "data": data,
        "reason": reason,
        "scheduler": scheduler,
        "expired": expired,
    }


def runner_run_heartbeat(
    conn: sqlite3.Connection, token: str | None, run_id: str
) -> dict:
    runner = authenticate_runner(conn, token)
    if runner["status"] == "offline":
        raise ApiError(403, "runner_offline", "runner has been revoked")
    row = conn.execute("SELECT * FROM run WHERE id = ?", (run_id,)).fetchone()
    if row is None or row["project_id"] != runner["project_id"]:
        raise ApiError(404, "not_found", "run not found")
    if row["assigned_runner_id"] != runner["id"]:
        raise ApiError(409, "not_owner", "run is not assigned to this runner")
    if row["status"] != "running":
        raise ApiError(409, "invalid_transition", f"run is '{row['status']}', not running")
    settings = get_scheduler_settings(conn, runner["project_id"])
    at = now_ms()
    lease_expires_at = _plus_seconds(at, max(settings["runner_stale_seconds"] * 2, 60))
    _begin(conn)
    try:
        conn.execute(
            "UPDATE run SET lease_expires_at = ?, updated_at = ? WHERE id = ? AND status = 'running'",
            (lease_expires_at, at, run_id),
        )
        conn.execute(
            "UPDATE runner SET status = 'online', last_heartbeat_at = ?, updated_at = ? WHERE id = ?",
            (at, at, runner["id"]),
        )
        conn.execute("COMMIT")
    except Exception:
        _rollback(conn)
        raise
    return {
        "run": get_run(conn, runner["project_id"], run_id),
        "lease_expires_at": lease_expires_at,
        "heartbeat_at": at,
    }


def complete_run(
    conn: sqlite3.Connection,
    token: str | None,
    run_id: str,
    payload: dict | None = None,
) -> dict:
    payload = payload or {}
    runner = authenticate_runner(conn, token)
    if runner["status"] == "offline":
        raise ApiError(403, "runner_offline", "runner has been revoked")
    target = payload.get("status")
    if target not in ("succeeded", "failed", "timeout"):
        raise ApiError(
            400,
            "validation_error",
            "'status' must be one of ['succeeded', 'failed', 'timeout']",
        )
    row = conn.execute("SELECT * FROM run WHERE id = ?", (run_id,)).fetchone()
    if row is None or row["project_id"] != runner["project_id"]:
        raise ApiError(404, "not_found", "run not found")
    if row["assigned_runner_id"] != runner["id"]:
        raise ApiError(409, "not_owner", "run is not assigned to this runner")
    summary = _optional_text(payload, "summary")
    result_payload = {"summary": summary, "result": payload.get("result")}
    result_json = _canonical_json(result_payload)
    at = now_ms()
    duration_seconds = _seconds_since(row["started_at"], at)
    _begin(conn)
    try:
        from . import costs

        reported_gpu = payload.get("gpu_seconds")
        if reported_gpu is not None and (
            isinstance(reported_gpu, bool) or not isinstance(reported_gpu, (int, float))
        ):
            raise ApiError(400, "validation_error", "'gpu_seconds' must be a number")
        costs.record_runner_usage(
            conn,
            runner["project_id"],
            run_id,
            gpu_seconds=float(reported_gpu) if reported_gpu is not None else None,
            duration_seconds=duration_seconds,
            runner_id=runner["id"],
            at=at,
        )
        quota = costs.enforce_run_quota(
            conn,
            runner["project_id"],
            run_id,
            actor=f"runner:{runner['name']}",
            stage="runner_complete",
        )
        if quota.get("terminated"):
            target = "cancelled"
            result_json = _canonical_json(
                {
                    "summary": summary,
                    "result": payload.get("result"),
                    "quota_termination": quota.get("termination"),
                }
            )
            transition_reason = "quota_exceeded"
        else:
            transition_reason = _optional_text(payload, "reason") or "runner_completed"
        _apply_run_transition_locked(
            conn,
            run_id,
            target,
            source="runner",
            actor=runner["name"],
            reason=transition_reason,
            detail={
                "runner_id": runner["id"],
                "duration_seconds": duration_seconds,
                "summary": summary,
                "quota": quota if quota.get("exceeded") else None,
            },
            at=at,
            fields={"finished_at": at, "result_json": result_json},
        )
        conn.execute(
            "UPDATE runner SET last_heartbeat_at = ?, updated_at = ? WHERE id = ?",
            (at, at, runner["id"]),
        )
        conn.execute("COMMIT")
    except Exception:
        _rollback(conn)
        raise
    return get_run(conn, runner["project_id"], run_id)


def cancel_run(
    conn: sqlite3.Connection,
    actor: dict,
    project_id: str,
    run_id: str,
    payload: dict | None = None,
    source: str = "api",
) -> dict:
    require_project(actor, project_id)
    require_write(actor)
    payload = payload or {}
    reason = _optional_text(payload, "reason")
    row = conn.execute(
        "SELECT * FROM run WHERE id = ? AND project_id = ?", (run_id, project_id)
    ).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "run not found")
    if row["status"] == "cancelled":
        data = get_run(conn, project_id, run_id)
        data["cancel_result"] = "already_cancelled"
        return data
    if row["status"] in TERMINAL_RUN_STATUSES:
        raise ApiError(409, "invalid_transition", f"run is already '{row['status']}'")
    at = now_ms()
    _begin(conn)
    try:
        _apply_run_transition_locked(
            conn,
            run_id,
            "cancelled",
            source="api",
            actor=f"{actor['name']}/{actor['role']}",
            reason=reason or "cancelled by member",
            detail={
                "previous_status": row["status"],
                "cancel_source": source,
                "actor_id": actor["id"],
                "assigned_runner_id": row["assigned_runner_id"],
            },
            at=at,
            fields={
                "finished_at": at,
                "cancel_source": source,
                "cancel_reason": reason,
                "cancelled_by": actor["name"],
                "cancelled_at": at,
                "lease_expires_at": None,
            },
        )
        conn.execute("COMMIT")
    except Exception:
        _rollback(conn)
        raise
    data = get_run(conn, project_id, run_id)
    data["cancel_result"] = "cancelled"
    data["cancel"]["previous_status"] = row["status"]
    return data


def rerun_run(
    conn: sqlite3.Connection,
    actor: dict,
    project_id: str,
    run_id: str,
    payload: dict | None = None,
) -> dict:
    require_project(actor, project_id)
    require_write(actor)
    payload = payload or {}
    source = conn.execute(
        "SELECT * FROM run WHERE id = ? AND project_id = ?", (run_id, project_id)
    ).fetchone()
    if source is None:
        raise ApiError(404, "not_found", "run not found")
    request = {
        "spec_version_id": source["spec_version_id"],
        "commit_sha": source["commit_sha"],
        "branch": source["branch"],
        "env": source["env"],
        "trigger": payload.get("trigger") or source["trigger"],
        "sampling_config": _json_value(source["sampling_config_json"]),
        "model_snapshot": _json_value(source["model_snapshot_json"]),
        "baseline_run_id": source["baseline_run_id"],
        "priority": payload.get("priority", source["priority"]),
        "timeout_seconds": payload.get("timeout_seconds", source["timeout_seconds"]),
        "queue_timeout_seconds": payload.get(
            "queue_timeout_seconds", source["queue_timeout_seconds"]
        ),
        "force": True,
        "_rerun_of_run_id": source["id"],
    }
    data = create_run(conn, actor, project_id, request)
    data["rerun_of"] = source["id"]
    return data
