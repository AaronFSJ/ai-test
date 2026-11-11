#!/usr/bin/env python3
"""Run reproducible T01 checks and write machine-readable evidence.

This script is intentionally standalone (standard library only). It starts a
real local HTTP server, exercises the API and HTML view, and writes JSON
evidence under deliverables/evidence (or --out).
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


def excerpt(value: Any, limit: int = 1600) -> Any:
    """Keep evidence readable without hiding the important fields."""
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

    def as_dict(self) -> dict:
        return {
            "allPassed": all(item["passed"] for item in self.items),
            "checks": self.items,
        }


def api_call(base: str, method: str, path: str, token: str | None = None, payload=None) -> tuple[int, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            body = response.read().decode("utf-8")
            try:
                return response.status, json.loads(body)
            except json.JSONDecodeError:
                return response.status, {"raw": body}
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8")
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                parsed = {"raw": body}
            return exc.code, parsed
        finally:
            exc.close()


def ui_call(base: str, token: str, extra: str = "") -> tuple[int, str]:
    request = urllib.request.Request(
        base + f"/ui?token={token}{extra}", headers={"Accept": "text/html"}
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read().decode("utf-8")
        finally:
            exc.close()


def migration_evidence(tmp: Path) -> tuple[dict, str]:
    db1 = tmp / "empty-one.db"
    db2 = tmp / "empty-two.db"
    first1 = migrations.migrate(db1)
    second1 = migrations.migrate(db1)
    first2 = migrations.migrate(db2)
    second2 = migrations.migrate(db2)
    conn = db.connect(db1)
    try:
        tables = [
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        fks = db.foreign_keys(conn)
        fk_check = [dict(row) for row in conn.execute("PRAGMA foreign_key_check").fetchall()]
        counts = {
            table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in tables
        }
        schema_dump = db.schema_sql(conn)
    finally:
        conn.close()

    core = {
        "project",
        "member",
        "spec",
        "spec_version",
        "run",
        "run_item",
        "verdict",
        "attribution",
        "evidence",
        "review",
        "cost_ledger",
    }
    evidence = {
        "checkedAt": now(),
        "database": "empty-one.db (temporary, created by verifier)",
        "firstApply": {
            "appliedNow": len(first1["appliedNow"]),
            "appliedTotal": first1["appliedTotal"],
            "schemaDigest": first1["schemaDigest"],
        },
        "secondApply": {
            "appliedNow": len(second1["appliedNow"]),
            "appliedTotal": second1["appliedTotal"],
            "schemaDigest": second1["schemaDigest"],
        },
        "secondEmptyDatabase": {
            "firstApplyDigest": first2["schemaDigest"],
            "secondApplyDigest": second2["schemaDigest"],
        },
        "digestStable": first1["schemaDigest"]
        == second1["schemaDigest"]
        == first2["schemaDigest"]
        == second2["schemaDigest"],
        "tables": tables,
        "coreTablesPresent": sorted(core.intersection(tables)) == sorted(core),
        "emptyDatabaseRows": counts,
        "emptyEntityTables": {name: value for name, value in counts.items() if name != "schema_migrations"},
        "foreignKeys": fks,
        "foreignKeyCheck": fk_check,
        "foreignKeyEnforcement": None,
    }
    # The connection above is closed; read the pragma on a fresh one.
    conn = db.connect(db1)
    try:
        evidence["foreignKeyEnforcement"] = conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()
    return evidence, schema_dump


def setup_platform(db_path: Path) -> dict:
    migrations.migrate(db_path)
    conn = db.connect(db_path)
    try:
        alpha = service.create_project(conn, "Alpha")
        beta = service.create_project(conn, "Beta")
        alpha_member = service.add_member(conn, alpha["id"], "alice", "member")
        alpha_ro = service.add_member(conn, alpha["id"], "alice-ro", "readonly")
        beta_member = service.add_member(conn, beta["id"], "bob", "member")
        beta_ro = service.add_member(conn, beta["id"], "bob-ro", "readonly")
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
            {
                "repo_path": "github.com/acme/web",
                "repo_commit": "a" * 40,
                "params": {"suite": "smoke"},
            },
        )
        # Bind a run before the second version exists.
        alpha_run_v1 = service.create_run(
            conn,
            alpha_actor,
            alpha["id"],
            {
                "spec_version_id": alpha_v1["id"],
                "commit_sha": "c" * 40,
                "branch": "main",
                "trigger": "ci",
            },
        )
        alpha_v2 = service.add_spec_version(
            conn,
            alpha_actor,
            alpha["id"],
            alpha_spec["id"],
            {
                "repo_path": "github.com/acme/web",
                "repo_commit": "b" * 40,
                "params": {"suite": "smoke", "retries": 2},
            },
        )

        beta_spec = service.create_spec(
            conn, beta_actor, beta["id"], {"channel": "B", "name": "beta-eval"}
        )
        beta_v1 = service.add_spec_version(
            conn,
            beta_actor,
            beta["id"],
            beta_spec["id"],
            {
                "dataset_version": "eval-2026.10.1",
                "scorer_version": "scorer-v1",
                "model_snapshot": {"model": "qwen2.5-7b", "temperature": 0},
            },
        )
        beta_run_v1 = service.create_run(
            conn,
            beta_actor,
            beta["id"],
            {
                "spec_version_id": beta_v1["id"],
                "commit_sha": "d" * 40,
                "trigger": "manual",
            },
        )
        beta_v2 = service.add_spec_version(
            conn,
            beta_actor,
            beta["id"],
            beta_spec["id"],
            {
                "dataset_version": "eval-2026.11.1",
                "scorer_version": "scorer-v2",
                "model_snapshot": {"model": "qwen2.5-7b", "temperature": 0.2},
            },
        )
    finally:
        conn.close()
    return {
        "alpha": alpha,
        "beta": beta,
        "alpha_member": alpha_member,
        "alpha_ro": alpha_ro,
        "beta_member": beta_member,
        "beta_ro": beta_ro,
        "alpha_spec": alpha_spec,
        "alpha_v1": alpha_v1,
        "alpha_v2": alpha_v2,
        "alpha_run_v1": alpha_run_v1,
        "beta_spec": beta_spec,
        "beta_v1": beta_v1,
        "beta_v2": beta_v2,
        "beta_run_v1": beta_run_v1,
    }


def run_flow(db_path: Path, checks: Checks, with_records: bool = True) -> dict:
    data = setup_platform(db_path)
    alpha_id = data["alpha"]["id"]
    beta_id = data["beta"]["id"]
    alpha_token = data["alpha_member"]["token"]
    alpha_ro_token = data["alpha_ro"]["token"]
    beta_token = data["beta_member"]["token"]
    records: dict[str, dict] = {}
    outcomes: dict[str, int] = {}

    def call(key, method, path, token=None, payload=None, actor=None):
        status, body = api_call(base, method, path, token, payload)
        outcomes[key] = status
        if with_records:
            records[key] = {
                "actor": actor,
                "request": {"method": method, "path": path},
                "status": status,
                "response": excerpt(body),
            }
        return status, body

    def ui(key, token, extra="", flags=None):
        status, page = ui_call(base, token, extra)
        outcomes[key] = status
        if with_records:
            records[key] = {
                "request": {"method": "GET", "path": f"/ui{extra}"},
                "status": status,
                "checks": flags or {},
                "snippet": excerpt(page, 600),
            }
        return status, page

    server = http_api.make_server(db_path, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        status, health = call("health", "GET", "/health")
        checks.add("health", status == 200 and health.get("status") == "ok", health)

        status, alpha_specs = call(
            "alpha_specs", "GET", f"/projects/{alpha_id}/specs", alpha_token, actor="alice/member"
        )
        alpha_names = {item["name"] for item in alpha_specs.get("data", [])}
        checks.add(
            "isolation.api.specs.only_own",
            status == 200 and alpha_names == {"alpha-checkout"},
            {"status": status, "names": sorted(alpha_names)},
        )

        status, alpha_runs = call(
            "alpha_runs", "GET", f"/projects/{alpha_id}/runs", alpha_token, actor="alice/member"
        )
        alpha_run_ids = {item["id"] for item in alpha_runs.get("data", [])}
        checks.add(
            "isolation.api.runs.only_own",
            status == 200 and data["alpha_run_v1"]["id"] in alpha_run_ids
            and data["beta_run_v1"]["id"] not in alpha_run_ids,
            {"status": status, "runIds": sorted(alpha_run_ids)},
        )

        status, projects = call("projects_alpha", "GET", "/projects", alpha_token, actor="alice/member")
        checks.add(
            "isolation.api.projects.only_own",
            status == 200 and [p["id"] for p in projects.get("data", [])] == [alpha_id],
            {"status": status, "projectIds": [p["id"] for p in projects.get("data", [])]},
        )

        status, body = call(
            "cross_project_specs",
            "GET",
            f"/projects/{beta_id}/specs",
            alpha_token,
            actor="alice/member",
        )
        checks.add("isolation.api.cross_specs_404", status == 404, {"status": status, "body": body})
        status, body = call(
            "cross_project_runs",
            "GET",
            f"/projects/{beta_id}/runs",
            alpha_token,
            actor="alice/member",
        )
        checks.add("isolation.api.cross_runs_404", status == 404, {"status": status, "body": body})
        status, body = call(
            "cross_project_run_detail",
            "GET",
            f"/projects/{beta_id}/runs/{data['beta_run_v1']['id']}",
            alpha_token,
            actor="alice/member",
        )
        checks.add("isolation.api.cross_run_detail_404", status == 404, {"status": status, "body": body})

        status, page = ui("ui_alpha", alpha_token)
        alpha_flags = {
            "status200": status == 200,
            "ownSpecVisible": "alpha-checkout" in page,
            "ownRunVisible": data["alpha_run_v1"]["id"] in page,
            "otherSpecHidden": "beta-eval" not in page,
            "otherRunHidden": data["beta_run_v1"]["id"] not in page,
            "projectId": f'data-project-id="{alpha_id}"' in page,
        }
        if with_records:
            records["ui_alpha"]["checks"] = alpha_flags
        checks.add("isolation.ui.alpha_only_own", all(alpha_flags.values()), alpha_flags)

        status, page = ui("ui_beta", beta_token)
        beta_flags = {
            "status200": status == 200,
            "ownSpecVisible": "beta-eval" in page,
            "ownRunVisible": data["beta_run_v1"]["id"] in page,
            "otherSpecHidden": "alpha-checkout" not in page,
            "otherRunHidden": data["alpha_run_v1"]["id"] not in page,
            "projectId": f'data-project-id="{beta_id}"' in page,
        }
        if with_records:
            records["ui_beta"]["checks"] = beta_flags
        checks.add("isolation.ui.beta_only_own", all(beta_flags.values()), beta_flags)

        status, page = ui("ui_alpha_cross", alpha_token, f"&project_id={beta_id}")
        cross_ui_flags = {
            "status404": status == 404,
            "otherSpecHidden": "beta-eval" not in page,
        }
        if with_records:
            records["ui_alpha_cross"]["checks"] = cross_ui_flags
        checks.add("isolation.ui.cross_project_404", all(cross_ui_flags.values()), cross_ui_flags)

        # -- permissions -------------------------------------------------
        status, body = call(
            "ro_create_spec",
            "POST",
            f"/projects/{alpha_id}/specs",
            alpha_ro_token,
            {"channel": "A", "name": "readonly-must-not-create"},
            actor="alice-ro/readonly",
        )
        checks.add(
            "permissions.readonly.write_spec_denied",
            status == 403 and body.get("error", {}).get("code") == "forbidden",
            {"status": status, "body": body},
        )

        status, body = call(
            "ro_add_version",
            "POST",
            f"/projects/{alpha_id}/specs/{data['alpha_spec']['id']}/versions",
            alpha_ro_token,
            {"repo_path": "github.com/acme/web", "repo_commit": "e" * 40},
            actor="alice-ro/readonly",
        )
        checks.add("permissions.readonly.write_version_denied", status == 403, {"status": status, "body": body})

        status, body = call(
            "ro_create_run",
            "POST",
            f"/projects/{alpha_id}/runs",
            alpha_ro_token,
            {"spec_version_id": data["alpha_v2"]["id"], "commit_sha": "f" * 40},
            actor="alice-ro/readonly",
        )
        checks.add("permissions.readonly.write_run_denied", status == 403, {"status": status, "body": body})

        status, body = call(
            "ro_read_specs",
            "GET",
            f"/projects/{alpha_id}/specs",
            alpha_ro_token,
            actor="alice-ro/readonly",
        )
        checks.add("permissions.readonly.read_allowed", status == 200, {"status": status})

        status, body = call(
            "member_create_spec",
            "POST",
            f"/projects/{alpha_id}/specs",
            alpha_token,
            {"channel": "A", "name": "alpha-member-created"},
            actor="alice/member",
        )
        member_spec_id = body.get("data", {}).get("id")
        checks.add(
            "permissions.member.write_spec_allowed",
            status == 201 and bool(member_spec_id),
            {"status": status, "specId": member_spec_id},
        )

        status, body = call(
            "member_add_version",
            "POST",
            f"/projects/{alpha_id}/specs/{member_spec_id}/versions",
            alpha_token,
            {"repo_path": "github.com/acme/web", "repo_commit": "e" * 40},
            actor="alice/member",
        )
        member_version_id = body.get("data", {}).get("id")
        checks.add(
            "permissions.member.write_version_allowed",
            status == 201 and bool(member_version_id),
            {"status": status, "versionId": member_version_id},
        )

        status, body = call(
            "member_create_run",
            "POST",
            f"/projects/{alpha_id}/runs",
            alpha_token,
            {"spec_version_id": member_version_id, "commit_sha": "f" * 40},
            actor="alice/member",
        )
        member_run_id = body.get("data", {}).get("id")
        checks.add(
            "permissions.member.write_run_allowed",
            status == 201 and body.get("data", {}).get("spec_version", {}).get("version") == 1,
            {"status": status, "runId": member_run_id},
        )

        status, body = call(
            "member_add_item",
            "POST",
            f"/projects/{alpha_id}/runs/{member_run_id}/items",
            alpha_token,
            {"case_ref": "checkout-1", "state": "fail", "confidence": 0.9, "duration_ms": 1234},
            actor="alice/member",
        )
        checks.add("permissions.member.write_item_allowed", status == 201, {"status": status, "body": body})

        status, body = call(
            "ro_add_item",
            "POST",
            f"/projects/{alpha_id}/runs/{member_run_id}/items",
            alpha_ro_token,
            {"case_ref": "checkout-2", "state": "pass"},
            actor="alice-ro/readonly",
        )
        checks.add("permissions.readonly.write_item_denied", status == 403, {"status": status, "body": body})

        status, body = call(
            "ro_repeat_create_spec",
            "POST",
            f"/projects/{alpha_id}/specs",
            alpha_ro_token,
            {"channel": "A", "name": "readonly-repeat"},
            actor="alice-ro/readonly",
        )
        checks.add("permissions.repeatable_denial", status == 403, {"status": status, "body": body})

        # -- spec versioning --------------------------------------------
        status, alpha_spec_detail = call(
            "alpha_spec_detail",
            "GET",
            f"/projects/{alpha_id}/specs/{data['alpha_spec']['id']}",
            alpha_token,
            actor="alice/member",
        )
        versions = [v["version"] for v in alpha_spec_detail.get("data", {}).get("versions", [])]
        checks.add(
            "spec_versions.two_versions_coexist",
            status == 200 and versions == [1, 2],
            {"status": status, "versions": versions},
        )

        status, alpha_run_v1_detail = call(
            "alpha_run_v1_detail",
            "GET",
            f"/projects/{alpha_id}/runs/{data['alpha_run_v1']['id']}",
            alpha_token,
            actor="alice/member",
        )
        bound = alpha_run_v1_detail.get("data", {}).get("spec_version", {})
        alpha_binding_flags = {
            "status200": status == 200,
            "specVersionId": bound.get("id") == data["alpha_v1"]["id"],
            "version": bound.get("version") == 1,
            "repoCommit": bound.get("repo_commit") == "a" * 40,
            "channel": bound.get("channel") == "A",
        }
        checks.add("spec_versions.historical_run_binding", all(alpha_binding_flags.values()), alpha_binding_flags)

        status, body = call(
            "alpha_create_run_v2",
            "POST",
            f"/projects/{alpha_id}/runs",
            alpha_token,
            {"spec_id": data["alpha_spec"]["id"], "version": 2, "commit_sha": "1" * 40},
            actor="alice/member",
        )
        v2_bound = body.get("data", {}).get("spec_version", {})
        checks.add(
            "spec_versions.run_binds_explicit_version",
            status == 201 and v2_bound.get("version") == 2 and v2_bound.get("repo_commit") == "b" * 40,
            {"status": status, "specVersion": v2_bound},
        )

        status, body = call(
            "alpha_run_missing_version",
            "POST",
            f"/projects/{alpha_id}/runs",
            alpha_token,
            {"spec_id": data["alpha_spec"]["id"], "commit_sha": "2" * 40},
            actor="alice/member",
        )
        checks.add("spec_versions.run_requires_explicit_version", status == 400, {"status": status, "body": body})

        conn = db.connect(db_path)
        try:
            try:
                conn.execute(
                    "UPDATE spec_version SET repo_commit = 'tampered' WHERE id = ?",
                    (data["alpha_v1"]["id"],),
                )
                update_error = None
            except sqlite3.IntegrityError as exc:
                update_error = str(exc)
            try:
                conn.execute("DELETE FROM spec_version WHERE id = ?", (data["alpha_v1"]["id"],))
                delete_error = None
            except sqlite3.IntegrityError as exc:
                delete_error = str(exc)
        finally:
            conn.close()
        if with_records:
            records["spec_version_immutability"] = {
                "updateError": update_error,
                "deleteError": delete_error,
            }
        checks.add(
            "spec_versions.immutable",
            update_error is not None and delete_error is not None,
            {"updateError": update_error, "deleteError": delete_error},
        )

        # -- A/B channel fields ------------------------------------------
        a_v1 = alpha_spec_detail.get("data", {}).get("versions", [{}])[0]
        a_flags = {
            "repoPath": a_v1.get("repo_path") == "github.com/acme/web",
            "repoCommit": a_v1.get("repo_commit") == "a" * 40,
            "noDatasetVersion": a_v1.get("dataset_version") is None,
            "noScorerVersion": a_v1.get("scorer_version") is None,
            "noModelSnapshot": a_v1.get("model_snapshot") is None,
        }
        checks.add("channel.A.repo_fields", all(a_flags.values()), a_flags)

        status, beta_spec_detail = call(
            "beta_spec_detail",
            "GET",
            f"/projects/{beta_id}/specs/{data['beta_spec']['id']}",
            beta_token,
            actor="bob/member",
        )
        b_v1 = beta_spec_detail.get("data", {}).get("versions", [{}])[0]
        b_flags = {
            "status200": status == 200,
            "datasetVersion": b_v1.get("dataset_version") == "eval-2026.10.1",
            "scorerVersion": b_v1.get("scorer_version") == "scorer-v1",
            "modelSnapshot": b_v1.get("model_snapshot") == {"model": "qwen2.5-7b", "temperature": 0},
            "noRepoPath": b_v1.get("repo_path") is None,
            "noRepoCommit": b_v1.get("repo_commit") is None,
        }
        checks.add("channel.B.dataset_scorer_fields", all(b_flags.values()), b_flags)

        status, beta_run_detail = call(
            "beta_run_detail",
            "GET",
            f"/projects/{beta_id}/runs/{data['beta_run_v1']['id']}",
            beta_token,
            actor="bob/member",
        )
        b_bound = beta_run_detail.get("data", {}).get("spec_version", {})
        b_run_flags = {
            "status200": status == 200,
            "version": b_bound.get("version") == 1,
            "datasetVersion": b_bound.get("dataset_version") == "eval-2026.10.1",
            "scorerVersion": b_bound.get("scorer_version") == "scorer-v1",
            "modelSnapshot": b_bound.get("model_snapshot") == {"model": "qwen2.5-7b", "temperature": 0},
        }
        checks.add("channel.B.run_snapshot", all(b_run_flags.values()), b_run_flags)

        status, body = call(
            "a_reject_dataset_field",
            "POST",
            f"/projects/{alpha_id}/specs/{data['alpha_spec']['id']}/versions",
            alpha_token,
            {
                "repo_path": "github.com/acme/web",
                "repo_commit": "3" * 40,
                "dataset_version": "not-allowed",
            },
            actor="alice/member",
        )
        checks.add("channel.A.rejects_B_fields", status == 400, {"status": status, "body": body})

        status, body = call(
            "b_reject_repo_field",
            "POST",
            f"/projects/{beta_id}/specs/{data['beta_spec']['id']}/versions",
            beta_token,
            {
                "dataset_version": "eval-x",
                "scorer_version": "scorer-x",
                "model_snapshot": {"model": "qwen2.5-7b"},
                "repo_path": "not-allowed",
            },
            actor="bob/member",
        )
        checks.add("channel.B.rejects_A_fields", status == 400, {"status": status, "body": body})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    return {
        "records": records if with_records else None,
        "outcome": outcomes,
        "checks": checks.as_dict(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run T01 verification and write evidence")
    parser.add_argument(
        "--out",
        default=str(ROOT.parent / "deliverables" / "evidence"),
        help="evidence directory",
    )
    args = parser.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="aitest-t01-verify-") as tmp:
        tmp_path = Path(tmp)
        migration, schema_dump = migration_evidence(tmp_path)
        (out / "schema.sql").write_text(schema_dump, encoding="utf-8")

        flow1_checks = Checks()
        flow2_checks = Checks()
        flow1 = run_flow(tmp_path / "flow-one.db", flow1_checks, with_records=True)
        flow2 = run_flow(tmp_path / "flow-two.db", flow2_checks, with_records=False)

        reproducibility = Checks()
        reproducibility.add(
            "repeatability.outcomes_equal",
            flow1["outcome"] == flow2["outcome"],
            {"first": flow1["outcome"], "second": flow2["outcome"]},
        )
        reproducibility.add(
            "repeatability.all_checks_pass_twice",
            flow1_checks.as_dict()["allPassed"] and flow2_checks.as_dict()["allPassed"],
            {
                "first": flow1_checks.as_dict()["allPassed"],
                "second": flow2_checks.as_dict()["allPassed"],
            },
        )

        files = {
            "01-migrations.json": {
                "taskId": "T01",
                "generatedAt": now(),
                "check": "C-T01-1",
                "evidence": migration,
                "checkResult": migration["digestStable"]
                and migration["coreTablesPresent"]
                and migration["foreignKeyEnforcement"]
                and not migration["foreignKeyCheck"]
                and all(value == 0 for value in migration["emptyEntityTables"].values()),
            },
            "02-project-isolation.json": {
                "taskId": "T01",
                "generatedAt": now(),
                "check": "T01-a2",
                "checks": [
                    item for item in flow1["checks"]["checks"] if item["name"].startswith("isolation.")
                ],
                "records": {
                    key: value
                    for key, value in flow1["records"].items()
                    if key.startswith("alpha_")
                    or key.startswith("beta_")
                    or key.startswith("cross_")
                    or key.startswith("ui_")
                    or key == "projects_alpha"
                },
            },
            "03-permissions.json": {
                "taskId": "T01",
                "generatedAt": now(),
                "check": "C-T01-2",
                "checks": [
                    item for item in flow1["checks"]["checks"] if item["name"].startswith("permissions.")
                ],
                "records": {
                    key: value
                    for key, value in flow1["records"].items()
                    if key.startswith("ro_") or key.startswith("member_")
                },
            },
            "04-spec-versioning.json": {
                "taskId": "T01",
                "generatedAt": now(),
                "check": "C-T01-3 / T01-a4",
                "checks": [
                    item for item in flow1["checks"]["checks"] if item["name"].startswith("spec_versions.")
                ],
                "records": {
                    key: value
                    for key, value in flow1["records"].items()
                    if key.startswith("alpha_spec_detail")
                    or key.startswith("alpha_run")
                    or key.startswith("alpha_create_run")
                    or key == "spec_version_immutability"
                },
            },
            "05-channel-fields.json": {
                "taskId": "T01",
                "generatedAt": now(),
                "check": "T01-a5",
                "checks": [
                    item for item in flow1["checks"]["checks"] if item["name"].startswith("channel.")
                ],
                "records": {
                    key: value
                    for key, value in flow1["records"].items()
                    if key in ("alpha_spec_detail", "beta_spec_detail", "beta_run_detail", "a_reject_dataset_field", "b_reject_repo_field")
                },
            },
            "06-reproducibility.json": {
                "taskId": "T01",
                "generatedAt": now(),
                "checks": reproducibility.as_dict(),
                "note": "The same API/UI flow was executed against two independent empty databases.",
            },
        }

        for name, payload in files.items():
            (out / name).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )

        all_checks = migration["digestStable"] and migration["coreTablesPresent"]
        for item in flow1["checks"]["checks"] + flow2["checks"]["checks"] + reproducibility.items:
            all_checks = all_checks and item["passed"]
        summary = {
            "schemaVersion": 1,
            "taskId": "T01",
            "title": "平台骨架、数据模型与项目/成员权限",
            "generatedAt": now(),
            "allChecksPassed": bool(all_checks),
            "migration": {
                "digestStable": migration["digestStable"],
                "coreTablesPresent": migration["coreTablesPresent"],
                "foreignKeyEnforcement": migration["foreignKeyEnforcement"],
                "foreignKeyCheck": migration["foreignKeyCheck"],
                "tableCount": len(migration["tables"]),
            },
            "flowChecks": flow1["checks"]["checks"],
            "reproducibility": reproducibility.as_dict(),
            "evidenceFiles": {},
        }
        for name in sorted(files):
            path = out / name
            summary["evidenceFiles"][name] = {
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        (out / "verification-summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if all_checks else 1


if __name__ == "__main__":
    sys.exit(main())
