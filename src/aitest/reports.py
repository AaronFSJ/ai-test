"""Immutable run-report snapshots.

A snapshot is built from the run's bound spec version and the records that
exist at generation time, serialised to canonical JSON, digested and stored.
Later scorer/model/retention configuration changes cannot rewrite it: the
database rejects UPDATE/DELETE on ``report_snapshot`` and the read path
verifies the stored digest on every access.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from . import evidence as evidence_service
from . import redaction, service, storage


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _snapshot_dict(row: sqlite3.Row, *, include_content: bool = True) -> dict:
    data = dict(row)
    text = data.pop("content_json")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if digest != data["content_sha256"]:
        raise service.ApiError(
            500, "report_corrupt", "stored report snapshot content does not match its digest"
        )
    data["uri"] = storage.URI_SCHEME + data["storage_key"]
    if include_content:
        data["content"] = json.loads(text)
    return data


def _build_content(
    conn: sqlite3.Connection,
    actor: dict,
    project_id: str,
    run_id: str,
    snapshot_id: str,
    report_version: int,
    created_at: str,
) -> dict:
    run = service.get_run(conn, project_id, run_id)
    items: list[dict] = []
    counts = {"pass": 0, "fail": 0, "uncertain": 0, "error": 0}
    evidence_total = 0
    for item in run["items"]:
        counts[item["state"]] = counts.get(item["state"], 0) + 1
        verdict_row = conn.execute(
            "SELECT * FROM verdict WHERE run_item_id = ?", (item["id"],)
        ).fetchone()
        verdict = service._verdict_dict(verdict_row) if verdict_row is not None else None
        ev_rows = conn.execute(
            "SELECT * FROM evidence WHERE run_item_id = ? ORDER BY created_at, id", (item["id"],)
        ).fetchall()
        item_evidence = []
        for row in ev_rows:
            record = evidence_service._evidence_dict(row)
            evidence_total += 1
            item_evidence.append(
                {
                    "id": record["id"],
                    "kind": record["kind"],
                    "filename": record["filename"],
                    "uri": record["uri"],
                    "bytes": record["bytes"],
                    "bytesOriginal": record["bytes_original"],
                    "contentSha256": record["content_sha256"],
                    "contentType": record["content_type"],
                    "redacted": record["redacted"],
                    "redaction": record["redaction"],
                    "retentionClass": record["retention_class"],
                    "retentionExpiresAt": record["retention_expires_at"],
                    "deletedAt": record["deleted_at"],
                }
            )
        attribution_rows = conn.execute(
            "SELECT * FROM attribution WHERE run_item_id = ? ORDER BY created_at", (item["id"],)
        ).fetchall()
        attributions = []
        for row in attribution_rows:
            record = dict(row)
            record["evidence_refs"] = json.loads(record.pop("evidence_refs_json") or "[]")
            attributions.append(record)
        items.append(
            {
                "id": item["id"],
                "caseRef": item["case_ref"],
                "priority": item.get("priority"),
                "state": item["state"],
                "confidence": item["confidence"],
                "durationMs": item["duration_ms"],
                "verdict": verdict,
                "attributions": attributions,
                "evidence": item_evidence,
            }
        )

    policy = evidence_service.get_retention_policy(conn, actor, project_id)
    spec_version = run["spec_version"]
    content = {
        "schemaVersion": 1,
        "kind": "run_report_snapshot",
        "id": snapshot_id,
        "reportVersion": report_version,
        "projectId": project_id,
        "runId": run_id,
        "generatedAt": created_at,
        "generatedBy": {"id": actor["id"], "name": actor["name"]},
        "run": {
            "id": run["id"],
            "channel": run["channel"],
            "status": run["status"],
            "commitSha": run["commit_sha"],
            "branch": run["branch"],
            "environment": run["env"],
            "trigger": run["trigger"],
            "specVersionId": run["spec_version_id"],
            "specVersion": {
                "id": spec_version["id"],
                "specId": spec_version["spec_id"],
                "specName": spec_version["spec_name"],
                "version": spec_version["version"],
                "channel": spec_version["channel"],
                "repoPath": spec_version["repo_path"],
                "repoCommit": spec_version["repo_commit"],
                "datasetVersion": spec_version["dataset_version"],
                "scorerVersion": spec_version["scorer_version"],
                "modelSnapshot": spec_version["model_snapshot"],
                "params": spec_version["params"],
            },
            "modelSnapshot": run["model_snapshot"],
            "samplingConfig": run["sampling_config"],
            "createdAt": run["created_at"],
            "startedAt": run["started_at"],
            "finishedAt": run["finished_at"],
        },
        "summary": {
            "totalItems": len(items),
            "counts": counts,
            "evidenceCount": evidence_total,
            "verdictCount": sum(1 for item in items if item["verdict"] is not None),
        },
        "configSnapshot": {
            "specVersionId": run["spec_version_id"],
            "scorerVersion": spec_version["scorer_version"],
            "datasetVersion": spec_version["dataset_version"],
            "modelSnapshot": spec_version["model_snapshot"],
            "samplingConfig": run["sampling_config"],
            "retentionPolicy": policy,
            "redactionRulesVersion": redaction.RULES_VERSION,
        },
        "items": items,
    }
    redacted_content, report = redaction.redact_json_document(content)
    redacted_content["snapshotRedaction"] = report
    return redacted_content


def create_report_snapshot(
    conn: sqlite3.Connection,
    store: storage.ObjectStore,
    actor: dict,
    project_id: str,
    run_id: str,
) -> dict:
    service.require_project(actor, project_id)
    service.require_write(actor)
    if conn.execute(
        "SELECT 1 FROM run WHERE id = ? AND project_id = ?", (run_id, project_id)
    ).fetchone() is None:
        raise service.ApiError(404, "not_found", "run not found")

    snapshot_id = service.new_id("rpt")
    created_at = service.now()
    version = conn.execute(
        "SELECT COALESCE(MAX(report_version), 0) + 1 FROM report_snapshot WHERE run_id = ?",
        (run_id,),
    ).fetchone()[0]
    content = _build_content(conn, actor, project_id, run_id, snapshot_id, version, created_at)
    text = _canonical(content)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    run_row = conn.execute(
        "SELECT spec_version_id FROM run WHERE id = ? AND project_id = ?", (run_id, project_id)
    ).fetchone()
    storage_key = f"projects/{project_id}/runs/{run_id}/reports/{snapshot_id}.json"
    store.put(storage_key, text.encode("utf-8"))
    try:
        conn.execute(
            """
            INSERT INTO report_snapshot
              (id, project_id, run_id, spec_version_id, report_version,
               content_json, content_sha256, storage_key, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                project_id,
                run_id,
                run_row["spec_version_id"],
                version,
                text,
                digest,
                storage_key,
                actor["id"],
                created_at,
            ),
        )
    except Exception:
        store.delete(storage_key)
        raise
    return get_report_snapshot(conn, actor, project_id, run_id, snapshot_id)


