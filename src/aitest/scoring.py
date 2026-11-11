"""T09: scorer persistence, versioning and Run scoring.

Scorer configurations and rubrics are immutable ``scorer_version`` rows.
Profiles group several scorers behind a combinator and are versioned the same
way. Scoring a B-channel Run resolves the configuration bound to the Run (or
the dataset/sample binding), executes each scorer once, records one
``run_item`` + ``verdict`` per sample, and never aborts the Run because one
scorer or model endpoint failed.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from typing import Any

from . import datasets
from .scorers import (
    COMBINATORS,
    MISSING,
    SCORER_TYPES,
    CombinatorConfigError,
    ConfigError,
    ScoreResult,
    build_scorer,
    canonical_json,
    combine,
    error_result,
    normalize_members,
    validate_config,
    validate_profile_config,
)
from .service import ApiError, new_id, now, require_project, require_write


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _json_value(text: str | None) -> Any:
    return None if text is None else json.loads(text)


def _json_text(value: Any) -> str:
    return canonical_json(value)


def _require_text(payload: dict, field: str, *, max_len: int = 200) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ApiError(400, "validation_error", f"'{field}' is required and must be a non-empty string")
    value = value.strip()
    if len(value) > max_len:
        raise ApiError(400, "validation_error", f"'{field}' must be at most {max_len} characters")
    return value


def _optional_text(payload: dict, field: str) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ApiError(400, "validation_error", f"'{field}' must be a string")
    return value.strip() or None


def _payload_json(payload: dict, key: str, *, default: Any = None) -> Any:
    if key not in payload or payload[key] is None:
        return default
    value = payload[key]
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ApiError(400, "validation_error", f"'{key}' must be valid JSON: {exc.msg}") from exc
    return value


def _frozen_digest(scorer_type: str, config: dict, rubric_text: str | None) -> str:
    return _sha256(canonical_json({"type": scorer_type, "config": config, "rubric": rubric_text}))


def _profile_digest(combinator: str, config: dict, members: list[dict]) -> str:
    return _sha256(canonical_json({"combinator": combinator, "config": config, "members": members}))


def _scorer_version_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["config"] = _json_value(data.pop("config_json"))
    data["rubric"] = data.pop("rubric_text")
    return data


def _profile_version_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["config"] = _json_value(data.pop("config_json"))
    data["members"] = _json_value(data.pop("members_json"))
    return data


def _scorer_version_row(conn: sqlite3.Connection, project_id: str, version_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM scorer_version WHERE id = ? AND project_id = ?", (version_id, project_id)
    ).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "scorer version not found in this project")
    return row


def _latest_scorer_version(conn: sqlite3.Connection, project_id: str, scorer_id: str) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT * FROM scorer_version
        WHERE scorer_id = ? AND project_id = ?
        ORDER BY version DESC LIMIT 1
        """,
        (scorer_id, project_id),
    ).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "scorer has no versions")
    return row


def _validate_config_or_400(scorer_type: str, config: Any) -> dict:
    try:
        return validate_config(scorer_type, config)
    except ConfigError as exc:
        raise ApiError(400, "validation_error", str(exc)) from exc


def _rubric_text(payload: dict, *, required: bool) -> str | None:
    value = payload.get("rubric", payload.get("rubric_text"))
    if value is None:
        if required:
            raise ApiError(400, "validation_error", "'rubric' is required for llm_judge")
        return None
    if not isinstance(value, str):
        raise ApiError(400, "validation_error", "'rubric' must be a string")
    value = value.strip()
    if not value:
        if required:
            raise ApiError(400, "validation_error", "'rubric' must not be empty for llm_judge")
        return None
    return value


