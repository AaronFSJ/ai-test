"""Acceptance tests for T16: cost aggregation and the quota gate."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aitest import costs, db, http_api, migrations, service  # noqa: E402

from test_t09 import ScorerFixture, create_profile, create_run, create_scorer  # noqa: E402


class NoUsageJudge:
    """OpenAI-compatible stub that omits the usage object entirely."""

    def __init__(self) -> None:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # pragma: no cover
                return

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                content = json.dumps({"score": 0.95, "verdict": "pass", "reason": "ok"})
                response = {
                    "id": "stub-no-usage",
                    "model": body.get("model", "stub-judge"),
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
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


class CostFixture(ScorerFixture):
    """ScorerFixture with a swappable model stub and cost configuration helpers."""

    def __init__(self, no_usage: bool = False) -> None:
        super().__init__()
        if no_usage:
            self.judge.close()
            self.judge = NoUsageJudge()

    def set_pricing(self, pricing: dict | None = None, run_quota: dict | None = None,
                    period_quota: dict | None = None):
        payload = {}
        if pricing is not None:
            payload["pricing"] = pricing
        if run_quota is not None:
            payload["run_quota"] = run_quota
        if period_quota is not None:
            payload["period_quota"] = period_quota
        status, body = self.api(
            "POST", f"/projects/{self.alpha_id}/costs/quota", self.alpha_token, payload
        )
        assert status == 200, body
        return body["data"]

    def judge_scorer(self, name: str = "judge", rubric: str = "Pass when the answer is correct.") -> dict:
        return create_scorer(
            self,
            name,
            "llm_judge",
            {
                "endpoint": self.judge.endpoint,
                "model": "stub-judge",
                "pass_threshold": 0.7,
                "timeout_s": 3,
            },
            rubric=rubric,
        )

    def bind_default(self, dataset: dict, profile: dict) -> dict:
        status, body = self.api(
            "POST",
            f"/projects/{self.alpha_id}/datasets/{dataset['id']}/scorer-bindings",
            self.alpha_token,
            {"profile_id": profile["profile_id"]},
        )
        assert status == 201, body
        return body["data"]

    def score(self, run: dict, outputs: dict):
        return self.api(
            "POST",
            f"/projects/{self.alpha_id}/runs/{run['id']}/score",
            self.alpha_token,
            {"actual_outputs": outputs},
        )

    def run_cost(self, run_id: str):
        return self.api(
            "GET", f"/projects/{self.alpha_id}/runs/{run_id}/cost", self.alpha_token
        )

    def summary(self, group_by: str = "project,branch,channel", **filters):
        query = "&".join(f"{key}={value}" for key, value in filters.items() if value)
        suffix = f"?group_by={group_by}" + (f"&{query}" if query else "")
        return self.api(
            "GET", f"/projects/{self.alpha_id}/costs/summary{suffix}", self.alpha_token
        )


class T16CostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = CostFixture()

    def tearDown(self) -> None:
        self.fx.close()

    def _judge_run(self, rows: list[dict] | None = None, commit: str = "1" * 40) -> tuple[dict, dict]:
        dataset = self.fx.dataset(
            "cost-eval",
            rows
            or [
                {"sample_id": "s1", "input": "q1", "expected": "yes", "priority": "P0"},
                {"sample_id": "s2", "input": "q2", "expected": "no", "priority": "P0"},
            ],
        )
        scorer = self.fx.judge_scorer()
        profile = create_profile(
            self.fx, "judge-profile", "and", [{"scorer_id": scorer["id"]}]
        )["versions"][0]
        self.fx.bind_default(dataset, profile)
        run = create_run(self.fx, dataset, "bindings", commit=commit)
        return dataset, run

    def test_model_call_detail_reconciles_with_summary(self) -> None:
        self.fx.set_pricing(
            {"tokens_in_per_1m": 1.0, "tokens_out_per_1m": 2.0, "currency": "USD"}
        )
        _, run = self._judge_run()
        status, body = self.fx.score(run, {"s1": "yes", "s2": "no"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["data"]["summary"]["itemCount"], 2)

        status, body = self.fx.run_cost(run["id"])
        self.assertEqual(status, 200, body)
        cost = body["data"]
        self.assertEqual(len(cost["entries"]), 2)
        self.assertEqual(cost["ledger"]["tokens_in"], 22)
        self.assertEqual(cost["ledger"]["tokens_out"], 14)
        self.assertEqual(cost["ledger"]["tokens_total"], 36)
        self.assertEqual(cost["ledger"]["cost_status"], "complete")
        self.assertAlmostEqual(cost["ledger"]["est_cost"], 0.00005, places=10)
        for entry in cost["entries"]:
            self.assertEqual(entry["tokens_in"], 11)
            self.assertEqual(entry["tokens_out"], 7)
            self.assertEqual(entry["model"], "stub-judge")
            self.assertEqual(entry["cost_status"], "complete")
            self.assertAlmostEqual(entry["est_cost"], 0.000025, places=10)

        status, body = self.fx.summary()
        self.assertEqual(status, 200, body)
        summary = body["data"]
        self.assertEqual(summary["totals"]["entryCount"], 2)
        self.assertEqual(summary["totals"]["tokens_in"], 22)
        self.assertEqual(summary["totals"]["tokens_out"], 14)
        self.assertEqual(summary["totals"]["tokens_total"], 36)
        self.assertAlmostEqual(summary["totals"]["est_cost"], 0.00005, places=10)
        self.assertTrue(summary["reconciliation"]["entriesMatchGroupTotal"])
        self.assertTrue(summary["reconciliation"]["tokensMatch"])
        group = summary["groups"][0]
        self.assertEqual(group["channel"], "B")
        self.assertEqual(group["branch"], "main")
        self.assertEqual(sorted(group["entryIds"]), sorted(e["id"] for e in cost["entries"]))
        self.assertTrue(group["detail_ref"].startswith("/projects/"))

        status, body = self.fx.api(
            "GET",
            f"/projects/{self.fx.alpha_id}/costs/entries?channel=B",
            self.fx.alpha_token,
        )
        self.assertEqual(status, 200, body)
        entries = body["data"]["entries"]
        self.assertEqual(len(entries), 2)
        self.assertEqual(sum(e["tokens_in"] for e in entries), cost["ledger"]["tokens_in"])
        self.assertEqual(sum(e["tokens_out"] for e in entries), cost["ledger"]["tokens_out"])

    def test_quota_terminates_run_with_reason_and_consumed_amount(self) -> None:
        self.fx.set_pricing(
            {"tokens_in_per_1m": 1.0, "tokens_out_per_1m": 2.0, "currency": "USD"},
            run_quota={"max_est_cost": 0.000001},
        )
        _, run = self._judge_run()
        status, body = self.fx.score(run, {"s1": "yes", "s2": "no"})
        self.assertEqual(status, 200, body)
        result = body["data"]
        self.assertTrue(result["summary"]["quotaTerminated"])
        self.assertEqual(result["summary"]["status"], "cancelled")
        self.assertEqual(result["summary"]["itemCount"], 1)
        self.assertEqual(result["run"]["status"], "cancelled")
        self.assertEqual(result["run"]["cancel"]["source"], "quota")
        self.assertTrue(result["run"]["cancel"]["reason"].startswith("quota_exceeded:run.max_est_cost"))
        quota = result["run"]["cost"]["quota"]
        self.assertTrue(quota["exceeded"])
        self.assertTrue(quota["terminated"])
        self.assertGreater(quota["run"]["consumed"]["est_cost"], 0.000001)
        self.assertEqual(quota["termination"]["scope"], "run")
        self.assertGreater(quota["termination"]["consumed"]["est_cost"], 0.000001)

        status, body = self.fx.api(
            "GET", f"/projects/{self.fx.alpha_id}/runs/{run['id']}", self.fx.alpha_token
        )
        self.assertEqual(status, 200, body)
        detail = body["data"]
        self.assertEqual(detail["status"], "cancelled")
        self.assertEqual(detail["cost"]["ledger"]["entry_count"], 1)
        self.assertTrue(detail["cost"]["quota"]["termination"]["reason"].startswith("quota_exceeded"))
        events = detail["events"]
        self.assertTrue(any(event["reason"] == "quota_exceeded" for event in events))

    def test_runner_gpu_seconds_are_recorded(self) -> None:
        self.fx.set_pricing({"gpu_second": 0.01, "currency": "USD"})
        conn = self.fx.connect()
        try:
            actor = service.authenticate(conn, self.fx.alpha_token)
            spec = service.create_spec(
                conn, actor, self.fx.alpha_id, {"channel": "A", "name": "a-gpu"}
            )
            version = service.add_spec_version(
                conn,
                actor,
                self.fx.alpha_id,
                spec["id"],
                {
                    "repo_path": "github.com/acme/web",
                    "repo_commit": "a" * 40,
                    "params": {"suite": "gpu"},
                },
            )
            runner = service.register_runner(
                conn, actor, self.fx.alpha_id, {"name": "gpu-runner", "capacity": 1}
            )
            run = service.create_run(
                conn,
                actor,
                self.fx.alpha_id,
                {"spec_version_id": version["id"], "commit_sha": "b" * 40, "branch": "main"},
            )
            claimed = service.claim_run(conn, runner["token"], {})
            self.assertEqual(claimed["data"]["id"], run["id"])
            completed = service.complete_run(
                conn,
                runner["token"],
                run["id"],
                {"status": "succeeded", "summary": "ok", "gpu_seconds": 12.5},
            )
        finally:
            conn.close()
        self.assertEqual(completed["status"], "succeeded")
        ledger = completed["cost"]["ledger"]
        self.assertEqual(ledger["gpu_seconds"], 12.5)
        self.assertAlmostEqual(ledger["est_cost"], 0.125, places=10)
        self.assertEqual(ledger["cost_status"], "complete")
        self.assertEqual(completed["cost"]["entries"][0]["entry_type"], "runner")
        self.assertEqual(
            completed["cost"]["entries"][0]["metadata"]["gpu_seconds_source"], "reported"
        )


class T16MissingCostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = CostFixture(no_usage=True)

    def tearDown(self) -> None:
        self.fx.close()

    def test_missing_usage_is_missing_not_zero(self) -> None:
        self.fx.set_pricing(
            {"tokens_in_per_1m": 1.0, "tokens_out_per_1m": 2.0, "currency": "USD"}
        )
        dataset = self.fx.dataset(
            "missing-usage",
            [{"sample_id": "s1", "input": "q1", "expected": "yes", "priority": "P0"}],
        )
        scorer = self.fx.judge_scorer("no-usage-judge")
        profile = create_profile(
            self.fx, "no-usage-profile", "and", [{"scorer_id": scorer["id"]}]
        )["versions"][0]
        self.fx.bind_default(dataset, profile)
        run = create_run(self.fx, dataset, "bindings", commit="9" * 40)
        status, body = self.fx.score(run, {"s1": "yes"})
        self.assertEqual(status, 200, body)

        status, body = self.fx.run_cost(run["id"])
        self.assertEqual(status, 200, body)
        cost = body["data"]
        self.assertEqual(len(cost["entries"]), 1)
        entry = cost["entries"][0]
        self.assertIsNone(entry["tokens_in"])
        self.assertIsNone(entry["tokens_out"])
        self.assertIsNone(entry["est_cost"])
        self.assertEqual(entry["cost_status"], "missing")
        self.assertEqual(entry["missing_reason"], "usage_not_reported")
        ledger = cost["ledger"]
        self.assertIsNone(ledger["est_cost"])
        self.assertIsNone(ledger["tokens_total"])
        self.assertEqual(ledger["cost_status"], "missing")
        self.assertEqual(ledger["missing_entries"], 1)
        self.assertIn("token_usage", cost["missing"]["components"])

        status, body = self.fx.summary()
        self.assertEqual(status, 200, body)
        summary = body["data"]
        self.assertEqual(summary["missing"]["entryCount"], 1)
        self.assertEqual(summary["missing"]["reasons"]["usage_not_reported"], 1)
        self.assertIsNone(summary["totals"]["est_cost"])
        self.assertEqual(summary["totals"]["cost_status"], "missing")


class T16AggregationAndPermissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = CostFixture()

    def tearDown(self) -> None:
        self.fx.close()

    def _manual(self, run_id: str, tokens_in: int, tokens_out: int, est_cost: float):
        return self.fx.api(
            "POST",
            f"/projects/{self.fx.alpha_id}/runs/{run_id}/costs",
            self.fx.alpha_token,
            {"tokens_in": tokens_in, "tokens_out": tokens_out, "est_cost": est_cost},
        )

    def test_summary_by_branch_and_channel_traces_to_entries(self) -> None:
        conn = self.fx.connect()
        try:
            actor = service.authenticate(conn, self.fx.alpha_token)
            b_spec = service.create_spec(
                conn, actor, self.fx.alpha_id, {"channel": "B", "name": "b-cost"}
            )
            b_version = service.add_spec_version(
                conn,
                actor,
                self.fx.alpha_id,
                b_spec["id"],
                {
                    "dataset_version": "manual-v1",
                    "scorer_version": "bindings",
                    "model_snapshot": {"model": "stub"},
                    "params": {"suite": "cost"},
                },
            )
            a_spec = service.create_spec(
                conn, actor, self.fx.alpha_id, {"channel": "A", "name": "a-cost"}
            )
            a_version = service.add_spec_version(
                conn,
                actor,
                self.fx.alpha_id,
                a_spec["id"],
                {"repo_path": "github.com/acme/web", "repo_commit": "c" * 40, "params": {}},
            )
            run_a = service.create_run(
                conn,
                actor,
                self.fx.alpha_id,
                {"spec_version_id": b_version["id"], "commit_sha": "1" * 40, "branch": "feature-a"},
            )
            run_b = service.create_run(
                conn,
                actor,
                self.fx.alpha_id,
                {"spec_version_id": b_version["id"], "commit_sha": "2" * 40, "branch": "feature-b"},
            )
            run_c = service.create_run(
                conn,
                actor,
                self.fx.alpha_id,
                {"spec_version_id": a_version["id"], "commit_sha": "3" * 40, "branch": "main"},
            )
        finally:
            conn.close()
        self.assertEqual(self._manual(run_a["id"], 100, 20, 0.5)[0], 201)
        self.assertEqual(self._manual(run_b["id"], 200, 40, 1.0)[0], 201)
        self.assertEqual(self._manual(run_c["id"], 50, 10, 0.25)[0], 201)

        status, body = self.fx.summary(group_by="branch,channel")
        self.assertEqual(status, 200, body)
        summary = body["data"]
        groups = {(group["branch"], group["channel"]): group for group in summary["groups"]}
        self.assertEqual(set(groups), {("feature-a", "B"), ("feature-b", "B"), ("main", "A")})
        self.assertEqual(groups[("feature-a", "B")]["tokens_in"], 100)
        self.assertAlmostEqual(groups[("feature-a", "B")]["est_cost"], 0.5)
        self.assertEqual(summary["totals"]["tokens_in"], 350)
        self.assertEqual(summary["totals"]["tokens_out"], 70)
        self.assertAlmostEqual(summary["totals"]["est_cost"], 1.75)
        for group in summary["groups"]:
            self.assertTrue(group["detail_ref"])
            self.assertTrue(group["entryIds"])

        status, body = self.fx.api(
            "GET",
            f"/projects/{self.fx.alpha_id}/costs/entries?branch=feature-a&channel=B",
            self.fx.alpha_token,
        )
        self.assertEqual(status, 200, body)
        entries = body["data"]["entries"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["id"], groups[("feature-a", "B")]["entryIds"][0])
        self.assertEqual(entries[0]["tokens_in"], 100)

    def test_period_quota_blocks_a_second_run(self) -> None:
        self.fx.set_pricing(
            {"tokens_in_per_1m": 1.0, "tokens_out_per_1m": 2.0, "currency": "USD"},
            period_quota={"window": "day", "max_est_cost": 0.00003},
        )
        dataset = self.fx.dataset(
            "period-quota",
            [{"sample_id": "s1", "input": "q1", "expected": "yes", "priority": "P0"}],
        )
        scorer = self.fx.judge_scorer("period-judge")
        profile = create_profile(
            self.fx, "period-profile", "and", [{"scorer_id": scorer["id"]}]
        )["versions"][0]
        self.fx.bind_default(dataset, profile)
        run1 = create_run(self.fx, dataset, "bindings", commit="4" * 40)
        status, body = self.fx.score(run1, {"s1": "yes"})
        self.assertEqual(status, 200, body)
        self.assertFalse(body["data"]["summary"]["quotaTerminated"])
        self.assertEqual(body["data"]["run"]["status"], "succeeded")
        run2 = create_run(self.fx, dataset, "bindings", commit="5" * 40)
        status, body = self.fx.score(run2, {"s1": "yes"})
        self.assertEqual(status, 200, body)
        result = body["data"]
        self.assertTrue(result["summary"]["quotaTerminated"])
        self.assertEqual(result["run"]["status"], "cancelled")
        self.assertTrue(result["run"]["cancel"]["reason"].startswith("quota_exceeded:period.max_est_cost"))
        period = result["run"]["cost"]["quota"]["period"]
        self.assertGreater(period["consumed"]["est_cost"], 0.00003)

    def test_permissions_and_isolation(self) -> None:
        status, body = self.fx.api(
            "POST",
            f"/projects/{self.fx.alpha_id}/costs/quota",
            self.fx.ro_token,
            {"pricing": {"tokens_in_per_1m": 1}},
        )
        self.assertEqual(status, 403, body)
        status, body = self.fx.api(
            "GET", f"/projects/{self.fx.beta_id}/costs/summary", self.fx.beta_token
        )
        self.assertEqual(status, 200, body)
        status, body = self.fx.api(
            "GET", f"/projects/{self.fx.alpha_id}/costs/summary", self.fx.beta_token
        )
        self.assertEqual(status, 404, body)
        status, body = self.fx.api(
            "GET", f"/projects/{self.fx.alpha_id}/costs/summary", self.fx.ro_token
        )
        self.assertEqual(status, 200, body)
