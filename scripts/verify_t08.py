#!/usr/bin/env python3
"""Run reproducible T08 checks and write machine-readable evidence.

The script starts a real local HTTP server, exercises dataset import,
versioning, export/roundtrip, permissions and isolation, then writes JSON
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


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def excerpt(value: Any, limit: int = 1600) -> Any:
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


def http_call(
    base: str,
    method: str,
    path: str,
    token: str | None = None,
    payload=None,
    raw: bool = False,
):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8")
            response_headers = {key.lower(): value for key, value in response.headers.items()}
            if raw:
                return response.status, body, response_headers
            try:
                return response.status, json.loads(body)
            except json.JSONDecodeError:
                return response.status, {"raw": body}
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8")
            response_headers = {key.lower(): value for key, value in exc.headers.items()}
            if raw:
                return exc.code, body, response_headers
            try:
                return exc.code, json.loads(body)
            except json.JSONDecodeError:
                return exc.code, {"raw": body}
        finally:
            exc.close()


def jsonl(rows: list[dict]) -> str:
    return "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n"


def write_json(out: Path, name: str, payload: dict) -> Path:
    path = out / name
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return path


def schema_evidence(tmp: Path, checks: Checks) -> dict:
    db_one = tmp / "t08-one.db"
    db_two = tmp / "t08-two.db"
    first_one = migrations.migrate(db_one)
    second_one = migrations.migrate(db_one)
    first_two = migrations.migrate(db_two)
    conn = db.connect(db_one)
    try:
        tables = [
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        triggers = [
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' ORDER BY name"
            ).fetchall()
        ]
        fk_check = [dict(row) for row in conn.execute("PRAGMA foreign_key_check").fetchall()]
        counts = {
            table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in ("dataset", "dataset_version", "sample", "dataset_version_sample")
        }
    finally:
        conn.close()

    stable = (
        first_one["schemaDigest"]
        == second_one["schemaDigest"]
        == first_two["schemaDigest"]
    )
    dataset_tables = {"dataset", "dataset_version", "sample", "dataset_version_sample"}
    immutable_trigger = False
    conn = db.connect(db_one)
    try:
        immutable_trigger = (
            conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name='dataset_version_no_update'"
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()

    checks.add("schema.migrations_repeatable", stable, {
        "firstApply": first_one["appliedNow"],
        "secondApplyCount": len(second_one["appliedNow"]),
        "schemaDigest": first_one["schemaDigest"],
    })
    checks.add("schema.dataset_tables_present", dataset_tables.issubset(set(tables)), {
        "missing": sorted(dataset_tables - set(tables)),
    })
    checks.add("schema.foreign_keys_clean", fk_check == [] and counts and all(v == 0 for v in counts.values()), {
        "foreignKeyCheck": fk_check,
        "emptyCounts": counts,
    })
    checks.add("schema.dataset_version_immutable_trigger", immutable_trigger, {
        "trigger": "dataset_version_no_update",
    })
    def sanitized(result: dict) -> dict:
        cleaned = dict(result)
        cleaned["database"] = "temporary SQLite database"
        return cleaned

    return {
        "check": "T08 schema and migrations",
        "generatedAt": now(),
        "database": "temporary SQLite database",
        "firstApply": sanitized(first_one),
        "secondApply": sanitized(second_one),
        "secondDatabaseApply": sanitized(first_two),
        "digestStable": stable,
        "tables": tables,
        "triggers": triggers,
        "foreignKeyCheck": fk_check,
        "emptyDatasetTableCounts": counts,
        "checks": checks.as_dict(),
    }


def bootstrap(tmp: Path) -> tuple[str, dict, http_api.ApiServer, threading.Thread]:
    db_path = str(tmp / "platform.db")
    migrations.migrate(db_path)
    conn = db.connect(db_path)
    try:
        alpha = service.create_project(conn, "alpha-t08")
        beta = service.create_project(conn, "beta-t08")
        alpha_token = service.add_member(conn, alpha["id"], "alice", "member")["token"]
        alpha_ro_token = service.add_member(conn, alpha["id"], "alice-ro", "readonly")["token"]
        beta_token = service.add_member(conn, beta["id"], "bob", "member")["token"]
        identity = {
            "db_path": db_path,
            "alpha_id": alpha["id"],
            "beta_id": beta["id"],
            "alpha_token": alpha_token,
            "alpha_ro_token": alpha_ro_token,
            "beta_token": beta_token,
        }
    finally:
        conn.close()
    server = http_api.make_server(db_path, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return f"http://127.0.0.1:{server.server_port}", identity, server, thread


def create_dataset(base: str, token: str, project_id: str, name: str) -> tuple[int, dict]:
    return http_call(
        base,
        "POST",
        f"/projects/{project_id}/datasets",
        token,
        {"name": name},
    )


def import_content(
    base: str, token: str, project_id: str, dataset_id: str, content: str, fmt: str = "jsonl", mode: str = "abort"
) -> tuple[int, dict]:
    return http_call(
        base,
        "POST",
        f"/projects/{project_id}/datasets/{dataset_id}/import",
        token,
        {"format": fmt, "mode": mode, "content": content},
    )


def import_evidence(
    base: str, identity: dict, checks: Checks
) -> dict:
    alpha_id = identity["alpha_id"]
    token = identity["alpha_token"]
    status, created = create_dataset(base, token, alpha_id, "import-eval")
    checks.add("import.dataset_created", status == 201, {"status": status})
    dataset_id = created["data"]["id"]

    rows = [
        {
            "sample_id": f"s{i:03d}",
            "input": {"question": f"q{i}", "locale": "zh"},
            "expected": f"a{i}",
            "context": {"doc": f"doc-{i}", "lang": "zh"},
            "context_ref": f"kb://faq/{i}",
            "priority": "P0" if i % 2 == 0 else "P1",
            "tags": ["smoke"] if i % 5 == 0 else ["regression"],
        }
        for i in range(1, 101)
    ]
    content = jsonl(rows)
    success_status, imported = import_content(base, token, alpha_id, dataset_id, content)
    success = success_status == 201 and imported["data"]["imported"] == 100
    checks.add("import.jsonl_100_samples_one_request", success, {
        "status": success_status,
        "imported": imported.get("data", {}).get("imported"),
        "version": imported.get("data", {}).get("dataset_version", {}).get("version"),
        "sampleCount": imported.get("data", {}).get("dataset_version", {}).get("sample_count"),
        "digest": imported.get("data", {}).get("dataset_version", {}).get("content_digest"),
    })
    version = imported["data"]["dataset_version"]

    status, version_body = http_call(
        base, "GET", f"/projects/{alpha_id}/datasets/{dataset_id}/versions/1", token
    )
    samples = version_body.get("data", {}).get("samples", []) if status == 200 else []
    checks.add("import.version_snapshot_100", len(samples) == 100, {
        "status": status,
        "sampleCount": len(samples),
        "first": samples[0] if samples else None,
        "last": samples[-1] if samples else None,
    })

    invalid_lines = [
        json.dumps({"sample_id": "bad-json", "input": "ok"}),
        '{"sample_id": "broken", "input": ',
        json.dumps({"sample_id": "bad-priority", "input": "x", "priority": "P9"}),
        json.dumps({"sample_id": "s001", "input": "duplicate"}),
        json.dumps({"sample_id": "bad-null", "input": None}),
    ]
    invalid_status, invalid = import_content(base, token, alpha_id, dataset_id, "\n".join(invalid_lines))
    details = invalid.get("error", {}).get("details", {})
    by_line: dict[int, list[dict]] = {}
    for error in details.get("errors", []):
        by_line.setdefault(error.get("line"), []).append(error)
    line_checks = {
        "line2_invalid_json": any("invalid JSON" in e["reason"] for e in by_line.get(2, [])),
        "line3_bad_priority": any(e.get("field") == "priority" for e in by_line.get(3, [])),
        "line4_duplicate_sample_id": any("already exists" in e["reason"] for e in by_line.get(4, [])),
        "line5_null_input": any(e.get("field") == "input" for e in by_line.get(5, [])),
    }
    checks.add("import.invalid_lines_have_number_and_reason", invalid_status == 422 and all(line_checks.values()), {
        "status": invalid_status,
        "errors": details.get("errors"),
        "lineChecks": line_checks,
    })

    status, after = http_call(base, "GET", f"/projects/{alpha_id}/datasets/{dataset_id}", token)
    checks.add("import.abort_is_atomic", status == 200 and after["data"]["sample_count"] == 100 and len(after["data"]["versions"]) == 1, {
        "sampleCount": after.get("data", {}).get("sample_count"),
        "versionCount": len(after.get("data", {}).get("versions", [])),
    })

    skip_content = jsonl(
        [
            {"sample_id": "ok1", "input": 1},
            {"sample_id": "ok2", "input": 2, "priority": "P0"},
            {"sample_id": "ok3", "input": 3, "tags": ["smoke"]},
        ]
    ) + "{malformed\n"
    status, skipped = import_content(base, token, alpha_id, dataset_id, skip_content, mode="skip")
    checks.add("import.skip_mode_imports_valid_lines", status == 201 and skipped["data"]["imported"] == 3 and skipped["data"]["errors"][0]["line"] == 4, {
        "status": status,
        "summary": {key: skipped["data"].get(key) for key in ("imported", "skipped", "errorCount")},
        "errors": skipped["data"].get("errors"),
        "version": skipped["data"]["dataset_version"]["version"],
        "sampleCount": skipped["data"]["dataset_version"]["sample_count"],
    })
    return {
        "check": "C-T08-1 / T08-a1",
        "generatedAt": now(),
        "dataset": {"id": dataset_id, "name": "import-eval"},
        "request": {
            "format": "jsonl",
            "mode": "abort",
            "rows": 100,
            "contentBytes": len(content.encode("utf-8")),
        },
        "success": {
            "status": success_status,
            "imported": imported["data"]["imported"],
            "dataset_version": version,
            "firstSample": samples[0] if samples else None,
            "lastSample": samples[-1] if samples else None,
        },
        "invalidImport": {
            "status": invalid_status,
            "requestContent": invalid_lines,
            "response": invalid,
        },
        "atomicity": {
            "sampleCountAfterAbort": after["data"]["sample_count"],
            "versionCountAfterAbort": len(after["data"]["versions"]),
        },
        "skipImport": skipped["data"],
        "checks": checks.as_dict(),
    }


def versioning_evidence(base: str, identity: dict, checks: Checks) -> dict:
    alpha_id = identity["alpha_id"]
    token = identity["alpha_token"]
    status, created = create_dataset(base, token, alpha_id, "versioned-eval")
    dataset_id = created["data"]["id"]
    initial = jsonl(
        [
            {
                "sample_id": "s1",
                "input": "old input",
                "expected": "expected",
                "priority": "P0",
                "tags": ["smoke"],
                "context_ref": "kb://old/1",
            },
            {"sample_id": "s2", "input": {"q": 2}, "priority": "P1", "tags": ["reg"]},
        ]
    )
    status, imported = import_content(base, token, alpha_id, dataset_id, initial)
    dsv1 = imported["data"]["dataset_version"]

    status, spec = http_call(
        base, "POST", f"/projects/{alpha_id}/specs", token, {"channel": "B", "name": "versioned-spec"}
    )
    spec_id = spec["data"]["id"]
    status, sv1 = http_call(
        base,
        "POST",
        f"/projects/{alpha_id}/specs/{spec_id}/versions",
        token,
        {
            "dataset_version": dsv1["id"],
            "scorer_version": "scorer-v1",
            "model_snapshot": {"model": "qwen2.5-7b", "temperature": 0},
        },
    )
    status, run1 = http_call(
        base,
        "POST",
        f"/projects/{alpha_id}/runs",
        token,
        {"spec_version_id": sv1["data"]["id"], "commit_sha": "a" * 40, "trigger": "ci"},
    )
    run1_id = run1["data"]["id"]
    old_before = run1["data"]["dataset_samples"]

    status, samples = http_call(
        base, "GET", f"/projects/{alpha_id}/datasets/{dataset_id}/samples", token
    )
    s1 = [sample for sample in samples["data"] if sample["sample_id"] == "s1"][0]
    status, updated = http_call(
        base,
        "PATCH",
        f"/projects/{alpha_id}/datasets/{dataset_id}/samples/{s1['id']}",
        token,
        {"input": "new input", "context_ref": "kb://updated/1"},
    )
    dsv2 = updated["data"]["dataset_version"]

    status, old_run = http_call(base, "GET", f"/projects/{alpha_id}/runs/{run1_id}", token)
    status, old_snapshot = http_call(
        base, "GET", f"/projects/{alpha_id}/runs/{run1_id}/dataset-samples", token
    )
    status, current = http_call(base, "GET", f"/projects/{alpha_id}/datasets/{dataset_id}", token)
    current_s1 = [sample for sample in current["data"]["samples"] if sample["sample_id"] == "s1"][0]

    status, sv2 = http_call(
        base,
        "POST",
        f"/projects/{alpha_id}/specs/{spec_id}/versions",
        token,
        {
            "dataset_version": dsv2["id"],
            "scorer_version": "scorer-v1",
            "model_snapshot": {"model": "qwen2.5-7b", "temperature": 0},
        },
    )
    status, run2 = http_call(
        base,
        "POST",
        f"/projects/{alpha_id}/runs",
        token,
        {"spec_version_id": sv2["data"]["id"], "commit_sha": "b" * 40},
    )
    status, old_again = http_call(base, "GET", f"/projects/{alpha_id}/runs/{run1_id}", token)

    checks.add("versioning.sample_update_creates_new_version", dsv2["version"] == 2 and dsv2["sample_count"] == 2, {
        "from": dsv1["version"],
        "to": dsv2["version"],
        "digestBefore": dsv1["content_digest"],
        "digestAfter": dsv2["content_digest"],
    })
    checks.add(
        "versioning.old_run_keeps_old_samples",
        old_run["data"]["dataset_samples"][0]["input"] == "old input"
        and old_run["data"]["dataset_samples"][0]["context_ref"] == "kb://old/1"
        and old_run["data"]["dataset_version_detail"]["version"] == 1,
        {
            "run": run1_id,
            "boundSpecVersion": old_run["data"]["spec_version"]["version"],
            "boundDatasetVersion": old_run["data"]["spec_version"]["dataset_version"],
            "oldSample": old_run["data"]["dataset_samples"][0],
            "currentSample": current_s1,
        },
    )
    checks.add(
        "versioning.old_run_snapshot_endpoint",
        old_snapshot["data"]["dataset_version"]["version"] == 1
        and old_snapshot["data"]["samples"][0]["input"] == "old input",
        {"endpoint": "GET /runs/{rid}/dataset-samples", "version": old_snapshot["data"]["dataset_version"]["version"]},
    )
    checks.add(
        "versioning.new_run_uses_new_version",
        run2["data"]["dataset_samples"][0]["input"] == "new input"
        and run2["data"]["dataset_version_detail"]["version"] == 2,
        {
            "run": run2["data"]["id"],
            "datasetVersion": run2["data"]["dataset_version_detail"]["version"],
            "sample": run2["data"]["dataset_samples"][0],
        },
    )
    checks.add(
        "versioning.history_stable_after_second_run",
        old_again["data"]["dataset_samples"][0]["input"] == "old input",
        {"run": run1_id, "sample": old_again["data"]["dataset_samples"][0]},
    )
    return {
        "check": "C-T08-2 / T08-a2",
        "generatedAt": now(),
        "dataset": {"id": dataset_id, "name": "versioned-eval"},
        "spec": {"id": spec_id, "name": "versioned-spec"},
        "datasetVersions": {
            "v1": dsv1,
            "v2": dsv2,
        },
        "runs": {
            "old": {
                "id": run1_id,
                "specVersion": sv1["data"]["version"],
                "boundDatasetVersionId": sv1["data"]["dataset_version"],
                "datasetSamplesBeforeEdit": old_before,
                "datasetSamplesAfterEdit": old_run["data"]["dataset_samples"],
                "snapshotEndpoint": old_snapshot["data"],
            },
            "new": {
                "id": run2["data"]["id"],
                "specVersion": sv2["data"]["version"],
                "boundDatasetVersionId": sv2["data"]["dataset_version"],
                "datasetVersionDetail": run2["data"]["dataset_version_detail"],
                "datasetSamples": run2["data"]["dataset_samples"],
            },
        },
        "currentWorkingSet": {"s1": current_s1, "all": current["data"]["samples"]},
        "checks": checks.as_dict(),
    }


def roundtrip_evidence(base: str, identity: dict, checks: Checks) -> dict:
    alpha_id = identity["alpha_id"]
    token = identity["alpha_token"]
    status, created = create_dataset(base, token, alpha_id, "roundtrip-eval")
    dataset_id = created["data"]["id"]
    rows = [
        {"sample_id": "s1", "input": {"q": 1}, "expected": "a1", "priority": "P0", "tags": ["smoke", "core"], "context_ref": "kb://1"},
        {"sample_id": "s2", "input": {"q": 2}, "expected": "a2", "priority": "P0", "tags": ["smoke"]},
        {"sample_id": "s3", "input": {"q": 3}, "expected": "a3", "priority": "P1", "tags": ["smoke"]},
        {"sample_id": "s4", "input": {"q": 4}, "expected": "a4", "priority": "P0", "tags": ["reg"]},
        {"sample_id": "s5", "input": "plain", "expected": None, "priority": "P1", "tags": ["reg"]},
        {"sample_id": "s6", "input": [1, 2], "expected": {"ok": True}, "priority": "P0", "tags": ["core"]},
        {"sample_id": "s7", "input": {"q": 7}, "expected": "a7", "priority": "P1", "tags": ["smoke", "core"]},
        {"sample_id": "s8", "input": {"q": 8}, "expected": "a8", "priority": "P0", "tags": ["smoke"]},
    ]
    status, imported = import_content(base, token, alpha_id, dataset_id, jsonl(rows))
    original = imported["data"]["dataset_version"]

    export_path = (
        f"/projects/{alpha_id}/datasets/{dataset_id}/versions/1/export"
        "?tag=smoke&priority=P0&format=jsonl"
    )
    subset_status, subset_content, subset_headers = http_call(
        base, "GET", export_path, token, raw=True
    )
    subset_rows = [json.loads(line) for line in subset_content.splitlines() if line.strip()]
    subset_digest = subset_headers.get("x-sample-digest", "")

    status, subset_created = create_dataset(base, token, alpha_id, "subset-eval")
    subset_id = subset_created["data"]["id"]
    status, subset_import = import_content(base, token, alpha_id, subset_id, subset_content)
    subset_version = subset_import["data"]["dataset_version"]
    status, reexport, reexport_headers = http_call(
        base, "GET", f"/projects/{alpha_id}/datasets/{subset_id}/export?format=jsonl", token, raw=True
    )

    status, csv_content, csv_headers = http_call(
        base,
        "GET",
        f"/projects/{alpha_id}/datasets/{dataset_id}/versions/1/export?format=csv",
        token,
        raw=True,
    )
    status, csv_created = create_dataset(base, token, alpha_id, "csv-roundtrip-eval")
    csv_id = csv_created["data"]["id"]
    status, csv_import = import_content(base, token, alpha_id, csv_id, csv_content, fmt="csv")
    csv_version = csv_import["data"]["dataset_version"]

    checks.add(
        "roundtrip.subset_filter_count",
        len(subset_rows) == 3 and subset_headers.get("x-sample-count") == "3"
        and {row["sample_id"] for row in subset_rows} == {"s1", "s2", "s8"},
        {
            "filter": {"tag": "smoke", "priority": "P0"},
            "headerCount": subset_headers.get("x-sample-count"),
            "sampleIds": [row["sample_id"] for row in subset_rows],
            "digest": subset_digest,
        },
    )
    checks.add(
        "roundtrip.subset_reimport_digest_matches",
        subset_version["sample_count"] == 3 and subset_version["content_digest"] == subset_digest,
        {
            "exportedDigest": subset_digest,
            "reimportedDigest": subset_version["content_digest"],
            "sampleCount": subset_version["sample_count"],
        },
    )
    checks.add(
        "roundtrip.subset_reexport_identical",
        reexport == subset_content and reexport_headers.get("x-sample-digest") == subset_digest,
        {
            "byteIdentical": reexport == subset_content,
            "bytes": len(reexport.encode("utf-8")),
            "digest": reexport_headers.get("x-sample-digest"),
        },
    )
    checks.add(
        "roundtrip.csv_export_reimport_digest_matches",
        csv_version["sample_count"] == 8 and csv_version["content_digest"] == original["content_digest"],
        {
            "originalDigest": original["content_digest"],
            "csvReimportDigest": csv_version["content_digest"],
            "originalSamples": original["sample_count"],
            "csvSamples": csv_version["sample_count"],
            "csvHeader": csv_content.splitlines()[0] if csv_content else None,
        },
    )
    return {
        "check": "C-T08-3 / T08-a3",
        "generatedAt": now(),
        "sourceDataset": {"id": dataset_id, "name": "roundtrip-eval", "version": original},
        "filter": {"tags": ["smoke"], "priorities": ["P0"], "allTags": False},
        "export": {
            "path": export_path,
            "status": subset_status,
            "headers": {
                key: subset_headers.get(key)
                for key in ("x-sample-count", "x-sample-digest", "x-dataset-version-id", "content-type")
            },
            "content": subset_content,
            "rows": subset_rows,
        },
        "reimport": {
            "dataset": {"id": subset_id, "name": "subset-eval"},
            "result": subset_import["data"],
        },
        "reexport": {
            "content": reexport,
            "digest": reexport_headers.get("x-sample-digest"),
            "byteIdentical": reexport == subset_content,
        },
        "csvRoundtrip": {
            "exportContent": csv_content,
            "header": csv_content.splitlines()[0] if csv_content else None,
            "reimportDataset": {"id": csv_id, "name": "csv-roundtrip-eval"},
            "reimportResult": csv_import["data"],
        },
        "checks": checks.as_dict(),
    }


def permission_evidence(base: str, identity: dict, checks: Checks) -> dict:
    alpha_id = identity["alpha_id"]
    beta_id = identity["beta_id"]
    token = identity["alpha_token"]
    ro_token = identity["alpha_ro_token"]
    beta_token = identity["beta_token"]
    status, created = create_dataset(base, token, alpha_id, "permission-eval")
    dataset_id = created["data"]["id"]
    status, imported = import_content(
        base,
        token,
        alpha_id,
        dataset_id,
        jsonl(
            [
                {"sample_id": "a1", "input": "x", "priority": "P0", "tags": ["smoke", "core"]},
                {"sample_id": "a2", "input": "y", "priority": "P0", "tags": ["core"]},
                {"sample_id": "a3", "input": "z", "priority": "P1", "tags": ["reg"]},
            ]
        ),
    )
    status, sample_list = http_call(
        base, "GET", f"/projects/{alpha_id}/datasets/{dataset_id}/samples", token
    )
    sample = sample_list["data"][0]

    ro_create = http_call(base, "POST", f"/projects/{alpha_id}/datasets", ro_token, {"name": "ro-denied"})
    ro_import = import_content(base, ro_token, alpha_id, dataset_id, jsonl([{"input": "x"}]))
    ro_patch = http_call(
        base,
        "PATCH",
        f"/projects/{alpha_id}/datasets/{dataset_id}/samples/{sample['id']}",
        ro_token,
        {"priority": "P0"},
    )
    ro_delete = http_call(
        base,
        "DELETE",
        f"/projects/{alpha_id}/datasets/{dataset_id}/samples/{sample['id']}",
        ro_token,
    )
    beta_get = http_call(base, "GET", f"/projects/{alpha_id}/datasets/{dataset_id}", beta_token)
    beta_export = http_call(
        base, "GET", f"/projects/{alpha_id}/datasets/{dataset_id}/export", beta_token, raw=True
    )
    beta_import = import_content(base, beta_token, alpha_id, dataset_id, jsonl([{"input": "x"}]))
    beta_id_guess = http_call(base, "GET", f"/projects/{beta_id}/datasets/{dataset_id}", beta_token)

    member_add = http_call(
        base,
        "POST",
        f"/projects/{alpha_id}/datasets/{dataset_id}/samples",
        token,
        {"input": "member-added", "priority": "P1", "tags": ["reg"]},
    )
    member_patch = http_call(
        base,
        "PATCH",
        f"/projects/{alpha_id}/datasets/{dataset_id}/samples/{sample['id']}",
        token,
        {"expected": "expected", "context_ref": "kb://ctx/1", "priority": "P1"},
    )
    member_delete = http_call(
        base,
        "DELETE",
        f"/projects/{alpha_id}/datasets/{dataset_id}/samples/{member_add[1]['data']['sample']['id']}",
        token,
    )
    stats = http_call(base, "GET", f"/projects/{alpha_id}/datasets/{dataset_id}/stats", token)
    filtered = http_call(
        base,
        "GET",
        f"/projects/{alpha_id}/datasets/{dataset_id}/samples?tag=core&priority=P0",
        token,
    )
    conflict = http_call(base, "POST", f"/projects/{alpha_id}/datasets", token, {"name": "permission-eval"})
    invalid_priority = http_call(
        base,
        "POST",
        f"/projects/{alpha_id}/datasets/{dataset_id}/samples",
        token,
        {"input": "x", "priority": "P2"},
    )

    immutable_error = None
    conn = db.connect(identity["db_path"])
    try:
        try:
            conn.execute("UPDATE dataset_version SET sample_count = 999 WHERE id = ?", (imported["data"]["dataset_version"]["id"],))
            conn.execute("ROLLBACK")
        except sqlite3.IntegrityError as exc:
            immutable_error = str(exc)
    finally:
        conn.close()

    checks.add(
        "permissions.readonly_writes_403",
        all(response[0] == 403 for response in (ro_create, ro_import, ro_patch, ro_delete)),
        {
            "create": ro_create[0],
            "import": ro_import[0],
            "patch": ro_patch[0],
            "delete": ro_delete[0],
        },
    )
    checks.add(
        "permissions.cross_project_404",
        beta_get[0] == 404 and beta_export[0] == 404 and beta_import[0] == 404 and beta_id_guess[0] == 404,
        {"get": beta_get[0], "export": beta_export[0], "import": beta_import[0], "ownProjectWrongId": beta_id_guess[0]},
    )
    checks.add(
        "permissions.member_crud_and_versions",
        member_add[0] == 201
        and member_patch[0] == 200
        and member_patch[1]["data"]["changed"] is True
        and member_delete[0] == 200
        and member_delete[1]["data"]["dataset_version"]["version"] > member_add[1]["data"]["dataset_version"]["version"],
        {
            "addVersion": member_add[1]["data"]["dataset_version"]["version"],
            "patchVersion": member_patch[1]["data"]["dataset_version"]["version"],
            "deleteVersion": member_delete[1]["data"]["dataset_version"]["version"],
            "remainingSamples": member_delete[1]["data"]["dataset_version"]["sample_count"],
        },
    )
    checks.add(
        "permissions.grouping_and_filters",
        stats[0] == 200
        and stats[1]["data"]["by_priority"] == {"P0": 1, "P1": 2}
        and stats[1]["data"]["by_tag"].get("core") == 2
        and {row["sample_id"] for row in filtered[1]["data"]} == {"a2"},
        {
            "stats": stats[1]["data"],
            "filteredIds": [row["sample_id"] for row in filtered[1]["data"]],
        },
    )
    checks.add(
        "permissions.validation_errors",
        conflict[0] == 409 and invalid_priority[0] == 400 and bool(immutable_error),
        {
            "duplicateName": conflict[0],
            "invalidPriority": invalid_priority[0],
            "immutableError": immutable_error,
        },
    )
    return {
        "check": "permissions, isolation and grouping",
        "generatedAt": now(),
        "dataset": {"id": dataset_id, "name": "permission-eval"},
        "readonly": {
            "create": ro_create,
            "import": ro_import,
            "patch": ro_patch,
            "delete": ro_delete,
        },
        "crossProject": {
            "get": beta_get,
            "export": beta_export,
            "import": beta_import,
            "wrongProjectId": beta_id_guess,
        },
        "member": {
            "add": member_add,
            "patch": member_patch,
            "delete": member_delete,
        },
        "stats": stats[1],
        "filteredSamples": filtered[1],
        "validation": {
            "duplicateDatasetName": conflict,
            "invalidPriority": invalid_priority,
            "immutableDatasetVersionError": immutable_error,
        },
        "checks": checks.as_dict(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run T08 verification and write evidence")
    parser.add_argument("--out", default="../deliverables/evidence")
    args = parser.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    section_checks: dict[str, Checks] = {
        "schema": Checks(),
        "import": Checks(),
        "versioning": Checks(),
        "roundtrip": Checks(),
        "permissions": Checks(),
    }
    files: dict[str, dict] = {}
    tmp = tempfile.TemporaryDirectory(prefix="aitest-t08-verify-")
    server = None
    thread = None
    try:
        tmp_path = Path(tmp.name)
        schema = schema_evidence(tmp_path, section_checks["schema"])

        schema_db = tmp_path / "schema-dump.db"
        migrations.migrate(schema_db)
        conn = db.connect(schema_db)
        try:
            schema_sql = db.schema_sql(conn)
        finally:
            conn.close()
        schema_path = out / "t08-schema.sql"
        schema_path.write_text(schema_sql, encoding="utf-8")
        schema["schemaSql"] = {
            "path": schema_path.name,
            "bytes": schema_path.stat().st_size,
            "sha256": hashlib.sha256(schema_path.read_bytes()).hexdigest(),
        }

        base, identity, server, thread = bootstrap(tmp_path)
        imported = import_evidence(base, identity, section_checks["import"])
        versioning = versioning_evidence(base, identity, section_checks["versioning"])
        roundtrip = roundtrip_evidence(base, identity, section_checks["roundtrip"])
        permissions = permission_evidence(base, identity, section_checks["permissions"])

        sections = {
            "07-dataset-schema.json": schema,
            "08-dataset-import.json": imported,
            "09-sample-versioning.json": versioning,
            "10-subset-roundtrip.json": roundtrip,
            "11-dataset-permissions.json": permissions,
        }
        for name, payload in sections.items():
            path = write_json(out, name, payload)
            files[name] = {
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        files[schema_path.name] = schema["schemaSql"]
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=5)
        tmp.cleanup()

    all_checks = []
    for name, checks in section_checks.items():
        all_checks.extend(checks.items)
    all_passed = all(item["passed"] for item in all_checks)

    def passed_prefix(prefix: str) -> bool:
        selected = [item for item in all_checks if item["name"].startswith(prefix)]
        return bool(selected) and all(item["passed"] for item in selected)

    summary = {
        "schemaVersion": 1,
        "taskId": "T08",
        "title": "评测集管理与样本导入",
        "generatedAt": now(),
        "allChecksPassed": all_passed,
        "checkCount": len(all_checks),
        "failedChecks": [item for item in all_checks if not item["passed"]],
        "acceptance": {
            "T08-a1": passed_prefix("import."),
            "T08-a2": passed_prefix("versioning."),
            "T08-a3": passed_prefix("roundtrip."),
        },
        "checks": all_checks,
        "sections": {
            name: checks.as_dict() for name, checks in section_checks.items()
        },
        "evidenceFiles": files,
    }
    summary_path = out / "t08-summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