def create_scorer(conn: sqlite3.Connection, actor: dict, project_id: str, payload: dict) -> dict:
    require_project(actor, project_id)
    require_write(actor)
    name = _require_text(payload, "name")
    scorer_type = payload.get("type")
    if scorer_type not in SCORER_TYPES:
        raise ApiError(400, "validation_error", f"'type' must be one of {list(SCORER_TYPES)}")
    config = _validate_config_or_400(scorer_type, _payload_json(payload, "config", default={}))
    rubric = _rubric_text(payload, required=scorer_type == "llm_judge")
    rubric_digest = _sha256(rubric) if rubric is not None else None
    frozen = _frozen_digest(scorer_type, config, rubric)
    description = _optional_text(payload, "description")
    if conn.execute(
        "SELECT 1 FROM scorer WHERE project_id = ? AND name = ?", (project_id, name)
    ).fetchone():
        raise ApiError(409, "conflict", f"scorer name already exists: {name}")
    scorer_id = new_id("scr")
    version_id = new_id("scv")
    timestamp = now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO scorer (id, project_id, name, description, created_at) VALUES (?, ?, ?, ?, ?)",
            (scorer_id, project_id, name, description, timestamp),
        )
        conn.execute(
            """
            INSERT INTO scorer_version
              (id, scorer_id, project_id, version, type, config_json, rubric_text,
               rubric_digest, frozen_digest, created_by, created_at)
            VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version_id,
                scorer_id,
                project_id,
                scorer_type,
                _json_text(config),
                rubric,
                rubric_digest,
                frozen,
                actor["id"],
                timestamp,
            ),
        )
        conn.execute("COMMIT")
    except sqlite3.IntegrityError as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise ApiError(409, "conflict", f"could not create scorer: {exc}") from exc
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return get_scorer(conn, actor, project_id, scorer_id)


def list_scorers(conn: sqlite3.Connection, actor: dict, project_id: str) -> list[dict]:
    require_project(actor, project_id)
    rows = conn.execute(
        "SELECT id FROM scorer WHERE project_id = ? ORDER BY name", (project_id,)
    ).fetchall()
    return [get_scorer(conn, actor, project_id, row["id"]) for row in rows]


def get_scorer(conn: sqlite3.Connection, actor: dict, project_id: str, scorer_id: str) -> dict:
    require_project(actor, project_id)
    row = conn.execute(
        "SELECT * FROM scorer WHERE id = ? AND project_id = ?", (scorer_id, project_id)
    ).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "scorer not found")
    data = dict(row)
    versions = conn.execute(
        "SELECT * FROM scorer_version WHERE scorer_id = ? AND project_id = ? ORDER BY version",
        (scorer_id, project_id),
    ).fetchall()
    data["versions"] = [_scorer_version_dict(version) for version in versions]
    data["latest_version"] = data["versions"][-1] if data["versions"] else None
    return data


def add_scorer_version(
    conn: sqlite3.Connection, actor: dict, project_id: str, scorer_id: str, payload: dict
) -> dict:
    require_project(actor, project_id)
    require_write(actor)
    scorer = conn.execute(
        "SELECT * FROM scorer WHERE id = ? AND project_id = ?", (scorer_id, project_id)
    ).fetchone()
    if scorer is None:
        raise ApiError(404, "not_found", "scorer not found")
    previous = _latest_scorer_version(conn, project_id, scorer_id)
    scorer_type = previous["type"]
    if payload.get("type") not in (None, scorer_type):
        raise ApiError(400, "validation_error", "scorer type cannot change; create a new scorer")
    config = (
        _payload_json(payload, "config")
        if "config" in payload
        else _json_value(previous["config_json"])
    )
    config = _validate_config_or_400(scorer_type, config)
    if "rubric" in payload or "rubric_text" in payload:
        rubric = _rubric_text(payload, required=scorer_type == "llm_judge")
    else:
        rubric = previous["rubric_text"]
    rubric_digest = _sha256(rubric) if rubric is not None else None
    frozen = _frozen_digest(scorer_type, config, rubric)
    version_id = new_id("scv")
    timestamp = now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        next_version = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM scorer_version WHERE scorer_id = ?",
            (scorer_id,),
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO scorer_version
              (id, scorer_id, project_id, version, type, config_json, rubric_text,
               rubric_digest, frozen_digest, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version_id,
                scorer_id,
                project_id,
                next_version,
                scorer_type,
                _json_text(config),
                rubric,
                rubric_digest,
                frozen,
                actor["id"],
                timestamp,
            ),
        )
        conn.execute("COMMIT")
    except sqlite3.IntegrityError as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise ApiError(409, "conflict", f"could not create scorer version: {exc}") from exc
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return _scorer_version_dict(
        conn.execute("SELECT * FROM scorer_version WHERE id = ?", (version_id,)).fetchone()
    )


def get_scorer_version(
    conn: sqlite3.Connection, actor: dict, project_id: str, scorer_id: str, version: int
) -> dict:
    require_project(actor, project_id)
    row = conn.execute(
        """
        SELECT * FROM scorer_version
        WHERE scorer_id = ? AND project_id = ? AND version = ?
        """,
        (scorer_id, project_id, version),
    ).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "scorer version not found")
    return _scorer_version_dict(row)


def _validate_profile_members(
    conn: sqlite3.Connection, project_id: str, members: Any
) -> list[dict]:
    try:
        normalized = normalize_members(members)
    except CombinatorConfigError as exc:
        raise ApiError(400, "validation_error", str(exc)) from exc
    for member in normalized:
        scorer = conn.execute(
            "SELECT id, name FROM scorer WHERE id = ? AND project_id = ?",
            (member["scorer_id"], project_id),
        ).fetchone()
        if scorer is None:
            raise ApiError(404, "not_found", f"scorer not found: {member['scorer_id']}")
        if member["version_id"] is not None:
            row = conn.execute(
                """
                SELECT 1 FROM scorer_version
                WHERE id = ? AND scorer_id = ? AND project_id = ?
                """,
                (member["version_id"], member["scorer_id"], project_id),
            ).fetchone()
            if row is None:
                raise ApiError(
                    404, "not_found", f"scorer version not found: {member['version_id']}"
                )
    return normalized


def _validate_profile_or_400(combinator: str, config: Any, members: list[dict]) -> dict:
    try:
        return validate_profile_config(combinator, config, members)
    except CombinatorConfigError as exc:
        raise ApiError(400, "validation_error", str(exc)) from exc


def _profile_row(conn: sqlite3.Connection, project_id: str, profile_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM scorer_profile WHERE id = ? AND project_id = ?", (profile_id, project_id)
    ).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "scorer profile not found in this project")
    return row


def _latest_profile_version(conn: sqlite3.Connection, project_id: str, profile_id: str) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT * FROM scorer_profile_version
        WHERE profile_id = ? AND project_id = ?
        ORDER BY version DESC LIMIT 1
        """,
        (profile_id, project_id),
    ).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "scorer profile has no versions")
    return row


def _profile_version_row(
    conn: sqlite3.Connection, project_id: str, version_id: str
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM scorer_profile_version WHERE id = ? AND project_id = ?",
        (version_id, project_id),
    ).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "scorer profile version not found in this project")
    return row


def create_profile(conn: sqlite3.Connection, actor: dict, project_id: str, payload: dict) -> dict:
    require_project(actor, project_id)
    require_write(actor)
    name = _require_text(payload, "name")
    description = _optional_text(payload, "description")
    combinator = payload.get("combinator")
    if combinator not in COMBINATORS:
        raise ApiError(400, "validation_error", f"'combinator' must be one of {list(COMBINATORS)}")
    members = _validate_profile_members(conn, project_id, payload.get("members"))
    config = _validate_profile_or_400(
        combinator, _payload_json(payload, "config", default={}), members
    )
    if conn.execute(
        "SELECT 1 FROM scorer_profile WHERE project_id = ? AND name = ?", (project_id, name)
    ).fetchone():
        raise ApiError(409, "conflict", f"scorer profile name already exists: {name}")
    profile_id = new_id("spr")
    version_id = new_id("scpv")
    frozen = _profile_digest(combinator, config, members)
    timestamp = now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            INSERT INTO scorer_profile (id, project_id, name, description, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (profile_id, project_id, name, description, timestamp),
        )
        conn.execute(
            """
            INSERT INTO scorer_profile_version
              (id, profile_id, project_id, version, combinator, config_json,
               members_json, frozen_digest, created_by, created_at)
            VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
            """,
            (
                version_id,
                profile_id,
                project_id,
                combinator,
                _json_text(config),
                _json_text(members),
                frozen,
                actor["id"],
                timestamp,
            ),
        )
        conn.execute("COMMIT")
    except sqlite3.IntegrityError as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise ApiError(409, "conflict", f"could not create scorer profile: {exc}") from exc
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return get_profile(conn, actor, project_id, profile_id)


def list_profiles(conn: sqlite3.Connection, actor: dict, project_id: str) -> list[dict]:
    require_project(actor, project_id)
    rows = conn.execute(
        "SELECT id FROM scorer_profile WHERE project_id = ? ORDER BY name", (project_id,)
    ).fetchall()
    return [get_profile(conn, actor, project_id, row["id"]) for row in rows]