def list_report_snapshots(
    conn: sqlite3.Connection, actor: dict, project_id: str, run_id: str
) -> list[dict]:
    service.require_project(actor, project_id)
    if conn.execute(
        "SELECT 1 FROM run WHERE id = ? AND project_id = ?", (run_id, project_id)
    ).fetchone() is None:
        raise service.ApiError(404, "not_found", "run not found")
    rows = conn.execute(
        """
        SELECT * FROM report_snapshot
        WHERE project_id = ? AND run_id = ?
        ORDER BY report_version DESC, id
        """,
        (project_id, run_id),
    ).fetchall()
    return [_snapshot_dict(row, include_content=False) for row in rows]


def get_report_snapshot(
    conn: sqlite3.Connection,
    actor: dict,
    project_id: str,
    run_id: str,
    snapshot_id: str,
    *,
    include_content: bool = True,
) -> dict:
    service.require_project(actor, project_id)
    row = conn.execute(
        """
        SELECT * FROM report_snapshot
        WHERE id = ? AND project_id = ? AND run_id = ?
        """,
        (snapshot_id, project_id, run_id),
    ).fetchone()
    if row is None:
        raise service.ApiError(404, "not_found", "report snapshot not found")
    return _snapshot_dict(row, include_content=include_content)


def read_report_content(
    conn: sqlite3.Connection,
    actor: dict,
    project_id: str,
    run_id: str,
    snapshot_id: str,
) -> dict:
    """Return the exact canonical text that was stored at generation time."""

    service.require_project(actor, project_id)
    row = conn.execute(
        """
        SELECT * FROM report_snapshot
        WHERE id = ? AND project_id = ? AND run_id = ?
        """,
        (snapshot_id, project_id, run_id),
    ).fetchone()
    if row is None:
        raise service.ApiError(404, "not_found", "report snapshot not found")
    text = row["content_json"]
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if digest != row["content_sha256"]:
        raise service.ApiError(
            500, "report_corrupt", "stored report snapshot content does not match its digest"
        )
    return {
        "snapshot_id": row["id"],
        "run_id": row["run_id"],
        "content_sha256": digest,
        "content_type": "application/json",
        "filename": f"report-{row['id']}.json",
        "content": text.encode("utf-8"),
        "report_version": row["report_version"],
        "created_at": row["created_at"],
    }
