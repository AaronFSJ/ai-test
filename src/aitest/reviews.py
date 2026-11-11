"""T12: human review queue, write-back and calibration samples."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from . import datasets
from .service import ApiError, new_id, now, require_project, require_write

CONCLUSIONS = ("confirmed_defect", "false_positive", "sample_error")
CONCLUSION_LABELS = {
    "confirmed_defect": "确认为缺陷",
    "false_positive": "误报（脚本或评分器问题）",
    "sample_error": "样本本身有误",
}
CONCLUSION_TO_STATE = {
    "confirmed_defect": "fail",
    "false_positive": "pass",
    "sample_error": "excluded",
}
QUEUE_KINDS = ("uncertain", "error")
STATUS_FILTERS = ("open", "reviewed", "all")

_MISSING = object()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_value(text: str | None) -> Any:
    return None if text is None else json.loads(text)


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _optional_text(payload: dict, field: str, *, max_len: int = 2000) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ApiError(400, "validation_error", f"'{field}' must be a string")
    value = value.strip()
    if len(value) > max_len:
        raise ApiError(400, "validation_error", f"'{field}' must be at most {max_len} characters")
    return value or None


def _review_row_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["reviewerId"] = data.pop("reviewer_id", None)
    data["priorState"] = data.pop("prior_state", None)
    data["resultingState"] = data.pop("resulting_state", None)
    data["scorerType"] = data.pop("scorer_type", None)
    data["attributionLabel"] = data.pop("attribution_label", None)
    snapshot = data.pop("evidence_snapshot_json", None)
    data["evidenceSnapshot"] = _json_value(snapshot) if snapshot else None
    data["calibrationSampleId"] = data.pop("calibration_sample_id", None)
    data["createdAt"] = data.pop("created_at", None)
    data["conclusionLabel"] = CONCLUSION_LABELS.get(data.get("conclusion"), data.get("conclusion"))
    return data


def _latest_reviews(conn: sqlite3.Connection, item_ids: list[str]) -> dict[str, dict]:
    if not item_ids:
        return {}
    placeholders = ",".join("?" for _ in item_ids)
    rows = conn.execute(
        f"SELECT * FROM review WHERE run_item_id IN ({placeholders}) ORDER BY created_at, rowid",
        item_ids,
    ).fetchall()
    latest: dict[str, dict] = {}
    for row in rows:
        latest[row["run_item_id"]] = _review_row_dict(row)
    return latest


def _latest_attributions(conn: sqlite3.Connection, item_ids: list[str]) -> dict[str, dict]:
    if not item_ids:
        return {}
    placeholders = ",".join("?" for _ in item_ids)
    rows = conn.execute(
        f"SELECT * FROM attribution WHERE run_item_id IN ({placeholders}) ORDER BY created_at, rowid",
        item_ids,
    ).fetchall()
    latest: dict[str, dict] = {}
    for row in rows:
        data = dict(row)
        data["evidenceRefs"] = _json_value(data.pop("evidence_refs_json"))
        data["createdAt"] = data.pop("created_at")
        data["subLabel"] = data.pop("sub_label")
        data["modelUsed"] = data.pop("model_used")
        latest[row["run_item_id"]] = data
    return latest


def _scorer_summary(trace: dict | None) -> dict:
    if not trace:
        return {
            "types": [],
            "primaryType": None,
            "names": [],
            "versionIds": [],
            "combinator": None,
            "profile": None,
            "members": [],
            "aggregate": None,
            "error": None,
            "errors": [],
        }
    members = []
    types: list[str] = []
    names: list[str] = []
    version_ids: list[str] = []
    errors = []
    for member in trace.get("members") or []:
        member_type = member.get("scorer_type")
        if member_type and member_type not in types:
            types.append(member_type)
        if member.get("scorer_name"):
            names.append(member["scorer_name"])
        if member.get("scorer_version_id"):
            version_ids.append(member["scorer_version_id"])
        member_error = member.get("error")
        if member_error:
            errors.append(member_error)
        members.append(
            {
                "name": member.get("scorer_name"),
                "type": member.get("scorer_type"),
                "versionId": member.get("scorer_version_id"),
                "version": member.get("scorer_version"),
                "state": member.get("state"),
                "confidence": member.get("confidence"),
                "score": member.get("score"),
                "label": member.get("label"),
                "detail": member.get("detail"),
                "error": member_error,
            }
        )
    return {
        "types": types,
        "primaryType": types[0] if types else trace.get("scorer_type"),
        "names": names,
        "versionIds": version_ids,
        "combinator": trace.get("combinator"),
        "profile": trace.get("profile"),
        "members": members,
        "aggregate": trace.get("aggregate"),
        "error": trace.get("error") or (errors[0] if errors else None),
        "errors": errors,
    }


def _trace_actual(trace: dict | None) -> Any:
    if not trace:
        return _MISSING
    for member in trace.get("members") or []:
        detail = member.get("detail") or {}
        for key in ("actual", "actual_text", "actual_value"):
            if key in detail:
                return detail[key]
    detail = trace.get("detail") or {}
    for key in ("actual", "actual_text", "actual_value"):
        if key in detail:
            return detail[key]
    return _MISSING


def _run_samples(conn: sqlite3.Connection, project_id: str, run_id: str) -> dict[str, dict]:
    row = conn.execute(
        """
        SELECT sv.dataset_version AS dataset_version
        FROM run r JOIN spec_version sv ON sv.id = r.spec_version_id
        WHERE r.id = ? AND r.project_id = ?
        """,
        (run_id, project_id),
    ).fetchone()
    if row is None:
        return {}
    version = datasets.resolve_version_ref(conn, project_id, row["dataset_version"])
    if version is None:
        return {}
    return {
        sample["sample_id"]: sample
        for sample in datasets.dataset_version_samples(conn, version["id"])
    }


def _build_item(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    review: dict | None,
    attribution: dict | None,
    samples: dict[str, dict],
) -> dict:
    raw_state = row["state"]
    review_state = row["review_state"] if "review_state" in row.keys() else None
    current_state = review_state or raw_state
    trace = _json_value(row["scorer_trace_json"]) if row["scorer_trace_json"] else None
    scorer = _scorer_summary(trace)
    sample = samples.get(row["case_ref"])
    actual_present = row["actual_json"] is not None
    if actual_present:
        actual = _json_value(row["actual_json"])
    else:
        trace_actual = _trace_actual(trace)
        actual = None if trace_actual is _MISSING else trace_actual
        actual_present = trace_actual is not _MISSING
    expected = sample.get("expected") if sample else _MISSING
    input_value = sample.get("input") if sample else _MISSING
    context_value = sample.get("context") if sample else None

    missing: list[str] = []
    if sample is None or input_value is _MISSING:
        missing.append("sample_input")
    if expected is _MISSING or expected is None:
        missing.append("expected")
    if not actual_present:
        missing.append("actual_output")
    if trace is None:
        missing.append("scorer_trace")
    elif not scorer["members"] and not scorer.get("error"):
        missing.append("scorer_trace_members")

    if current_state == "error":
        missing = [item for item in missing if item not in ("expected", "actual_output")]
        if not scorer.get("error"):
            missing.append("error_detail")

    return {
        "itemId": row["id"],
        "runId": row["run_id"],
        "caseRef": row["case_ref"],
        "queueKind": current_state if current_state in QUEUE_KINDS else None,
        "rawState": raw_state,
        "currentState": current_state,
        "confidence": row["confidence"],
        "durationMs": row["duration_ms"],
        "createdAt": row["created_at"],
        "reviewedAt": row["reviewed_at"],
        "run": {
            "commitSha": row["commit_sha"],
            "branch": row["branch"],
            "channel": row["channel"],
            "status": row["run_status"],
            "specVersionId": row["spec_version_id"],
        },
        "sample": (
            {
                "sampleId": sample.get("sample_id"),
                "input": sample.get("input"),
                "expected": sample.get("expected"),
                "context": sample.get("context"),
                "priority": sample.get("priority"),
                "tags": sample.get("tags"),
            }
            if sample is not None
            else None
        ),
        "actual": actual,
        "actualPresent": actual_present,
        "expected": None if expected is _MISSING else expected,
        "context": context_value,
        "scorer": {
            "types": scorer["types"],
            "primaryType": scorer["primaryType"],
            "names": scorer["names"],
            "versionIds": scorer["versionIds"],
            "combinator": scorer["combinator"],
            "profile": scorer["profile"],
            "state": trace.get("state") if trace else None,
            "aggregate": scorer["aggregate"],
            "members": scorer["members"],
            "error": scorer.get("error"),
        },
        "attribution": attribution,
        "review": review,
        "decisionEvidence": {
            "sample": (
                {
                    "sampleId": sample.get("sample_id"),
                    "input": sample.get("input"),
                    "expected": sample.get("expected"),
                    "context": sample.get("context"),
                }
                if sample is not None
                else None
            ),
            "actual": actual,
            "scorer": scorer,
            "attribution": attribution,
        },
        "decisionReady": not missing,
        "missingEvidence": missing,
    }


def _base_select() -> str:
    return """
        SELECT ri.*, r.commit_sha, r.branch, r.channel, r.status AS run_status,
               r.spec_version_id, r.created_at AS run_created_at,
               v.scorer_trace_json
        FROM run_item ri
        JOIN run r ON r.id = ri.run_id
        LEFT JOIN verdict v ON v.run_item_id = ri.id
    """


def review_queue(
    conn: sqlite3.Connection,
    actor: dict,
    project_id: str,
    *,
    kind: str = "all",
    status: str = "open",
    attribution_label: str | None = None,
    scorer_type: str | None = None,
    run_id: str | None = None,
    limit: int = 1000,
) -> dict:
    """Return uncertain and error items in two explicitly separate groups."""

    require_project(actor, project_id)
    if kind not in ("all",) + QUEUE_KINDS:
        raise ApiError(400, "validation_error", f"'kind' must be one of {['all', *QUEUE_KINDS]}")
    if status not in STATUS_FILTERS:
        raise ApiError(400, "validation_error", f"'status' must be one of {list(STATUS_FILTERS)}")
    if limit < 1 or limit > 1000:
        raise ApiError(400, "validation_error", "'limit' must be between 1 and 1000")
    if attribution_label is not None and attribution_label not in (
        "product_defect",
        "script_failure",
        "env_issue",
        "data_issue",
        "unknown",
    ):
        raise ApiError(400, "validation_error", "'attribution_label' is not a known label")
    if run_id:
        if conn.execute(
            "SELECT 1 FROM run WHERE id = ? AND project_id = ?", (run_id, project_id)
        ).fetchone() is None:
            raise ApiError(404, "not_found", "run not found in this project")

    sql = _base_select() + " WHERE r.project_id = ?"
    params: list[Any] = [project_id]
    if run_id:
        sql += " AND r.id = ?"
        params.append(run_id)
    sql += " ORDER BY ri.created_at, ri.case_ref"
    rows = conn.execute(sql, params).fetchall()
    item_ids = [row["id"] for row in rows]
    reviews = _latest_reviews(conn, item_ids)
    attributions = _latest_attributions(conn, item_ids)
    sample_cache: dict[str, dict[str, dict]] = {}

    groups = {
        key: {"count": 0, "open": 0, "reviewed": 0, "truncated": False, "items": []}
        for key in QUEUE_KINDS
    }
    totals = {"items": 0, "open": 0, "reviewed": 0}
    for row in rows:
        review = reviews.get(row["id"])
        if status == "open" and review is not None:
            continue
        if status == "reviewed" and review is None:
            continue
        row_run_id = row["run_id"]
        if row_run_id not in sample_cache:
            sample_cache[row_run_id] = _run_samples(conn, project_id, row_run_id)
        item = _build_item(
            conn,
            row,
            review=review,
            attribution=attributions.get(row["id"]),
            samples=sample_cache[row_run_id],
        )
        queue_kind = item["queueKind"]
        if queue_kind not in QUEUE_KINDS:
            continue
        if attribution_label is not None:
            label = (item.get("attribution") or {}).get("label")
            if label != attribution_label:
                continue
        if scorer_type is not None and scorer_type not in item["scorer"]["types"]:
            continue
        totals["items"] += 1
        totals["reviewed" if review is not None else "open"] += 1
        group = groups[queue_kind]
        group["count"] += 1
        group["reviewed" if review is not None else "open"] += 1
        if kind not in ("all", queue_kind):
            continue
        if len(group["items"]) >= limit:
            group["truncated"] = True
            continue
        group["items"].append(item)

    return {
        "projectId": project_id,
        "generatedAt": now(),
        "filters": {
            "kind": kind,
            "status": status,
            "attributionLabel": attribution_label,
            "scorerType": scorer_type,
            "runId": run_id,
            "limit": limit,
        },
        "counts": totals,
        "groups": groups,
        "separation": {
            "uncertain": "uncertain and unresolved by review",
            "error": "execution errors; never part of the quality denominator",
        },
    }


def _evidence_snapshot(item: dict) -> dict:
    return {
        "itemId": item["itemId"],
        "runId": item["runId"],
        "caseRef": item["caseRef"],
        "rawState": item["rawState"],
        "currentState": item["currentState"],
        "sample": item["sample"],
        "actual": item["actual"],
        "actualPresent": item.get("actualPresent", item["actual"] is not None),
        "expected": item["expected"],
        "context": item["context"],
        "scorer": item["scorer"],
        "attribution": item["attribution"],
        "run": item["run"],
    }


def record_review(
    conn: sqlite3.Connection,
    actor: dict,
    project_id: str,
    run_id: str,
    item_id: str,
    payload: dict,
) -> dict:
    require_project(actor, project_id)
    require_write(actor)
    conclusion = payload.get("conclusion")
    if conclusion not in CONCLUSIONS:
        raise ApiError(
            400,
            "validation_error",
            f"'conclusion' must be one of {list(CONCLUSIONS)}",
        )
    note = _optional_text(payload, "note")
    row = conn.execute(
        """
        SELECT ri.*, r.commit_sha, r.branch, r.channel, r.status AS run_status,
               r.spec_version_id, r.created_at AS run_created_at,
               v.scorer_trace_json
        FROM run_item ri
        JOIN run r ON r.id = ri.run_id
        LEFT JOIN verdict v ON v.run_item_id = ri.id
        WHERE ri.id = ? AND ri.run_id = ? AND r.project_id = ?
        """,
        (item_id, run_id, project_id),
    ).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "run item not found")
    prior_review = _latest_reviews(conn, [item_id]).get(item_id)
    review_state = row["review_state"]
    prior_state = review_state or row["state"]
    if prior_state not in QUEUE_KINDS:
        raise ApiError(
            409,
            "conflict",
            f"item current state '{prior_state}' is not in the review queue",
        )
    sample = _run_samples(conn, project_id, run_id).get(row["case_ref"])
    item = _build_item(
        conn,
        row,
        review=prior_review,
        attribution=_latest_attributions(conn, [item_id]).get(item_id),
        samples={row["case_ref"]: sample} if sample is not None else {},
    )
    if prior_state == "uncertain":
        resulting_state = CONCLUSION_TO_STATE[conclusion]
    else:
        # Execution errors stay errors: reviewing them records triage but never
        # moves them into the quality denominator.
        resulting_state = "error"
    snapshot = _evidence_snapshot(item)
    timestamp = now()
    review_id = new_id("rev")
    scorer = item["scorer"]
    primary_type = scorer["primaryType"]
    attribution = item["attribution"] or {}
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO review
              (id, run_item_id, reviewer, reviewer_id, project_id, conclusion, note,
               prior_state, resulting_state, scorer_type, attribution_label,
               evidence_snapshot_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                review_id,
                item_id,
                actor["name"],
                actor["id"],
                project_id,
                conclusion,
                note,
                prior_state,
                resulting_state,
                primary_type,
                attribution.get("label"),
                _json_text(snapshot),
                timestamp,
            ),
        )
        if prior_state == "uncertain":
            conn.execute(
                "UPDATE run_item SET review_state = ?, reviewed_at = ? WHERE id = ?",
                (resulting_state, timestamp, item_id),
            )
        else:
            conn.execute(
                "UPDATE run_item SET reviewed_at = ? WHERE id = ?", (timestamp, item_id)
            )
        calibration = _upsert_calibration(
            conn,
            project_id=project_id,
            review_id=review_id,
            run_id=run_id,
            item_id=item_id,
            item=item,
            conclusion=conclusion,
            prior_state=prior_state,
            snapshot=snapshot,
            actor=actor,
            timestamp=timestamp,
        )
        conn.execute(
            "UPDATE review SET calibration_sample_id = ? WHERE id = ?",
            (calibration["calibrationId"], review_id),
        )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise

    review_row = conn.execute("SELECT * FROM review WHERE id = ?", (review_id,)).fetchone()
    return {
        "review": _review_row_dict(review_row),
        "item": {
            "itemId": item_id,
            "caseRef": row["case_ref"],
            "rawState": item["rawState"],
            "currentState": resulting_state,
            "reviewedAt": timestamp,
        },
        "calibration": calibration,
        "report": review_report(conn, actor, project_id, run_id, include_items=False),
    }


def _upsert_calibration(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    review_id: str,
    run_id: str,
    item_id: str,
    item: dict,
    conclusion: str,
    prior_state: str,
    snapshot: dict,
    actor: dict,
    timestamp: str,
) -> dict:
    sample = item["sample"] or {}
    sample_key = item["caseRef"]
    input_value = sample.get("input") if sample else _MISSING
    if input_value is _MISSING:
        raise ApiError(
            409,
            "conflict",
            "the run item's sample input is unavailable; refusing to sink an unusable calibration sample",
        )
    expected_state = None
    usable = 0
    exclusion_reason = None
    if prior_state == "uncertain":
        if conclusion == "confirmed_defect":
            expected_state = "fail"
            usable = 1
        elif conclusion == "false_positive":
            expected_state = "pass"
            usable = 1
        else:
            exclusion_reason = "sample_error"
    else:
        exclusion_reason = "execution_error"
    scorer = item["scorer"]
    primary_type = scorer["primaryType"]
    primary_name = scorer["names"][0] if scorer["names"] else None
    primary_version = scorer["versionIds"][0] if scorer["versionIds"] else None
    existing = conn.execute(
        "SELECT id, created_at FROM calibration_sample WHERE run_item_id = ?", (item_id,)
    ).fetchone()
    calibration_id = existing["id"] if existing is not None else new_id("cal")
    created_at = existing["created_at"] if existing is not None else timestamp
    dataset_id = None
    dataset_version_id = None
    row = conn.execute(
        """
        SELECT sv.dataset_version FROM run r
        JOIN spec_version sv ON sv.id = r.spec_version_id
        WHERE r.id = ? AND r.project_id = ?
        """,
        (run_id, project_id),
    ).fetchone()
    if row is not None:
        version = datasets.resolve_version_ref(conn, project_id, row["dataset_version"])
        if version is not None:
            dataset_id = version["dataset_id"]
            dataset_version_id = version["id"]
    values = (
        project_id,
        review_id,
        run_id,
        item_id,
        dataset_id,
        dataset_version_id,
        sample_key,
        _json_text(input_value),
        _json_text(sample.get("expected")) if sample.get("expected") is not None else None,
        _json_text(item["actual"]) if item.get("actualPresent") else None,
        _json_text(sample.get("context")) if sample.get("context") is not None else None,
        item["rawState"],
        expected_state,
        conclusion,
        primary_type,
        primary_name,
        primary_version,
        (scorer["members"][0].get("score") if scorer["members"] else None),
        item.get("confidence"),
        _json_text(snapshot),
        usable,
        exclusion_reason,
        actor["id"],
        created_at,
        timestamp,
    )
    if existing is None:
        conn.execute(
            """
            INSERT INTO calibration_sample
              (id, project_id, review_id, run_id, run_item_id, dataset_id, dataset_version_id,
               sample_key, input_json, expected_json, actual_json, context_json,
               scored_state, expected_state, conclusion, scorer_type, scorer_name,
               scorer_version_id, score, confidence, evidence_json, usable,
               exclusion_reason, created_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (calibration_id, *values),
        )
    else:
        conn.execute(
            """
            UPDATE calibration_sample
            SET project_id = ?, review_id = ?, run_id = ?, run_item_id = ?, dataset_id = ?,
                dataset_version_id = ?, sample_key = ?, input_json = ?, expected_json = ?,
                actual_json = ?, context_json = ?, scored_state = ?, expected_state = ?,
                conclusion = ?, scorer_type = ?, scorer_name = ?, scorer_version_id = ?,
                score = ?, confidence = ?, evidence_json = ?, usable = ?,
                exclusion_reason = ?, created_by = ?, created_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (*values, calibration_id),
        )
    row = conn.execute(
        """
        SELECT cs.*, rv.reviewer AS reviewer_name, rv.created_at AS review_created_at
        FROM calibration_sample cs JOIN review rv ON rv.id = cs.review_id
        WHERE cs.id = ?
        """,
        (calibration_id,),
    ).fetchone()
    return _calibration_dict(row)


def _calibration_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["calibrationId"] = data.pop("id")
    data["projectId"] = data.pop("project_id")
    data["reviewId"] = data.pop("review_id")
    data["runId"] = data.pop("run_id")
    data["runItemId"] = data.pop("run_item_id")
    data["datasetId"] = data.pop("dataset_id")
    data["datasetVersionId"] = data.pop("dataset_version_id")
    data["sampleId"] = data.pop("sample_key")
    data["input"] = _json_value(data.pop("input_json"))
    data["expected"] = _json_value(data.pop("expected_json"))
    data["actual"] = _json_value(data.pop("actual_json"))
    data["context"] = _json_value(data.pop("context_json"))
    data["scoredState"] = data.pop("scored_state")
    data["expectedState"] = data.pop("expected_state")
    data["scorerType"] = data.pop("scorer_type")
    data["scorerName"] = data.pop("scorer_name")
    data["scorerVersionId"] = data.pop("scorer_version_id")
    data["countedScore"] = data.pop("score")
    data["evidence"] = _json_value(data.pop("evidence_json"))
    data["usable"] = bool(data.pop("usable"))
    data["exclusionReason"] = data.pop("exclusion_reason")
    data["createdBy"] = data.pop("created_by")
    data["createdAt"] = data.pop("created_at")
    data["updatedAt"] = data.pop("updated_at")
    data["reviewer"] = data.pop("reviewer_name", None) or data["createdBy"]
    data["reviewedAt"] = data.pop("review_created_at", None) or data["updatedAt"]
    return data


def list_reviews(
    conn: sqlite3.Connection,
    actor: dict,
    project_id: str,
    *,
    run_id: str | None = None,
    item_id: str | None = None,
) -> list[dict]:
    require_project(actor, project_id)
    sql = """
        SELECT rv.*, ri.case_ref, ri.run_id
        FROM review rv JOIN run_item ri ON ri.id = rv.run_item_id
        WHERE rv.project_id = ?
    """
    params: list[Any] = [project_id]
    if run_id:
        sql += " AND ri.run_id = ?"
        params.append(run_id)
    if item_id:
        sql += " AND rv.run_item_id = ?"
        params.append(item_id)
    sql += " ORDER BY rv.created_at, rv.rowid"
    return [
        {**_review_row_dict(row), "caseRef": row["case_ref"], "runId": row["run_id"]}
        for row in conn.execute(sql, params).fetchall()
    ]


def _latest_reviews_for_run(conn: sqlite3.Connection, run_id: str) -> dict[str, dict]:
    rows = conn.execute(
        """
        SELECT rv.* FROM review rv JOIN run_item ri ON ri.id = rv.run_item_id
        WHERE ri.run_id = ? ORDER BY rv.created_at, rv.rowid
        """,
        (run_id,),
    ).fetchall()
    latest: dict[str, dict] = {}
    for row in rows:
        latest[row["run_item_id"]] = _review_row_dict(row)
    return latest


def review_report(
    conn: sqlite3.Connection,
    actor: dict,
    project_id: str,
    run_id: str,
    *,
    include_items: bool = True,
) -> dict:
    require_project(actor, project_id)
    run = conn.execute(
        "SELECT id, status, channel FROM run WHERE id = ? AND project_id = ?",
        (run_id, project_id),
    ).fetchone()
    if run is None:
        raise ApiError(404, "not_found", "run not found")
    rows = conn.execute(
        """
        SELECT ri.id, ri.case_ref, ri.state, ri.review_state, ri.reviewed_at, ri.confidence
        FROM run_item ri WHERE ri.run_id = ? ORDER BY ri.case_ref
        """,
        (run_id,),
    ).fetchall()
    reviews = _latest_reviews_for_run(conn, run_id)
    raw_states = {"pass": 0, "fail": 0, "uncertain": 0, "error": 0}
    effective_states = {"pass": 0, "fail": 0, "uncertain": 0, "error": 0, "excluded": 0}
    queue = {"uncertain": {"open": 0, "reviewed": 0}, "error": {"open": 0, "reviewed": 0}}
    by_conclusion = {key: 0 for key in CONCLUSIONS}
    items = []
    for row in rows:
        raw_states[row["state"]] += 1
        review_state = row["review_state"]
        current = review_state or row["state"]
        effective_states[current] = effective_states.get(current, 0) + 1
        review = reviews.get(row["id"])
        if current in QUEUE_KINDS:
            queue[current]["reviewed" if review is not None else "open"] += 1
        if review is not None:
            by_conclusion[review["conclusion"]] = by_conclusion.get(review["conclusion"], 0) + 1
        if include_items:
            items.append(
                {
                    "itemId": row["id"],
                    "caseRef": row["case_ref"],
                    "rawState": row["state"],
                    "currentState": current,
                    "confidence": row["confidence"],
                    "reviewedAt": row["reviewed_at"],
                    "review": review,
                }
            )
    denominator = (
        effective_states["pass"] + effective_states["fail"] + effective_states["uncertain"]
    )
    reviewed_items = sum(by_conclusion.values())
    quality = {
        "denominator": denominator,
        "pass": effective_states["pass"],
        "fail": effective_states["fail"],
        "uncertain": effective_states["uncertain"],
        "passRate": round(effective_states["pass"] / denominator, 6) if denominator else None,
        "failRate": round(effective_states["fail"] / denominator, 6) if denominator else None,
        "uncertainRate": round(effective_states["uncertain"] / denominator, 6) if denominator else None,
        "excludedFromDenominator": {
            "error": effective_states["error"],
            "sampleError": effective_states["excluded"],
            "total": effective_states["error"] + effective_states["excluded"],
        },
        "errorExcludedFromDenominator": True,
        "policy": (
            "quality denominator = effective pass + fail + uncertain; "
            "error and sample_error are reported separately and never counted"
        ),
    }
    fingerprint_payload = {
        "runId": run_id,
        "rawStates": raw_states,
        "effectiveStates": effective_states,
        "byConclusion": by_conclusion,
        "denominator": denominator,
    }
    report = {
        "schemaVersion": 1,
        "runId": run_id,
        "runStatus": run["status"],
        "channel": run["channel"],
        "generatedAt": now(),
        "totalItems": len(rows),
        "rawStates": raw_states,
        "effectiveStates": effective_states,
        "reviewedItems": reviewed_items,
        "unreviewedItems": max(0, len(rows) - reviewed_items),
        "reviewsByConclusion": by_conclusion,
        "queue": queue,
        "quality": quality,
        "reportFingerprint": _sha256(_canonical(fingerprint_payload)),
    }
    if include_items:
        report["items"] = items
    return report


def run_review_summary(conn: sqlite3.Connection, project_id: str, run_id: str) -> dict:
    """Lightweight projection embedded in GET /runs/{id}."""

    try:
        report = review_report(
            conn,
            {"project_id": project_id, "role": "readonly"},
            project_id,
            run_id,
            include_items=False,
        )
    except ApiError:
        return {}
    return {
        key: report[key]
        for key in (
            "rawStates",
            "effectiveStates",
            "reviewedItems",
            "unreviewedItems",
            "reviewsByConclusion",
            "queue",
            "quality",
            "reportFingerprint",
        )
    }


def _calibration_query(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    scorer_type: str | None,
    conclusion: str | None,
    include_unusable: bool,
    run_id: str | None,
    dataset_id: str | None,
) -> list[sqlite3.Row]:
    sql = """
        SELECT cs.*, rv.reviewer AS reviewer_name, rv.created_at AS review_created_at
        FROM calibration_sample cs JOIN review rv ON rv.id = cs.review_id
        WHERE cs.project_id = ?
    """
    params: list[Any] = [project_id]
    if scorer_type:
        sql += " AND cs.scorer_type = ?"
        params.append(scorer_type)
    if conclusion:
        if conclusion not in CONCLUSIONS:
            raise ApiError(400, "validation_error", f"'conclusion' must be one of {list(CONCLUSIONS)}")
        sql += " AND cs.conclusion = ?"
        params.append(conclusion)
    if not include_unusable:
        sql += " AND cs.usable = 1"
    if run_id:
        sql += " AND cs.run_id = ?"
        params.append(run_id)
    if dataset_id:
        sql += " AND cs.dataset_id = ?"
        params.append(dataset_id)
    sql += " ORDER BY cs.created_at, cs.id"
    return conn.execute(sql, params).fetchall()


def list_calibration_samples(
    conn: sqlite3.Connection,
    actor: dict,
    project_id: str,
    *,
    scorer_type: str | None = None,
    conclusion: str | None = None,
    include_unusable: bool = False,
    run_id: str | None = None,
    dataset_id: str | None = None,
) -> dict:
    require_project(actor, project_id)
    rows = _calibration_query(
        conn,
        project_id,
        scorer_type=scorer_type,
        conclusion=conclusion,
        include_unusable=include_unusable,
        run_id=run_id,
        dataset_id=dataset_id,
    )
    items = [_calibration_dict(row) for row in rows]
    return {
        "projectId": project_id,
        "count": len(items),
        "usableCount": sum(1 for item in items if item["usable"]),
        "unusableCount": sum(1 for item in items if not item["usable"]),
        "includeUnusable": include_unusable,
        "items": items,
    }


def _export_row(item: dict) -> dict:
    return {
        "schemaVersion": 1,
        "calibrationId": item["calibrationId"],
        "projectId": item["projectId"],
        "source": {
            "runId": item["runId"],
            "runItemId": item["runItemId"],
            "reviewId": item["reviewId"],
            "datasetId": item["datasetId"],
            "datasetVersionId": item["datasetVersionId"],
        },
        "sample": {
            "sampleId": item["sampleId"],
            "input": item["input"],
            "expected": item["expected"],
            "context": item["context"],
        },
        "actual": item["actual"],
        "expectedState": item["expectedState"],
        "usableForCalibration": item["usable"],
        "exclusionReason": item["exclusionReason"],
        "review": {
            "conclusion": item["conclusion"],
            "reviewer": item["reviewer"],
            "reviewedAt": item["reviewedAt"],
        },
        "scorer": {
            "type": item["scorerType"],
            "name": item["scorerName"],
            "versionId": item["scorerVersionId"],
            "state": item["scoredState"],
            "confidence": item["confidence"],
            "score": item["countedScore"],
        },
    }


def export_calibration(
    conn: sqlite3.Connection,
    actor: dict,
    project_id: str,
    *,
    fmt: str = "jsonl",
    scorer_type: str | None = None,
    conclusion: str | None = None,
    include_unusable: bool = False,
    run_id: str | None = None,
    dataset_id: str | None = None,
) -> dict:
    require_project(actor, project_id)
    if fmt not in ("jsonl", "json", "csv"):
        raise ApiError(400, "validation_error", "'format' must be jsonl, json or csv")
    rows = _calibration_query(
        conn,
        project_id,
        scorer_type=scorer_type,
        conclusion=conclusion,
        include_unusable=include_unusable,
        run_id=run_id,
        dataset_id=dataset_id,
    )
    items = [_calibration_dict(row) for row in rows]
    exported = [_export_row(item) for item in items]
    if fmt == "jsonl":
        content = "".join(_json_text(row) + "\n" for row in exported)
    elif fmt == "json":
        content = json.dumps(exported, ensure_ascii=False, indent=2) + "\n"
    else:
        import csv
        import io

        buffer = io.StringIO()
        fieldnames = [
            "calibrationId",
            "runId",
            "runItemId",
            "sampleId",
            "input",
            "expected",
            "actual",
            "expectedState",
            "usableForCalibration",
            "exclusionReason",
            "conclusion",
            "scorerType",
            "scorerName",
            "scorerVersionId",
            "reviewer",
            "reviewedAt",
        ]
        writer = csv.DictWriter(buffer, fieldnames=fieldnames)
        writer.writeheader()
        for row in exported:
            writer.writerow(
                {
                    "calibrationId": row["calibrationId"],
                    "runId": row["source"]["runId"],
                    "runItemId": row["source"]["runItemId"],
                    "sampleId": row["sample"]["sampleId"],
                    "input": _json_text(row["sample"]["input"]),
                    "expected": _json_text(row["sample"]["expected"]),
                    "actual": _json_text(row["actual"]),
                    "expectedState": row["expectedState"] or "",
                    "usableForCalibration": "true" if row["usableForCalibration"] else "false",
                    "exclusionReason": row["exclusionReason"] or "",
                    "conclusion": row["review"]["conclusion"],
                    "scorerType": row["scorer"]["type"] or "",
                    "scorerName": row["scorer"]["name"] or "",
                    "scorerVersionId": row["scorer"]["versionId"] or "",
                    "reviewer": row["review"]["reviewer"],
                    "reviewedAt": row["review"]["reviewedAt"],
                }
            )
        content = buffer.getvalue()
    digest = _sha256(content)
    return {
        "format": fmt,
        "projectId": project_id,
        "content": content,
        "count": len(exported),
        "usableCount": sum(1 for row in exported if row["usableForCalibration"]),
        "unusableCount": sum(1 for row in exported if not row["usableForCalibration"]),
        "includeUnusable": include_unusable,
        "contentSha256": digest,
        "filters": {
            "scorerType": scorer_type,
            "conclusion": conclusion,
            "runId": run_id,
            "datasetId": dataset_id,
        },
        "rows": exported,
    }