def get_profile(conn: sqlite3.Connection, actor: dict, project_id: str, profile_id: str) -> dict:
    require_project(actor, project_id)
    data = dict(_profile_row(conn, project_id, profile_id))
    versions = conn.execute(
        """
        SELECT * FROM scorer_profile_version
        WHERE profile_id = ? AND project_id = ? ORDER BY version
        """,
        (profile_id, project_id),
    ).fetchall()
    data["versions"] = [_profile_version_dict(version) for version in versions]
    data["latest_version"] = data["versions"][-1] if data["versions"] else None
    return data


def add_profile_version(
    conn: sqlite3.Connection, actor: dict, project_id: str, profile_id: str, payload: dict
) -> dict:
    require_project(actor, project_id)
    require_write(actor)
    _profile_row(conn, project_id, profile_id)
    previous = _latest_profile_version(conn, project_id, profile_id)
    combinator = payload.get("combinator", previous["combinator"])
    if combinator not in COMBINATORS:
        raise ApiError(400, "validation_error", f"'combinator' must be one of {list(COMBINATORS)}")
    raw_members = payload.get("members")
    if raw_members is None:
        raw_members = _json_value(previous["members_json"])
    members = _validate_profile_members(conn, project_id, raw_members)
    if "config" in payload:
        config_value = _payload_json(payload, "config", default={})
    else:
        config_value = _json_value(previous["config_json"])
    config = _validate_profile_or_400(combinator, config_value, members)
    version_id = new_id("scpv")
    frozen = _profile_digest(combinator, config, members)
    timestamp = now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        next_version = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM scorer_profile_version WHERE profile_id = ?",
            (profile_id,),
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO scorer_profile_version
              (id, profile_id, project_id, version, combinator, config_json,
               members_json, frozen_digest, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version_id,
                profile_id,
                project_id,
                next_version,
                combinator,
                _json_text(config),
                _json_text(members),
                frozen,
                actor["id"],
                timestamp,
            ),
        )
        conn.execute("COMMIT")
    except sqlite3.IntegrityError as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise ApiError(409, "conflict", f"could not create profile version: {exc}") from exc
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return _profile_version_dict(
        conn.execute("SELECT * FROM scorer_profile_version WHERE id = ?", (version_id,)).fetchone()
    )


def get_profile_version(
    conn: sqlite3.Connection, actor: dict, project_id: str, profile_id: str, version: int
) -> dict:
    require_project(actor, project_id)
    row = conn.execute(
        """
        SELECT * FROM scorer_profile_version
        WHERE profile_id = ? AND project_id = ? AND version = ?
        """,
        (profile_id, project_id, version),
    ).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "scorer profile version not found")
    return _profile_version_dict(row)


def _binding_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    data = dict(row)
    profile = conn.execute(
        "SELECT name FROM scorer_profile WHERE id = ?", (data["profile_id"],)
    ).fetchone()
    data["profile_name"] = profile["name"] if profile is not None else None
    if data["profile_version_id"]:
        version = conn.execute(
            "SELECT version FROM scorer_profile_version WHERE id = ?",
            (data["profile_version_id"],),
        ).fetchone()
        data["pinned_version"] = version["version"] if version is not None else None
    else:
        data["pinned_version"] = None
    return data


def _require_dataset_row(conn: sqlite3.Connection, project_id: str, dataset_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM dataset WHERE id = ? AND project_id = ?", (dataset_id, project_id)
    ).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "dataset not found")
    return row


def bind_scorer(
    conn: sqlite3.Connection, actor: dict, project_id: str, dataset_id: str, payload: dict
) -> dict:
    require_project(actor, project_id)
    require_write(actor)
    _require_dataset_row(conn, project_id, dataset_id)
    sample_key = payload.get("sample_key")
    if sample_key is not None:
        if not isinstance(sample_key, str) or not sample_key.strip():
            raise ApiError(400, "validation_error", "'sample_key' must be a non-empty string")
        sample_key = sample_key.strip()
        exists = conn.execute(
            "SELECT 1 FROM sample WHERE dataset_id = ? AND project_id = ? AND sample_key = ?",
            (dataset_id, project_id, sample_key),
        ).fetchone()
        if exists is None:
            raise ApiError(404, "not_found", f"sample not found in dataset: {sample_key}")
    profile_id = payload.get("profile_id")
    if not isinstance(profile_id, str) or not profile_id.startswith("spr_"):
        raise ApiError(400, "validation_error", "'profile_id' must be a scorer profile id")
    _profile_row(conn, project_id, profile_id)
    profile_version_id = payload.get("profile_version_id")
    if profile_version_id is not None:
        if not isinstance(profile_version_id, str) or not profile_version_id.startswith("scpv_"):
            raise ApiError(400, "validation_error", "'profile_version_id' must be a profile version id")
        row = conn.execute(
            """
            SELECT 1 FROM scorer_profile_version
            WHERE id = ? AND profile_id = ? AND project_id = ?
            """,
            (profile_version_id, profile_id, project_id),
        ).fetchone()
        if row is None:
            raise ApiError(404, "not_found", "profile version not found for this profile")
    existing = conn.execute(
        "SELECT * FROM scorer_binding WHERE dataset_id = ? AND sample_key IS ?",
        (dataset_id, sample_key),
    ).fetchone()
    timestamp = now()
    if existing is None:
        binding_id = new_id("sbd")
        conn.execute(
            """
            INSERT INTO scorer_binding
              (id, project_id, dataset_id, sample_key, profile_id, profile_version_id,
               created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                binding_id,
                project_id,
                dataset_id,
                sample_key,
                profile_id,
                profile_version_id,
                timestamp,
                timestamp,
            ),
        )
    else:
        binding_id = existing["id"]
        conn.execute(
            """
            UPDATE scorer_binding
            SET profile_id = ?, profile_version_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (profile_id, profile_version_id, timestamp, binding_id),
        )
    row = conn.execute("SELECT * FROM scorer_binding WHERE id = ?", (binding_id,)).fetchone()
    return _binding_dict(conn, row)


