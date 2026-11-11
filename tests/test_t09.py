"""Acceptance tests for T09: scorers, combinators, versioning and failures."""

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

from aitest import db, http_api, migrations, scoring, service  # noqa: E402


class StubJudgeServer:
    """Deterministic OpenAI-compatible endpoint used for protocol tests only."""

    def __init__(self) -> None:
        outer = self
        self.requests: list[dict] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # pragma: no cover - silence
                return

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                outer.requests.append(body)
                user = body["messages"][-1]["content"]
                if "INVALID_JSON" in user:
                    content = "not a json object"
                elif '"actual": "bad"' in user or "bad" in user:
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


class ScorerFixture:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="aitest-t09-")
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
        self.judge = StubJudgeServer()

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
            with urllib.request.urlopen(request, timeout=15) as response:
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

    def dataset(self, name: str = "eval", rows: list[dict] | None = None) -> dict:
        status, body = self.api(
            "POST", f"/projects/{self.alpha_id}/datasets", self.alpha_token, {"name": name}
        )
        assert status == 201, body
        dataset = body["data"]
        rows = rows or [
            {"sample_id": "s1", "input": "q1", "expected": "yes", "priority": "P0"},
            {"sample_id": "s2", "input": "q2", "expected": "no", "priority": "P1"},
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


def create_scorer(fx, name: str, scorer_type: str, config=None, rubric=None) -> dict:
    payload = {"name": name, "type": scorer_type, "config": config or {}}
    if rubric is not None:
        payload["rubric"] = rubric
    status, body = fx.api("POST", f"/projects/{fx.alpha_id}/scorers", fx.alpha_token, payload)
    assert status == 201, body
    return body["data"]


def create_profile(fx, name: str, combinator: str, members: list[dict], config=None) -> dict:
    status, body = fx.api(
        "POST",
        f"/projects/{fx.alpha_id}/scorer-profiles",
        fx.alpha_token,
        {"name": name, "combinator": combinator, "members": members, "config": config or {}},
    )
    assert status == 201, body
    return body["data"]


def create_run(fx, dataset: dict, scorer_ref: str, commit: str = "a" * 40) -> dict:
    conn = fx.connect()
    try:
        actor = service.authenticate(conn, fx.alpha_token)
        specs = service.list_specs(conn, actor, fx.alpha_id)
        spec = next((item for item in specs if item["name"] == f"spec-{dataset['name']}"), None)
        if spec is None:
            spec = service.create_spec(
                conn, actor, fx.alpha_id, {"channel": "B", "name": f"spec-{dataset['name']}"}
            )
        version = service.add_spec_version(
            conn,
            actor,
            fx.alpha_id,
            spec["id"],
            {
                "dataset_version": dataset["_version"]["id"],
                "scorer_version": scorer_ref,
                "model_snapshot": {"model": "stub", "temperature": 0},
                "params": {"suite": dataset["name"]},
            },
        )
        return service.create_run(
            conn,
            actor,
            fx.alpha_id,
            {"spec_version_id": version["id"], "commit_sha": commit, "branch": "main"},
        )
    finally:
        conn.close()


def preview(fx, scorer_type: str, config: dict, sample: dict, actual, rubric=None) -> dict:
    conn = fx.connect()
    try:
        actor = service.authenticate(conn, fx.alpha_token)
        payload = {"type": scorer_type, "config": config, "sample": sample, "actual": actual}
        if rubric is not None:
            payload["rubric"] = rubric
        return scoring.preview_scorer(conn, actor, fx.alpha_id, payload)["result"]
    finally:
        conn.close()


class T09BuiltinScorerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = ScorerFixture()

    def tearDown(self) -> None:
        self.fx.close()

    def test_seven_builtin_scorers_offline(self) -> None:
        exact_cfg = {"case_sensitive": False, "trim": True}
        self.assertEqual(
            preview(self.fx, "exact_match", exact_cfg, {"expected": " Yes "}, "yes")["state"], "pass"
        )
        self.assertEqual(
            preview(self.fx, "exact_match", exact_cfg, {"expected": "yes"}, "no")["state"], "fail"
        )

        regex_cfg = {"pattern": r"^ORD-\d{4}$", "mode": "fullmatch"}
        self.assertEqual(preview(self.fx, "regex", regex_cfg, {}, "ORD-1234")["state"], "pass")
        self.assertEqual(preview(self.fx, "regex", regex_cfg, {}, "ORD-12")["state"], "fail")

        schema = {
            "type": "object",
            "required": ["answer", "confidence"],
            "properties": {
                "answer": {"type": "string", "minLength": 1},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "additionalProperties": False,
        }
        schema_cfg = {"schema": schema, "parse_string": True}
        self.assertEqual(
            preview(self.fx, "json_schema", schema_cfg, {}, '{"answer":"ok","confidence":0.8}')["state"],
            "pass",
        )
        self.assertEqual(
            preview(self.fx, "json_schema", schema_cfg, {}, '{"answer":"","confidence":2}')["state"],
            "fail",
        )

        rule_cfg = {
            "assertions": [
                {"path": "$.answer", "op": "exists"},
                {"path": "$.score", "op": "gte", "value": 0.8},
                {"path": "$.source", "op": "in", "value": ["kb", "web"]},
            ]
        }
        self.assertEqual(
            preview(self.fx, "rule_assertion", rule_cfg, {}, {"answer": "x", "score": 0.9, "source": "kb"})["state"],
            "pass",
        )
        self.assertEqual(
            preview(self.fx, "rule_assertion", rule_cfg, {}, {"answer": "x", "score": 0.5, "source": "kb"})["state"],
            "fail",
        )
        self.assertEqual(
            preview(self.fx, "rule_assertion", rule_cfg, {}, {"answer": "x"})["state"], "uncertain"
        )

        semantic_cfg = {"threshold": 0.6, "metric": "lexical_cosine"}
        self.assertEqual(
            preview(self.fx, "semantic_similarity", semantic_cfg, {"expected": "退款政策是什么"}, "退款政策是什么？")["state"],
            "pass",
        )
        self.assertEqual(
            preview(self.fx, "semantic_similarity", semantic_cfg, {"expected": "退款政策是什么"}, "发票如何开具")["state"],
            "fail",
        )

        human_cfg = {"queue": "manual-review"}
        human = preview(self.fx, "human_review", human_cfg, {"sample_id": "s1"}, {})
        self.assertEqual(human["state"], "uncertain")
        self.assertTrue(human["detail"]["escalated"])
        decided = preview(
            self.fx,
            "human_review",
            human_cfg,
            {"sample_id": "s1"},
            {"human_review": {"conclusion": "false_positive", "reviewer": "qa"}},
        )
        self.assertEqual(decided["state"], "pass")

        judge_cfg = {
            "endpoint": self.fx.judge.endpoint,
            "model": "stub-judge",
            "pass_threshold": 0.7,
            "timeout_s": 3,
        }
        judge_pass = preview(
            self.fx, "llm_judge", judge_cfg, {"sample_id": "s1", "input": "q", "expected": "yes"}, "good", rubric="Pass when correct."
        )
        self.assertEqual(judge_pass["state"], "pass", judge_pass)
        self.assertEqual(judge_pass["detail"]["score"], 0.93)
        self.assertEqual(judge_pass["usage"]["total_tokens"], 18)
        judge_fail = preview(
            self.fx, "llm_judge", judge_cfg, {"sample_id": "s1", "input": "q", "expected": "yes"}, "bad", rubric="Pass when correct."
        )
        self.assertEqual(judge_fail["state"], "fail", judge_fail)

        invalid = preview(
            self.fx,
            "llm_judge",
            judge_cfg,
            {"sample_id": "s1", "input": "INVALID_JSON", "expected": "yes"},
            "bad",
            rubric="Pass when correct.",
        )
        self.assertEqual(invalid["state"], "error")
        self.assertEqual(invalid["error"]["code"], "endpoint_invalid_response")

    def test_model_endpoint_unavailable_is_error_not_exception(self) -> None:
        dead = {
            "endpoint": "http://127.0.0.1:9",
            "model": "missing",
            "timeout_s": 0.5,
            "pass_threshold": 0.7,
        }
        result = preview(
            self.fx, "llm_judge", dead, {"sample_id": "s1", "input": "q", "expected": "yes"}, "yes", rubric="rubric"
        )
        self.assertEqual(result["state"], "error")
        self.assertEqual(result["error"]["code"], "endpoint_unreachable")
        self.assertTrue(result["error"]["retryable"])
        self.assertEqual(result["confidence"], 0.0)


def score_with_profile(fx, profile_version_id: str, actual, sample=None) -> tuple[dict, dict]:
    conn = fx.connect()
    try:
        actor = service.authenticate(conn, fx.alpha_token)
        sample = sample or {"sample_id": "s1", "input": "q", "expected": "yes", "priority": "P0"}
        result, trace = scoring.score_sample(
            conn,
            fx.alpha_id,
            dataset_id=None,
            sample=sample,
            actual=actual,
            context=None,
            spec_scorer_ref=None,
            override={"profile_version_id": profile_version_id},
        )
        return result.to_dict(), trace
    finally:
        conn.close()


class T09CombinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = ScorerFixture()
        self.eq = create_scorer(self.fx, "eq", "exact_match", {"case_sensitive": False})
        self.re_yes = create_scorer(self.fx, "re-yes", "regex", {"pattern": "^yes$", "mode": "fullmatch"})
        self.re_no = create_scorer(self.fx, "re-no", "regex", {"pattern": "^no$", "mode": "fullmatch"})
        self.re_maybe = create_scorer(self.fx, "re-maybe", "regex", {"pattern": "^maybe$", "mode": "fullmatch"})
        self.human = create_scorer(self.fx, "human", "human_review", {})

    def tearDown(self) -> None:
        self.fx.close()

    def test_and_or_weighted_majority_semantics(self) -> None:
        and_profile = create_profile(
            self.fx,
            "and-profile",
            "and",
            [{"scorer_id": self.eq["id"]}, {"scorer_id": self.re_yes["id"]}],
        )["versions"][0]
        or_profile = create_profile(
            self.fx,
            "or-profile",
            "or",
            [{"scorer_id": self.eq["id"]}, {"scorer_id": self.re_yes["id"]}],
        )["versions"][0]
        weighted_profile = create_profile(
            self.fx,
            "weighted-profile",
            "weighted",
            [
                {"scorer_id": self.eq["id"], "weight": 3},
                {"scorer_id": self.re_no["id"], "weight": 1},
            ],
            {"threshold": 0.7},
        )["versions"][0]
        majority_profile = create_profile(
            self.fx,
            "majority-profile",
            "majority",
            [
                {"scorer_id": self.eq["id"]},
                {"scorer_id": self.re_yes["id"]},
                {"scorer_id": self.human["id"]},
            ],
        )["versions"][0]

        and_pass, and_trace = score_with_profile(self.fx, and_profile["id"], "yes")
        self.assertEqual(and_pass["state"], "pass")
        self.assertEqual(len(and_trace["members"]), 2)
        self.assertEqual(and_trace["aggregate"]["counts"]["pass"], 2)
        and_fail, _ = score_with_profile(self.fx, and_profile["id"], "no")
        self.assertEqual(and_fail["state"], "fail")

        or_pass, _ = score_with_profile(self.fx, or_profile["id"], "yes")
        self.assertEqual(or_pass["state"], "pass")
        or_fail, _ = score_with_profile(self.fx, or_profile["id"], "no")
        self.assertEqual(or_fail["state"], "fail")

        weighted, weighted_trace = score_with_profile(self.fx, weighted_profile["id"], "yes")
        self.assertEqual(weighted["state"], "pass")
        self.assertAlmostEqual(weighted_trace["aggregate"]["score"], 0.75, places=6)
        self.assertAlmostEqual(weighted_trace["aggregate"]["threshold"], 0.7, places=6)
        self.assertEqual(len(weighted_trace["aggregate"]["contributions"]), 2)

        majority_pass, majority_trace = score_with_profile(self.fx, majority_profile["id"], "yes")
        self.assertEqual(majority_pass["state"], "pass")  # eq=pass, re-yes=pass, human=uncertain
        self.assertEqual(majority_trace["aggregate"]["passVotes"], 2)
        self.assertEqual(majority_trace["aggregate"]["uncertainVotes"], 1)
        majority_fail, _ = score_with_profile(self.fx, majority_profile["id"], "no")
        self.assertEqual(majority_fail["state"], "fail")

        tie_profile = create_profile(
            self.fx,
            "tie-profile",
            "majority",
            [{"scorer_id": self.eq["id"]}, {"scorer_id": self.re_maybe["id"]}, {"scorer_id": self.human["id"]}],
        )["versions"][0]
        tie, tie_trace = score_with_profile(self.fx, tie_profile["id"], "maybe")
        self.assertEqual(tie["state"], "uncertain")
        self.assertEqual(tie_trace["aggregate"]["passVotes"], 1)
        self.assertEqual(tie_trace["aggregate"]["failVotes"], 1)

    def test_weighted_threshold_change_creates_new_profile_version(self) -> None:
        profile = create_profile(
            self.fx,
            "weighted-threshold",
            "weighted",
            [
                {"scorer_id": self.eq["id"], "weight": 3},
                {"scorer_id": self.re_no["id"], "weight": 1},
            ],
            {"threshold": 0.7},
        )
        version1 = profile["versions"][0]
        first, _ = score_with_profile(self.fx, version1["id"], "yes")
        self.assertEqual(first["state"], "pass")
        conn = self.fx.connect()
        try:
            actor = service.authenticate(conn, self.fx.alpha_token)
            version2 = scoring.add_profile_version(
                conn,
                actor,
                self.fx.alpha_id,
                profile["id"],
                {"config": {"threshold": 0.8}},
            )
        finally:
            conn.close()
        self.assertEqual(version2["version"], 2)
        second, trace = score_with_profile(self.fx, version2["id"], "yes")
        self.assertEqual(second["state"], "fail")
        self.assertAlmostEqual(trace["aggregate"]["score"], 0.75, places=6)
        self.assertAlmostEqual(trace["aggregate"]["threshold"], 0.8, places=6)


class T09VersioningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = ScorerFixture()

    def tearDown(self) -> None:
        self.fx.close()

    def test_scorer_and_rubric_versions_preserve_historical_trace(self) -> None:
        dataset = self.fx.dataset(
            "versioning",
            [{"sample_id": "s1", "input": "greet", "expected": "Hello", "priority": "P0"}],
        )
        scorer = create_scorer(self.fx, "exact", "exact_match", {"case_sensitive": True})
        profile = create_profile(
            self.fx, "exact-profile", "and", [{"scorer_id": scorer["id"]}]
        )
        profile_version = profile["versions"][0]
        run1 = create_run(self.fx, dataset, profile_version["id"], commit="1" * 40)
        conn = self.fx.connect()
        try:
            actor = service.authenticate(conn, self.fx.alpha_token)
            first = scoring.score_run(
                conn, actor, self.fx.alpha_id, run1["id"], {"actual_outputs": {"s1": "hello"}}
            )
        finally:
            conn.close()
        self.assertEqual(first["summary"]["states"]["fail"], 1)

        conn = self.fx.connect()
        try:
            actor = service.authenticate(conn, self.fx.alpha_token)
            version2 = scoring.add_scorer_version(
                conn,
                actor,
                self.fx.alpha_id,
                scorer["id"],
                {"config": {"case_sensitive": False}},
            )
            version3 = scoring.add_scorer_version(
                conn,
                actor,
                self.fx.alpha_id,
                scorer["id"],
                {"rubric": "A different rubric for documentation."},
            )
        finally:
            conn.close()
        self.assertEqual(version2["version"], 2)
        self.assertEqual(version3["version"], 3)
        self.assertEqual(version2["config"]["case_sensitive"], False)
        self.assertEqual(version3["config"], version2["config"])
        self.assertNotEqual(version3["rubric_digest"], version2["rubric_digest"])
        self.assertNotEqual(version3["frozen_digest"], version2["frozen_digest"])

        run2 = create_run(self.fx, dataset, profile_version["id"], commit="2" * 40)
        conn = self.fx.connect()
        try:
            actor = service.authenticate(conn, self.fx.alpha_token)
            second = scoring.score_run(
                conn, actor, self.fx.alpha_id, run2["id"], {"actual_outputs": {"s1": "hello"}}
            )
            trace1 = scoring.run_scorer_trace(conn, actor, self.fx.alpha_id, run1["id"])
            trace2 = scoring.run_scorer_trace(conn, actor, self.fx.alpha_id, run2["id"])
            old_version = scoring.get_scorer_version(conn, actor, self.fx.alpha_id, scorer["id"], 1)
        finally:
            conn.close()
        self.assertEqual(second["summary"]["states"]["pass"], 1)
        self.assertEqual(trace1["items"][0]["state"], "fail")
        member1 = trace1["items"][0]["trace"]["members"][0]
        self.assertEqual(member1["scorer_version"], 1)
        self.assertEqual(trace1["scorer_versions"][member1["scorer_version_id"]]["version"], 1)
        self.assertEqual(trace1["latest_versions"][scorer["id"]]["version"], 3)
        member2 = trace2["items"][0]["trace"]["members"][0]
        self.assertEqual(member2["scorer_version"], 3)
        self.assertEqual(old_version["config"]["case_sensitive"], True)
        self.assertEqual(old_version["frozen_digest"], member1["frozen_digest"])


class T09RunFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = ScorerFixture()

    def tearDown(self) -> None:
        self.fx.close()

    def test_unavailable_model_endpoint_marks_item_error_and_run_continues(self) -> None:
        dataset = self.fx.dataset(
            "failure",
            [
                {"sample_id": "s1", "input": "q1", "expected": "yes", "priority": "P0"},
                {"sample_id": "s2", "input": "q2", "expected": "no", "priority": "P0"},
            ],
        )
        eq = create_scorer(self.fx, "eq", "exact_match", {})
        dead = create_scorer(
            self.fx,
            "dead-judge",
            "llm_judge",
            {
                "endpoint": "http://127.0.0.1:9",
                "model": "missing-model",
                "timeout_s": 0.5,
                "pass_threshold": 0.7,
            },
            rubric="Pass only when the answer is correct.",
        )
        default_profile = create_profile(
            self.fx, "default-profile", "and", [{"scorer_id": eq["id"]}]
        )["versions"][0]
        judge_profile = create_profile(
            self.fx, "judge-profile", "and", [{"scorer_id": dead["id"]}]
        )["versions"][0]
        status, body = self.fx.api(
            "POST",
            f"/projects/{self.fx.alpha_id}/datasets/{dataset['id']}/scorer-bindings",
            self.fx.alpha_token,
            {"profile_id": default_profile["profile_id"]},
        )
        self.assertEqual(status, 201, body)
        default_binding = body["data"]
        self.assertIsNone(default_binding["sample_key"])
        status, body = self.fx.api(
            "POST",
            f"/projects/{self.fx.alpha_id}/datasets/{dataset['id']}/scorer-bindings",
            self.fx.alpha_token,
            {"sample_key": "s1", "profile_id": judge_profile["profile_id"]},
        )
        self.assertEqual(status, 201, body)

        run = create_run(self.fx, dataset, "bindings", commit="3" * 40)
        status, body = self.fx.api(
            "POST",
            f"/projects/{self.fx.alpha_id}/runs/{run['id']}/score",
            self.fx.alpha_token,
            {"actual_outputs": {"s1": "yes", "s2": "no"}},
        )
        self.assertEqual(status, 200, body)
        summary = body["data"]["summary"]
        self.assertEqual(summary["status"], "succeeded")
        self.assertEqual(summary["itemCount"], 2)
        self.assertEqual(summary["states"]["error"], 1)
        self.assertEqual(summary["states"]["pass"], 1)

        status, body = self.fx.api(
            "GET",
            f"/projects/{self.fx.alpha_id}/runs/{run['id']}/scorer-trace",
            self.fx.alpha_token,
        )
        self.assertEqual(status, 200, body)
        items = {item["case_ref"]: item for item in body["data"]["items"]}
        self.assertEqual(items["s1"]["state"], "error")
        member = items["s1"]["trace"]["members"][0]
        self.assertEqual(member["scorer_type"], "llm_judge")
        self.assertEqual(member["error"]["code"], "endpoint_unreachable")
        self.assertIn("unreachable", member["error"]["message"])
        self.assertEqual(member["detail"]["error"]["code"], "endpoint_unreachable")
        self.assertEqual(items["s2"]["state"], "pass")
        self.assertEqual(items["s2"]["trace"]["origin"]["binding_scope"], "dataset")

        status, body = self.fx.api(
            "GET", f"/projects/{self.fx.alpha_id}/runs/{run['id']}", self.fx.alpha_token
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["status"], "succeeded")
        self.assertEqual(len(body["data"]["items"]), 2)


class T09ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = ScorerFixture()

    def tearDown(self) -> None:
        self.fx.close()

    def test_api_permissions_versioning_and_scoring_routes(self) -> None:
        status, body = self.fx.api(
            "POST",
            f"/projects/{self.fx.alpha_id}/scorers",
            self.fx.ro_token,
            {"name": "forbidden", "type": "exact_match", "config": {}},
        )
        self.assertEqual(status, 403, body)
        status, body = self.fx.api(
            "GET", f"/projects/{self.fx.alpha_id}/scorers", self.fx.beta_token
        )
        self.assertEqual(status, 404, body)

        status, body = self.fx.api(
            "POST",
            f"/projects/{self.fx.alpha_id}/scorers",
            self.fx.alpha_token,
            {"name": "api-exact", "type": "exact_match", "config": {"case_sensitive": False}},
        )
        self.assertEqual(status, 201, body)
        scorer = body["data"]
        status, body = self.fx.api(
            "POST",
            f"/projects/{self.fx.alpha_id}/scorers/{scorer['id']}/versions",
            self.fx.alpha_token,
            {"rubric": "rubric v2"},
        )
        self.assertEqual(status, 201, body)
        self.assertEqual(body["data"]["version"], 2)
        status, body = self.fx.api(
            "GET",
            f"/projects/{self.fx.alpha_id}/scorers/{scorer['id']}/versions/1",
            self.fx.alpha_token,
        )
        self.assertEqual(status, 200, body)
        self.assertIsNone(body["data"]["rubric"])

        status, body = self.fx.api(
            "POST",
            f"/projects/{self.fx.alpha_id}/scorers/preview",
            self.fx.alpha_token,
            {
                "scorer_version_id": scorer["versions"][0]["id"],
                "sample": {"expected": "Yes"},
                "actual": "yes",
            },
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["data"]["result"]["state"], "pass")

        dataset = self.fx.dataset("api", [{"sample_id": "s1", "input": "q", "expected": "yes", "priority": "P0"}])
        profile = create_profile(
            self.fx, "api-profile", "and", [{"scorer_id": scorer["id"]}]
        )["versions"][0]
        status, body = self.fx.api(
            "POST",
            f"/projects/{self.fx.alpha_id}/datasets/{dataset['id']}/scorer-bindings",
            self.fx.alpha_token,
            {"profile_id": profile["profile_id"]},
        )
        self.assertEqual(status, 201, body)
        binding = body["data"]
        status, body = self.fx.api(
            "GET",
            f"/projects/{self.fx.alpha_id}/datasets/{dataset['id']}/scorer-bindings",
            self.fx.alpha_token,
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body["data"]), 1)

        run = create_run(self.fx, dataset, "bindings", commit="4" * 40)
        status, body = self.fx.api(
            "POST",
            f"/projects/{self.fx.alpha_id}/runs/{run['id']}/score",
            self.fx.alpha_token,
            {"actual_outputs": {"s1": "YES"}},
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["data"]["summary"]["states"]["pass"], 1)
        status, body = self.fx.api(
            "POST",
            f"/projects/{self.fx.alpha_id}/runs/{run['id']}/score",
            self.fx.alpha_token,
            {"actual_outputs": {"s1": "YES"}},
        )
        self.assertEqual(status, 409, body)
        status, body = self.fx.api(
            "GET",
            f"/projects/{self.fx.alpha_id}/runs/{run['id']}/scorer-trace",
            self.fx.beta_token,
        )
        self.assertEqual(status, 404, body)

        status, body = self.fx.api(
            "DELETE",
            f"/projects/{self.fx.alpha_id}/datasets/{dataset['id']}/scorer-bindings/{binding['id']}",
            self.fx.alpha_token,
        )
        self.assertEqual(status, 200, body)

    def test_invalid_configurations_are_rejected(self) -> None:
        cases = [
            {"name": "bad-schema", "type": "json_schema", "config": {"schema": {"type": "object", "oneOfx": []}}},
            {"name": "bad-regex", "type": "regex", "config": {"pattern": "("}},
            {"name": "bad-key", "type": "exact_match", "config": {"unknown": True}},
            {"name": "bad-human", "type": "human_review", "config": {"default_state": "pass"}},
            {"name": "bad-key-literal", "type": "llm_judge", "config": {"endpoint": "http://x", "model": "m", "api_key": "secret"}, "rubric": "r"},
            {"name": "bad-no-rubric", "type": "llm_judge", "config": {"endpoint": "http://x", "model": "m"}},
        ]
        for case in cases:
            status, body = self.fx.api(
                "POST", f"/projects/{self.fx.alpha_id}/scorers", self.fx.alpha_token, case
            )
            self.assertEqual(status, 400, (case, body))
        scorer = create_scorer(self.fx, "quorum", "exact_match", {})
        status, body = self.fx.api(
            "POST",
            f"/projects/{self.fx.alpha_id}/scorer-profiles",
            self.fx.alpha_token,
            {
                "name": "bad-quorum",
                "combinator": "majority",
                "members": [{"scorer_id": scorer["id"]}],
                "config": {"quorum": 2},
            },
        )
        self.assertEqual(status, 400, body)
