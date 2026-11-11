"""Business rules: project isolation, two-tier permissions and Spec versioning."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from datetime import datetime, timezone
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
        "INSERT INTO project (id, name, default_branch, quota_config, created_at) VALUES (?, ?, ?, ?, ?)",
        (
            project_id,
            name,
            default_branch,
            _json_text({} if quota_config is None else quota_config, "quota_config") or "{}",
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

    baseline_run_id = payload.get("baseline_run_id")
    if baseline_run_id:
        baseline = conn.execute(
            "SELECT 1 FROM run WHERE id = ? AND project_id = ?", (baseline_run_id, project_id)
        ).fetchone()
        if baseline is None:
            raise ApiError(404, "not_found", "baseline run not found in this project")

    run_id = new_id("run")
    try:
        conn.execute(
            """
            INSERT INTO run
              (id, project_id, spec_version_id, channel, commit_sha, branch, env,
               model_snapshot_json, sampling_config_json, trigger, status,
               baseline_run_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
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
                now(),
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise ApiError(409, "conflict", f"could not create run: {exc}") from exc
    return get_run(conn, project_id, run_id)


def get_run(conn: sqlite3.Connection, project_id: str, run_id: str) -> dict:
    row = conn.execute(
        """
        SELECT r.*,
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
    data.pop("spec_id", None)
    data.pop("spec_name", None)
    data.pop("spec_type", None)
    data.pop("spec_channel", None)
    data["model_snapshot"] = _json_value(data.pop("model_snapshot_json"))
    data["sampling_config"] = _json_value(data.pop("sampling_config_json"))
    data["spec_version"] = spec_version
    data["items"] = [
        _run_item_dict(item)
        for item in conn.execute(
            "SELECT * FROM run_item WHERE run_id = ? ORDER BY case_ref", (run_id,)
        ).fetchall()
    ]
    if data["channel"] == "B":
        from . import datasets as dataset_service

        dataset_service.attach_dataset_to_run(conn, data)
    return data


def list_runs(conn: sqlite3.Connection, actor: dict, project_id: str) -> list[dict]:
    require_project(actor, project_id)
    rows = conn.execute(
        """
        SELECT r.id, r.channel, r.commit_sha, r.branch, r.trigger, r.status,
               r.created_at, r.spec_version_id,
               sv.version AS spec_version, s.id AS spec_id, s.name AS spec_name
        FROM run r
        JOIN spec_version sv ON sv.id = r.spec_version_id
        JOIN spec s ON s.id = sv.spec_id
        WHERE r.project_id = ?
        ORDER BY r.created_at DESC, r.id
        """,
        (project_id,),
    ).fetchall()
    return [dict(row) for row in rows]


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