def list_bindings(
    conn: sqlite3.Connection, actor: dict, project_id: str, dataset_id: str
) -> list[dict]:
    require_project(actor, project_id)
    _require_dataset_row(conn, project_id, dataset_id)
    rows = conn.execute(
        """
        SELECT * FROM scorer_binding
        WHERE dataset_id = ? AND project_id = ?
        ORDER BY sample_key IS NULL DESC, sample_key
        """,
        (dataset_id, project_id),
    ).fetchall()
    return [_binding_dict(conn, row) for row in rows]


def delete_binding(
    conn: sqlite3.Connection, actor: dict, project_id: str, dataset_id: str, binding_id: str
) -> dict:
    require_project(actor, project_id)
    require_write(actor)
    _require_dataset_row(conn, project_id, dataset_id)
    row = conn.execute(
        "SELECT * FROM scorer_binding WHERE id = ? AND dataset_id = ? AND project_id = ?",
        (binding_id, dataset_id, project_id),
    ).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "scorer binding not found")
    conn.execute("DELETE FROM scorer_binding WHERE id = ?", (binding_id,))
    return {"deleted": True, "binding": _binding_dict(conn, row)}


def _resolve_binding_row(
    conn: sqlite3.Connection, project_id: str, dataset_id: str, sample_key: str
) -> sqlite3.Row | None:
    row = conn.execute(
        """
        SELECT * FROM scorer_binding
        WHERE dataset_id = ? AND project_id = ? AND sample_key = ?
        """,
        (dataset_id, project_id, sample_key),
    ).fetchone()
    if row is not None:
        return row
    return conn.execute(
        """
        SELECT * FROM scorer_binding
        WHERE dataset_id = ? AND project_id = ? AND sample_key IS NULL
        """,
        (dataset_id, project_id),
    ).fetchone()


def _profile_resolution(conn: sqlite3.Connection, row: sqlite3.Row, origin: dict) -> dict:
    profile = conn.execute(
        "SELECT id, name FROM scorer_profile WHERE id = ? AND project_id = ?",
        (row["profile_id"], row["project_id"]),
    ).fetchone()
    return {
        "kind": "profile",
        "profile_id": row["profile_id"],
        "profile_name": profile["name"] if profile is not None else None,
        "profile_version_id": row["id"],
        "profile_version": row["version"],
        "frozen_digest": row["frozen_digest"],
        "combinator": row["combinator"],
        "config": _json_value(row["config_json"]),
        "members": _json_value(row["members_json"]),
        "origin": origin,
    }


def _scorer_resolution(conn: sqlite3.Connection, row: sqlite3.Row, origin: dict) -> dict:
    scorer = conn.execute(
        "SELECT id, name FROM scorer WHERE id = ? AND project_id = ?",
        (row["scorer_id"], row["project_id"]),
    ).fetchone()
    return {
        "kind": "single",
        "profile_id": None,
        "profile_name": None,
        "profile_version_id": None,
        "profile_version": None,
        "frozen_digest": None,
        "combinator": "single",
        "config": {},
        "members": [
            {"scorer_id": row["scorer_id"], "version_id": row["id"], "weight": 1.0, "label": None}
        ],
        "scorer_name": scorer["name"] if scorer is not None else None,
        "origin": origin,
    }


def _resolution_for_ref(conn: sqlite3.Connection, project_id: str, ref: str, origin: dict) -> dict | None:
    if ref.startswith("scpv_"):
        return _profile_resolution(conn, _profile_version_row(conn, project_id, ref), origin)
    if ref.startswith("scv_"):
        return _scorer_resolution(conn, _scorer_version_row(conn, project_id, ref), origin)
    if ref.startswith("spr_"):
        return _profile_resolution(
            conn, _latest_profile_version(conn, project_id, ref), origin
        )
    if ref.startswith("scr_"):
        return _scorer_resolution(
            conn, _latest_scorer_version(conn, project_id, ref), origin
        )
    profile = conn.execute(
        "SELECT id FROM scorer_profile WHERE project_id = ? AND name = ?", (project_id, ref)
    ).fetchone()
    if profile is not None:
        return _profile_resolution(
            conn, _latest_profile_version(conn, project_id, profile["id"]), origin
        )
    scorer = conn.execute(
        "SELECT id FROM scorer WHERE project_id = ? AND name = ?", (project_id, ref)
    ).fetchone()
    if scorer is not None:
        return _scorer_resolution(
            conn, _latest_scorer_version(conn, project_id, scorer["id"]), origin
        )
    return None


def resolve_scorer_config(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    dataset_id: str | None,
    sample_key: str | None,
    spec_scorer_ref: str | None,
    override: dict | None = None,
) -> dict | None:
    """Resolve one effective scorer configuration for a sample.

    Precedence: explicit scoring request override, then the Run's spec version
    pin, then a sample binding, then the dataset default binding, then a name
    lookup for compatibility with free-text T08 spec values.
    """

    override = override or {}
    if override.get("profile_version_id"):
        return _profile_resolution(
            conn,
            _profile_version_row(conn, project_id, override["profile_version_id"]),
            {"source": "request_override"},
        )
    if override.get("scorer_version_id"):
        return _scorer_resolution(
            conn,
            _scorer_version_row(conn, project_id, override["scorer_version_id"]),
            {"source": "request_override"},
        )
    use_bindings = spec_scorer_ref in ("bindings", "auto")
    if spec_scorer_ref and not use_bindings:
        origin = {"source": "spec_version", "scorer_version": spec_scorer_ref}
        if spec_scorer_ref.startswith(("scpv_", "scv_", "spr_", "scr_")):
            # An explicit id that no longer exists is an error, not a reason to
            # silently fall back to a different binding.
            return _resolution_for_ref(conn, project_id, spec_scorer_ref, origin)
        try:
            resolved = _resolution_for_ref(conn, project_id, spec_scorer_ref, origin)
        except ApiError:
            resolved = None
        if resolved is not None:
            return resolved
    if dataset_id and sample_key:
        binding = _resolve_binding_row(conn, project_id, dataset_id, sample_key)
        if binding is not None:
            origin = {
                "source": "binding",
                "binding_id": binding["id"],
                "binding_scope": "sample" if binding["sample_key"] is not None else "dataset",
            }
            if binding["profile_version_id"]:
                return _profile_resolution(
                    conn,
                    _profile_version_row(conn, project_id, binding["profile_version_id"]),
                    origin,
                )
            return _profile_resolution(
                conn, _latest_profile_version(conn, project_id, binding["profile_id"]), origin
            )
    if spec_scorer_ref and not use_bindings:
        try:
            return _resolution_for_ref(
                conn,
                project_id,
                spec_scorer_ref,
                {"source": "spec_version_name", "scorer_version": spec_scorer_ref},
            )
        except ApiError:
            return None
    return None


