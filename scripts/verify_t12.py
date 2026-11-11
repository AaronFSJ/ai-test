#!/usr/bin/env python3
"""Generate reproducible T12 evidence: review queue, write-back, calibration."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aitest import db, http_api, migrations, scoring, service  # noqa: E402


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class Client:
    def __init__(self, base: str) -> None:
        self.base = base

    def call(self, method: str, path: str, token: str | None = None, payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read().decode("utf-8")
                content_type = response.headers.get("Content-Type", "")
                if "jsonl" not in content_type and content_type.startswith("application/json"):
                    return response.status, json.loads(raw)
                return response.status, raw
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            exc.close()
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, {"raw": raw}


def _checks() -> list[dict]:
    return []


def _add(checks: list[dict], name: str, ok: bool, detail) -> None:
    checks.append({"name": name, "passed": bool(ok), "detail": detail})
    if not ok:
        raise AssertionError(f"check failed: {name}: {detail}")


def _setup(client: Client) -> dict:
    conn = db.connect(os.environ["T12_DB"])
    try:
        alpha = service.create_project(conn, "Alpha")
        beta = service.create_project(conn, "Beta")
        alpha_token = service.add_member(conn, alpha["id"], "alice", "member")["token"]
        ro_token = service.add_member(conn, alpha["id"], "viewer", "readonly")["token"]
        beta_token = service.add_member(conn, beta["id"], "bob", "member")["token"]
    finally:
        conn.close()
    project_id = alpha["id"]
    status, body = client.call(
        "POST", f"/projects/{project_id}/datasets", alpha_token, {"name": "eval"}
    )
    assert status == 201, body
    dataset = body["data"]
    rows = [
        {"sample_id": "u1", "input": "q1", "expected": "yes", "priority": "P0"},
        {"sample_id": "u2", "input": "q2", "expected": "yes", "priority": "P0"},
        {"sample_id": "u3", "input": "q3", "expected": "yes", "priority": "P1"},
        {"sample_id": "e1", "input": "q4", "expected": "yes", "priority": "P1"},
    ]
    content = "\n".join(json.dumps(row) for row in rows) + "\n"
    status, body = client.call(
        "POST",
        f"/projects/{project_id}/datasets/{dataset['id']}/import",
        alpha_token,
        {"format": "jsonl", "mode": "abort", "content": content},
    )
    assert status == 201, body
    version = body["data"]["dataset_version"]
    status, body = client.call(
        "POST",
        f"/projects/{project_id}/scorers",
        alpha_token,
        {"name": "human", "type": "human_review", "config": {"queue": "manual-review"}},
    )
    assert status == 201, body
    human = body["data"]
    status, body = client.call(
        "POST",
        f"/projects/{project_id}/scorers",
        alpha_token,
        {"name": "exact", "type": "exact_match", "config": {"case_sensitive": False, "trim": True}},
    )
    assert status == 201, body
    exact = body["data"]
    status, body = client.call(
        "POST",
        f"/projects/{project_id}/specs",
        alpha_token,
        {"channel": "B", "name": "spec-eval"},
    )
    assert status == 201, body
    spec = body["data"]
    status, body = client.call(
        "POST",
        f"/projects/{project_id}/specs/{spec['id']}/versions",
        alpha_token,
        {
            "dataset_version": version["id"],
            "scorer_version": human["id"],
            "model_snapshot": {"model": "stub", "temperature": 0},
        },
    )
    assert status == 201, body
    spec_version = body["data"]
    status, body = client.call(
        "POST",
        f"/projects/{project_id}/runs",
        alpha_token,
        {"spec_version_id": spec_version["id"], "commit_sha": "a" * 40, "branch": "main"},
    )
    assert status == 201, body
    run = body["data"]
    status, body = client.call(
        "POST",
        f"/projects/{project_id}/runs/{run['id']}/score",
        alpha_token,
        {"actual_outputs": {"u1": "no", "u2": "yes", "u3": "yes"}},
    )
    assert status == 200, body
    return {
        "projectId": project_id,
        "betaId": beta["id"],
        "alphaToken": alpha_token,
        "roToken": ro_token,
        "betaToken": beta_token,
        "dataset": dataset,
        "version": version,
        "humanScorer": human,
        "exactScorer": exact,
        "spec": spec,
        "specVersion": spec_version,
        "run": run,
        "score": body["data"],
    }


def _queue_evidence(client: Client, fx: dict, checks: list[dict]) -> dict:
    project_id = fx["projectId"]
    conn = db.connect(os.environ["T12_DB"])
    try:
        item_id = conn.execute(
            "SELECT id FROM run_item WHERE run_id = ? AND case_ref = 'u3'",
            (fx["run"]["id"],),
        ).fetchone()["id"]
        conn.execute(
            """
            INSERT INTO attribution
              (id, run_item_id, label, sub_label, evidence_refs_json, model_used,
               confidence, created_at)
            VALUES (?, ?, 'data_issue', 'missing fixture', '[]', 'stub', 0.4, ?)
            """,
            (service.new_id("att"), item_id, service.now()),
        )
    finally:
        conn.close()
    status, body = client.call(
        "GET", f"/projects/{project_id}/reviews/queue", fx["alphaToken"]
    )
    assert status == 200, body
    queue = body["data"]
    uncertain = queue["groups"]["uncertain"]
    error = queue["groups"]["error"]
    _add(checks, "queue.uncertain_count", uncertain["count"] == 3, uncertain["count"])
    _add(checks, "queue.error_count", error["count"] == 1, error["count"])
    _add(
        checks,
        "queue.groups_separated",
        set(queue["groups"]) == {"uncertain", "error"}
        and len(uncertain["items"]) == 3
        and len(error["items"]) == 1,
        {"groups": list(queue["groups"]), "counts": queue["counts"]},
    )
    conn = db.connect(os.environ["T12_DB"])
    try:
        raw_uncertain = conn.execute(
            "SELECT COUNT(*) FROM run_item WHERE run_id = ? AND state = 'uncertain'",
            (fx["run"]["id"],),
        ).fetchone()[0]
    finally:
        conn.close()
    _add(
        checks,
        "queue.uncertain_all_enqueued",
        raw_uncertain == uncertain["count"] == 3,
        {"rawUncertain": raw_uncertain, "queued": uncertain["count"]},
    )
    ready = [item for item in uncertain["items"] if item["decisionReady"]]
    _add(
        checks,
        "queue.uncertain_decision_ready",
        len(ready) == 3,
        [
            {
                "caseRef": item["caseRef"],
                "missingEvidence": item["missingEvidence"],
                "hasSample": item["sample"] is not None,
                "hasActual": item["actual"] is not None,
                "scorerType": item["scorer"]["primaryType"],
            }
            for item in uncertain["items"]
        ],
    )
    complete = all(
        item["sample"]
        and item["sample"].get("input") is not None
        and item["sample"].get("expected") is not None
        and item["actual"] is not None
        and item["scorer"]["members"]
        for item in uncertain["items"]
    )
    _add(checks, "queue.evidence_without_raw_logs", complete, "sample+expected+actual+scorer detail")
    error_item = error["items"][0]
    _add(
        checks,
        "queue.error_has_error_detail",
        error_item["currentState"] == "error"
        and bool((error_item["scorer"].get("error") or {}).get("code")),
        error_item["scorer"].get("error"),
    )

    status, body = client.call(
        "GET",
        f"/projects/{project_id}/reviews/queue?scorer_type=human_review",
        fx["alphaToken"],
    )
    assert status == 200, body
    _add(
        checks,
        "queue.filter_scorer_type",
        body["data"]["groups"]["uncertain"]["count"] == 3
        and body["data"]["groups"]["error"]["count"] == 0,
        body["data"]["counts"],
    )
    status, body = client.call(
        "GET",
        f"/projects/{project_id}/reviews/queue?attribution_label=product_defect",
        fx["alphaToken"],
    )
    assert status == 200, body
    _add(checks, "queue.filter_attribution", body["data"]["counts"]["items"] == 0, body["data"]["counts"])
    status, body = client.call(
        "GET",
        f"/projects/{project_id}/reviews/queue?attribution_label=data_issue",
        fx["alphaToken"],
    )
    assert status == 200, body
    _add(
        checks,
        "queue.filter_attribution_match",
        body["data"]["counts"]["items"] == 1
        and body["data"]["groups"]["uncertain"]["items"][0]["caseRef"] == "u3",
        body["data"]["counts"],
    )
    status, body = client.call(
        "GET",
        f"/projects/{project_id}/reviews/queue?status=reviewed",
        fx["alphaToken"],
    )
    assert status == 200, body
    _add(checks, "queue.filter_status_reviewed", body["data"]["counts"]["items"] == 0, body["data"]["counts"])
    status, body = client.call(
        "GET", f"/projects/{project_id}/reviews/queue", fx["roToken"]
    )
    _add(checks, "queue.readonly_can_read", status == 200, status)
    status, body = client.call(
        "POST",
        f"/projects/{project_id}/runs/{fx['run']['id']}/items/{uncertain['items'][0]['itemId']}/review",
        fx["roToken"],
        {"conclusion": "confirmed_defect"},
    )
    _add(
        checks,
        "queue.readonly_cannot_review",
        status == 403 and body.get("error", {}).get("code") == "forbidden",
        {"status": status, "body": body},
    )
    status, body = client.call(
        "GET", f"/projects/{project_id}/reviews/queue", fx["betaToken"]
    )
    _add(checks, "queue.project_isolation", status == 404, {"status": status})
    return queue


def _record_reviews(client: Client, fx: dict, queue: dict, checks: list[dict]) -> dict:
    project_id = fx["projectId"]
    items = {
        item["caseRef"]: item
        for group in queue["groups"].values()
        for item in group["items"]
    }
    status, body = client.call(
        "GET", f"/projects/{project_id}/runs/{fx['run']['id']}/review-report", fx["alphaToken"]
    )
    assert status == 200, body
    before = body["data"]
    reviews = []
    expectations = {
        "u1": ("confirmed_defect", "fail"),
        "u2": ("false_positive", "pass"),
        "u3": ("sample_error", "excluded"),
        "e1": ("false_positive", "error"),
    }
    for case_ref, (conclusion, expected_state) in expectations.items():
        status, body = client.call(
            "POST",
            f"/projects/{project_id}/runs/{fx['run']['id']}/items/{items[case_ref]['itemId']}/review",
            fx["alphaToken"],
            {"conclusion": conclusion, "note": f"review evidence for {case_ref}"},
        )
        assert status == 201, body
        result = body["data"]
        reviews.append({"caseRef": case_ref, **result})
        _add(
            checks,
            f"writeback.{case_ref}.state",
            result["item"]["currentState"] == expected_state,
            result["item"],
        )
        _add(
            checks,
            f"writeback.{case_ref}.reviewer_time",
            result["review"]["reviewer"] == "alice" and bool(result["review"]["createdAt"]),
            {
                "reviewer": result["review"]["reviewer"],
                "createdAt": result["review"]["createdAt"],
            },
        )
        _add(
            checks,
            f"writeback.{case_ref}.evidence_snapshot",
            result["review"]["evidenceSnapshot"] is not None,
            {
                "rawState": result["review"]["evidenceSnapshot"].get("rawState"),
                "actual": result["review"]["evidenceSnapshot"].get("actual"),
            },
        )
    status, body = client.call(
        "GET", f"/projects/{project_id}/runs/{fx['run']['id']}/review-report", fx["alphaToken"]
    )
    assert status == 200, body
    after = body["data"]
    _add(
        checks,
        "report.raw_vs_effective",
        before["rawStates"] == {"pass": 0, "fail": 0, "uncertain": 3, "error": 1}
        and after["rawStates"] == before["rawStates"]
        and after["effectiveStates"] == {"pass": 1, "fail": 1, "uncertain": 0, "error": 1, "excluded": 1},
        {"before": before["rawStates"], "after": after["effectiveStates"]},
    )
    _add(
        checks,
        "report.denominator_excludes_error_and_sample_error",
        after["quality"]["denominator"] == 2
        and after["quality"]["excludedFromDenominator"] == {
            "error": 1,
            "sampleError": 1,
            "total": 2,
        }
        and after["quality"]["errorExcludedFromDenominator"] is True,
        after["quality"],
    )
    _add(
        checks,
        "report.review_counts",
        after["reviewsByConclusion"]
        == {"confirmed_defect": 1, "false_positive": 2, "sample_error": 1},
        after["reviewsByConclusion"],
    )
    status, body = client.call(
        "GET", f"/projects/{project_id}/runs/{fx['run']['id']}/items/{items['u1']['itemId']}", fx["alphaToken"]
    )
    assert status == 200, body
    item = body["data"]
    _add(
        checks,
        "report.item_state_sync",
        item["state"] == "fail" and item["scored_state"] == "uncertain",
        {"state": item["state"], "scored_state": item["scored_state"]},
    )
    status, body = client.call(
        "GET", f"/projects/{project_id}/runs/{fx['run']['id']}", fx["alphaToken"]
    )
    assert status == 200, body
    summary = body["data"]["review_summary"]
    _add(
        checks,
        "report.run_detail_sync",
        summary["quality"]["denominator"] == after["quality"]["denominator"]
        and summary["effectiveStates"] == after["effectiveStates"],
        summary,
    )
    status, body = client.call(
        "GET", f"/projects/{project_id}/reviews?run_id={fx['run']['id']}", fx["alphaToken"]
    )
    assert status == 200, body
    _add(checks, "report.audit_rows", len(body["data"]) == 4, len(body["data"]))
    _add(
        checks,
        "queue.error_still_separate_after_review",
        after["queue"]["error"] == {"open": 0, "reviewed": 1}
        and after["queue"]["uncertain"] == {"open": 0, "reviewed": 0},
        after["queue"],
    )
    return {"before": before, "after": after, "reviews": reviews, "audit": body["data"]}


def _calibration_evidence(
    client: Client, fx: dict, checks: list[dict], evidence_dir: Path
) -> dict:
    project_id = fx["projectId"]
    status, usable_text = client.call(
        "GET",
        f"/projects/{project_id}/calibration-samples/export?format=jsonl",
        fx["alphaToken"],
    )
    assert status == 200, usable_text
    usable_rows = [json.loads(line) for line in usable_text.splitlines() if line.strip()]
    (evidence_dir / "18-calibration-export.jsonl").write_text(usable_text, encoding="utf-8")
    _add(checks, "calibration.export_usable_count", len(usable_rows) == 2, len(usable_rows))
    _add(
        checks,
        "calibration.export_shape",
        all(
            row.get("sample", {}).get("sampleId")
            and row.get("sample", {}).get("input") is not None
            and row.get("actual") is not None
            and row.get("expectedState") in ("pass", "fail")
            and row.get("usableForCalibration") is True
            for row in usable_rows
        ),
        usable_rows,
    )
    _add(
        checks,
        "calibration.export_reviewer",
        all(row["review"]["reviewer"] == "alice" and row["review"]["reviewedAt"] for row in usable_rows),
        [row["review"] for row in usable_rows],
    )

    status, all_text = client.call(
        "GET",
        f"/projects/{project_id}/calibration-samples/export?format=jsonl&include_unusable=true",
        fx["alphaToken"],
    )
    assert status == 200, all_text
    all_rows = [json.loads(line) for line in all_text.splitlines() if line.strip()]
    (evidence_dir / "18-calibration-export-all.jsonl").write_text(all_text, encoding="utf-8")
    excluded = {
        row["sample"]["sampleId"]: row["exclusionReason"]
        for row in all_rows
        if not row["usableForCalibration"]
    }
    _add(
        checks,
        "calibration.unusable_reasons",
        len(all_rows) == 4 and excluded == {"u3": "sample_error", "e1": "execution_error"},
        {"count": len(all_rows), "excluded": excluded},
    )

    status, csv_text = client.call(
        "GET",
        f"/projects/{project_id}/calibration-samples/export?format=csv&include_unusable=true",
        fx["alphaToken"],
    )
    assert status == 200, csv_text
    _add(
        checks,
        "calibration.csv_export",
        csv_text.startswith("calibrationId,")
        and len([line for line in csv_text.splitlines() if line.strip()]) == 5,
        {"header": csv_text.splitlines()[0], "lineCount": len(csv_text.splitlines())},
    )

    conn = db.connect(os.environ["T12_DB"])
    try:
        actor = service.authenticate(conn, fx["alphaToken"])
        evaluations = []
        for row in usable_rows:
            preview = scoring.preview_scorer(
                conn,
                actor,
                project_id,
                {
                    "scorer_version_id": fx["exactScorer"]["versions"][0]["id"],
                    "sample": {
                        **row["sample"],
                        "sample_id": row["sample"]["sampleId"],
                    },
                    "actual": row["actual"],
                    "context": row["sample"].get("context"),
                },
            )["result"]
            evaluations.append(
                {
                    "sampleId": row["sample"]["sampleId"],
                    "expectedState": row["expectedState"],
                    "scorerState": preview["state"],
                    "correct": preview["state"] == row["expectedState"],
                }
            )
    finally:
        conn.close()
    correct = sum(1 for row in evaluations if row["correct"])
    _add(
        checks,
        "calibration.directly_consumable",
        correct == len(evaluations) == 2,
        {"evaluations": evaluations, "accuracy": correct / len(evaluations)},
    )

    status, second_text = client.call(
        "GET",
        f"/projects/{project_id}/calibration-samples/export?format=jsonl",
        fx["alphaToken"],
    )
    assert status == 200, second_text
    _add(checks, "calibration.export_deterministic", second_text == usable_text, "byte-identical export")

    return {
        "usableRows": usable_rows,
        "allRows": all_rows,
        "evaluations": evaluations,
        "accuracy": correct / len(evaluations),
        "usableSha256": hashlib.sha256(usable_text.encode("utf-8")).hexdigest(),
        "allSha256": hashlib.sha256(all_text.encode("utf-8")).hexdigest(),
        "csvSha256": hashlib.sha256(csv_text.encode("utf-8")).hexdigest(),
    }


def _cli_smoke(fx: dict, evidence_dir: Path) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    commands = [
        ["review-queue", "--status", "all", "--run", fx["run"]["id"]],
        ["review-report", "--run", fx["run"]["id"]],
        ["review-list", "--run", fx["run"]["id"]],
        ["calibration-list", "--run", fx["run"]["id"]],
        [
            "calibration-export",
            "--run",
            fx["run"]["id"],
            "--out",
            str(evidence_dir / "18-calibration-cli.jsonl"),
        ],
    ]
    lines = []
    results = []
    for command in commands:
        full = [
            sys.executable,
            "-m",
            "aitest.cli",
            *command,
            "--db",
            os.environ["T12_DB"],
            "--token",
            fx["alphaToken"],
            "--project",
            fx["projectId"],
        ]
        completed = subprocess.run(
            full, capture_output=True, text=True, env=env, cwd=str(ROOT)
        )
        lines.append(f"$ aitest {' '.join(command)}")
        lines.append(f"exit={completed.returncode}")
        lines.append(completed.stdout.strip())
        if completed.stderr.strip():
            lines.append("stderr:")
            lines.append(completed.stderr.strip())
        lines.append("")
        results.append({"command": command, "exitCode": completed.returncode})
    (evidence_dir / "t12-cli-smoke.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"commands": results, "allExitZero": all(row["exitCode"] == 0 for row in results)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT.parent / "deliverables" / "evidence"))
    args = parser.parse_args(argv)
    evidence_dir = Path(args.out)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    checks: list[dict] = []
    tmp = tempfile.TemporaryDirectory(prefix="aitest-t12-verify-")
    db_path = str(Path(tmp.name) / "platform.db")
    migrations.migrate(db_path)
    os.environ["T12_DB"] = db_path
    server = http_api.make_server(db_path, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = Client(f"http://127.0.0.1:{server.server_port}")
    try:
        fx = _setup(client)
        queue = _queue_evidence(client, fx, checks)
        _json(
            evidence_dir / "16-review-queue.json",
            {
                "task": "T12",
                "generatedAt": service.now(),
                "runId": fx["run"]["id"],
                "queue": queue,
                "checks": [item for item in checks if item["name"].startswith("queue.")],
            },
        )
        writeback = _record_reviews(client, fx, queue, checks)
        _json(
            evidence_dir / "17-review-writeback.json",
            {
                "task": "T12",
                "generatedAt": service.now(),
                "runId": fx["run"]["id"],
                "beforeReport": writeback["before"],
                "afterReport": writeback["after"],
                "reviews": writeback["reviews"],
                "audit": writeback["audit"],
                "checks": [item for item in checks if item["name"].startswith(("writeback.", "report."))],
            },
        )
        calibration = _calibration_evidence(client, fx, checks, evidence_dir)
        _json(
            evidence_dir / "18-calibration-summary.json",
            {
                "task": "T12",
                "generatedAt": service.now(),
                "runId": fx["run"]["id"],
                "usableRows": calibration["usableRows"],
                "allRows": calibration["allRows"],
                "evaluations": calibration["evaluations"],
                "accuracy": calibration["accuracy"],
                "sha256": {
                    "usableJsonl": calibration["usableSha256"],
                    "allJsonl": calibration["allSha256"],
                    "csv": calibration["csvSha256"],
                },
                "checks": [item for item in checks if item["name"].startswith("calibration.")],
            },
        )
        quality = {
            "task": "T12",
            "generatedAt": service.now(),
            "runId": fx["run"]["id"],
            "scoredStates": fx["score"]["summary"]["states"],
            "rawStatesBeforeReview": writeback["before"]["rawStates"],
            "effectiveStatesAfterReview": writeback["after"]["effectiveStates"],
            "quality": writeback["after"]["quality"],
            "queue": writeback["after"]["queue"],
            "errorCountedInDenominator": False,
        }
        _json(evidence_dir / "19-quality-statistics.json", quality)
        cli = _cli_smoke(fx, evidence_dir)
        _add(checks, "cli.all_commands_exit_zero", cli["allExitZero"], cli)
        _json(
            evidence_dir / "18-calibration-summary.json",
            {
                "task": "T12",
                "generatedAt": service.now(),
                "runId": fx["run"]["id"],
                "usableRows": calibration["usableRows"],
                "allRows": calibration["allRows"],
                "evaluations": calibration["evaluations"],
                "accuracy": calibration["accuracy"],
                "sha256": {
                    "usableJsonl": calibration["usableSha256"],
                    "allJsonl": calibration["allSha256"],
                    "csv": calibration["csvSha256"],
                },
                "checks": [item for item in checks if item["name"].startswith("calibration.")],
            },
        )
        evidence_files = [
            "16-review-queue.json",
            "17-review-writeback.json",
            "18-calibration-export.jsonl",
            "18-calibration-export-all.jsonl",
            "18-calibration-cli.jsonl",
            "18-calibration-summary.json",
            "19-quality-statistics.json",
            "t12-cli-smoke.txt",
        ]
        if (evidence_dir / "t12-test-results.txt").exists():
            evidence_files.append("t12-test-results.txt")
        evidence = []
        for name in evidence_files:
            path = evidence_dir / name
            evidence.append(
                {
                    "path": name,
                    "bytes": path.stat().st_size,
                    "sha256": _sha256(path),
                }
            )
        summary = {
            "task": "T12",
            "generatedAt": service.now(),
            "allChecksPassed": all(item["passed"] for item in checks),
            "checkCount": len(checks),
            "checks": checks,
            "evidence": evidence,
            "limitations": [
                "Evidence uses an in-process HTTP server and a temporary SQLite database, not production PostgreSQL or object storage.",
                "The calibration consumption check uses the exact_match scorer against human labels; it proves the export is scorer-ready, not that every scorer type is calibrated.",
                "No attribution labels were seeded in the B-channel run; attribution filtering is exercised as an empty result and is covered structurally.",
                "Review assignment/workflows, SLA and notification routing are outside T12.",
            ],
        }
        _json(evidence_dir / "t12-summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if summary["allChecksPassed"] else 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        tmp.cleanup()


if __name__ == "__main__":
    sys.exit(main())
