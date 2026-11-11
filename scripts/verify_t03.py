#!/usr/bin/env python3
"""Run reproducible T03 checks against a real local HTTP server.

Writes machine-readable evidence under deliverables/evidence (or --out):
schema/migrations, evidence drill-down, redaction before/after, report
snapshot stability, and retention cleanup. Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aitest import db, http_api, migrations, redaction, service  # noqa: E402

SAMPLE_BEFORE = "\n".join(
    [
        "2026-11-11T09:00:00Z INFO login ok user=alice",
        "Authorization: Bearer " + "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
        "cookie: sessionid=abc123; theme=dark",
        "phone=13800138000 email=alice@example.com",
        "GET https://files.example.com/report.pdf?X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20261111&X-Amz-Signature=deadbeefcafe&token=abc123&page=2#access_token=zzz",
        '{"access_token": "sk-live-very-secret", "password": "hunter2", "tokens_in": 100, "note": "keep this"}',
    ]
)


def now(delta_days: int = 0) -> str:
    moment = datetime.now(timezone.utc) + timedelta(days=delta_days)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def excerpt(value: Any, limit: int = 1200) -> Any:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if len(text) <= limit:
            return value
        return {"excerpt": text[:limit] + "...", "truncated": True}
    if isinstance(value, bytes):
        try:
            text = value.decode("utf-8")
        except UnicodeDecodeError:
            return {"bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
        if len(text) > limit:
            return text[:limit] + "..."
        return text
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "..."
    return value


class Checks:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, name: str, passed: bool, detail: Any = None) -> None:
        self.items.append({"name": name, "passed": bool(passed), "detail": excerpt(detail)})

    def as_dict(self) -> dict:
        return {
            "allPassed": all(item["passed"] for item in self.items),
            "passed": sum(1 for item in self.items if item["passed"]),
            "failed": sum(1 for item in self.items if not item["passed"]),
            "checks": self.items,
        }


def api_call(
    base: str,
    method: str,
    path: str,
    token: str | None = None,
    payload=None,
) -> tuple[int, Any, dict[str, str]]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read()
            response_headers = {key.lower(): value for key, value in response.headers.items()}
            content_type = response_headers.get("content-type", "")
            if "json" in content_type and "content-disposition" not in response_headers:
                return response.status, json.loads(body), response_headers
            return response.status, body, response_headers
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()
            response_headers = {key.lower(): value for key, value in exc.headers.items()}
            content_type = response_headers.get("content-type", "")
            if "json" in content_type and "content-disposition" not in response_headers:
                return exc.code, json.loads(body), response_headers
            return exc.code, body, response_headers
        finally:
            exc.close()


def setup_platform(db_path: Path) -> dict:
    migrations.migrate(db_path)
    conn = db.connect(db_path)
    try:
        alpha = service.create_project(conn, "Alpha")
        beta = service.create_project(conn, "Beta")
        alpha_member = service.add_member(conn, alpha["id"], "alice", "member")
        alpha_ro = service.add_member(conn, alpha["id"], "alice-ro", "readonly")
        beta_member = service.add_member(conn, beta["id"], "bob", "member")
    finally:
        conn.close()
    return {
        "alpha": alpha,
        "beta": beta,
        "alpha_token": alpha_member["token"],
        "alpha_ro_token": alpha_ro["token"],
        "beta_token": beta_member["token"],
    }


def schema_evidence(db_path: Path) -> tuple[dict, str]:
    first = migrations.migrate(db_path)
    second = migrations.migrate(db_path)
    conn = db.connect(db_path)
    try:
        tables = [
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        indexes = [
            dict(row)
            for row in conn.execute(
                "SELECT name, tbl_name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%' ORDER BY name"
            ).fetchall()
        ]
        triggers = [
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' ORDER BY name"
            ).fetchall()
        ]
        fk_check = [dict(row) for row in conn.execute("PRAGMA foreign_key_check").fetchall()]
        schema_dump = db.schema_sql(conn)
        columns = {
            table: [row["name"] for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]
            for table in ("evidence", "run_item", "report_snapshot", "retention_policy", "evidence_cleanup_log")
        }
    finally:
        conn.close()
    applied = [item["name"] for item in first["appliedNow"]]
    return {
        "checkedAt": now(),
        "appliedMigrations": applied,
        "secondApplyAdded": len(second["appliedNow"]),
        "schemaDigest": first["schemaDigest"],
        "tables": tables,
        "indexes": indexes,
        "triggers": triggers,
        "foreignKeyCheck": fk_check,
        "columns": columns,
        "repeatable": len(second["appliedNow"]) == 0,
    }, schema_dump


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


SECRET_MARKERS = [
    "eyJhbGciOiJIUzI1NiJ9",
    "13800138000",
    "deadbeefcafe",
    "abc123",
    "hunter2",
    "sk-live-very-secret",
    "AKIAIOSFODNN7EXAMPLE",
    "zzz",
]


def run_flow(base: str, db_path: Path, store_root: Path, out: Path, checks: Checks) -> dict:
    platform = setup_platform(db_path)
    alpha_id = platform["alpha"]["id"]
    beta_id = platform["beta"]["id"]
    token = platform["alpha_token"]
    ro_token = platform["alpha_ro_token"]
    beta_token = platform["beta_token"]
    records: dict[str, dict] = {}
    outcomes: dict[str, int] = {}

    def call(key, method, path, *, token=token, payload=None, actor="alice/member"):
        status, body, headers = api_call(base, method, path, token, payload)
        outcomes[key] = status
        if isinstance(body, bytes):
            records[key] = {
                "actor": actor,
                "request": {"method": method, "path": path},
                "status": status,
                "response": {
                    "bytes": len(body),
                    "contentSha256": hashlib.sha256(body).hexdigest(),
                    "contentType": headers.get("content-type"),
                    "snippet": excerpt(body),
                },
            }
        else:
            safe_body = body
            try:
                safe_body, _ = redaction.redact_json_document(body)
            except Exception:  # defensive: evidence recording must not change the flow
                safe_body = body
            records[key] = {
                "actor": actor,
                "request": {"method": method, "path": path},
                "status": status,
                "response": excerpt(safe_body),
            }
        return status, body, headers

    # --- create a B-channel run through the API ---------------------------
    status, spec, _ = call(
        "create_spec", "POST", f"/projects/{alpha_id}/specs",
        payload={"channel": "B", "name": "alpha-eval"},
    )
    spec_id = spec["data"]["id"]
    status, version, _ = call(
        "create_spec_version_v1", "POST", f"/projects/{alpha_id}/specs/{spec_id}/versions",
        payload={
            "dataset_version": "eval-2026.11.1",
            "scorer_version": "scorer-v1",
            "model_snapshot": {"model": "qwen2.5-7b", "temperature": 0, "api_key": "sk-config-secret"},
            "params": {"sampling": 5},
        },
    )
    version_id = version["data"]["id"]
    status, run, _ = call(
        "create_run", "POST", f"/projects/{alpha_id}/runs",
        payload={"spec_version_id": version_id, "commit_sha": "c" * 40, "trigger": "ci"},
    )
    run_id = run["data"]["id"]
    status, item_p0, _ = call(
        "create_item_p0", "POST", f"/projects/{alpha_id}/runs/{run_id}/items",
        payload={"case_ref": "case-p0", "state": "fail", "priority": "P0", "confidence": 0.9},
    )
    status, item_p1, _ = call(
        "create_item_p1", "POST", f"/projects/{alpha_id}/runs/{run_id}/items",
        payload={"case_ref": "case-p1", "state": "uncertain", "priority": "P1"},
    )
    item_p0_id = item_p0["data"]["id"]
    item_p1_id = item_p1["data"]["id"]
    checks.add(
        "setup.run_items_created",
        item_p0_id is not None and item_p1_id is not None and item_p0["data"]["priority"] == "P0",
        {"run": run_id, "itemP0": item_p0_id, "itemP1": item_p1_id},
    )

    # --- verdict and evidence --------------------------------------------
    status, verdict, _ = call(
        "create_verdict", "POST", f"/projects/{alpha_id}/runs/{run_id}/items/{item_p0_id}/verdict",
        payload={
            "state": "fail",
            "confidence": 0.81,
            "scorer_trace": {"reason": "expected 42 got 41", "api_key": "sk-hidden", "output": "call 13800138000"},
        },
    )
    checks.add(
        "C-T03-1.verdict_created",
        status == 201 and verdict["data"]["scorer_trace"]["api_key"] == "[REDACTED:JSON_KEY]",
        {"status": status, "verdict": verdict["data"]["id"], "trace": verdict["data"]["scorer_trace"]},
    )

    status, stored_log, _ = call(
        "upload_log", "POST", f"/projects/{alpha_id}/runs/{run_id}/items/{item_p0_id}/evidence",
        payload={"kind": "log", "filename": "run.log", "content": SAMPLE_BEFORE},
    )
    log_id = stored_log["data"]["id"]
    status, stored_request, _ = call(
        "upload_request", "POST", f"/projects/{alpha_id}/runs/{run_id}/items/{item_p0_id}/evidence",
        payload={
            "kind": "request",
            "filename": "request.json",
            "content_type": "application/json",
            "content": json.dumps(
                {"status": 500, "url": "https://api.example.com/x?sig=abc&q=1", "api_key": "sk-request", "note": "keep"}
            ),
        },
    )
    status, stored_shot, _ = call(
        "upload_screenshot", "POST", f"/projects/{alpha_id}/runs/{run_id}/items/{item_p0_id}/evidence",
        payload={
            "kind": "screenshot",
            "filename": "page.png",
            "content_type": "image/png",
            "content_base64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/lK3Q6wAAAABJRU5ErkJggg==",
        },
    )
    checks.add(
        "C-T03-1.evidence_indexed_with_redaction",
        stored_log["data"]["redacted"] is True
        and stored_log["data"]["redaction"]["matches"].get("phone_cn", 0) >= 1
        and stored_log["data"]["uri"].startswith("object://projects/")
        and f"/runs/{run_id}/" in stored_log["data"]["uri"],
        {
            "logId": log_id,
            "redaction": stored_log["data"]["redaction"],
            "uri": stored_log["data"]["uri"],
            "requestRedaction": stored_request["data"]["redaction"],
            "screenshotReview": stored_shot["data"]["redaction"]["manualReviewRequired"],
        },
    )

    # --- drill down: verdict -> item -> all evidence -> fetch files ------
    status, drill, _ = call(
        "drilldown", "GET",
        f"/projects/{alpha_id}/runs/{run_id}/items/{item_p0_id}/drilldown",
    )
    drill_data = drill["data"]
    fetched = []
    for record in drill_data["evidence"]:
        fetch_status, body, headers = call(
            f"fetch_{record['kind']}", "GET", record["contentPath"]
        )
        fetched.append(
            {
                "evidenceId": record["id"],
                "kind": record["kind"],
                "status": fetch_status,
                "bytes": len(body) if isinstance(body, bytes) else None,
                "sha256": hashlib.sha256(body).hexdigest() if isinstance(body, bytes) else None,
                "metadataSha256": record["content_sha256"],
                "headerSha256": headers.get("x-content-sha256"),
            }
        )
    checks.add(
        "C-T03-1.drilldown_lists_all_evidence",
        status == 200
        and drill_data["verdict"]["id"] == verdict["data"]["id"]
        and drill_data["counts"]["evidence"] == 3
        and drill_data["counts"]["byKind"] == {"log": 1, "request": 1, "screenshot": 1}
        and all(row["status"] == 200 for row in fetched)
        and all(row["sha256"] == row["metadataSha256"] == row["headerSha256"] for row in fetched),
        {
            "verdict": drill_data["verdict"]["id"],
            "counts": drill_data["counts"],
            "fetched": fetched,
            "contentPaths": [record["contentPath"] for record in drill_data["evidence"]],
        },
    )
    status, search, _ = call(
        "search_metadata", "GET",
        f"/projects/{alpha_id}/evidence?run_id={run_id}&kind=log&filename=run&include_deleted=false",
    )
    found_ids = [record["id"] for record in search["data"]]
    checks.add(
        "C-T03-1.metadata_search",
        status == 200 and found_ids == [log_id] and search["data"][0]["run_item_id"] == item_p0_id,
        {"status": status, "query": {"run_id": run_id, "kind": "log", "filename": "run"}, "found": found_ids},
    )
    status, _, _ = call("readonly_upload", "POST", f"/projects/{alpha_id}/runs/{run_id}/items/{item_p0_id}/evidence",
                     token=ro_token, payload={"kind": "log", "content": "nope"}, actor="alice-ro/readonly")
    status_beta, _, _ = call(
        "cross_project_read", "GET",
        f"/projects/{alpha_id}/runs/{run_id}/items/{item_p0_id}/evidence", token=beta_token, actor="bob/member"
    )
    checks.add(
        "C-T03-1.permissions_and_isolation",
        status == 403 and status_beta == 404,
        {"readonlyUpload": status, "crossProjectRead": status_beta},
    )
    write_json(
        out / "01-evidence-drilldown.json",
        {
            "check": "C-T03-1 / T03-a1",
            "taskId": "T03",
            "generatedAt": now(),
            "runId": run_id,
            "itemId": item_p0_id,
            "verdictId": verdict["data"]["id"],
            "drilldown": drill_data,
            "fetchedFiles": fetched,
            "metadataSearch": search["data"],
            "records": records,
            "outcomes": outcomes,
            "checks": checks.as_dict(),
        },
    )
    # --- redaction before/after and rule inventory ----------------------
    status, rules, _ = call("redaction_rules", "GET", f"/projects/{alpha_id}/redaction-rules")
    log_content_path = f"/projects/{alpha_id}/runs/{run_id}/items/{item_p0_id}/evidence/{log_id}/content"
    status_after, after_bytes, _ = call("fetch_log_for_redaction", "GET", log_content_path)
    after_text = after_bytes.decode("utf-8")
    (out / "redaction-before.log").write_text(SAMPLE_BEFORE + "\n", encoding="utf-8")
    (out / "redaction-after.log").write_text(after_text + "\n", encoding="utf-8")
    leaked_markers = [marker for marker in SECRET_MARKERS if marker in after_text]
    disk_leaks = []
    for path in sorted(store_root.rglob("*")):
        if not path.is_file() or path.name.startswith(".tmp-"):
            continue
        data = path.read_bytes()
        for marker in SECRET_MARKERS:
            if marker.encode() in data:
                disk_leaks.append({"object": str(path.relative_to(store_root)), "marker": marker})
    rule_ids = [rule["id"] for rule in rules["data"]["rules"]]
    checks.add(
        "C-T03-2.redaction_before_after",
        status_after == 200 and not leaked_markers and not disk_leaks and "[REDACTED" in after_text,
        {
            "beforeBytes": len(SAMPLE_BEFORE.encode()),
            "afterBytes": len(after_bytes),
            "leakedMarkers": leaked_markers,
            "diskLeaks": disk_leaks,
            "beforeFile": "redaction-before.log",
            "afterFile": "redaction-after.log",
            "storedRedaction": stored_log["data"]["redaction"],
        },
    )
    checks.add(
        "C-T03-2.rule_inventory",
        status == 200
        and len(rule_ids) >= 10
        and {"url_query_secret", "authorization_header", "secret_key_value", "phone_cn", "email", "json_sensitive_key"}.issubset(rule_ids),
        {"rulesVersion": rules["data"]["rulesVersion"], "ruleIds": rule_ids, "rules": rules["data"]["rules"]},
    )
    request_path = f"/projects/{alpha_id}/runs/{run_id}/items/{item_p0_id}/evidence/{stored_request['data']['id']}/content"
    _, request_bytes, _ = call("fetch_request_for_redaction", "GET", request_path)
    request_json = json.loads(request_bytes)
    checks.add(
        "C-T03-2.json_evidence_redacted",
        request_json["api_key"] == "[REDACTED:JSON_KEY]"
        and "sig=REDACTED" in request_json["url"]
        and request_json["note"] == "keep",
        request_json,
    )
    write_json(
        out / "02-redaction.json",
        {
            "check": "C-T03-2 / T03-a2",
            "taskId": "T03",
            "generatedAt": now(),
            "sample": {"before": SAMPLE_BEFORE, "after": after_text},
            "sampleNote": "Synthetic tokens, phone number and signed-URL values are used only to exercise redaction; they are not real credentials.",
            "redactionReport": stored_log["data"]["redaction"],
            "rulesVersion": rules["data"]["rulesVersion"],
            "rules": rules["data"]["rules"],
            "jsonEvidence": request_json,
            "screenshot": stored_shot["data"]["redaction"],
            "leakedMarkers": leaked_markers,
            "diskLeaks": disk_leaks,
            "records": records,
            "outcomes": outcomes,
            "checks": checks.as_dict(),
        },
    )
    # --- report snapshot before/after a scorer config change ------------
    status, snapshot, _ = call(
        "create_report", "POST", f"/projects/{alpha_id}/runs/{run_id}/reports"
    )
    snapshot_id = snapshot["data"]["id"]
    report_path = f"/projects/{alpha_id}/runs/{run_id}/reports/{snapshot_id}/content"
    status_first, first_bytes, first_headers = call("report_content_before", "GET", report_path)
    first_sha = hashlib.sha256(first_bytes).hexdigest()
    status, version_v2, _ = call(
        "create_spec_version_v2", "POST", f"/projects/{alpha_id}/specs/{spec_id}/versions",
        payload={
            "dataset_version": "eval-2026.12.1",
            "scorer_version": "scorer-v2",
            "model_snapshot": {"model": "qwen3-32b", "temperature": 0.2},
            "params": {"sampling": 5, "changed": True},
        },
    )
    status, policy_after_change, _ = call(
        "retention_change_for_snapshot", "POST", f"/projects/{alpha_id}/retention",
        payload={"P0": 7, "P1": 3},
    )
    status_second, second_bytes, second_headers = call("report_content_after", "GET", report_path)
    second_sha = hashlib.sha256(second_bytes).hexdigest()
    status, snapshot_meta, _ = call(
        "report_metadata", "GET", f"/projects/{alpha_id}/runs/{run_id}/reports/{snapshot_id}"
    )
    content = snapshot_meta["data"]["content"]
    checks.add(
        "C-T03-3.snapshot_unchanged_after_config_change",
        status_first == 200
        and status_second == 200
        and first_bytes == second_bytes
        and first_sha == second_sha == snapshot["data"]["content_sha256"]
        and first_headers.get("x-content-sha256") == first_sha
        and content["configSnapshot"]["scorerVersion"] == "scorer-v1"
        and content["configSnapshot"]["modelSnapshot"]["model"] == "qwen2.5-7b"
        and content["configSnapshot"]["modelSnapshot"]["api_key"] == "[REDACTED:JSON_KEY]",
        {
            "snapshotId": snapshot_id,
            "shaBefore": first_sha,
            "shaAfter": second_sha,
            "bytesEqual": first_bytes == second_bytes,
            "configSnapshot": content["configSnapshot"],
            "newSpecVersion": version_v2["data"]["id"],
            "policyAfterChange": policy_after_change["data"]["policies"],
        },
    )
    immutable_results = {}
    conn = db.connect(db_path)
    try:
        for operation, statement in (
            ("update", "UPDATE report_snapshot SET content_json = '{}' WHERE id = ?"),
            ("delete", "DELETE FROM report_snapshot WHERE id = ?"),
        ):
            try:
                conn.execute(statement, (snapshot_id,))
                immutable_results[operation] = {"blocked": False}
            except sqlite3.DatabaseError as exc:
                immutable_results[operation] = {"blocked": True, "error": str(exc)}
        stored_row = conn.execute(
            "SELECT content_sha256, length(content_json) AS chars FROM report_snapshot WHERE id = ?",
            (snapshot_id,),
        ).fetchone()
        immutable_results["rowStillPresent"] = dict(stored_row) if stored_row else None
    finally:
        conn.close()
    checks.add(
        "C-T03-3.snapshot_row_immutable",
        immutable_results["update"]["blocked"]
        and immutable_results["delete"]["blocked"]
        and immutable_results["rowStillPresent"]["content_sha256"] == first_sha,
        immutable_results,
    )
    write_json(
        out / "03-report-snapshot.json",
        {
            "check": "C-T03-3 / T03-a3",
            "taskId": "T03",
            "generatedAt": now(),
            "snapshot": snapshot["data"],
            "contentSha256Before": first_sha,
            "contentSha256After": second_sha,
            "bytesEqual": first_bytes == second_bytes,
            "contentLength": len(first_bytes),
            "snapshotContent": content,
            "configChange": {"newSpecVersion": version_v2["data"], "retentionPolicy": policy_after_change["data"]},
            "immutabilityAttempts": immutable_results,
            "records": records,
            "outcomes": outcomes,
            "checks": checks.as_dict(),
        },
    )
    # --- retention: configurable, effective and audited -------------------
    status, policy, _ = call("retention_before", "GET", f"/projects/{alpha_id}/retention")
    status, policy_set, _ = call(
        "retention_set_zero", "POST", f"/projects/{alpha_id}/retention",
        payload={"P0": 0, "P1": 365},
    )
    status, expiring, _ = call(
        "upload_expiring", "POST", f"/projects/{alpha_id}/runs/{run_id}/items/{item_p0_id}/evidence",
        payload={"kind": "log", "filename": "expiring.log", "content": "expires immediately token=expiring-token"},
    )
    status, live, _ = call(
        "upload_live", "POST", f"/projects/{alpha_id}/runs/{run_id}/items/{item_p1_id}/evidence",
        payload={"kind": "log", "filename": "live.log", "content": "kept for a year"},
    )
    status, policy_effective, _ = call("retention_effective", "GET", f"/projects/{alpha_id}/retention")
    expiring_id = expiring["data"]["id"]
    live_id = live["data"]["id"]
    expiring_path = f"/projects/{alpha_id}/runs/{run_id}/items/{item_p0_id}/evidence/{expiring_id}/content"
    live_path = f"/projects/{alpha_id}/runs/{run_id}/items/{item_p1_id}/evidence/{live_id}/content"
    checks.add(
        "T03-a4.retention_config_read",
        status == 200
        and policy["data"]["policies"]["P0"]["retain_days"] == 7
        and policy_effective["data"]["policies"]["P0"]["retain_days"] == 0
        and policy_effective["data"]["policies"]["P0"]["source"] == "configured",
        {
            "before": policy["data"]["policies"],
            "set": policy_set["data"]["policies"],
            "effective": policy_effective["data"]["policies"],
        },
    )
    checks.add(
        "T03-a4.retention_applied_at_ingest",
        expiring["data"]["retention_class"] == "P0"
        and expiring["data"]["retention_expires_at"] <= now(1)
        and live["data"]["retention_class"] == "P1"
        and live["data"]["retention_expires_at"] > now(180),
        {
            "expiring": {
                "class": expiring["data"]["retention_class"],
                "expires": expiring["data"]["retention_expires_at"],
            },
            "live": {
                "class": live["data"]["retention_class"],
                "expires": live["data"]["retention_expires_at"],
            },
        },
    )
    status, dry, _ = call(
        "cleanup_dry_run", "POST", f"/projects/{alpha_id}/evidence/cleanup",
        payload={"cutoff": now(1), "dry_run": True},
    )
    status_before, expiring_before, _ = call("expiring_before_cleanup", "GET", expiring_path)
    status, applied, _ = call(
        "cleanup_apply", "POST", f"/projects/{alpha_id}/evidence/cleanup",
        payload={"cutoff": now(1), "dry_run": False},
    )
    status_after, expiring_after, _ = call("expiring_after_cleanup", "GET", expiring_path)
    status_live, live_after, _ = call("live_after_cleanup", "GET", live_path)
    status_log, cleanup_log, _ = call(
        "cleanup_log", "GET", f"/projects/{alpha_id}/evidence/cleanup-log"
    )
    status, again, _ = call(
        "cleanup_again", "POST", f"/projects/{alpha_id}/evidence/cleanup",
        payload={"cutoff": now(1), "dry_run": False},
    )
    status, tombstone_search, _ = call(
        "tombstones", "GET",
        f"/projects/{alpha_id}/evidence?run_id={run_id}&include_deleted=true&limit=100",
    )
    tombstones = [row for row in tombstone_search["data"] if row["id"] == expiring_id]
    checks.add(
        "T03-a4.cleanup_effective_and_recorded",
        dry["data"]["matched"] == 1
        and dry["data"]["deleted"] == 0
        and status_before == 200
        and applied["data"]["matched"] == 1
        and applied["data"]["deleted"] == 1
        and applied["data"]["records"][0]["object_removed"] is True
        and status_after == 410
        and status_live == 200
        and len(cleanup_log["data"]) == 1
        and cleanup_log["data"][0]["evidence_id"] == expiring_id
        and cleanup_log["data"][0]["reason"] == "expired"
        and again["data"]["matched"] == 0
        and tombstones
        and tombstones[0]["deleted"] is True,
        {
            "dryRun": dry["data"],
            "applied": applied["data"],
            "expiringBeforeStatus": status_before,
            "expiringAfterStatus": status_after,
            "liveAfterStatus": status_live,
            "cleanupLog": cleanup_log["data"],
            "repeatCleanup": again["data"],
            "tombstone": tombstones[0] if tombstones else None,
            "liveReadable": len(live_after) if isinstance(live_after, bytes) else None,
        },
    )
    status, report_after_cleanup, _ = call("report_after_cleanup", "GET", report_path)
    checks.add(
        "C-T03-3.snapshot_survives_retention_cleanup",
        status == 200 and report_after_cleanup == first_bytes,
        {
            "status": status,
            "sha": hashlib.sha256(report_after_cleanup).hexdigest() if isinstance(report_after_cleanup, bytes) else None,
            "expected": first_sha,
        },
    )
    write_json(
        out / "04-retention.json",
        {
            "check": "T03-a4",
            "taskId": "T03",
            "generatedAt": now(),
            "policyBefore": policy["data"],
            "policyAfterSet": policy_set["data"],
            "policyEffective": policy_effective["data"],
            "expiringEvidence": expiring["data"],
            "liveEvidence": live["data"],
            "dryRun": dry["data"],
            "applied": applied["data"],
            "cleanupLog": cleanup_log["data"],
            "repeatCleanup": again["data"],
            "tombstones": tombstones,
            "records": records,
            "outcomes": outcomes,
            "checks": checks.as_dict(),
        },
    )
    return {
        "alphaId": alpha_id,
        "betaId": beta_id,
        "specId": spec_id,
        "versionId": version_id,
        "runId": run_id,
        "itemP0": item_p0_id,
        "itemP1": item_p1_id,
        "logId": log_id,
        "requestId": stored_request["data"]["id"],
        "screenshotId": stored_shot["data"]["id"],
        "snapshotId": snapshot_id,
        "snapshotSha": first_sha,
        "records": records,
        "outcomes": outcomes,
        "redaction": stored_log["data"]["redaction"],
        "rules": rules["data"],
        "drilldown": drill_data,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify T03 evidence, redaction, retention and snapshots")
    parser.add_argument("--out", default=str(ROOT.parent / "deliverables" / "evidence"))
    parser.add_argument("--keep", action="store_true", help="keep the temporary database and object store")
    args = parser.parse_args()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    checks = Checks()
    tmp = tempfile.TemporaryDirectory(prefix="aitest-t03-verify-")
    root = Path(tmp.name)
    db_path = root / "platform.db"
    store_root = root / "objects"
    server = None
    thread = None
    try:
        schema, schema_dump = schema_evidence(db_path)
        (out / "schema.sql").write_text(schema_dump, encoding="utf-8")
        write_json(out / "00-schema.json", schema)
        checks.add(
            "schema.migrations_repeatable",
            schema["repeatable"] and "002_evidence_report.sql" in schema["appliedMigrations"],
            {
                "appliedMigrations": schema["appliedMigrations"],
                "secondApplyAdded": schema["secondApplyAdded"],
                "tables": schema["tables"],
            },
        )
        checks.add(
            "schema.t03_columns_present",
            all(
                column in schema["columns"]["evidence"]
                for column in ("storage_key", "content_sha256", "redaction_json", "retention_expires_at", "deleted_at")
            )
            and "priority" in schema["columns"]["run_item"]
            and {"report_snapshot", "retention_policy", "evidence_cleanup_log"}.issubset(set(schema["tables"])),
            schema["columns"],
        )

        server = http_api.make_server(db_path, port=0, storage_root=store_root)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        result = run_flow(base, db_path, store_root, out, checks)
        summary = {
            "taskId": "T03",
            "title": "证据存储、脱敏与只读报告快照",
            "generatedAt": now(),
            "server": base,
            "database": "temporary SQLite database created by the verifier",
            "storage": "temporary local object store created by the verifier",
            "checks": checks.as_dict(),
            "result": {
                "projectId": result["alphaId"],
                "runId": result["runId"],
                "itemP0": result["itemP0"],
                "itemP1": result["itemP1"],
                "snapshotId": result["snapshotId"],
                "snapshotSha": result["snapshotSha"],
                "redactionMatches": result["redaction"]["matches"],
                "ruleCount": len(result["rules"]["rules"]),
                "drilldownCounts": result["drilldown"]["counts"],
                "outcomes": result["outcomes"],
            },
            "evidenceFiles": [
                "00-schema.json",
                "schema.sql",
                "01-evidence-drilldown.json",
                "02-redaction.json",
                "redaction-before.log",
                "redaction-after.log",
                "03-report-snapshot.json",
                "04-retention.json",
            ],
            "limitations": [
                "Object storage is a local filesystem adapter, not S3/OSS/MinIO.",
                "Evidence and reports run against a temporary SQLite database and local HTTP server.",
                "Binary screenshots are stored unchanged and flagged for manual review; pixel-level redaction is out of scope.",
                "Cleanup is invoked explicitly with an ISO cutoff; no scheduler runs inside this verifier.",
            ],
        }
        write_json(out / "verification-summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if summary["checks"]["allPassed"] else 1
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if args.keep:
            print(f"temporary files kept at {root}")
        else:
            tmp.cleanup()


if __name__ == "__main__":
    sys.exit(main())