def _evaluate_member(
    conn: sqlite3.Connection,
    project_id: str,
    member_spec: dict,
    sample: dict,
    actual: Any,
    context: dict | None,
) -> dict:
    started = time.monotonic()
    scorer_name = None
    version_row = None
    try:
        scorer_row = conn.execute(
            "SELECT * FROM scorer WHERE id = ? AND project_id = ?",
            (member_spec["scorer_id"], project_id),
        ).fetchone()
        if scorer_row is None:
            raise ApiError(404, "not_found", f"scorer not found: {member_spec['scorer_id']}")
        scorer_name = scorer_row["name"]
        if member_spec.get("version_id"):
            version_row = _scorer_version_row(conn, project_id, member_spec["version_id"])
        else:
            version_row = _latest_scorer_version(conn, project_id, member_spec["scorer_id"])
        if version_row["scorer_id"] != member_spec["scorer_id"]:
            raise ApiError(404, "not_found", "scorer version belongs to another scorer")
        scorer = build_scorer(
            version_row["type"],
            scorer_id=member_spec["scorer_id"],
            scorer_name=scorer_name,
            version_id=version_row["id"],
            version=version_row["version"],
            frozen_digest=version_row["frozen_digest"],
            config=_json_value(version_row["config_json"]),
            rubric_text=version_row["rubric_text"],
            rubric_digest=version_row["rubric_digest"],
        )
        trace = scorer.trace()
        result = scorer.evaluate(sample, actual, context)
    except ApiError as exc:
        result = error_result(
            "scorer_not_found" if exc.code == "not_found" else exc.code,
            exc.message,
            detail={"scorer_id": member_spec["scorer_id"]},
        )
        trace = {
            "scorer_id": member_spec["scorer_id"],
            "scorer_name": scorer_name,
            "scorer_type": version_row["type"] if version_row is not None else None,
            "scorer_version_id": version_row["id"] if version_row is not None else member_spec.get("version_id"),
            "scorer_version": version_row["version"] if version_row is not None else None,
            "frozen_digest": version_row["frozen_digest"] if version_row is not None else None,
            "rubric_digest": version_row["rubric_digest"] if version_row is not None else None,
            "config": _json_value(version_row["config_json"]) if version_row is not None else None,
        }
    except Exception as exc:  # noqa: BLE001 - one scorer must never abort the Run
        result = error_result(
            "scorer_exception",
            f"{type(exc).__name__}: {exc}",
            detail={"scorer_id": member_spec["scorer_id"]},
        )
        trace = {
            "scorer_id": member_spec["scorer_id"],
            "scorer_name": scorer_name,
            "scorer_type": version_row["type"] if version_row is not None else None,
            "scorer_version_id": version_row["id"] if version_row is not None else member_spec.get("version_id"),
            "scorer_version": version_row["version"] if version_row is not None else None,
            "frozen_digest": version_row["frozen_digest"] if version_row is not None else None,
            "rubric_digest": version_row["rubric_digest"] if version_row is not None else None,
            "config": _json_value(version_row["config_json"]) if version_row is not None else None,
        }
    duration_ms = int((time.monotonic() - started) * 1000)
    trace.update(
        {
            "weight": float(member_spec.get("weight", 1.0)),
            "label": member_spec.get("label"),
            "state": result.state,
            "confidence": result.confidence,
            "score": result.score,
            "detail": result.detail,
            "error": result.error,
            "usage": result.usage,
            "duration_ms": duration_ms,
        }
    )
    return trace


def _counts(members: list[dict]) -> dict:
    counts = {"pass": 0, "fail": 0, "uncertain": 0, "error": 0}
    for member in members:
        if member.get("state") in counts:
            counts[member["state"]] += 1
    return counts


def _evaluate_resolution(
    conn: sqlite3.Connection,
    project_id: str,
    resolution: dict,
    sample: dict,
    actual: Any,
    context: dict | None,
) -> tuple[ScoreResult, dict]:
    members = [
        _evaluate_member(conn, project_id, spec, sample, actual, context)
        for spec in resolution["members"]
    ]
    if resolution["kind"] == "single":
        member = members[0]
        result = ScoreResult(
            state=member["state"],
            confidence=member["confidence"],
            detail={
                "combinator": "single",
                "members": members,
                "aggregate": {"counts": _counts(members)},
                "scorer_detail": member["detail"],
            },
            error=member.get("error"),
            usage=member.get("usage"),
        )
    else:
        result = combine(resolution["combinator"], resolution["config"], members)
    trace = {
        "schemaVersion": 1,
        "resolved_at": now(),
        "profile": (
            {
                "id": resolution.get("profile_id"),
                "name": resolution.get("profile_name"),
                "version": resolution.get("profile_version"),
                "version_id": resolution.get("profile_version_id"),
                "frozen_digest": resolution.get("frozen_digest"),
            }
            if resolution["kind"] == "profile"
            else None
        ),
        "combinator": resolution["combinator"],
        "config": resolution["config"],
        "origin": resolution["origin"],
        "members": members,
        "aggregate": dict(result.detail.get("aggregate") or {"counts": _counts(members)}),
        "state": result.state,
        "confidence": result.confidence,
        "scorer_versions": [member.get("scorer_version_id") for member in members],
        "rubric_digests": {
            member.get("scorer_version_id"): member.get("rubric_digest") for member in members
        },
    }
    return result, trace


