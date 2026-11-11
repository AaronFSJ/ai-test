#!/usr/bin/env python3
"""Reproducible T10 evidence: N=5 sampling, consistency strategies, Run
traceability, repeatability and bounded-batch cost visibility.

Starts a real local HTTP server. The LLM judge check uses a deterministic
OpenAI-compatible stub only to verify protocol, token accounting and model
snapshot resolution; it is not evidence of model quality.
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aitest import db, http_api, migrations, sampling, service  # noqa: E402


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def excerpt(value: Any, limit: int = 6000) -> Any:
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

    def get(self, name: str) -> bool:
        return any(item["name"] == name and item["passed"] for item in self.items)

    def as_dict(self) -> dict:
        return {"allPassed": all(item["passed"] for item in self.items), "checks": self.items}


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
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8")
                try:
                    return exc.code, json.loads(body)
                except json.JSONDecodeError:
                    return exc.code, {"raw": body}
            finally:
                exc.close()

    def ok(self, method: str, path: str, token: str | None = None, payload=None, status: int = 200) -> dict:
        code, body = self.call(method, path, token, payload)
        if code != status:
            raise RuntimeError(f"{method} {path} -> {code}: {body}")
        return body["data"]

    def html(self, path: str, token: str) -> tuple[int, str]:
        request = urllib.request.Request(self.base + path, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, exc.read().decode("utf-8")
            finally:
                exc.close()


class StubJudge:
    def __init__(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # pragma: no cover
                return

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                user = body.get("messages", [{}])[-1].get("content", "")
                if "bad" in user:
                    content = json.dumps({"score": 0.1, "verdict": "fail", "reason": "bad"})
                else:
                    content = json.dumps({"score": 0.93, "verdict": "pass", "reason": "good"})
                response = {
                    "id": "stub-t10",
                    "model": body.get("model", "stub-judge"),
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
                }
                raw = json.dumps(response).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class Harness:
    def __init__(self, tmp: Path) -> None:
        self.db_path = str(tmp / "t10.db")
        migrations.migrate(self.db_path)
        conn = db.connect(self.db_path)
        try:
            project = service.create_project(conn, "T10 Evidence")
            self.project_id = project["id"]
            self.token = service.add_member(conn, self.project_id, "alice", "member")["token"]
            self.ro_token = service.add_member(conn, self.project_id, "viewer", "readonly")["token"]
        finally:
            conn.close()
        self.server = http_api.make_server(self.db_path, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.client = Client(self.base)
        self.stub = StubJudge()
        self._seq = 0

    def close(self) -> None:
        self.stub.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def connect(self) -> sqlite3.Connection:
        return db.connect(self.db_path)

    def dataset(self, name: str, rows: list[dict]) -> dict:
        dataset = self.client.ok(
            "POST", f"/projects/{self.project_id}/datasets", self.token, {"name": name}, status=201
        )
        content = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n"
        imported = self.client.ok(
            "POST",
            f"/projects/{self.project_id}/datasets/{dataset['id']}/import",
            self.token,
            {"format": "jsonl", "mode": "abort", "content": content},
            status=201,
        )
        detail = self.client.ok(
            "GET", f"/projects/{self.project_id}/datasets/{dataset['id']}", self.token
        )
        detail["_version"] = imported["dataset_version"]
        return detail

    def scorer(self, name: str, scorer_type: str, config=None, rubric=None) -> dict:
        payload = {"name": name, "type": scorer_type, "config": config or {}}
        if rubric is not None:
            payload["rubric"] = rubric
        return self.client.ok(
            "POST", f"/projects/{self.project_id}/scorers", self.token, payload, status=201
        )

    def profile(self, name: str, members: list[dict]) -> dict:
        return self.client.ok(
            "POST",
            f"/projects/{self.project_id}/scorer-profiles",
            self.token,
            {"name": name, "combinator": "and", "members": members},
            status=201,
        )

    def bind(self, dataset_id: str, profile_id: str) -> dict:
        return self.client.ok(
            "POST",
            f"/projects/{self.project_id}/datasets/{dataset_id}/scorer-bindings",
            self.token,
            {"profile_id": profile_id},
            status=201,
        )

    def spec_run(
        self,
        label: str,
        dataset: dict,
        sampling_config: dict,
        commit_char: str,
        force: bool = False,
        model_snapshot: dict | None = None,
    ) -> dict:
        self._seq += 1
        spec = self.client.ok(
            "POST",
            f"/projects/{self.project_id}/specs",
            self.token,
            {"channel": "B", "name": f"spec-{label}-{self._seq}"},
            status=201,
        )
        version = self.client.ok(
            "POST",
            f"/projects/{self.project_id}/specs/{spec['id']}/versions",
            self.token,
            {
                "dataset_version": dataset["_version"]["id"],
                "scorer_version": "bindings",
                "model_snapshot": model_snapshot or {"model": "stub-under-test", "temperature": 0},
                "params": {"suite": dataset["name"]},
            },
            status=201,
        )
        payload = {
            "spec_version_id": version["id"],
            "commit_sha": commit_char * 40,
            "branch": "main",
            "sampling_config": sampling_config,
        }
        if force:
            payload["force"] = True
        return self.client.ok(
            "POST", f"/projects/{self.project_id}/runs", self.token, payload, status=201
        )


def collect_n5(harness: Harness, checks: Checks) -> tuple[dict, str]:
    dataset = harness.dataset(
        "n5",
        [
            {"sample_id": "s1", "input": "q1", "expected": "yes", "priority": "P0"},
            {"sample_id": "s2", "input": "q2", "expected": "no", "priority": "P0"},
        ],
    )
    scorer = harness.scorer("exact-n5", "exact_match", {})
    profile = harness.profile("profile-n5", [{"scorer_id": scorer["id"]}])
    harness.bind(dataset["id"], profile["id"])
    run = harness.spec_run("n5", dataset, {"n": 5, "strategy": "all_pass", "concurrency": 3}, "a")
    actuals = {"s1": ["yes", "yes", "no", "yes", "no"], "s2": ["no"] * 5}
    scored = harness.client.ok(
        "POST",
        f"/projects/{harness.project_id}/runs/{run['id']}/score",
        harness.token,
        {"actual_samples": actuals},
        status=200,
    )
    detail = harness.client.ok(
        "GET", f"/projects/{harness.project_id}/runs/{run['id']}", harness.token
    )
    sampling_view = harness.client.ok(
        "GET", f"/projects/{harness.project_id}/runs/{run['id']}/sampling", harness.token
    )
    status, page = harness.client.html(f"/ui/runs/{run['id']}?token={harness.token}", harness.token)

    summary = scored["summary"]
    s1 = next(item for item in summary["items"] if item["case_ref"] == "s1")
    s1_detail = next(item for item in detail["items"] if item["case_ref"] == "s1")
    sample_states = [sample["state"] for sample in s1_detail["samples"]]
    checks.add(
        "n5.per_item_sample_count",
        len(s1_detail["samples"]) == 5 and s1["sampleCount"] == 5,
        {"samples": len(s1_detail["samples"]), "summarySampleCount": s1["sampleCount"]},
    )
    checks.add(
        "n5.per_item_states",
        sample_states == ["pass", "pass", "fail", "pass", "fail"],
        {"states": sample_states},
    )
    checks.add(
        "n5.aggregate_all_pass",
        s1["state"] == "fail"
        and s1["aggregation"]["counts"] == {"pass": 3, "fail": 2, "uncertain": 0, "error": 0}
        and "at least one repetition failed" in s1["aggregation"]["reason"],
        {"state": s1["state"], "aggregation": s1["aggregation"]},
    )
    checks.add(
        "n5.s2_all_pass",
        next(item for item in summary["items"] if item["case_ref"] == "s2")["state"] == "pass",
        next(item for item in summary["items"] if item["case_ref"] == "s2"),
    )
    checks.add(
        "run.sampling_config_traceable",
        detail["sampling"]["config"]["n"] == 5
        and detail["sampling"]["strategy"] == "all_pass"
        and detail["sampling"]["effectiveConfig"]["n"] == 5
        and detail["sampling"]["effectiveConfig"]["concurrency"] in (3, 5),
        {"requested": detail["sampling"]["config"], "effective": detail["sampling"]["effectiveConfig"]},
    )
    checks.add(
        "run.model_snapshot_traceable",
        detail["sampling"]["modelSnapshot"]["model"] == "stub-under-test"
        and detail["sampling"]["effectiveModelSnapshot"]["sampling"]["n"] == 5
        and detail["sampling"]["effectiveModelSnapshot"]["sampling"]["strategy"] == "all_pass",
        {
            "run": detail["sampling"]["modelSnapshot"],
            "effective": detail["sampling"]["effectiveModelSnapshot"],
        },
    )
    checks.add(
        "run.cost_multiplier_visible",
        detail["sampling"]["cost"]["multiplier"] == 5
        and detail["sampling"]["cost"]["scorerCalls"] == 10
        and detail["sampling"]["cost"]["tokens_total"] is None,
        detail["sampling"]["cost"],
    )
    checks.add(
        "ui.per_sample_and_aggregate_visible",
        status == 200
        and 'data-sampling-n="5"' in page
        and 'data-sampling-strategy="all_pass"' in page
        and 'data-sample-count="5"' in page
        and page.count('class="samples"') == 2
        and "at least one repetition failed" in page
        and "stub-under-test" in page
        and "Cost multiplier" in page,
        {
            "status": status,
            "sampleTables": page.count('class="samples"'),
            "hasN5": 'data-sampling-n="5"' in page,
            "hasAggregate": "at least one repetition failed" in page,
            "hasModelSnapshot": "stub-under-test" in page,
            "bytes": len(page.encode("utf-8")),
        },
    )
    checks.add(
        "api.sampling_endpoint",
        sampling_view["itemCount"] == 2
        and len(sampling_view["items"][0]["samples"]) == 5
        and sampling_view["sampling"]["effectiveConfig"]["n"] == 5,
        {
            "itemCount": sampling_view["itemCount"],
            "firstItemSamples": len(sampling_view["items"][0]["samples"]),
        },
    )
    evidence = {
        "check": "T10-a1 / T10-a3",
        "runId": run["id"],
        "request": {"sampling_config": {"n": 5, "strategy": "all_pass", "concurrency": 3}, "actual_samples": actuals},
        "summary": summary,
        "runDetail": {
            "sampling": detail["sampling"],
            "items": detail["items"],
        },
        "samplingEndpoint": sampling_view,
        "ui": {
            "status": status,
            "contentType": "text/html",
            "bytes": len(page.encode("utf-8")),
            "file": "t10-ui-run.html",
        },
    }
    checks.add(
        "n5.sample_digest_evidence",
        all(len(sample["actualDigest"]) == 64 for sample in s1_detail["samples"]),
        [sample["actualDigest"] for sample in s1_detail["samples"]],
    )
    return evidence, page.replace(harness.token, "<redacted-token>")


def collect_strategy_switch(harness: Harness, checks: Checks) -> dict:
    dataset = harness.dataset(
        "strategy",
        [{"sample_id": "s1", "input": "q1", "expected": "yes", "priority": "P0"}],
    )
    scorer = harness.scorer("exact-strategy", "exact_match", {})
    profile = harness.profile("profile-strategy", [{"scorer_id": scorer["id"]}])
    harness.bind(dataset["id"], profile["id"])
    actuals = {"s1": ["yes", "yes", "no", "yes", "no"]}
    outcomes = {}
    for strategy, commit_char in (
        ("all_pass", "b"),
        ("majority", "c"),
        ("expected_variance", "d"),
    ):
        run = harness.spec_run(
            f"strategy-{strategy}", dataset, {"n": 5, "strategy": strategy}, commit_char
        )
        scored = harness.client.ok(
            "POST",
            f"/projects/{harness.project_id}/runs/{run['id']}/score",
            harness.token,
            {"actual_samples": actuals},
            status=200,
        )
        item = scored["summary"]["items"][0]
        outcomes[strategy] = {
            "runId": run["id"],
            "state": item["state"],
            "counts": item["aggregation"]["counts"],
            "meanScore": item["aggregation"]["mean_score"],
            "variance": item["aggregation"]["variance"],
            "reason": item["aggregation"]["reason"],
            "rule": item["aggregation"]["rule"],
        }
    checks.add(
        "strategy.all_pass_fails",
        outcomes["all_pass"]["state"] == "fail",
        outcomes["all_pass"],
    )
    checks.add(
        "strategy.majority_passes",
        outcomes["majority"]["state"] == "pass" and outcomes["majority"]["counts"]["pass"] == 3,
        outcomes["majority"],
    )
    checks.add(
        "strategy.expected_variance_fails_low_mean",
        outcomes["expected_variance"]["state"] == "fail"
        and outcomes["expected_variance"]["meanScore"] == 0.6,
        outcomes["expected_variance"],
    )
    checks.add(
        "strategy.difference_explained",
        len({outcomes[key]["reason"] for key in outcomes}) == 3
        and all(outcomes[key]["rule"] for key in outcomes),
        {key: outcomes[key]["reason"] for key in outcomes},
    )

    preview_samples = [
        {"state": "pass", "confidence": 1, "score": 1},
        {"state": "pass", "confidence": 1, "score": 1},
        {"state": "pass", "confidence": 1, "score": 1},
        {"state": "pass", "confidence": 1, "score": 1},
        {"state": "fail", "confidence": 1, "score": 0},
    ]
    preview = {}
    for strategy in ("all_pass", "majority", "expected_variance"):
        preview[strategy] = harness.client.ok(
            "POST",
            f"/projects/{harness.project_id}/sampling/preview",
            harness.token,
            {"samples": preview_samples, "config": {"n": 5, "strategy": strategy}},
        )
    checks.add(
        "strategy.same_batch_preview_changes",
        preview["all_pass"]["state"] == "fail"
        and preview["majority"]["state"] == "pass"
        and preview["expected_variance"]["state"] == "uncertain"
        and "variance" in preview["expected_variance"]["aggregation"]["reason"],
        {key: preview[key]["state"] for key in preview},
    )
    return {
        "check": "T10-a2",
        "sameBatchActuals": actuals,
        "runs": outcomes,
        "preview": {key: preview[key] for key in preview},
    }


def fingerprint(harness: Harness, run_id: str) -> dict:
    data = harness.client.ok(
        "GET", f"/projects/{harness.project_id}/runs/{run_id}/sampling", harness.token
    )
    return {
        "config": data["sampling"]["effectiveConfig"],
        "items": [
            {
                "case_ref": item["case_ref"],
                "state": item["state"],
                "sampleStates": [sample["state"] for sample in item["samples"]],
                "counts": item["aggregation"]["counts"],
                "mean": item["aggregation"]["mean_score"],
                "variance": item["aggregation"]["variance"],
            }
            for item in data["items"]
        ],
    }


def collect_repeatability(harness: Harness, checks: Checks) -> dict:
    dataset = harness.dataset(
        "repeat",
        [
            {"sample_id": "s1", "input": "q1", "expected": "yes", "priority": "P0"},
            {"sample_id": "s2", "input": "q2", "expected": "no", "priority": "P0"},
        ],
    )
    scorer = harness.scorer("exact-repeat", "exact_match", {})
    profile = harness.profile("profile-repeat", [{"scorer_id": scorer["id"]}])
    harness.bind(dataset["id"], profile["id"])
    actuals = {"s1": ["yes", "yes", "no", "yes", "no"], "s2": ["no"] * 5}
    first = harness.spec_run("repeat-1", dataset, {"n": 5, "strategy": "majority"}, "e")
    harness.client.ok(
        "POST",
        f"/projects/{harness.project_id}/runs/{first['id']}/score",
        harness.token,
        {"actual_samples": actuals},
    )
    second = harness.client.ok(
        "POST",
        f"/projects/{harness.project_id}/runs",
        harness.token,
        {
            "spec_version_id": first["spec_version_id"],
            "commit_sha": "e" * 40,
            "branch": "main",
            "sampling_config": {"n": 5, "strategy": "majority"},
            "force": True,
        },
        status=201,
    )
    harness.client.ok(
        "POST",
        f"/projects/{harness.project_id}/runs/{second['id']}/score",
        harness.token,
        {"actual_samples": actuals},
    )
    first_fp = fingerprint(harness, first["id"])
    second_fp = fingerprint(harness, second["id"])
    same = first_fp == second_fp
    differences = {
        "runIds": [first["id"], second["id"]],
        "idsDiffer": first["id"] != second["id"],
        "timestampsAndIdsAreTheOnlyExpectedDifference": True,
        "aggregateDifference": None if same else {"first": first_fp, "second": second_fp},
        "explanation": (
            "Both runs used the same dataset version, scorer versions, model snapshot, "
            "sampling configuration and actual outputs. Therefore item states, per-sample "
            "states, counts, mean and variance are identical; only run ids, timestamps and "
            "the rerun linkage differ. Any state change would indicate nondeterminism that "
            "must be explained by a nondeterministic model endpoint, not by aggregation."
        ),
    }
    checks.add(
        "repeat.identical_config_identical_result",
        same and first["id"] != second["id"],
        differences,
    )
    return {
        "check": "T10-a4 (PRD AC-1.2)",
        "actuals": actuals,
        "first": {"runId": first["id"], "fingerprint": first_fp},
        "second": {"runId": second["id"], "fingerprint": second_fp},
        "differences": differences,
    }


def collect_batch_cost(harness: Harness, checks: Checks) -> dict:
    lock = threading.Lock()
    active = {"now": 0, "max": 0}

    def worker(task: int) -> int:
        with lock:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        time.sleep(0.05)
        with lock:
            active["now"] -= 1
        return task * 2

    started = time.monotonic()
    results, stats = sampling.run_batch(range(9), worker, concurrency=3)
    elapsed = round(time.monotonic() - started, 3)
    checks.add(
        "batch.bounded_concurrency",
        results == [task * 2 for task in range(9)]
        and stats["concurrency"] == 3
        and 2 <= stats["maxInflight"] <= 3
        and active["max"] == stats["maxInflight"]
        and elapsed >= 0.15,
        {"stats": stats, "observedMax": active["max"], "elapsedSeconds": elapsed},
    )

    dataset = harness.dataset(
        "judge", [{"sample_id": "j1", "input": "q", "expected": "good", "priority": "P0"}]
    )
    judge = harness.scorer(
        "judge-t10",
        "llm_judge",
        {
            "endpoint": harness.stub.endpoint,
            "model": "stub-judge",
            "temperature": 0.2,
            "timeout_s": 3,
            "pass_threshold": 0.7,
        },
        rubric="Pass when the answer is good.",
    )
    profile = harness.profile("profile-judge", [{"scorer_id": judge["id"]}])
    harness.bind(dataset["id"], profile["id"])
    run = harness.spec_run(
        "judge",
        dataset,
        {"n": 3, "strategy": "majority", "temperature": 0.2},
        "f",
        model_snapshot={"model": "stub-under-test", "temperature": 0.2},
    )
    scored = harness.client.ok(
        "POST",
        f"/projects/{harness.project_id}/runs/{run['id']}/score",
        harness.token,
        {"actual_samples": {"j1": ["good", "bad", "good"]}},
        status=200,
    )
    detail = harness.client.ok(
        "GET", f"/projects/{harness.project_id}/runs/{run['id']}", harness.token
    )
    cost = scored["summary"]["sampling"]["cost"]
    models = detail["sampling"]["effectiveModelSnapshot"]["scorer_models"]
    judge_model = next((model for model in models if model.get("model") == "stub-judge"), None)
    checks.add(
        "cost.llm_tokens_and_multiplier",
        cost["multiplier"] == 3
        and cost["tokens_total"] == 54
        and cost["tokens_in"] == 33
        and cost["tokens_out"] == 21,
        cost,
    )
    checks.add(
        "model_snapshot.judge_model_and_temperature",
        judge_model is not None
        and judge_model.get("temperature") == 0.2
        and detail["sampling"]["effectiveConfig"]["temperature"] == 0.2,
        {"judgeModel": judge_model, "effectiveConfig": detail["sampling"]["effectiveConfig"]},
    )
    return {
        "check": "T10-a1 cost visibility / T10-a3 model snapshot",
        "batch": {"stats": stats, "observedMax": active["max"], "elapsedSeconds": elapsed},
        "judgeRun": {
            "runId": run["id"],
            "summary": scored["summary"],
            "sampling": detail["sampling"],
            "stubNote": "Deterministic protocol stub only; no model quality claim.",
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate T10 evidence")
    parser.add_argument("--out", default=str(ROOT.parent / "deliverables" / "evidence"))
    args = parser.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    checks = Checks()
    with tempfile.TemporaryDirectory(prefix="aitest-t10-evidence-") as tmp:
        harness = Harness(Path(tmp))
        try:
            n5, ui_page = collect_n5(harness, checks)
            strategy = collect_strategy_switch(harness, checks)
            repeat = collect_repeatability(harness, checks)
            batch = collect_batch_cost(harness, checks)
        finally:
            harness.close()

    payloads = {
        "16-sampling-n5.json": n5,
        "17-sampling-strategy-switch.json": strategy,
        "18-sampling-repeatability.json": repeat,
        "19-sampling-batch-cost.json": batch,
    }
    paths = {}
    for filename, payload in payloads.items():
        path = out / filename
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n"
        path.write_text(text, encoding="utf-8")
        paths[filename] = {"path": str(path), "bytes": len(text.encode("utf-8")), "sha256": sha256(text)}
    ui_path = out / "t10-ui-run.html"
    ui_path.write_text(ui_page, encoding="utf-8")
    paths[ui_path.name] = {
        "path": str(ui_path),
        "bytes": len(ui_page.encode("utf-8")),
        "sha256": sha256(ui_page),
        "note": "Token redacted before saving.",
    }

    acceptance = {
        "T10-a1": checks.get("n5.per_item_sample_count")
        and checks.get("n5.per_item_states")
        and checks.get("n5.aggregate_all_pass")
        and checks.get("n5.s2_all_pass")
        and checks.get("ui.per_sample_and_aggregate_visible")
        and checks.get("api.sampling_endpoint"),
        "T10-a2": checks.get("strategy.all_pass_fails")
        and checks.get("strategy.majority_passes")
        and checks.get("strategy.expected_variance_fails_low_mean")
        and checks.get("strategy.difference_explained")
        and checks.get("strategy.same_batch_preview_changes"),
        "T10-a3": checks.get("run.sampling_config_traceable")
        and checks.get("run.model_snapshot_traceable")
        and checks.get("model_snapshot.judge_model_and_temperature"),
        "T10-a4": checks.get("repeat.identical_config_identical_result"),
    }
    summary = {
        "generatedAt": now(),
        "task": "T10",
        "allChecksPassed": checks.as_dict()["allPassed"],
        "checkCount": len(checks.items),
        "checks": checks.items,
        "acceptance": acceptance,
        "evidence": paths,
        "environment": {
            "python": sys.version.split()[0],
            "database": "temporary SQLite database",
            "http": "real local ThreadingHTTPServer",
            "ui": "real HTML page captured from /ui/runs/{rid}",
            "llmJudge": (
                "deterministic OpenAI-compatible stub for protocol/token/model-snapshot tests; "
                "real model quality is not claimed."
            ),
        },
    }
    summary_text = json.dumps(summary, ensure_ascii=False, indent=2, default=str)
    (out / "t10-summary.json").write_text(summary_text + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "allChecksPassed": summary["allChecksPassed"],
                "checkCount": summary["checkCount"],
                "acceptance": acceptance,
                "out": str(out),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if summary["allChecksPassed"] else 2


if __name__ == "__main__":
    sys.exit(main())
