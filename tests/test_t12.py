"""Acceptance tests for T12: review queue and calibration samples."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aitest import db, http_api, migrations, scoring, service  # noqa: E402


class ReviewFixture:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="aitest-t12-")
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

    def close(self) -> None:
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
        request = urllib.request.Request(
            self.base + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
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


class T12Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = ReviewFixture()
        self.dataset = self.create_dataset()
        self.human = self.create_scorer("human", "human_review", {"queue": "manual-review"})
        self.exact = self.create_scorer(
            "exact",
            "exact_match",
            {"case_sensitive": False, "trim": True},
        )
        self.spec = self.create_spec(self.dataset["version"], self.human["id"], "spec-main")
        self.run = self.create_run(self.spec["version"]["id"], commit="a" * 40)
        self.score(self.run["id"], {"u1": "no", "u2": "yes", "u3": "yes"})
        status, body = self.fx.api(
            "GET",
            f"/projects/{self.fx.alpha_id}/reviews/queue",
            self.fx.alpha_token,
        )
        self.assertEqual(status, 200, body)
        self.queue = body["data"]
        self.items = {
            item["caseRef"]: item
            for group in self.queue["groups"].values()
            for item in group["items"]
        }

    def tearDown(self) -> None:
        self.fx.close()

    # -- helpers -------------------------------------------------------

    def create_dataset(self) -> dict:
        status, body = self.fx.api(
            "POST",
            f"/projects/{self.fx.alpha_id}/datasets",
            self.fx.alpha_token,
            {"name": "eval"},
        )
        self.assertEqual(status, 201, body)
        dataset = body["data"]
        rows = [
            {"sample_id": "u1", "input": "q1", "expected": "yes", "priority": "P0"},
            {"sample_id": "u2", "input": "q2", "expected": "yes", "priority": "P0"},
            {"sample_id": "u3", "input": "q3", "expected": "yes", "priority": "P1"},
            {"sample_id": "e1", "input": "q4", "expected": "yes", "priority": "P1"},
        ]
        content = "\n".join(json.dumps(row) for row in rows) + "\n"
        status, body = self.fx.api(
            "POST",
            f"/projects/{self.fx.alpha_id}/datasets/{dataset['id']}/import",
            self.fx.alpha_token,
            {"format": "jsonl", "mode": "abort", "content": content},
        )
        self.assertEqual(status, 201, body)
        version = body["data"]["dataset_version"]
        return {"id": dataset["id"], "version": version, "versionId": version["id"]}

    def create_scorer(self, name: str, scorer_type: str, config: dict) -> dict:
        status, body = self.fx.api(
            "POST",
            f"/projects/{self.fx.alpha_id}/scorers",
            self.fx.alpha_token,
            {"name": name, "type": scorer_type, "config": config},
        )
        self.assertEqual(status, 201, body)
        return body["data"]

    def create_spec(self, dataset_version: dict, scorer_ref: str, name: str) -> dict:
        status, body = self.fx.api(
            "POST",
            f"/projects/{self.fx.alpha_id}/specs",
            self.fx.alpha_token,
            {"channel": "B", "name": name},
        )
        self.assertEqual(status, 201, body)
        spec = body["data"]
        status, body = self.fx.api(
            "POST",
            f"/projects/{self.fx.alpha_id}/specs/{spec['id']}/versions",
            self.fx.alpha_token,
            {
                "dataset_version": dataset_version["id"],
                "scorer_version": scorer_ref,
                "model_snapshot": {"model": "stub", "temperature": 0},
            },
        )
        self.assertEqual(status, 201, body)
        return {"id": spec["id"], "version": body["data"]}

    def create_run(self, spec_version_id: str, commit: str) -> dict:
        status, body = self.fx.api(
            "POST",
            f"/projects/{self.fx.alpha_id}/runs",
            self.fx.alpha_token,
            {"spec_version_id": spec_version_id, "commit_sha": commit, "branch": "main"},
        )
        self.assertEqual(status, 201, body)
        return body["data"]

    def score(self, run_id: str, outputs: dict, scorer_version_id: str | None = None) -> dict:
        payload = {"actual_outputs": outputs}
        if scorer_version_id:
            payload["scorer_version_id"] = scorer_version_id
        status, body = self.fx.api(
            "POST",
            f"/projects/{self.fx.alpha_id}/runs/{run_id}/score",
            self.fx.alpha_token,
            payload,
        )
        self.assertEqual(status, 200, body)
        return body["data"]

    def review(self, item_id: str, conclusion: str, run_id: str | None = None, note=None):
        payload = {"conclusion": conclusion}
        if note is not None:
            payload["note"] = note
        return self.fx.api(
            "POST",
            f"/projects/{self.fx.alpha_id}/runs/{run_id or self.run['id']}/items/{item_id}/review",
            self.fx.alpha_token,
            payload,
        )

    # -- T12-a1 --------------------------------------------------------

    def test_all_uncertain_are_queued_with_decision_ready_evidence(self) -> None:
        self.assertEqual(self.queue["groups"]["uncertain"]["count"], 3)
        self.assertEqual(self.queue["groups"]["error"]["count"], 1)
        self.assertEqual(self.queue["counts"], {"items": 4, "open": 4, "reviewed": 0})
        self.assertEqual(
            sorted(item["caseRef"] for item in self.queue["groups"]["uncertain"]["items"]),
            ["u1", "u2", "u3"],
        )
        self.assertEqual(
            [item["caseRef"] for item in self.queue["groups"]["error"]["items"]], ["e1"]
        )
        for item in self.queue["groups"]["uncertain"]["items"]:
            self.assertTrue(item["decisionReady"], item["missingEvidence"])
            self.assertEqual(item["missingEvidence"], [])
            self.assertEqual(item["sample"]["sampleId"], item["caseRef"])
            self.assertIn(item["sample"]["input"], {"q1", "q2", "q3"})
            self.assertEqual(item["sample"]["expected"], "yes")
            self.assertIn(item["actual"], {"no", "yes"})
            self.assertEqual(item["scorer"]["primaryType"], "human_review")
            self.assertEqual(item["scorer"]["members"][0]["type"], "human_review")
            self.assertEqual(item["scorer"]["members"][0]["state"], "uncertain")
            self.assertIn("queue", item["scorer"]["members"][0]["detail"])
            self.assertEqual(item["rawState"], "uncertain")
            self.assertEqual(item["currentState"], "uncertain")
        error = self.queue["groups"]["error"]["items"][0]
        self.assertTrue(error["decisionReady"], error["missingEvidence"])
        self.assertEqual(error["currentState"], "error")
        self.assertTrue((error["scorer"].get("error") or {}).get("code"))

    def test_queue_filters_permissions_and_project_isolation(self) -> None:
        conn = self.fx.connect()
        try:
            conn.execute(
                """
                INSERT INTO attribution
                  (id, run_item_id, label, sub_label, evidence_refs_json, model_used,
                   confidence, created_at)
                VALUES (?, ?, 'data_issue', 'missing fixture', '[]', 'stub', 0.4, ?)
                """,
                (service.new_id("att"), self.items["u3"]["itemId"], service.now()),
            )
        finally:
            conn.close()
        path = f"/projects/{self.fx.alpha_id}/reviews/queue?scorer_type=human_review"
        status, body = self.fx.api("GET", path, self.fx.alpha_token)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["data"]["groups"]["uncertain"]["count"], 3)
        self.assertEqual(body["data"]["groups"]["error"]["count"], 0)

        status, body = self.fx.api(
            "GET",
            f"/projects/{self.fx.alpha_id}/reviews/queue?attribution_label=product_defect",
            self.fx.alpha_token,
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["data"]["counts"]["items"], 0)
        status, body = self.fx.api(
            "GET",
            f"/projects/{self.fx.alpha_id}/reviews/queue?attribution_label=data_issue",
            self.fx.alpha_token,
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["data"]["counts"]["items"], 1)
        self.assertEqual(
            body["data"]["groups"]["uncertain"]["items"][0]["caseRef"], "u3"
        )

        status, body = self.fx.api(
            "GET", f"/projects/{self.fx.alpha_id}/reviews/queue?status=reviewed", self.fx.alpha_token
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["data"]["counts"]["items"], 0)

        status, body = self.fx.api("GET", f"/projects/{self.fx.alpha_id}/reviews/queue", self.fx.ro_token)
        self.assertEqual(status, 200, body)
        item_id = self.items["u1"]["itemId"]
        status, body = self.fx.api(
            "POST",
            f"/projects/{self.fx.alpha_id}/runs/{self.run['id']}/items/{item_id}/review",
            self.fx.ro_token,
            {"conclusion": "confirmed_defect"},
        )
        self.assertEqual(status, 403, body)
        self.assertEqual(body["error"]["code"], "forbidden")

        status, body = self.fx.api(
            "GET", f"/projects/{self.fx.alpha_id}/reviews/queue", self.fx.beta_token
        )
        self.assertEqual(status, 404, body)

    # -- T12-a2 --------------------------------------------------------

    def test_review_writeback_updates_item_and_report_with_audit_fields(self) -> None:
        u1 = self.items["u1"]["itemId"]
        status, body = self.review(u1, "confirmed_defect", note="output contradicts expectation")
        self.assertEqual(status, 201, body)
        result = body["data"]
        self.assertEqual(result["item"]["rawState"], "uncertain")
        self.assertEqual(result["item"]["currentState"], "fail")
        self.assertEqual(result["review"]["reviewer"], "alice")
        self.assertEqual(result["review"]["conclusion"], "confirmed_defect")
        self.assertIsNotNone(result["review"]["createdAt"])
        self.assertEqual(
            result["review"]["calibrationSampleId"], result["calibration"]["calibrationId"]
        )
        self.assertEqual(result["calibration"]["expectedState"], "fail")
        self.assertTrue(result["calibration"]["usable"])

        status, body = self.fx.api(
            "GET",
            f"/projects/{self.fx.alpha_id}/runs/{self.run['id']}/items/{u1}",
            self.fx.alpha_token,
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["data"]["state"], "fail")
        self.assertEqual(body["data"]["scored_state"], "uncertain")

        status, body = self.fx.api(
            "GET",
            f"/projects/{self.fx.alpha_id}/runs/{self.run['id']}/review-report",
            self.fx.alpha_token,
        )
        self.assertEqual(status, 200, body)
        report = body["data"]
        self.assertEqual(report["rawStates"]["uncertain"], 3)
        self.assertEqual(report["effectiveStates"]["fail"], 1)
        self.assertEqual(report["effectiveStates"]["uncertain"], 2)
        self.assertEqual(report["quality"]["denominator"], 3)
        self.assertTrue(report["quality"]["errorExcludedFromDenominator"])

        self.review(self.items["u2"]["itemId"], "false_positive")
        self.review(self.items["u3"]["itemId"], "sample_error")
        status, body = self.review(self.items["e1"]["itemId"], "false_positive")
        self.assertEqual(status, 201, body)
        self.assertEqual(body["data"]["item"]["currentState"], "error")

        status, body = self.fx.api(
            "GET",
            f"/projects/{self.fx.alpha_id}/runs/{self.run['id']}/review-report",
            self.fx.alpha_token,
        )
        self.assertEqual(status, 200, body)
        report = body["data"]
        self.assertEqual(
            report["effectiveStates"],
            {"pass": 1, "fail": 1, "uncertain": 0, "error": 1, "excluded": 1},
        )
        self.assertEqual(report["quality"]["denominator"], 2)
        self.assertEqual(report["quality"]["excludedFromDenominator"]["error"], 1)
        self.assertEqual(report["quality"]["excludedFromDenominator"]["sampleError"], 1)
        self.assertEqual(
            report["reviewsByConclusion"],
            {"confirmed_defect": 1, "false_positive": 2, "sample_error": 1},
        )
        self.assertEqual(report["queue"]["uncertain"], {"open": 0, "reviewed": 0})
        self.assertEqual(report["queue"]["error"], {"open": 0, "reviewed": 1})

        status, body = self.fx.api(
            "GET", f"/projects/{self.fx.alpha_id}/runs/{self.run['id']}", self.fx.alpha_token
        )
        self.assertEqual(status, 200, body)
        summary = body["data"]["review_summary"]
        self.assertEqual(summary["quality"]["denominator"], 2)
        self.assertTrue(summary["quality"]["errorExcludedFromDenominator"])

        status, body = self.fx.api(
            "GET",
            f"/projects/{self.fx.alpha_id}/reviews?run_id={self.run['id']}",
            self.fx.alpha_token,
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body["data"]), 4)
        self.assertTrue(all(row["reviewer"] == "alice" for row in body["data"]))
        self.assertTrue(all(row["createdAt"] for row in body["data"]))
        snapshots = {row["caseRef"]: row["evidenceSnapshot"] for row in body["data"]}
        for case_ref in ("u1", "u2", "u3"):
            self.assertIsNotNone(snapshots[case_ref]["actual"])
        self.assertIsNotNone(snapshots["e1"]["scorer"]["error"])

    def test_review_validation_and_conflict(self) -> None:
        status, body = self.review(self.items["u1"]["itemId"], "not-a-conclusion")
        self.assertEqual(status, 400, body)
        status, body = self.review(self.items["u1"]["itemId"], "confirmed_defect")
        self.assertEqual(status, 201, body)
        status, body = self.review(self.items["u1"]["itemId"], "false_positive")
        self.assertEqual(status, 409, body)
        status, body = self.review("itm_missing", "confirmed_defect")
        self.assertEqual(status, 404, body)
        status, body = self.fx.api(
            "POST",
            f"/projects/{self.fx.alpha_id}/runs/{self.run['id']}/items/{self.items['u2']['itemId']}/review",
            self.fx.ro_token,
            {"conclusion": "false_positive"},
        )
        self.assertEqual(status, 403, body)

    # -- T12-a3 / T12-a4 ----------------------------------------------

    def _review_all(self) -> None:
        self.review(self.items["u1"]["itemId"], "confirmed_defect")
        self.review(self.items["u2"]["itemId"], "false_positive")
        self.review(self.items["u3"]["itemId"], "sample_error")
        self.review(self.items["e1"]["itemId"], "false_positive")

    def test_calibration_export_is_directly_usable_by_a_scorer(self) -> None:
        self._review_all()
        status, body = self.fx.api(
            "GET",
            f"/projects/{self.fx.alpha_id}/calibration-samples/export?format=jsonl",
            self.fx.alpha_token,
        )
        self.assertEqual(status, 200, body)
        lines = [json.loads(line) for line in body.splitlines() if line.strip()]
        self.assertEqual(len(lines), 2)
        self.assertEqual({row["sample"]["sampleId"] for row in lines}, {"u1", "u2"})
        self.assertTrue(all(row["usableForCalibration"] for row in lines))
        self.assertTrue(all(row["expectedState"] in ("pass", "fail") for row in lines))
        self.assertTrue(all(row["review"]["reviewer"] == "alice" for row in lines))
        self.assertTrue(all(row["actual"] is not None for row in lines))

        conn = self.fx.connect()
        try:
            actor = service.authenticate(conn, self.fx.alpha_token)
            by_id = {row["sample"]["sampleId"]: row for row in lines}
            for sample_id, expected_state in (("u1", "fail"), ("u2", "pass")):
                row = by_id[sample_id]
                preview = scoring.preview_scorer(
                    conn,
                    actor,
                    self.fx.alpha_id,
                    {
                        "scorer_version_id": self.exact["versions"][0]["id"],
                        "sample": {**row["sample"], "sample_id": sample_id},
                        "actual": row["actual"],
                        "context": row["sample"]["context"],
                    },
                )["result"]
                self.assertEqual(preview["state"], expected_state)
        finally:
            conn.close()

        status, all_body = self.fx.api(
            "GET",
            f"/projects/{self.fx.alpha_id}/calibration-samples/export?format=jsonl&include_unusable=true",
            self.fx.alpha_token,
        )
        self.assertEqual(status, 200, all_body)
        all_rows = [json.loads(line) for line in all_body.splitlines() if line.strip()]
        self.assertEqual(len(all_rows), 4)
        reasons = {
            row["sample"]["sampleId"]: row["exclusionReason"]
            for row in all_rows
            if not row["usableForCalibration"]
        }
        self.assertEqual(reasons, {"u3": "sample_error", "e1": "execution_error"})

        status, csv_body = self.fx.api(
            "GET",
            f"/projects/{self.fx.alpha_id}/calibration-samples/export?format=csv&include_unusable=true",
            self.fx.alpha_token,
        )
        self.assertEqual(status, 200, csv_body)
        self.assertTrue(csv_body.startswith("calibrationId,"))
        self.assertEqual(len([line for line in csv_body.splitlines() if line.strip()]), 5)

        status, body = self.fx.api(
            "GET",
            f"/projects/{self.fx.alpha_id}/calibration-samples",
            self.fx.alpha_token,
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["data"]["count"], 2)
        self.assertEqual(body["data"]["usableCount"], 2)

    def test_error_is_separate_and_never_in_quality_denominator(self) -> None:
        status, body = self.fx.api(
            "GET",
            f"/projects/{self.fx.alpha_id}/runs/{self.run['id']}/review-report",
            self.fx.alpha_token,
        )
        self.assertEqual(status, 200, body)
        report = body["data"]
        self.assertEqual(report["rawStates"]["error"], 1)
        self.assertEqual(report["quality"]["denominator"], 3)
        self.assertEqual(report["quality"]["excludedFromDenominator"]["error"], 1)
        self.assertTrue(report["quality"]["errorExcludedFromDenominator"])
        self.assertEqual(report["queue"]["error"], {"open": 1, "reviewed": 0})
        self.assertEqual(report["queue"]["uncertain"], {"open": 3, "reviewed": 0})

        self._review_all()
        status, body = self.fx.api(
            "GET",
            f"/projects/{self.fx.alpha_id}/runs/{self.run['id']}/review-report",
            self.fx.alpha_token,
        )
        self.assertEqual(status, 200, body)
        report = body["data"]
        self.assertEqual(report["quality"]["denominator"], 2)
        self.assertEqual(report["quality"]["excludedFromDenominator"]["error"], 1)
        self.assertEqual(report["quality"]["excludedFromDenominator"]["sampleError"], 1)

    def test_calibration_digest_is_stable_and_filterable(self) -> None:
        self._review_all()
        path = f"/projects/{self.fx.alpha_id}/calibration-samples/export?format=jsonl"
        status, first = self.fx.api("GET", path, self.fx.alpha_token)
        self.assertEqual(status, 200, first)
        status, second = self.fx.api("GET", path, self.fx.alpha_token)
        self.assertEqual(status, 200, second)
        self.assertEqual(first, second)
        status, filtered = self.fx.api(
            "GET", path + "&conclusion=confirmed_defect", self.fx.alpha_token
        )
        self.assertEqual(status, 200, filtered)
        self.assertEqual(len(filtered.strip().splitlines()), 1)
        self.assertNotEqual(first, filtered)


if __name__ == "__main__":
    unittest.main()