def score_sample(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    dataset_id: str | None,
    sample: dict,
    actual: Any,
    context: dict | None,
    spec_scorer_ref: str | None,
    override: dict | None = None,
) -> tuple[ScoreResult, dict]:
    sample_key = sample.get("sample_id")
    try:
        resolution = resolve_scorer_config(
            conn,
            project_id,
            dataset_id=dataset_id,
            sample_key=sample_key,
            spec_scorer_ref=spec_scorer_ref,
            override=override,
        )
    except ApiError as exc:
        resolution = None
        resolution_error = error_result(
            "scorer_config_error",
            exc.message,
            detail={"scorer_ref": spec_scorer_ref, **(exc.details or {})},
        )
    else:
        resolution_error = None
    if resolution is None:
        result = resolution_error or error_result(
            "scorer_not_configured",
            "no scorer profile or scorer version is bound to this sample",
            detail={"scorer_ref": spec_scorer_ref, "sample_id": sample_key},
        )
        trace = {
            "schemaVersion": 1,
            "resolved_at": now(),
            "profile": None,
            "combinator": None,
            "config": {},
            "origin": {"source": "unresolved", "scorer_ref": spec_scorer_ref},
            "members": [],
            "aggregate": {"counts": _counts([])},
            "state": result.state,
            "confidence": result.confidence,
            "scorer_versions": [],
            "rubric_digests": {},
        }
        return result, trace
    try:
        return _evaluate_resolution(conn, project_id, resolution, sample, actual, context)
    except Exception as exc:  # noqa: BLE001 - defensive, scoring must continue
        result = error_result(
            "scorer_execution_error", f"{type(exc).__name__}: {exc}", detail={"sample_id": sample_key}
        )
        return result, {
            "schemaVersion": 1,
            "resolved_at": now(),
            "profile": None,
            "combinator": resolution.get("combinator"),
            "config": resolution.get("config", {}),
            "origin": resolution.get("origin", {}),
            "members": [],
            "aggregate": {"counts": _counts([])},
            "state": result.state,
            "confidence": result.confidence,
            "scorer_versions": [],
            "rubric_digests": {},
            "error": result.error,
        }


def preview_scorer(
    conn: sqlite3.Connection, actor: dict, project_id: str, payload: dict
) -> dict:
    """Evaluate one scorer against a supplied sample/actual pair (offline)."""

    require_project(actor, project_id)
    sample = payload.get("sample")
    if not isinstance(sample, dict):
        raise ApiError(400, "validation_error", "'sample' must be an object")
    actual = payload["actual"] if "actual" in payload else MISSING
    context = payload.get("context")
    if context is not None and not isinstance(context, dict):
        raise ApiError(400, "validation_error", "'context' must be an object")
    version_row = None
    if payload.get("scorer_version_id"):
        version_row = _scorer_version_row(conn, project_id, payload["scorer_version_id"])
    elif payload.get("scorer_id"):
        scored = conn.execute(
            "SELECT * FROM scorer WHERE id = ? AND project_id = ?",
            (payload["scorer_id"], project_id),
        ).fetchone()
        if scored is None:
            raise ApiError(404, "not_found", "scorer not found in this project")
        if payload.get("version") is not None:
            try:
                version_number = int(payload["version"])
            except (TypeError, ValueError) as exc:
                raise ApiError(400, "validation_error", "'version' must be an integer") from exc
            version_row = conn.execute(
                "SELECT * FROM scorer_version WHERE scorer_id = ? AND version = ?",
                (payload["scorer_id"], version_number),
            ).fetchone()
            if version_row is None:
                raise ApiError(404, "not_found", "scorer version not found")
        else:
            version_row = _latest_scorer_version(conn, project_id, payload["scorer_id"])
    else:
        scorer_type = payload.get("type")
        if scorer_type not in SCORER_TYPES:
            raise ApiError(400, "validation_error", f"'type' must be one of {list(SCORER_TYPES)}")
        config = _validate_config_or_400(scorer_type, _payload_json(payload, "config", default={}))
        rubric = _rubric_text(payload, required=scorer_type == "llm_judge")
        version_row = {
            "id": "scv_preview",
            "scorer_id": "scr_preview",
            "version": 0,
            "type": scorer_type,
            "config_json": _json_text(config),
            "rubric_text": rubric,
            "rubric_digest": _sha256(rubric) if rubric else None,
            "frozen_digest": _frozen_digest(scorer_type, config, rubric),
        }
    scorer_row = conn.execute(
        "SELECT name FROM scorer WHERE id = ? AND project_id = ?",
        (version_row["scorer_id"], project_id),
    ).fetchone()
    scorer = build_scorer(
        version_row["type"],
        scorer_id=version_row["scorer_id"],
        scorer_name=scorer_row["name"] if scorer_row is not None else "preview",
        version_id=version_row["id"],
        version=version_row["version"],
        frozen_digest=version_row["frozen_digest"],
        config=_json_value(version_row["config_json"]),
        rubric_text=version_row["rubric_text"],
        rubric_digest=version_row["rubric_digest"],
    )
    started = time.monotonic()
    result = scorer.evaluate(sample, actual, context)
    duration_ms = int((time.monotonic() - started) * 1000)
    return {
        "result": result.to_dict(),
        "trace": {
            **scorer.trace(),
            "duration_ms": duration_ms,
            "state": result.state,
            "confidence": result.confidence,
            "detail": result.detail,
            "error": result.error,
            "usage": result.usage,
        },
    }


def _outputs_map(payload: dict) -> dict:
    raw = payload.get("actual_outputs", payload.get("outputs"))
    if raw is None:
        raise ApiError(400, "validation_error", "'actual_outputs' is required")
    if isinstance(raw, dict):
        result = {}
        for key, value in raw.items():
            if not isinstance(key, str):
                raise ApiError(400, "validation_error", "'actual_outputs' keys must be sample ids")
            result[key] = value
        return result
    if isinstance(raw, list):
        result = {}
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                raise ApiError(400, "validation_error", f"actual_outputs[{index}] must be an object")
            sample_id = item.get("sample_id")
            if not isinstance(sample_id, str) or not sample_id:
                raise ApiError(
                    400, "validation_error", f"actual_outputs[{index}].sample_id is required"
                )
            if sample_id in result:
                raise ApiError(400, "validation_error", f"duplicate actual output for {sample_id}")
            if "actual" in item:
                result[sample_id] = item["actual"]
            elif "output" in item:
                result[sample_id] = item["output"]
            else:
                raise ApiError(
                    400, "validation_error", f"actual_outputs[{index}] needs 'actual' or 'output'"
                )
        return result
    raise ApiError(400, "validation_error", "'actual_outputs' must be an object or array")


def _contexts_map(payload: dict) -> dict:
    raw = payload.get("contexts")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ApiError(400, "validation_error", "'contexts' must be an object keyed by sample id")
    for key, value in raw.items():
        if value is not None and not isinstance(value, dict):
            raise ApiError(400, "validation_error", f"contexts[{key}] must be an object")
    return raw


