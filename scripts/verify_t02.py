#!/usr/bin/env python3
"""Run reproducible T02 checks and write machine-readable evidence.

The script starts a real local HTTP server and drives the API exactly as CI
and a runner would. It writes JSON evidence under deliverables/evidence (or
--out). Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aitest import db, http_api, migrations, service  # noqa: E402


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def excerpt(value: Any, limit: int = 2400) -> Any:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if len(text) <= limit:
            return value
        return {"excerpt": text[:limit] + "...", "truncated": True}
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "..."
    return value


class Checks:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, name: str, passed: bool, detail: Any = None) -> None:
        self.items.append({"name": name, "passed": bool(passed), "detail": excerpt(detail)})
        print(f"[{'PASS' if passed else 'FAIL'}] {name}")
        if not passed:
            print(f"       detail={json.dumps(excerpt(detail, 800), ensure_ascii=False)}")

    def as_dict(self) -> dict:
        return {
            "allPassed": all(item["passed"] for item in self.items),
            "passed": sum(1 for item in self.items if item["passed"]),
            "failed": sum(1 for item in self.items if not item["passed"]),
            "checks": self.items,
        }


def api_call(base: str, method: str, path: str, token: str | None = None, payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read().decode("utf-8")
            return response.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8")
            return exc.code, (json.loads(body) if body else {})
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
        alpha_actor = service.authenticate(conn, alpha_member["token"])
        beta_actor = service.authenticate(conn, beta_member["token"])

        alpha_spec = service.create_spec(
            conn, alpha_actor, alpha["id"], {"channel": "A", "name": "alpha-checkout"}
        )
        alpha_v1 = service.add_spec_version(
            conn,
            alpha_actor,
            alpha["id"],
            alpha_spec["id"],
            {"repo_path": "github.com/acme/web", "repo_commit": "a" * 40},
        )
        beta_spec = service.create_spec(
            conn, beta_actor, beta["id"], {"channel": "A", "name": "beta-checkout"}
        )
        beta_v1 = service.add_spec_version(
            conn,
            beta_actor,
            beta["id"],
            beta_spec["id"],
            {"repo_path": "github.com/acme/beta", "repo_commit": "b" * 40},
        )

        cancel_project = service.create_project(conn, "CancelFlow")
        cancel_member = service.add_member(conn, cancel_project["id"], "carol", "member")
        cancel_actor = service.authenticate(conn, cancel_member["token"])
        cancel_spec = service.create_spec(
            conn, cancel_actor, cancel_project["id"], {"channel": "A", "name": "cancel-checkout"}
        )
        cancel_v1 = service.add_spec_version(
            conn,
            cancel_actor,
            cancel_project["id"],
            cancel_spec["id"],
            {"repo_path": "github.com/acme/cancel", "repo_commit": "c" * 40},
        )

        queue_project = service.create_project(conn, "QueueFlow")
        queue_member = service.add_member(conn, queue_project["id"], "dave", "member")
        queue_actor = service.authenticate(conn, queue_member["token"])
        queue_spec = service.create_spec(
            conn, queue_actor, queue_project["id"], {"channel": "A", "name": "queue-checkout"}
        )
        queue_v1 = service.add_spec_version(
            conn,
            queue_actor,
            queue_project["id"],
            queue_spec["id"],
            {"repo_path": "github.com/acme/queue", "repo_commit": "d" * 40},
        )
    finally:
        conn.close()
    return {
        "alpha": alpha,
        "beta": beta,
        "alpha_member": alpha_member,
        "alpha_ro": alpha_ro,
        "beta_member": beta_member,
        "alpha_spec": alpha_spec,
        "alpha_v1": alpha_v1,
        "beta_spec": beta_spec,
        "beta_v1": beta_v1,
        "cancel_project": cancel_project,
        "cancel_member": cancel_member,
        "cancel_v1": cancel_v1,
        "queue_project": queue_project,
        "queue_member": queue_member,
        "queue_v1": queue_v1,
    }


def run_view(data: dict) -> dict:
    """Keep the fields a scheduler/reviewer actually needs."""
    keys = (
        "id",
        "status",
        "priority",
        "queue_position",
        "queue_depth",
        "created_at",
        "started_at",
        "finished_at",
        "queue_deadline_at",
        "deadline_at",
        "lease_expires_at",
        "assigned_runner_id",
        "rerun_of_run_id",
        "superseded_by_run_id",
        "cancel",
        "reused",
        "reuse",
        "idempotency",
    )
    view = {key: data.get(key) for key in keys}
    view["events"] = [
        {
            "seq": event["seq"],
            "from_status": event["from_status"],
            "to_status": event["to_status"],
            "source": event["source"],
            "actor": event["actor"],
            "reason": event["reason"],
            "created_at": event["created_at"],
        }
        for event in data.get("events", [])
    ]
    return view


def scheduler_view(data: dict) -> dict:
    return {
        "max_concurrency": data["max_concurrency"],
        "running_count": data["running_count"],
        "queued_count": data["queued_count"],
        "queue_depth": data["queue_depth"],
        "available_capacity": data["available_capacity"],
        "checked_at": data["checked_at"],
        "priority_order": data["priority_order"],
        "running": data["running"],
        "runners": data["runners"],
        "settings": data["settings"],
    }


def runner_view(data: dict) -> dict:
    return {
        "id": data["id"],
        "name": data["name"],
        "status": data["status"],
        "effective_status": data["effective_status"],
        "available": data["available"],
        "capacity": data["capacity"],
        "assigned_runs": data["assigned_runs"],
        "last_heartbeat_at": data["last_heartbeat_at"],
        "heartbeat_age_seconds": data["heartbeat_age_seconds"],
    }


def db_snapshot(db_path: Path, run_ids: list[str] | None = None) -> dict:
    conn = db.connect(db_path)
    try:
        snapshot = {
            "runCount": conn.execute("SELECT COUNT(*) FROM run").fetchone()[0],
            "eventCount": conn.execute("SELECT COUNT(*) FROM run_event").fetchone()[0],
            "runnerCount": conn.execute("SELECT COUNT(*) FROM runner").fetchone()[0],
            "runsByStatus": {
                row["status"]: row["count"]
                for row in conn.execute(
                    "SELECT status, COUNT(*) AS count FROM run GROUP BY status ORDER BY status"
                ).fetchall()
            },
        }
        if run_ids:
            placeholders = ",".join("?" for _ in run_ids)
            snapshot["runs"] = [
                dict(row)
                for row in conn.execute(
                    f"""
                    SELECT id, status, priority, idempotency_key, params_digest,
                           superseded_by_run_id, rerun_of_run_id, assigned_runner_id,
                           cancel_source, cancel_reason, cancelled_by, created_at,
                           started_at, finished_at, deadline_at
                    FROM run WHERE id IN ({placeholders}) ORDER BY created_at, rowid
                    """,
                    run_ids,
                ).fetchall()
            ]
        snapshot["schemaDigest"] = db.schema_digest(conn)
        snapshot["foreignKeyCheck"] = [
            dict(row) for row in conn.execute("PRAGMA foreign_key_check").fetchall()
        ]
    finally:
        conn.close()
    return snapshot


def write_evidence(out: Path, name: str, payload: dict) -> str:
    out.mkdir(parents=True, exist_ok=True)
    path = out / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return name


def flow_idempotency(base: str, data: dict, db_path: Path, checks: Checks, out: Path) -> dict:
    project_id = data["alpha"]["id"]
    token = data["alpha_member"]["token"]
    version_id = data["alpha_v1"]["id"]
    payload = {
        "spec_version_id": version_id,
        "commit_sha": "c" * 40,
        "branch": "main",
        "trigger": "ci",
    }
    records: dict[str, dict] = {}

    status, first = api_call(base, "POST", f"/projects/{project_id}/runs", token, payload)
    records["create_first"] = {
        "request": {"method": "POST", "path": f"/projects/{project_id}/runs", "body": payload},
        "status": status,
        "response": run_view(first.get("data", {})),
    }
    checks.add(
        "idempotency.first_create_201",
        status == 201 and first["data"]["status"] == "queued",
        {"status": status, "run": run_view(first.get("data", {}))},
    )
    first_run = first["data"]
    key = first_run["idempotency_key"]

    status, duplicate = api_call(base, "POST", f"/projects/{project_id}/runs", token, payload)
    records["create_duplicate"] = {
        "request": {"method": "POST", "path": f"/projects/{project_id}/runs", "body": payload},
        "status": status,
        "response": run_view(duplicate.get("data", {})),
    }
    checks.add(
        "idempotency.duplicate_returns_same_run",
        status == 200
        and duplicate["data"]["id"] == first_run["id"]
        and duplicate["data"]["reused"] is True
        and duplicate["data"]["reuse"]["reason"] == "idempotency_key_match",
        {
            "status": status,
            "firstRunId": first_run["id"],
            "duplicateRunId": duplicate["data"]["id"],
            "reused": duplicate["data"].get("reused"),
            "reuse": duplicate["data"].get("reuse"),
            "idempotencyKeyStable": duplicate["data"]["idempotency_key"] == key,
        },
    )

    snapshot_before_force = db_snapshot(db_path, [first_run["id"]])
    records["db_before_force"] = snapshot_before_force
    checks.add(
        "idempotency.only_one_run_row",
        snapshot_before_force["runCount"] == 1
        and snapshot_before_force["eventCount"] == 1,
        {
            "runCount": snapshot_before_force["runCount"],
            "eventCount": snapshot_before_force["eventCount"],
            "runs": snapshot_before_force["runs"],
        },
    )

    forced_payload = dict(payload, force=True)
    status, forced = api_call(
        base, "POST", f"/projects/{project_id}/runs", token, forced_payload
    )
    forced_run = forced.get("data", {})
    records["create_force"] = {
        "request": {"method": "POST", "path": f"/projects/{project_id}/runs", "body": forced_payload},
        "status": status,
        "response": run_view(forced_run),
    }
    checks.add(
        "idempotency.force_creates_new_run",
        status == 201
        and forced_run["id"] != first_run["id"]
        and forced_run["rerun_of_run_id"] == first_run["id"]
        and forced_run["idempotency_key"] == key
        and forced_run["reused"] is False,
        {
            "status": status,
            "oldRunId": first_run["id"],
            "newRunId": forced_run.get("id"),
            "rerunOf": forced_run.get("rerun_of_run_id"),
            "sameIdempotencyKey": forced_run.get("idempotency_key") == key,
            "reused": forced_run.get("reused"),
        },
    )

    status, old = api_call(
        base, "GET", f"/projects/{project_id}/runs/{first_run['id']}", token
    )
    old_view = run_view(old.get("data", {}))
    status_new, new = api_call(
        base, "GET", f"/projects/{project_id}/runs/{forced_run['id']}", token
    )
    new_view = run_view(new.get("data", {}))
    records["old_run_after_force"] = {"status": status, "response": old_view}
    records["new_run_after_force"] = {"status": status_new, "response": new_view}
    checks.add(
        "idempotency.old_and_new_are_distinguishable",
        status == 200
        and old_view["superseded_by_run_id"] == forced_run["id"]
        and old_view["status"] == "cancelled"
        and old_view["cancel"]["reason"] == "superseded_by_force_rerun"
        and old_view["idempotency"]["active"] is False
        and new_view["idempotency"]["active"] is True
        and new_view["superseded_by_run_id"] is None,
        {
            "old": {
                "status": old_view["status"],
                "supersededBy": old_view["superseded_by_run_id"],
                "cancel": old_view["cancel"],
                "active": old_view["idempotency"]["active"],
                "events": old_view["events"],
            },
            "new": {
                "status": new_view["status"],
                "rerunOf": new_view["rerun_of_run_id"],
                "supersededBy": new_view["superseded_by_run_id"],
                "active": new_view["idempotency"]["active"],
                "events": new_view["events"],
            },
        },
    )

    status, repeated = api_call(base, "POST", f"/projects/{project_id}/runs", token, payload)
    records["create_after_force"] = {
        "request": {"method": "POST", "path": f"/projects/{project_id}/runs", "body": payload},
        "status": status,
        "response": run_view(repeated.get("data", {})),
    }
    checks.add(
        "idempotency.repeat_after_force_uses_new_active_run",
        status == 200
        and repeated["data"]["id"] == forced_run["id"]
        and repeated["data"]["reused"] is True,
        {
            "status": status,
            "returnedRunId": repeated["data"]["id"],
            "expectedActiveRunId": forced_run["id"],
            "reused": repeated["data"].get("reused"),
        },
    )

    changed_payload = dict(payload, sampling_config={"n": 5})
    status, changed = api_call(
        base, "POST", f"/projects/{project_id}/runs", token, changed_payload
    )
    records["create_changed_params"] = {
        "request": {
            "method": "POST",
            "path": f"/projects/{project_id}/runs",
            "body": changed_payload,
        },
        "status": status,
        "response": run_view(changed.get("data", {})),
    }
    checks.add(
        "idempotency.parameter_change_creates_new_key",
        status == 201
        and changed["data"]["id"] != forced_run["id"]
        and changed["data"]["idempotency_key"] != key
        and changed["data"]["params_digest"] != forced_run["params_digest"],
        {
            "status": status,
            "runId": changed["data"]["id"],
            "idempotencyKeyChanged": changed["data"]["idempotency_key"] != key,
            "paramsDigestChanged": changed["data"]["params_digest"] != forced_run["params_digest"],
        },
    )

    status, runs = api_call(base, "GET", f"/projects/{project_id}/runs", token)
    run_ids = [item["id"] for item in runs.get("data", [])]
    records["run_list"] = {
        "status": status,
        "runIds": run_ids,
        "count": len(run_ids),
    }
    snapshot = db_snapshot(
        db_path, [first_run["id"], forced_run["id"], changed["data"]["id"]]
    )
    records["db_after_force"] = snapshot

    payload_out = {
        "schemaVersion": 1,
        "taskId": "T02",
        "check": "C-T02-1 / T02-a1 / T02-a2",
        "generatedAt": now(),
        "idempotencyKeyFormula": "sha256(project_id + commit_sha + spec_version_id + params_digest)",
        "checks": checks.as_dict(),
        "records": records,
        "database": snapshot,
    }
    write_evidence(out, "07-idempotency.json", payload_out)
    return {
        "first_run_id": first_run["id"],
        "forced_run_id": forced_run["id"],
        "changed_run_id": changed["data"]["id"],
        "key": key,
    }


def flow_cancel_timeout(base: str, data: dict, db_path: Path, checks: Checks, out: Path) -> dict:
    project_id = data["cancel_project"]["id"]
    token = data["cancel_member"]["token"]
    version_id = data["cancel_v1"]["id"]
    records: dict[str, dict] = {}
    run_ids: list[str] = []

    status, settings = api_call(
        base,
        "POST",
        f"/projects/{project_id}/scheduler/config",
        token,
        {"max_concurrency": 1, "runner_stale_seconds": 30},
    )
    records["configure"] = {"status": status, "response": settings}
    checks.add(
        "cancel_timeout.config_applied",
        status == 200 and settings["data"]["max_concurrency"] == 1,
        {"status": status, "settings": settings.get("data")},
    )

    payload_cancel = {
        "spec_version_id": version_id,
        "commit_sha": "1" * 40,
        "branch": "main",
        "trigger": "ci",
    }
    status, created = api_call(
        base, "POST", f"/projects/{project_id}/runs", token, payload_cancel
    )
    queued_id = created["data"]["id"]
    run_ids.append(queued_id)
    records["create_queued"] = {
        "request": {"method": "POST", "path": f"/projects/{project_id}/runs", "body": payload_cancel},
        "status": status,
        "response": run_view(created["data"]),
    }
    status, cancelled = api_call(
        base,
        "POST",
        f"/projects/{project_id}/runs/{queued_id}/cancel",
        token,
        {"reason": "new commit on the PR"},
    )
    records["cancel_queued"] = {
        "request": {
            "method": "POST",
            "path": f"/projects/{project_id}/runs/{queued_id}/cancel",
            "body": {"reason": "new commit on the PR"},
        },
        "status": status,
        "response": run_view(cancelled["data"]),
    }
    checks.add(
        "cancel.queued_run_is_cancelled_with_source",
        status == 200
        and cancelled["data"]["status"] == "cancelled"
        and cancelled["data"]["cancel"]["source"] == "api"
        and cancelled["data"]["cancel"]["by"] == "carol"
        and cancelled["data"]["cancel"]["reason"] == "new commit on the PR"
        and cancelled["data"]["events"][-1]["from_status"] == "queued"
        and cancelled["data"]["events"][-1]["to_status"] == "cancelled"
        and cancelled["data"]["events"][-1]["source"] == "api",
        {
            "status": status,
            "cancel": cancelled["data"]["cancel"],
            "events": run_view(cancelled["data"])["events"],
        },
    )

    status, runner = api_call(
        base,
        "POST",
        f"/projects/{project_id}/runners",
        token,
        {"name": "cancel-runner", "capacity": 1},
    )
    runner_token = runner["data"]["token"]
    records["register_runner"] = {
        "status": status,
        "response": runner_view(runner["data"]),
    }
    status, claim = api_call(base, "POST", "/runner/claim", runner_token, {})
    records["claim_after_cancel"] = {
        "status": status,
        "reason": claim.get("reason"),
        "data": run_view(claim["data"]) if claim.get("data") else None,
    }
    checks.add(
        "cancel.cancelled_run_is_never_claimed",
        status == 200 and claim.get("data") is None and claim.get("reason") == "no_work",
        {
            "status": status,
            "reason": claim.get("reason"),
            "returnedRun": claim.get("data"),
            "queueDepth": claim.get("scheduler", {}).get("queue_depth"),
        },
    )
    status, events = api_call(
        base, "GET", f"/projects/{project_id}/runs/{queued_id}/events", token
    )
    records["cancel_events"] = {"status": status, "response": events.get("data")}
    checks.add(
        "cancel.event_ledger_matches",
        status == 200
        and [
            (event["from_status"], event["to_status"], event["source"])
            for event in events["data"]
        ]
        == [(None, "queued", "api"), ("queued", "cancelled", "api")],
        {"events": events.get("data")},
    )

    payload_timeout = {
        "spec_version_id": version_id,
        "commit_sha": "2" * 40,
        "branch": "main",
        "trigger": "ci",
        "timeout_seconds": 1,
    }
    status, created = api_call(
        base, "POST", f"/projects/{project_id}/runs", token, payload_timeout
    )
    timeout_id = created["data"]["id"]
    run_ids.append(timeout_id)
    status, claim = api_call(base, "POST", "/runner/claim", runner_token, {})
    records["claim_timeout_run"] = {
        "status": status,
        "reason": claim.get("reason"),
        "response": run_view(claim.get("data") or {}),
    }
    checks.add(
        "timeout.run_is_claimed_with_deadline",
        status == 200
        and claim["data"]["id"] == timeout_id
        and claim["data"]["status"] == "running"
        and claim["data"]["deadline_at"] is not None,
        {
            "status": status,
            "runId": claim["data"]["id"] if claim.get("data") else None,
            "statusValue": claim["data"]["status"] if claim.get("data") else None,
            "deadlineAt": claim["data"]["deadline_at"] if claim.get("data") else None,
        },
    )
    time.sleep(1.2)
    # A status query alone must converge an overdue run; tick is invoked
    # afterwards only to capture the scheduler snapshot.
    status_get, timed_out = api_call(
        base, "GET", f"/projects/{project_id}/runs/{timeout_id}", token
    )
    timed_out_view = run_view(timed_out.get("data", {}))
    records["hard_timeout_run"] = {"status": status_get, "response": timed_out_view}
    status, tick = api_call(
        base, "POST", f"/projects/{project_id}/scheduler/tick", token, {}
    )
    records["tick_after_deadline"] = {"status": status, "response": tick.get("data")}
    checks.add(
        "timeout.status_query_alone_converges_hard_timeout",
        status_get == 200
        and timed_out_view["status"] == "timeout"
        and timed_out_view["events"][-1]["reason"] == "hard_timeout"
        and timed_out_view["events"][-1]["source"] == "timeout",
        {
            "status": timed_out_view["status"],
            "events": timed_out_view["events"],
        },
    )
    checks.add(
        "timeout.hard_timeout_is_explicit_and_recorded",
        status == 200
        and status_get == 200
        and timed_out_view["status"] == "timeout"
        and timed_out_view["finished_at"] is not None
        and timed_out_view["events"][-1]["from_status"] == "running"
        and timed_out_view["events"][-1]["to_status"] == "timeout"
        and timed_out_view["events"][-1]["reason"] == "hard_timeout"
        and timed_out_view["events"][-1]["source"] == "timeout",
        {
            "status": timed_out_view["status"],
            "finishedAt": timed_out_view["finished_at"],
            "events": timed_out_view["events"],
            "scheduler": scheduler_view(tick["data"]["scheduler"]),
        },
    )
    checks.add(
        "timeout.no_run_left_hanging",
        tick["data"]["scheduler"]["running_count"] == 0,
        {"runningCount": tick["data"]["scheduler"]["running_count"]},
    )

    payload_queue_timeout = {
        "spec_version_id": version_id,
        "commit_sha": "3" * 40,
        "branch": "main",
        "trigger": "ci",
        "queue_timeout_seconds": 1,
    }
    status, created = api_call(
        base, "POST", f"/projects/{project_id}/runs", token, payload_queue_timeout
    )
    queue_timeout_id = created["data"]["id"]
    run_ids.append(queue_timeout_id)
    time.sleep(1.2)
    status, tick = api_call(
        base, "POST", f"/projects/{project_id}/scheduler/tick", token, {}
    )
    status_get, queued_timeout = api_call(
        base, "GET", f"/projects/{project_id}/runs/{queue_timeout_id}", token
    )
    queued_timeout_view = run_view(queued_timeout.get("data", {}))
    records["queue_timeout_run"] = {
        "tickStatus": status,
        "tickResponse": tick.get("data"),
        "getStatus": status_get,
        "response": queued_timeout_view,
    }
    checks.add(
        "timeout.queue_timeout_is_explicit",
        status_get == 200
        and queued_timeout_view["status"] == "timeout"
        and queued_timeout_view["events"][-1]["reason"] == "queue_timeout"
        and queued_timeout_view["events"][-1]["source"] == "timeout",
        {
            "status": queued_timeout_view["status"],
            "events": queued_timeout_view["events"],
        },
    )

    snapshot = db_snapshot(db_path, run_ids)
    records["database"] = snapshot
    payload_out = {
        "schemaVersion": 1,
        "taskId": "T02",
        "check": "C-T02-2 / T02-a3 / T02-a4",
        "generatedAt": now(),
        "checks": checks.as_dict(),
        "records": records,
        "database": snapshot,
    }
    write_evidence(out, "08-cancel-timeout.json", payload_out)
    return {"runs": run_ids}


def flow_queue_concurrency(base: str, data: dict, db_path: Path, checks: Checks, out: Path) -> dict:
    project_id = data["queue_project"]["id"]
    token = data["queue_member"]["token"]
    version_id = data["queue_v1"]["id"]
    records: dict[str, dict] = {}

    status, settings = api_call(
        base,
        "POST",
        f"/projects/{project_id}/scheduler/config",
        token,
        {"max_concurrency": 1, "runner_stale_seconds": 30},
    )
    records["configure"] = {"status": status, "response": settings.get("data")}
    checks.add(
        "queue.max_concurrency_is_1",
        status == 200 and settings["data"]["max_concurrency"] == 1,
        {"status": status, "settings": settings.get("data")},
    )

    runs: dict[str, dict] = {}
    for label, priority, commit in (
        ("low", 10, "1" * 40),
        ("high", 90, "2" * 40),
        ("mid", 50, "3" * 40),
    ):
        payload = {
            "spec_version_id": version_id,
            "commit_sha": commit,
            "branch": "main",
            "trigger": "ci",
            "priority": priority,
        }
        status, created = api_call(
            base, "POST", f"/projects/{project_id}/runs", token, payload
        )
        runs[label] = created["data"]
        records[f"create_{label}"] = {
            "request": {"method": "POST", "path": f"/projects/{project_id}/runs", "body": payload},
            "status": status,
            "response": run_view(created["data"]),
        }
        checks.add(
            f"queue.create_{label}_queued",
            status == 201 and created["data"]["status"] == "queued",
            {"status": status, "runId": created["data"]["id"], "priority": priority},
        )

    status, scheduler = api_call(
        base, "GET", f"/projects/{project_id}/scheduler", token
    )
    priority_order = [item["run_id"] for item in scheduler["data"]["priority_order"]]
    records["scheduler_before_claim"] = {
        "status": status,
        "response": scheduler_view(scheduler["data"]),
    }
    checks.add(
        "queue.depth_and_priority_order_observable",
        status == 200
        and scheduler["data"]["queue_depth"] == 3
        and scheduler["data"]["running_count"] == 0
        and priority_order == [runs["high"]["id"], runs["mid"]["id"], runs["low"]["id"]]
        and [item["position"] for item in scheduler["data"]["priority_order"]] == [1, 2, 3],
        {
            "queueDepth": scheduler["data"]["queue_depth"],
            "runningCount": scheduler["data"]["running_count"],
            "priorityOrder": priority_order,
            "positions": [item["position"] for item in scheduler["data"]["priority_order"]],
        },
    )

    status, runner_one = api_call(
        base,
        "POST",
        f"/projects/{project_id}/runners",
        token,
        {"name": "queue-runner-a", "capacity": 1},
    )
    status_two, runner_two = api_call(
        base,
        "POST",
        f"/projects/{project_id}/runners",
        token,
        {"name": "queue-runner-b", "capacity": 1},
    )
    token_one = runner_one["data"]["token"]
    token_two = runner_two["data"]["token"]
    records["runners"] = {
        "runnerOne": runner_view(runner_one["data"]),
        "runnerTwo": runner_view(runner_two["data"]),
    }

    status, first = api_call(base, "POST", "/runner/claim", token_two, {})
    records["claim_first"] = {
        "status": status,
        "reason": first.get("reason"),
        "response": run_view(first.get("data") or {}),
        "scheduler": scheduler_view(first["scheduler"]),
    }
    checks.add(
        "queue.highest_priority_claimed_first",
        status == 200
        and first["data"]["id"] == runs["high"]["id"]
        and first["data"]["priority"] == 90
        and first["scheduler"]["running_count"] == 1
        and first["scheduler"]["queue_depth"] == 2,
        {
            "status": status,
            "claimedRun": first["data"]["id"] if first.get("data") else None,
            "expectedRun": runs["high"]["id"],
            "priority": first["data"]["priority"] if first.get("data") else None,
            "runningCount": first["scheduler"]["running_count"],
            "queueDepth": first["scheduler"]["queue_depth"],
        },
    )

    status, blocked = api_call(base, "POST", "/runner/claim", token_one, {})
    records["claim_blocked"] = {
        "status": status,
        "reason": blocked.get("reason"),
        "data": blocked.get("data"),
        "scheduler": scheduler_view(blocked["scheduler"]),
    }
    checks.add(
        "queue.concurrency_limit_keeps_work_queued",
        status == 200
        and blocked.get("data") is None
        and blocked.get("reason") == "concurrency_limit_reached"
        and blocked["scheduler"]["running_count"] == 1
        and blocked["scheduler"]["queue_depth"] == 2,
        {
            "status": status,
            "reason": blocked.get("reason"),
            "runningCount": blocked["scheduler"]["running_count"],
            "queueDepth": blocked["scheduler"]["queue_depth"],
            "priorityOrder": [item["run_id"] for item in blocked["scheduler"]["priority_order"]],
        },
    )

    status, completed = api_call(
        base,
        "POST",
        f"/runner/runs/{runs['high']['id']}/complete",
        token_two,
        {"status": "succeeded", "summary": "high priority done"},
    )
    records["complete_high"] = {"status": status, "response": run_view(completed.get("data", {}))}
    status, second = api_call(base, "POST", "/runner/claim", token_one, {})
    records["claim_second"] = {
        "status": status,
        "reason": second.get("reason"),
        "response": run_view(second.get("data") or {}),
        "scheduler": scheduler_view(second["scheduler"]),
    }
    checks.add(
        "queue.next_priority_after_completion",
        status == 200
        and second["data"]["id"] == runs["mid"]["id"]
        and second["data"]["priority"] == 50
        and second["scheduler"]["queue_depth"] == 1
        and [item["run_id"] for item in second["scheduler"]["priority_order"]]
        == [runs["low"]["id"]],
        {
            "status": status,
            "claimedRun": second["data"]["id"] if second.get("data") else None,
            "expectedRun": runs["mid"]["id"],
            "queueDepth": second["scheduler"]["queue_depth"],
            "priorityOrder": [item["run_id"] for item in second["scheduler"]["priority_order"]],
        },
    )

    api_call(
        base,
        "POST",
        f"/runner/runs/{runs['mid']['id']}/complete",
        token_one,
        {"status": "succeeded"},
    )
    status, third = api_call(base, "POST", "/runner/claim", token_one, {})
    records["claim_third"] = {
        "status": status,
        "reason": third.get("reason"),
        "response": run_view(third.get("data") or {}),
        "scheduler": scheduler_view(third["scheduler"]),
    }
    checks.add(
        "queue.lowest_priority_claimed_last",
        status == 200
        and third["data"]["id"] == runs["low"]["id"]
        and third["data"]["priority"] == 10
        and third["scheduler"]["queue_depth"] == 0,
        {
            "status": status,
            "claimedRun": third["data"]["id"] if third.get("data") else None,
            "expectedRun": runs["low"]["id"],
            "queueDepth": third["scheduler"]["queue_depth"],
        },
    )

    run_ids = [runs[label]["id"] for label in ("low", "high", "mid")]
    snapshot = db_snapshot(db_path, run_ids)
    records["database"] = snapshot
    payload_out = {
        "schemaVersion": 1,
        "taskId": "T02",
        "check": "C-T02-3 / T02-a4",
        "generatedAt": now(),
        "checks": checks.as_dict(),
        "records": records,
        "database": snapshot,
    }
    write_evidence(out, "09-queue-concurrency.json", payload_out)
    return {"runs": run_ids}


def flow_runner_heartbeat(base: str, data: dict, db_path: Path, checks: Checks, out: Path) -> dict:
    project_id = data["beta"]["id"]
    token = data["beta_member"]["token"]
    version_id = data["beta_v1"]["id"]
    records: dict[str, dict] = {}

    status, settings = api_call(
        base,
        "POST",
        f"/projects/{project_id}/scheduler/config",
        token,
        {"max_concurrency": 2, "runner_stale_seconds": 1},
    )
    records["configure"] = {"status": status, "response": settings.get("data")}
    checks.add(
        "runner.stale_window_is_1s",
        status == 200 and settings["data"]["runner_stale_seconds"] == 1,
        {"status": status, "settings": settings.get("data")},
    )

    status, live = api_call(
        base,
        "POST",
        f"/projects/{project_id}/runners",
        token,
        {"name": "live-runner", "capacity": 1},
    )
    status_dead, dead = api_call(
        base,
        "POST",
        f"/projects/{project_id}/runners",
        token,
        {"name": "dead-runner", "capacity": 1},
    )
    live_token = live["data"]["token"]
    dead_token = dead["data"]["token"]
    records["registered"] = {
        "live": runner_view(live["data"]),
        "dead": runner_view(dead["data"]),
    }

    time.sleep(1.2)
    status, heartbeat = api_call(
        base, "POST", "/runner/heartbeat", live_token, {"status": "online"}
    )
    records["live_heartbeat"] = {
        "status": status,
        "response": runner_view(heartbeat.get("data", {})),
    }
    checks.add(
        "runner.live_heartbeat_is_observed",
        status == 200 and heartbeat["data"]["available"] is True,
        {"status": status, "runner": runner_view(heartbeat.get("data", {}))},
    )

    status, scheduler = api_call(
        base, "GET", f"/projects/{project_id}/scheduler", token
    )
    by_name = {item["name"]: item for item in scheduler["data"]["runners"]["items"]}
    records["scheduler_after_stale_window"] = {
        "status": status,
        "response": scheduler_view(scheduler["data"]),
    }
    checks.add(
        "runner.stale_runner_is_marked_unavailable",
        status == 200
        and by_name["live-runner"]["available"] is True
        and by_name["dead-runner"]["available"] is False
        and by_name["dead-runner"]["effective_status"] == "unavailable"
        and by_name["dead-runner"]["status"] == "unavailable"
        and scheduler["data"]["runners"]["available"] == 1,
        {
            "runners": scheduler["data"]["runners"]["items"],
            "availableCount": scheduler["data"]["runners"]["available"],
        },
    )

    status, dead_detail = api_call(
        base, "GET", f"/projects/{project_id}/runners/{dead['data']['id']}", token
    )
    records["dead_runner_detail"] = {
        "status": status,
        "response": dead_detail.get("data"),
    }
    checks.add(
        "runner.unavailable_reason_and_event_are_queryable",
        status == 200
        and dead_detail["data"]["status"] == "unavailable"
        and dead_detail["data"]["heartbeat_age_seconds"] is not None
        and any(event["kind"] == "went_unavailable" for event in dead_detail["data"]["events"]),
        {
            "status": dead_detail["data"]["status"],
            "heartbeatAgeSeconds": dead_detail["data"]["heartbeat_age_seconds"],
            "events": dead_detail["data"]["events"],
        },
    )

    payload = {
        "spec_version_id": version_id,
        "commit_sha": "1" * 40,
        "branch": "main",
        "trigger": "ci",
    }
    status, created = api_call(
        base, "POST", f"/projects/{project_id}/runs", token, payload
    )
    records["create_run"] = {
        "status": status,
        "response": run_view(created["data"]),
    }
    status, claim = api_call(base, "POST", "/runner/claim", live_token, {})
    records["claim_with_live_runner"] = {
        "status": status,
        "reason": claim.get("reason"),
        "response": run_view(claim.get("data") or {}),
        "scheduler": scheduler_view(claim["scheduler"]),
    }
    checks.add(
        "runner.new_run_is_assigned_to_live_runner_only",
        status == 200
        and claim["data"]["assigned_runner_id"] == live["data"]["id"]
        and claim["data"]["assigned_runner_id"] != dead["data"]["id"]
        and any(
            item["name"] == "dead-runner" and item["available"] is False
            for item in claim["scheduler"]["runners"]["items"]
        ),
        {
            "status": status,
            "assignedRunner": claim["data"]["assigned_runner_id"] if claim.get("data") else None,
            "liveRunner": live["data"]["id"],
            "deadRunner": dead["data"]["id"],
            "runners": claim["scheduler"]["runners"]["items"],
        },
    )

    status, recovered = api_call(
        base, "POST", "/runner/heartbeat", dead_token, {"status": "online"}
    )
    records["dead_runner_recovered"] = {
        "status": status,
        "response": runner_view(recovered.get("data", {})),
    }
    checks.add(
        "runner.heartbeat_recovers_unavailable_runner",
        status == 200
        and recovered["data"]["status"] == "online"
        and recovered["data"]["available"] is True,
        {"status": status, "runner": runner_view(recovered.get("data", {}))},
    )

    status, events = api_call(
        base,
        "GET",
        f"/projects/{project_id}/runners/{dead['data']['id']}/events",
        token,
    )
    records["dead_runner_events"] = {"status": status, "response": events.get("data")}
    checks.add(
        "runner.lifecycle_events_are_append_only",
        status == 200
        and [event["kind"] for event in events["data"][:3]]
        == ["recovered", "went_unavailable", "registered"],
        {"kinds": [event["kind"] for event in events["data"]]},
    )

    snapshot = db_snapshot(db_path, [created["data"]["id"]])
    records["database"] = snapshot
    payload_out = {
        "schemaVersion": 1,
        "taskId": "T02",
        "check": "T02-a5",
        "generatedAt": now(),
        "checks": checks.as_dict(),
        "records": records,
        "database": snapshot,
    }
    write_evidence(out, "10-runner-heartbeat.json", payload_out)
    return {"run_id": created["data"]["id"], "runners": [live["data"]["id"], dead["data"]["id"]]}


def flow_event_ledger(db_path: Path, checks: Checks, out: Path) -> dict:
    conn = db.connect(db_path)
    try:
        runs = conn.execute(
            "SELECT id, project_id, status FROM run ORDER BY created_at, rowid"
        ).fetchall()
        ledger = []
        terminal = {"succeeded", "failed", "cancelled", "timeout"}
        for run in runs:
            events = [
                dict(row)
                for row in conn.execute(
                    "SELECT seq, from_status, to_status, source, actor, reason, created_at "
                    "FROM run_event WHERE run_id = ? ORDER BY seq",
                    (run["id"],),
                ).fetchall()
            ]
            ledger.append(
                {
                    "run_id": run["id"],
                    "project_id": run["project_id"],
                    "status": run["status"],
                    "transitions": events,
                }
            )
        checks.add(
            "events.every_run_has_contiguous_ledger",
            all(
                [event["seq"] for event in item["transitions"]]
                == list(range(1, len(item["transitions"]) + 1))
                and item["transitions"][0]["from_status"] is None
                and item["transitions"][0]["to_status"] == "queued"
                for item in ledger
            ),
            {
                "runCount": len(ledger),
                "eventCount": sum(len(item["transitions"]) for item in ledger),
            },
        )
        checks.add(
            "events.terminal_runs_have_no_outgoing_transition",
            all(
                item["transitions"][-1]["to_status"] == item["status"]
                and (
                    item["status"] not in terminal
                    or item["transitions"][-1]["to_status"] in terminal
                )
                for item in ledger
            ),
            {
                "terminalRuns": [
                    item["run_id"] for item in ledger if item["status"] in terminal
                ]
            },
        )
        sources = {
            event["source"]
            for item in ledger
            for event in item["transitions"]
        }
        checks.add(
            "events.sources_are_from_fixed_enum",
            sources.issubset({"api", "scheduler", "runner", "timeout", "system"}),
            {"sources": sorted(sources)},
        )
        payload_out = {
            "schemaVersion": 1,
            "taskId": "T02",
            "check": "C-T02-2 execution ledger",
            "generatedAt": now(),
            "checks": checks.as_dict(),
            "ledger": ledger,
        }
        write_evidence(out, "11-run-events.json", payload_out)
        return {"runs": len(ledger), "events": sum(len(item["transitions"]) for item in ledger)}
    finally:
        conn.close()


def flow_schema(db_path: Path, checks: Checks, out: Path) -> dict:
    conn = db.connect(db_path)
    try:
        migrations_rows = [
            dict(row)
            for row in conn.execute(
                "SELECT version, name, checksum, applied_at FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
        tables = [
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        schema_sql = db.schema_sql(conn)
        digest_value = db.schema_digest(conn)
        fk_check = [dict(row) for row in conn.execute("PRAGMA foreign_key_check").fetchall()]
        counters = {
            "runs": conn.execute("SELECT COUNT(*) FROM run").fetchone()[0],
            "runEvents": conn.execute("SELECT COUNT(*) FROM run_event").fetchone()[0],
            "runners": conn.execute("SELECT COUNT(*) FROM runner").fetchone()[0],
            "runnerEvents": conn.execute("SELECT COUNT(*) FROM runner_event").fetchone()[0],
        }
    finally:
        conn.close()
    out.mkdir(parents=True, exist_ok=True)
    (out / "t02-schema.sql").write_text(schema_sql, encoding="utf-8")
    checks.add(
        "schema.t02_tables_present",
        {"runner", "run_event", "runner_event"}.issubset(set(tables)),
        {"tables": tables},
    )
    checks.add(
        "schema.migrations_applied",
        [row["version"] for row in migrations_rows] == [1, 2],
        {"migrations": migrations_rows},
    )
    checks.add("schema.foreign_keys_clean", fk_check == [], {"foreignKeyCheck": fk_check})
    payload_out = {
        "schemaVersion": 1,
        "taskId": "T02",
        "generatedAt": now(),
        "checks": checks.as_dict(),
        "schemaDigest": digest_value,
        "migrations": migrations_rows,
        "tables": tables,
        "counters": counters,
        "schemaSql": "t02-schema.sql",
    }
    write_evidence(out, "12-schema.json", payload_out)
    return {"schemaDigest": digest_value, "counters": counters}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run reproducible T02 verification")
    parser.add_argument("--out", default=str(ROOT.parent / "deliverables" / "evidence"))
    args = parser.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    tmp = tempfile.TemporaryDirectory(prefix="aitest-t02-verify-")
    db_path = Path(tmp.name) / "platform.db"
    data = setup_platform(db_path)

    server = http_api.make_server(db_path, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    flows: list[dict] = []
    all_items: list[dict] = []
    results: dict[str, Any] = {}
    try:
        for name, func, key in (
            ("idempotency", flow_idempotency, "idempotency"),
            ("cancel_timeout", flow_cancel_timeout, "cancel_timeout"),
            ("queue_concurrency", flow_queue_concurrency, "queue_concurrency"),
            ("runner_heartbeat", flow_runner_heartbeat, "runner_heartbeat"),
        ):
            print(f"\n== flow: {name} ==")
            checks = Checks()
            result = func(base, data, db_path, checks, out)
            results[key] = result
            summary = checks.as_dict()
            all_items.extend(checks.items)
            flows.append({"name": name, "checks": summary})

        print("\n== flow: event_ledger ==")
        checks = Checks()
        results["event_ledger"] = flow_event_ledger(db_path, checks, out)
        all_items.extend(checks.items)
        flows.append({"name": "event_ledger", "checks": checks.as_dict()})

        print("\n== flow: schema ==")
        checks = Checks()
        results["schema"] = flow_schema(db_path, checks, out)
        all_items.extend(checks.items)
        flows.append({"name": "schema", "checks": checks.as_dict()})
    finally:
        server.shutdown()
        server.server_close()
        tmp.cleanup()

    failed = [item for item in all_items if not item["passed"]]
    evidence_files = []
    for path in sorted(out.glob("*.json")) + sorted(out.glob("*.sql")):
        if path.name == "t02-verification-summary.json":
            continue
        payload = path.read_bytes()
        evidence_files.append(
            {
                "path": path.name,
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    summary = {
        "schemaVersion": 1,
        "taskId": "T02",
        "title": "调度、Runner 管理与 Run 生命周期 API",
        "generatedAt": now(),
        "allPassed": not failed,
        "checkCount": len(all_items),
        "passed": len(all_items) - len(failed),
        "failed": len(failed),
        "failedChecks": [item["name"] for item in failed],
        "flows": flows,
        "results": results,
        "schemaDigest": results.get("schema", {}).get("schemaDigest"),
        "counters": results.get("schema", {}).get("counters"),
        "evidence": evidence_files,
        "command": "PYTHONPATH=src python3 scripts/verify_t02.py --out ../deliverables/evidence",
        "limitations": [
            "Runs against a temporary SQLite database and a local HTTP server, not a production deployment.",
            "Runner execution is simulated through the claim/complete API; no real browser or GPU workload was executed.",
            "Scheduler maintenance is tick-driven; the verification calls scheduler/tick explicitly for deterministic timeouts.",
        ],
    }
    (out / "t02-verification-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"\nT02 verification: {summary['passed']}/{summary['checkCount']} checks passed; "
        f"evidence={out}"
    )
    if failed:
        print("FAILED: " + ", ".join(item["name"] for item in failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
