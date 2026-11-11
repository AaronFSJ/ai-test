"""Acceptance tests for T10: multi-sample evaluation and consistency rules."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aitest import db, http_api, migrations, sampling, service  # noqa: E402


class StubJudge:
    """Deterministic OpenAI-compatible judge used for protocol/trace tests."""

    def __init__(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # pragma: no cover - silence
                return

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                user = body["messages"][-1]["content"]
                if "bad" in user:
                    content = json.dumps({"score": 0.1, "verdict": "fail", "reason": "bad"})
                else:
                    content = json.dumps({"score": 0.93, "verdict": "pass", "reason": "good"})
                response = {
                    "id": "stub-1",
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


class SamplingFixture:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="aitest-t10-")
        self.db_path = str(Path(self.tmp.name) / "platform.db")
        migrations.migrate(self.db_path)
        conn = db.connect(self.db_path)
        try:
            alpha = service.create_project(conn, "Alpha")
            beta = service.create_project(conn, "Beta")
            self.alpha_id = alpha["id"]
            self.beta_id = beta["id"]
            self.alpha_token = service.add_member(conn, self.alpha_id, "alice", "member")["token"]
            self.ro_token = service.add_member(conn, self.alpha_id, "viewer", "readonly")["token"]
            self.beta_token = service.add_member(conn, self.beta_id, "bob", "member")["token"]
        finally:
            conn.close()
        self.server = http_api.make_server(self.db_path, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.judge = StubJudge()

    def close(self) -> None:
        self.judge.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def connect(self):
        return db.connect(self.db_path)

    def api(self, method: str, path: str, token: str | None = None, payload=None):
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

    def dataset(self, name: str = "eval", rows: list[dict] | None = None) -> dict:
        status, body = self.api(
            "POST", f"/projects/{self.alpha_id}/datasets", self.alpha_token, {"name": name}
        )
        assert status == 201, body
        dataset = body["data"]
        rows = rows or [
            {"sample_id": "s1", "input": "q1", "expected": "yes", "priority": "P0"},
            {"sample_id": "s2", "input": "q2", "expected": "no", "priority": "P0"},
        ]
        content = "\n".join(json.dumps(row) for row in rows) + "\n"
        status, body = self.api(
            "POST",
            f"/projects/{self.alpha_id}/datasets/{dataset['id']}/import",
            self.alpha_token,
            {"format": "jsonl", "mode": "abort", "content": content},
        )
        assert status == 201, body
        detail = self.api(
            "GET", f"/projects/{self.alpha_id}/datasets/{dataset['id']}", self.alpha_token
        )[1]["data"]
        detail["_version"] = body["data"]["dataset_version"]
        return detail

    def scorer(self, name: str, scorer_type: str, config: dict, rubric: str | None = None) -> dict:
        payload = {"name": name, "type": scorer_type, "config": config}
        if rubric:
            payload["rubric"] = rubric
        status, body = self.api(
            "POST", f"/projects/{self.alpha_id}/scorers", self.alpha_token, payload
        )
        assert status == 201, body
        return body["data"]

    def profile(self, name: str, members: list[dict]) -> dict:
        status, body = self.api(
            "POST",
            f"/projects/{self.alpha_id}/scorer-profiles",
            self.alpha_token,
            {"name": name, "combinator": "and", "members": members},
        )
        assert status == 201, body
        return body["data"]

    def bind(self, dataset_id: str, profile_id: str) -> dict:
        status, body = self.api(
            "POST",
            f"/projects/{self.alpha_id}/datasets/{dataset_id}/scorer-bindings",
            self.alpha_token,
            {"profile_id": profile_id},
        )
        assert status == 201, body
        return body["data"]

    def run(self, dataset: dict, strategy: str, n: int = 5, commit: str = "a" * 40, force: bool = False) -> dict:
        conn = self.connect()
        try:
            actor = service.authenticate(conn, self.alpha_token)
            specs = service.list_specs(conn, actor, self.alpha_id)
            name = f"spec-{dataset['name']}"
            spec = next((item for item in specs if item["name"] == name), None)
            if spec is None:
                spec = service.create_spec(
                    conn, actor, self.alpha_id, {"channel": "B", "name": name}
                )
            version = service.add_spec_version(
                conn,
                actor,
                self.alpha_id,
                spec["id"],
                {
                    "dataset_version": dataset["_version"]["id"],
                    "scorer_version": "bindings",
                    "model_snapshot": {"model": "stub-under-test", "temperature": 0},
                    "params": {"suite": dataset["name"]},
                },
            )
            payload = {
                "spec_version_id": version["id"],
                "commit_sha": commit,
                "branch": "main",
                "sampling_config": {"n": n, "strategy": strategy},
            }
            if force:
                payload["force"] = True
            return service.create_run(conn, actor, self.alpha_id, payload)
        finally:
            conn.close()


def score(fx: SamplingFixture, run_id: str, actual_samples: dict, sampling_config=None):
    payload = {"actual_samples": actual_samples}
    if sampling_config:
        payload["sampling"] = sampling_config
    return fx.api(
        "POST", f"/projects/{fx.alpha_id}/runs/{run_id}/score", fx.alpha_token, payload
    )


class T10SamplingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = SamplingFixture()
        self.dataset = self.fx.dataset()
        self.scorer = self.fx.scorer("eq", "exact_match", {})
        self.profile = self.fx.profile("default", [{"scorer_id": self.scorer["id"]}])
        self.fx.bind(self.dataset["id"], self.profile["id"])
        self.actuals = {"s1": ["yes", "yes", "no", "yes", "no"], "s2": ["no"] * 5}

    def tearDown(self) -> None:
        self.fx.close()

    def test_n5_per_sample_results_aggregate_ui_and_run_traceability(self) -> None:
        run = self.fx.run(self.dataset, "all_pass")
        status, body = score(self.fx, run["id"], self.actuals)
        self.assertEqual(status, 200, body)
        summary = body["data"]["summary"]
        self.assertEqual(summary["itemCount"], 2)
        self.assertEqual(summary["states"], {"pass": 1, "fail": 1, "uncertain": 0, "error": 0})
        self.assertEqual(summary["sampleStates"]["pass"], 8)
        self.assertEqual(summary["sampleStates"]["fail"], 2)
        self.assertEqual(summary["sampling"]["config"]["n"], 5)
        self.assertEqual(summary["sampling"]["cost"]["multiplier"], 5)
        item = next(item for item in summary["items"] if item["case_ref"] == "s1")
        self.assertEqual(item["state"], "fail")
        self.assertEqual(item["sampleCount"], 5)
        self.assertEqual(item["aggregation"]["counts"]["pass"], 3)
        self.assertIn("at least one repetition failed", item["aggregation"]["reason"])

        status, body = self.fx.api(
            "GET", f"/projects/{self.fx.alpha_id}/runs/{run['id']}", self.fx.alpha_token
        )
        self.assertEqual(status, 200, body)
        detail = body["data"]
        self.assertEqual(detail["sampling"]["config"]["n"], 5)
        self.assertEqual(detail["sampling"]["strategy"], "all_pass")
        self.assertEqual(detail["sampling"]["effectiveConfig"]["n"], 5)
        self.assertEqual(detail["sampling"]["effectiveConfig"]["concurrency"], 4)
        self.assertEqual(detail["sampling"]["effectiveModelSnapshot"]["sampling"]["n"], 5)
        self.assertEqual(detail["sampling"]["modelSnapshot"]["model"], "stub-under-test")
        self.assertEqual(detail["sampling"]["cost"]["multiplier"], 5)
        self.assertIsNone(detail["sampling"]["cost"]["tokens_total"])
        s1 = next(item for item in detail["items"] if item["case_ref"] == "s1")
        self.assertEqual(len(s1["samples"]), 5)
        self.assertEqual(
            [sample["state"] for sample in s1["samples"]],
            ["pass", "pass", "fail", "pass", "fail"],
        )
        self.assertEqual(s1["samples"][0]["sampleIndex"], 1)
        self.assertEqual(len(s1["samples"][0]["actualDigest"]), 64)
        self.assertEqual(s1["aggregation"]["strategy"], "all_pass")

        status, body = self.fx.api(
            "GET", f"/projects/{self.fx.alpha_id}/runs/{run['id']}/sampling", self.fx.alpha_token
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["data"]["itemCount"], 2)
        self.assertEqual(len(body["data"]["items"][0]["samples"]), 5)

        status, page = self.fx.html(
            f"/ui/runs/{run['id']}?token={self.fx.alpha_token}", self.fx.alpha_token
        )
        self.assertEqual(status, 200)
        self.assertIn('data-sampling-n="5"', page)
        self.assertIn('data-sampling-strategy="all_pass"', page)
        self.assertIn('data-sample-count="5"', page)
        self.assertEqual(page.count('class="samples"'), 2)
        self.assertIn("at least one repetition failed", page)
        self.assertIn("stub-under-test", page)
        self.assertIn("Cost multiplier", page)

        status, page = self.fx.html(
            f"/ui/runs/{run['id']}?token={self.fx.beta_token}", self.fx.beta_token
        )
        self.assertEqual(status, 404)
        self.assertNotIn(run["id"], page)

    def test_strategy_switch_changes_aggregate_with_explanation(self) -> None:
        all_run = self.fx.run(self.dataset, "all_pass")
        status, body = score(self.fx, all_run["id"], self.actuals)
        self.assertEqual(status, 200, body)
        all_s1 = next(item for item in body["data"]["summary"]["items"] if item["case_ref"] == "s1")
        self.assertEqual(all_s1["state"], "fail")

        majority_run = self.fx.run(self.dataset, "majority")
        status, body = score(self.fx, majority_run["id"], self.actuals)
        self.assertEqual(status, 200, body)
        majority_s1 = next(
            item for item in body["data"]["summary"]["items"] if item["case_ref"] == "s1"
        )
        self.assertEqual(majority_s1["state"], "pass")
        self.assertEqual(majority_s1["aggregation"]["counts"]["pass"], 3)
        self.assertIn("met majority threshold", majority_s1["aggregation"]["reason"])

        expected_run = self.fx.run(self.dataset, "expected_variance")
        status, body = score(self.fx, expected_run["id"], self.actuals)
        self.assertEqual(status, 200, body)
        expected_s1 = next(
            item for item in body["data"]["summary"]["items"] if item["case_ref"] == "s1"
        )
        self.assertEqual(expected_s1["state"], "fail")
        self.assertAlmostEqual(expected_s1["aggregation"]["mean_score"], 0.6)
        self.assertIn("below expected_min", expected_s1["aggregation"]["reason"])

        # Same batch, only the consistency policy changes: 4 pass / 1 fail is
        # fail for all_pass, pass for majority, and unstable for expected_variance.
        preview_samples = [
            {"state": "pass", "confidence": 1, "score": 1},
            {"state": "pass", "confidence": 1, "score": 1},
            {"state": "pass", "confidence": 1, "score": 1},
            {"state": "pass", "confidence": 1, "score": 1},
            {"state": "fail", "confidence": 1, "score": 0},
        ]
        results = {}
        for strategy in ("all_pass", "majority", "expected_variance"):
            status, body = self.fx.api(
                "POST",
                f"/projects/{self.fx.alpha_id}/sampling/preview",
                self.fx.alpha_token,
                {"samples": preview_samples, "config": {"n": 5, "strategy": strategy}},
            )
            self.assertEqual(status, 200, body)
            results[strategy] = body["data"]
        self.assertEqual(results["all_pass"]["state"], "fail")
        self.assertEqual(results["majority"]["state"], "pass")
        self.assertEqual(results["expected_variance"]["state"], "uncertain")
        self.assertIn("variance", results["expected_variance"]["aggregation"]["reason"])
        self.assertGreater(results["expected_variance"]["aggregation"]["variance"], 0.01)

    def test_repeating_identical_config_is_deterministic(self) -> None:
        first = self.fx.run(self.dataset, "majority")
        status, body = score(self.fx, first["id"], self.actuals)
        self.assertEqual(status, 200, body)
        status, body = self.fx.api(
            "POST",
            f"/projects/{self.fx.alpha_id}/runs",
            self.fx.alpha_token,
            {
                "spec_version_id": first["spec_version_id"],
                "commit_sha": "a" * 40,
                "branch": "main",
                "sampling_config": {"n": 5, "strategy": "majority"},
                "force": True,
            },
        )
        self.assertEqual(status, 201, body)
        second = body["data"]
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(second["rerun_of_run_id"], first["id"])
        status, body = score(self.fx, second["id"], self.actuals)
        self.assertEqual(status, 200, body)

        def fingerprint(run_id: str) -> dict:
            status, body = self.fx.api(
                "GET", f"/projects/{self.fx.alpha_id}/runs/{run_id}/sampling", self.fx.alpha_token
            )
            self.assertEqual(status, 200, body)
            data = body["data"]
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

        self.assertEqual(fingerprint(first["id"]), fingerprint(second["id"]))

    def test_llm_judge_sampling_tokens_and_model_snapshot(self) -> None:
        dataset = self.fx.dataset(
            "judge", [{"sample_id": "j1", "input": "q", "expected": "good", "priority": "P0"}]
        )
        judge = self.fx.scorer(
            "judge",
            "llm_judge",
            {
                "endpoint": self.fx.judge.endpoint,
                "model": "stub-judge",
                "temperature": 0.2,
                "timeout_s": 3,
                "pass_threshold": 0.7,
            },
            rubric="Pass when the answer is good.",
        )
        profile = self.fx.profile("judge-profile", [{"scorer_id": judge["id"]}])
        self.fx.bind(dataset["id"], profile["id"])
        run = self.fx.run(dataset, "majority", n=3, commit="d" * 40)
        status, body = score(
            self.fx,
            run["id"],
            {"j1": ["good", "bad", "good"]},
            sampling_config={"temperature": 0.2},
        )
        self.assertEqual(status, 200, body)
        summary = body["data"]["summary"]
        self.assertEqual(summary["sampling"]["config"]["n"], 3)
        self.assertEqual(summary["sampling"]["cost"]["tokens_total"], 54)
        self.assertEqual(summary["sampling"]["cost"]["tokens_in"], 33)
        self.assertEqual(summary["sampling"]["cost"]["tokens_out"], 21)
        self.assertEqual(summary["sampling"]["cost"]["multiplier"], 3)
        status, body = self.fx.api(
            "GET", f"/projects/{self.fx.alpha_id}/runs/{run['id']}", self.fx.alpha_token
        )
        self.assertEqual(status, 200, body)
        models = body["data"]["sampling"]["effectiveModelSnapshot"]["scorer_models"]
        judge_model = next(model for model in models if model.get("model") == "stub-judge")
        self.assertEqual(judge_model["temperature"], 0.2)
        self.assertEqual(body["data"]["sampling"]["effectiveConfig"]["temperature"], 0.2)

    def test_validation_permissions_and_bounded_concurrency(self) -> None:
        run = self.fx.run(self.dataset, "all_pass")
        status, body = score(self.fx, run["id"], {"s1": ["yes"] * 4, "s2": ["no"] * 5})
        self.assertEqual(status, 400, body)
        self.assertIn("sampling.n is 5", body["error"]["message"])

        status, body = self.fx.api(
            "POST",
            f"/projects/{self.fx.alpha_id}/runs/{run['id']}/score",
            self.fx.ro_token,
            {"actual_samples": self.actuals},
        )
        self.assertEqual(status, 403, body)

        status, body = self.fx.api(
            "POST",
            f"/projects/{self.fx.beta_id}/runs/{run['id']}/score",
            self.fx.beta_token,
            {"actual_samples": self.actuals},
        )
        self.assertEqual(status, 404, body)

        for config in (
            {"n": 0, "strategy": "all_pass"},
            {"n": 51, "strategy": "all_pass"},
            {"n": 5, "strategy": "nonsense"},
        ):
            status, body = self.fx.api(
                "POST",
                f"/projects/{self.fx.alpha_id}/sampling/preview",
                self.fx.alpha_token,
                {"samples": [{"state": "pass", "confidence": 1, "score": 1}], "config": config},
            )
            self.assertEqual(status, 400, body)

        # A real bounded pool: no more than 3 workers can be inside the worker.
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

        results, stats = sampling.run_batch(range(9), worker, concurrency=3)
        self.assertEqual(results, [task * 2 for task in range(9)])
        self.assertEqual(stats["concurrency"], 3)
        self.assertLessEqual(stats["maxInflight"], 3)
        self.assertGreaterEqual(stats["maxInflight"], 2)
        self.assertEqual(active["max"], stats["maxInflight"])