def _record_item(
    conn: sqlite3.Connection,
    run_id: str,
    sample_key: str,
    result: ScoreResult,
    trace: dict,
    duration_ms: int,
) -> dict:
    usage = result.usage or {}
    cost = None
    if usage:
        cost = {}
        if isinstance(usage.get("prompt_tokens"), int):
            cost["tokens_in"] = usage["prompt_tokens"]
        if isinstance(usage.get("completion_tokens"), int):
            cost["tokens_out"] = usage["completion_tokens"]
        if isinstance(usage.get("total_tokens"), int):
            cost["tokens_total"] = usage["total_tokens"]
        if cost:
            cost["model"] = trace.get("members", [{}])[0].get("detail", {}).get("model") if trace.get("members") else None
    item_id = new_id("itm")
    verdict_id = new_id("vrd")
    timestamp = now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            INSERT INTO run_item
              (id, run_id, case_ref, state, confidence, duration_ms, cost_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item_id,
                run_id,
                sample_key,
                result.state,
                result.confidence,
                max(0, int(duration_ms)),
                _json_text(cost) if cost else None,
                timestamp,
            ),
        )
        conn.execute(
            """
            INSERT INTO verdict
              (id, run_item_id, state, confidence, scorer_trace_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                verdict_id,
                item_id,
                result.state,
                result.confidence,
                _json_text(trace),
                timestamp,
            ),
        )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return {"id": item_id, "verdict_id": verdict_id, "case_ref": sample_key}


def score_run(
    conn: sqlite3.Connection, actor: dict, project_id: str, run_id: str, payload: dict
) -> dict:
    require_project(actor, project_id)
    require_write(actor)
    run = conn.execute(
        """
        SELECT r.*, sv.scorer_version AS sv_scorer_version,
               sv.dataset_version AS sv_dataset_version
        FROM run r JOIN spec_version sv ON sv.id = r.spec_version_id
        WHERE r.id = ? AND r.project_id = ?
        """,
        (run_id, project_id),
    ).fetchone()
    if run is None:
        raise ApiError(404, "not_found", "run not found")
    if run["channel"] != "B":
        raise ApiError(400, "validation_error", "only B-channel runs can be scored with scorers")
    existing = conn.execute(
        "SELECT COUNT(*) FROM run_item WHERE run_id = ?", (run_id,)
    ).fetchone()[0]
    if existing:
        raise ApiError(409, "conflict", "run already has items; scoring is not repeated")
    version = datasets.resolve_version_ref(conn, project_id, run["sv_dataset_version"])
    if version is None:
        raise ApiError(
            409,
            "conflict",
            "run is not bound to a managed dataset version; create a B-channel spec version with a dsv_ id",
        )
    samples = datasets.dataset_version_samples(conn, version["id"])
    if not samples:
        raise ApiError(400, "validation_error", "the bound dataset version has no samples")
    outputs = _outputs_map(payload)
    contexts = _contexts_map(payload)
    sample_ids = {sample["sample_id"] for sample in samples}
    unknown_outputs = sorted(set(outputs) - sample_ids)
    if unknown_outputs:
        raise ApiError(
            400,
            "validation_error",
            "actual_outputs contains sample ids that are not in the bound dataset version",
            details={"unknownSampleIds": unknown_outputs},
        )
    unknown_contexts = sorted(set(contexts) - sample_ids)
    if unknown_contexts:
        raise ApiError(
            400,
            "validation_error",
            "contexts contains sample ids that are not in the bound dataset version",
            details={"unknownSampleIds": unknown_contexts},
        )
    override: dict = {}
    if payload.get("profile_version_id"):
        _profile_version_row(conn, project_id, payload["profile_version_id"])
        override["profile_version_id"] = payload["profile_version_id"]
    if payload.get("scorer_version_id"):
        _scorer_version_row(conn, project_id, payload["scorer_version_id"])
        override["scorer_version_id"] = payload["scorer_version_id"]
    if len(override) > 1:
        raise ApiError(
            400,
            "validation_error",
            "pass at most one of 'profile_version_id' or 'scorer_version_id'",
        )
    run_context = {
        "run_id": run_id,
        "commit_sha": run["commit_sha"],
        "branch": run["branch"],
        "env": run["env"],
        "spec_version_id": run["spec_version_id"],
        "scorer_version": run["sv_scorer_version"],
        "model_snapshot": _json_value(run["model_snapshot_json"]),
    }
    from . import costs as cost_service
    from .service import _apply_run_transition_locked

    started_at = now()
    if run["status"] in ("succeeded", "failed", "cancelled", "timeout"):
        raise ApiError(409, "invalid_transition", f"run is already '{run['status']}'")
    conn.execute("BEGIN IMMEDIATE")
    try:
        if run["status"] == "queued":
            _apply_run_transition_locked(
                conn,
                run_id,
                "running",
                source="api",
                actor=f"{actor['name']}/{actor['role']}",
                reason="scoring_started",
                detail={"channel": "B", "dataset_version_id": version["id"]},
                at=started_at,
                fields={"started_at": started_at},
            )
        else:
            conn.execute(
                "UPDATE run SET started_at = COALESCE(started_at, ?), updated_at = ? WHERE id = ?",
                (started_at, started_at, run_id),
            )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    recorded: list[dict] = []
    quota_terminated = False
    quota_state = cost_service.enforce_run_quota(
        conn, project_id, run_id, actor="cost_quota", stage="scoring_start"
    )
    if quota_state.get("terminated"):
        quota_terminated = True
    try:
        for sample in ([] if quota_terminated else samples):
            sample_key = sample["sample_id"]
            context = {
                "data": sample.get("context"),
                "run": run_context,
                "overrides": contexts.get(sample_key),
            }
            if sample_key not in outputs:
                result = error_result(
                    "missing_actual_output",
                    f"no actual output supplied for sample '{sample_key}'",
                    detail={"sample_id": sample_key},
                )
                trace = {
                    "schemaVersion": 1,
                    "resolved_at": now(),
                    "profile": None,
                    "combinator": None,
                    "config": {},
                    "origin": {"source": "actual_output", "sample_id": sample_key},
                    "members": [],
                    "aggregate": {"counts": _counts([])},
                    "state": result.state,
                    "confidence": result.confidence,
                    "scorer_versions": [],
                    "rubric_digests": {},
                    "error": result.error,
                }
                duration_ms = 0
            else:
                result, trace = score_sample(
                    conn,
                    project_id,
                    dataset_id=version["dataset_id"],
                    sample=sample,
                    actual=outputs[sample_key],
                    context=context,
                    spec_scorer_ref=run["sv_scorer_version"],
                    override=override or None,
                )
                duration_ms = sum(
                    int(member.get("duration_ms") or 0) for member in trace.get("members", [])
                )
            item_record = _record_item(conn, run_id, sample_key, result, trace, duration_ms)
            recorded.append(
                {
                    "case_ref": sample_key,
                    "state": result.state,
                    "confidence": result.confidence,
                    **item_record,
                }
            )
            cost_service.record_model_usage_from_trace(
                conn, project_id, run_id, item_record["id"], trace
            )
            quota_state = cost_service.enforce_run_quota(
                conn, project_id, run_id, actor="cost_quota", stage="scoring_item"
            )
            if quota_state.get("terminated"):
                quota_terminated = True
                break
            if quota_terminated:
                break
    except Exception:
        current = conn.execute("SELECT status FROM run WHERE id = ?", (run_id,)).fetchone()
        if current is not None and current["status"] == "running":
            conn.execute("BEGIN IMMEDIATE")
            try:
                _apply_run_transition_locked(
                    conn,
                    run_id,
                    "failed",
                    source="api",
                    actor=f"{actor['name']}/{actor['role']}",
                    reason="scoring_error",
                    at=now(),
                    fields={"finished_at": now()},
                )
                conn.execute("COMMIT")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        raise
    finished_at = now()
    current = conn.execute("SELECT status FROM run WHERE id = ?", (run_id,)).fetchone()
    if current is not None and current["status"] == "running":
        conn.execute("BEGIN IMMEDIATE")
        try:
            _apply_run_transition_locked(
                conn,
                run_id,
                "succeeded",
                source="api",
                actor=f"{actor['name']}/{actor['role']}",
                reason="scoring_completed",
                detail={"items": len(recorded), "quota_terminated": quota_terminated},
                at=finished_at,
                fields={"finished_at": finished_at},
            )
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    final_run = conn.execute("SELECT status, finished_at FROM run WHERE id = ?", (run_id,)).fetchone()
    states = {"pass": 0, "fail": 0, "uncertain": 0, "error": 0}
    for item in recorded:
        states[item["state"]] += 1
    from .service import get_run

    return {
        "run": get_run(conn, project_id, run_id),
        "summary": {
            "run_id": run_id,
            "status": final_run["status"] if final_run is not None else "unknown",
            "started_at": started_at,
            "finished_at": final_run["finished_at"] if final_run is not None else finished_at,
            "quotaTerminated": quota_terminated,
            "quota": quota_state,
            "itemCount": len(recorded),
            "states": states,
            "scorer_version": run["sv_scorer_version"],
            "dataset_version_id": version["id"],
            "dataset_version": version["version"],
            "items": [
                {"case_ref": item["case_ref"], "state": item["state"], "confidence": item["confidence"]}
                for item in recorded
            ],
        },
    }


def run_scorer_trace(
    conn: sqlite3.Connection, actor: dict, project_id: str, run_id: str
) -> dict:
    """Return the persisted per-item scorer trace for historical backtracking."""

    require_project(actor, project_id)
    run = conn.execute(
        """
        SELECT r.id, r.project_id, r.channel, r.commit_sha, r.status,
               r.spec_version_id, sv.version AS spec_version,
               sv.scorer_version AS spec_scorer_version,
               sv.dataset_version AS spec_dataset_version
        FROM run r JOIN spec_version sv ON sv.id = r.spec_version_id
        WHERE r.id = ? AND r.project_id = ?
        """,
        (run_id, project_id),
    ).fetchone()
    if run is None:
        raise ApiError(404, "not_found", "run not found")
    rows = conn.execute(
        """
        SELECT ri.id, ri.case_ref, ri.state, ri.confidence, ri.created_at,
               v.id AS verdict_id, v.scorer_trace_json
        FROM run_item ri LEFT JOIN verdict v ON v.run_item_id = ri.id
        WHERE ri.run_id = ? ORDER BY ri.case_ref
        """,
        (run_id,),
    ).fetchall()
    version_ids: set[str] = set()
    profile_version_ids: set[str] = set()
    items = []
    for row in rows:
        trace = _json_value(row["scorer_trace_json"]) if row["scorer_trace_json"] else None
        if trace:
            for member in trace.get("members") or []:
                if member.get("scorer_version_id"):
                    version_ids.add(member["scorer_version_id"])
            profile = trace.get("profile") or {}
            if profile.get("version_id"):
                profile_version_ids.add(profile["version_id"])
        items.append(
            {
                "case_ref": row["case_ref"],
                "state": row["state"],
                "confidence": row["confidence"],
                "verdict_id": row["verdict_id"],
                "created_at": row["created_at"],
                "trace": trace,
            }
        )
    versions = {}
    latest_versions = {}
    missing_version_ids = []
    for version_id in sorted(version_ids):
        row = conn.execute(
            "SELECT * FROM scorer_version WHERE id = ? AND project_id = ?",
            (version_id, project_id),
        ).fetchone()
        if row is None:
            missing_version_ids.append(version_id)
            continue
        versions[version_id] = _scorer_version_dict(row)
        latest = _latest_scorer_version(conn, project_id, row["scorer_id"])
        latest_versions[row["scorer_id"]] = {
            "id": latest["id"],
            "version": latest["version"],
            "frozen_digest": latest["frozen_digest"],
        }
    profile_versions = {}
    for version_id in sorted(profile_version_ids):
        row = conn.execute(
            "SELECT * FROM scorer_profile_version WHERE id = ? AND project_id = ?",
            (version_id, project_id),
        ).fetchone()
        if row is not None:
            profile_versions[version_id] = _profile_version_dict(row)
    return {
        "run_id": run_id,
        "run_status": run["status"],
        "spec_version_id": run["spec_version_id"],
        "spec_version": run["spec_version"],
        "spec_scorer_version": run["spec_scorer_version"],
        "spec_dataset_version": run["spec_dataset_version"],
        "itemCount": len(items),
        "items": items,
        "scorer_versions": versions,
        "profile_versions": profile_versions,
        "latest_versions": latest_versions,
        "missing_version_ids": missing_version_ids,
        "historyNote": (
            "Traces are stored on the verdict at scoring time. Later scorer or rubric "
            "versions do not rewrite them; scorer_versions returns the exact historical rows."
        ),
    }
