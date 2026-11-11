#!/usr/bin/env python3
"""Reproducible T09 evidence: built-in scorers, combinators, versioning and
model-endpoint failure.

The script starts a real local HTTP server and a deterministic OpenAI-compatible
stub endpoint. The stub validates the LLM judge protocol and parsing; it is not
evidence of model quality. A real closed local port is used for the unavailable-
endpoint case.
"""

from __future__ import annotations

import argparse
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

from aitest import db, http_api, migrations, scoring, service  # noqa: E402


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def excerpt(value: Any, limit: int = 4000) -> Any:
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
        return {"allPassed": all(item["passed"] for item in self.items), "checks": self.items}

    def get(self, name: str) -> bool:
        return any(item["name"] == name and item["passed"] for item in self.items)


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


class StubJudge:
    """Deterministic OpenAI-compatible chat endpoint for protocol tests only."""

    def __init__(self) -> None:
        outer = self
        self.requests: list[dict] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # pragma: no cover
                return

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                outer.requests.append(
                    {
                        "path": self.path,
                        "model": body.get("model"),
                        "response_format": body.get("response_format"),
                        "userChars": len(body.get("messages", [{}])[-1].get("content", "")),
                    }
                )
                user = body.get("messages", [{}])[-1].get("content", "")
                if "INVALID_JSON" in user:
                    content = "not a json object"
                elif '"actual": "bad"' in user or "bad" in user:
                    content = json.dumps({"score": 0.12, "verdict": "fail", "reason": "answer is wrong"})
                else:
                    content = json.dumps({"score": 0.93, "verdict": "pass", "reason": "answer is correct"})
                response = {
                    "id": "stub-judge-1",
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
        self.db_path = str(tmp / "t09.db")
        migrations.migrate(self.db_path)
        conn = db.connect(self.db_path)
        try:
            project = service.create_project(conn, "T09 Evidence")
            self.project_id = project["id"]
            self.token = service.add_member(conn, self.project_id, "alice", "member")["token"]
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

    def profile(self, name: str, combinator: str, members: list[dict], config=None) -> dict:
        return self.client.ok(
            "POST",
            f"/projects/{self.project_id}/scorer-profiles",
            self.token,
            {"name": name, "combinator": combinator, "members": members, "config": config or {}},
            status=201,
        )

    def spec_run(self, label: str, dataset: dict, scorer_ref: str, commit_char: str) -> dict:
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
                "scorer_version": scorer_ref,
                "model_snapshot": {"model": "stub-under-test", "temperature": 0},
                "params": {"suite": dataset["name"]},
            },
            status=201,
        )
        return self.client.ok(
            "POST",
            f"/projects/{self.project_id}/runs",
            self.token,
            {
                "spec_version_id": version["id"],
                "commit_sha": commit_char * 40,
                "branch": "main",
                "trigger": "manual",
            },
            status=201,
        )

    def score(self, run: dict, outputs: dict) -> dict:
        return self.client.ok(
            "POST",
            f"/projects/{self.project_id}/runs/{run['id']}/score",
            self.token,
            {"actual_outputs": outputs},
        )

    def trace(self, run: dict) -> dict:
        return self.client.ok(
            "GET", f"/projects/{self.project_id}/runs/{run['id']}/scorer-trace", self.token
        )

    def preview(self, scorer_type: str, config: dict, sample: dict, actual, rubric=None) -> dict:
        payload = {"type": scorer_type, "config": config, "sample": sample, "actual": actual}
        if rubric is not None:
            payload["rubric"] = rubric
        return self.client.ok(
            "POST", f"/projects/{self.project_id}/scorers/preview", self.token, payload
        )


def collect_builtins(h: Harness, checks: Checks) -> dict:
    exact_cfg = {"case_sensitive": False, "trim": True}
    regex_cfg = {"pattern": r"^ORD-\d{4}$", "mode": "fullmatch"}
    schema_cfg = {
        "parse_string": True,
        "schema": {
            "type": "object",
            "required": ["answer", "confidence"],
            "properties": {
                "answer": {"type": "string", "minLength": 1},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "additionalProperties": False,
        },
    }
    rule_cfg = {
        "assertions": [
            {"path": "$.answer", "op": "exists"},
            {"path": "$.score", "op": "gte", "value": 0.8},
            {"path": "$.source", "op": "in", "value": ["kb", "web"]},
        ]
    }
    semantic_cfg = {"threshold": 0.6, "metric": "lexical_cosine"}
    judge_cfg = {
        "endpoint": h.stub.endpoint,
        "model": "stub-judge",
        "pass_threshold": 0.7,
        "timeout_s": 3,
        "max_tokens": 128,
    }
    dead_cfg = {
        "endpoint": "http://127.0.0.1:9",
        "model": "missing-model",
        "timeout_s": 0.5,
        "pass_threshold": 0.7,
    }
    scorers = {
        "exact_match": h.scorer("bi-exact", "exact_match", exact_cfg),
        "regex": h.scorer("bi-regex", "regex", regex_cfg),
        "json_schema": h.scorer("bi-schema", "json_schema", schema_cfg),
        "rule_assertion": h.scorer("bi-rule", "rule_assertion", rule_cfg),
        "semantic_similarity": h.scorer("bi-semantic", "semantic_similarity", semantic_cfg),
        "human_review": h.scorer("bi-human", "human_review", {"queue": "manual-review"}),
        "llm_judge": h.scorer(
            "bi-judge",
            "llm_judge",
            judge_cfg,
            rubric="Pass when the actual answer is correct and matches the expected answer.",
        ),
        "llm_judge_dead": h.scorer(
            "bi-dead-judge", "llm_judge", dead_cfg, rubric="Pass when correct."
        ),
    }
    cases: list[dict] = []

    def case(label: str, scorer: dict, sample: dict, actual, expect_state: str) -> dict:
        version = scorer["versions"][0]
        preview = h.client.ok(
            "POST",
            f"/projects/{h.project_id}/scorers/preview",
            h.token,
            {
                "scorer_version_id": version["id"],
                "sample": sample,
                "actual": actual,
            },
        )
        result = preview["result"]
        cases.append(
            {
                "label": label,
                "scorerId": scorer["id"],
                "scorerName": scorer["name"],
                "scorerVersionId": version["id"],
                "scorerVersion": version["version"],
                "type": version["type"],
                "config": version["config"],
                "rubricDigest": version["rubric_digest"],
                "frozenDigest": version["frozen_digest"],
                "sample": sample,
                "actual": actual,
                "state": result["state"],
                "confidence": result["confidence"],
                "detail": result.get("detail"),
                "error": result.get("error"),
                "usage": result.get("usage"),
                "expectedState": expect_state,
            }
        )
        checks.add(
            f"builtin.{label}",
            result["state"] == expect_state,
            {
                "state": result["state"],
                "expected": expect_state,
                "scorerVersionId": version["id"],
                "detail": result.get("detail"),
            },
        )
        return result

    case(
        "exact_match_pass",
        scorers["exact_match"],
        {"sample_id": "b1", "input": "greet", "expected": " Hello "},
        "hello",
        "pass",
    )
    case(
        "exact_match_fail",
        scorers["exact_match"],
        {"sample_id": "b1", "input": "greet", "expected": "hello"},
        "goodbye",
        "fail",
    )
    case("regex_pass", scorers["regex"], {"sample_id": "b2"}, "ORD-1234", "pass")
    case("regex_fail", scorers["regex"], {"sample_id": "b2"}, "ORD-12", "fail")
    case(
        "json_schema_pass",
        scorers["json_schema"],
        {"sample_id": "b3"},
        '{"answer":"ok","confidence":0.8}',
        "pass",
    )
    case(
        "json_schema_fail",
        scorers["json_schema"],
        {"sample_id": "b3"},
        '{"answer":"","confidence":2,"extra":true}',
        "fail",
    )
    case(
        "rule_assertion_pass",
        scorers["rule_assertion"],
        {"sample_id": "b4"},
        {"answer": "x", "score": 0.9, "source": "kb"},
        "pass",
    )
    case(
        "rule_assertion_fail",
        scorers["rule_assertion"],
        {"sample_id": "b4"},
        {"answer": "x", "score": 0.5, "source": "kb"},
        "fail",
    )
    case(
        "rule_assertion_uncertain",
        scorers["rule_assertion"],
        {"sample_id": "b4"},
        {"answer": "x"},
        "uncertain",
    )
    case(
        "semantic_similarity_pass",
        scorers["semantic_similarity"],
        {"sample_id": "b5", "expected": "退款政策是什么"},
        "退款政策是什么？",
        "pass",
    )
    case(
        "semantic_similarity_fail",
        scorers["semantic_similarity"],
        {"sample_id": "b5", "expected": "退款政策是什么"},
        "发票如何开具",
        "fail",
    )
    case("human_review_undecided", scorers["human_review"], {"sample_id": "b6"}, {}, "uncertain")
    case(
        "human_review_decided",
        scorers["human_review"],
        {"sample_id": "b6"},
        {"human_review": {"conclusion": "false_positive", "reviewer": "qa"}},
        "pass",
    )
    case(
        "llm_judge_pass",
        scorers["llm_judge"],
        {"sample_id": "b7", "input": "2+2?", "expected": "4"},
        "good",
        "pass",
    )
    case(
        "llm_judge_fail",
        scorers["llm_judge"],
        {"sample_id": "b7", "input": "2+2?", "expected": "4"},
        "bad",
        "fail",
    )
    case(
        "llm_judge_invalid_response",
        scorers["llm_judge"],
        {"sample_id": "b7", "input": "INVALID_JSON", "expected": "4"},
        "bad",
        "error",
    )
    case(
        "llm_judge_unreachable",
        scorers["llm_judge_dead"],
        {"sample_id": "b8", "input": "q", "expected": "4"},
        "4",
        "error",
    )
    types = sorted({item["type"] for item in cases})
    checks.add(
        "builtin.at_least_five_types",
        len(types) >= 5,
        {"types": types, "typeCount": len(types), "caseCount": len(cases)},
    )
    return {
        "generatedAt": now(),
        "engine": "persisted scorer versions evaluated via HTTP /scorers/preview",
        "stubJudge": {
            "endpoint": h.stub.endpoint,
            "note": "deterministic protocol stub only; not evidence of LLM quality",
            "requests": h.stub.requests,
        },
        "types": types,
        "caseCount": len(cases),
        "cases": cases,
    }


def collect_combinators(h: Harness, checks: Checks) -> dict:
    dataset = h.dataset(
        "combinator-eval",
        [
            {"sample_id": "s1", "input": "q1", "expected": "yes", "priority": "P0"},
            {"sample_id": "s2", "input": "q2", "expected": "no", "priority": "P0"},
        ],
    )
    eq = h.scorer("comb-eq", "exact_match", {"case_sensitive": False})
    re_yes = h.scorer("comb-re-yes", "regex", {"pattern": "^yes$", "mode": "fullmatch"})
    re_no = h.scorer("comb-re-no", "regex", {"pattern": "^no$", "mode": "fullmatch"})
    human = h.scorer("comb-human", "human_review", {})
    eq2 = h.scorer("comb-eq2", "exact_match", {"case_sensitive": False})

    and_profile = h.profile(
        "comb-and", "and", [{"scorer_id": eq["id"]}, {"scorer_id": re_yes["id"]}]
    )["versions"][0]
    or_profile = h.profile(
        "comb-or", "or", [{"scorer_id": eq["id"]}, {"scorer_id": re_yes["id"]}]
    )["versions"][0]
    weighted = h.profile(
        "comb-weighted",
        "weighted",
        [{"scorer_id": eq["id"], "weight": 3}, {"scorer_id": re_no["id"], "weight": 1}],
        {"threshold": 0.7},
    )
    majority = h.profile(
        "comb-majority",
        "majority",
        [
            {"scorer_id": eq["id"]},
            {"scorer_id": re_yes["id"]},
            {"scorer_id": re_no["id"]},
            {"scorer_id": eq2["id"]},
            {"scorer_id": human["id"]},
        ],
    )["versions"][0]
    weighted_v1 = weighted["versions"][0]
    weighted_v2 = h.client.ok(
        "POST",
        f"/projects/{h.project_id}/scorer-profiles/{weighted['id']}/versions",
        h.token,
        {"config": {"threshold": 0.8}},
        status=201,
    )

    runs = []

    def run_case(label: str, profile_version: dict, outputs: dict, commit_char: str) -> dict:
        run = h.spec_run(label, dataset, profile_version["id"], commit_char)
        scored = h.score(run, outputs)
        trace = h.trace(run)
        entry = {
            "label": label,
            "run_id": run["id"],
            "spec_version_id": run["spec_version_id"],
            "profile_id": profile_version["profile_id"],
            "profile_version_id": profile_version["id"],
            "profile_version": profile_version["version"],
            "combinator": profile_version["combinator"],
            "config": profile_version["config"],
            "actualOutputs": outputs,
            "summary": scored["summary"],
            "items": [
                {
                    "case_ref": item["case_ref"],
                    "state": item["state"],
                    "confidence": item["confidence"],
                    "aggregate": (item["trace"] or {}).get("aggregate"),
                    "members": [
                        {
                            "scorer_name": member.get("scorer_name"),
                            "state": member.get("state"),
                            "confidence": member.get("confidence"),
                            "score": member.get("score"),
                            "weight": member.get("weight"),
                        }
                        for member in ((item["trace"] or {}).get("members") or [])
                    ],
                }
                for item in trace["items"]
            ],
        }
        runs.append(entry)
        return entry

    and_run = run_case("and-pass-fail", and_profile, {"s1": "yes", "s2": "no"}, "a")
    checks.add(
        "combinator.and",
        and_run["summary"]["states"]["pass"] == 1
        and and_run["summary"]["states"]["fail"] == 1
        and {item["case_ref"]: item["state"] for item in and_run["items"]}
        == {"s1": "pass", "s2": "fail"},
        and_run["summary"]["states"],
    )

    or_run = run_case("or-fail-pass", or_profile, {"s1": "no", "s2": "no"}, "b")
    checks.add(
        "combinator.or",
        {item["case_ref"]: item["state"] for item in or_run["items"]}
        == {"s1": "fail", "s2": "pass"},
        or_run["summary"]["states"],
    )

    weighted_run = run_case("weighted-threshold-0.7", weighted_v1, {"s1": "yes", "s2": "yes"}, "c")
    weighted_items = {item["case_ref"]: item for item in weighted_run["items"]}
    weighted_score = weighted_items["s1"]["aggregate"]["score"]
    checks.add(
        "combinator.weighted",
        weighted_items["s1"]["state"] == "pass"
        and weighted_items["s2"]["state"] == "fail"
        and abs(weighted_score - 0.75) < 1e-9,
        {"states": weighted_run["summary"]["states"], "s1Score": weighted_score},
    )

    weighted2_run = run_case("weighted-threshold-0.8", weighted_v2, {"s1": "yes", "s2": "yes"}, "d")
    weighted2_items = {item["case_ref"]: item for item in weighted2_run["items"]}
    checks.add(
        "combinator.weighted_threshold_change",
        weighted2_items["s1"]["state"] == "fail"
        and abs(weighted2_items["s1"]["aggregate"]["score"] - 0.75) < 1e-9
        and abs(weighted2_items["s1"]["aggregate"]["threshold"] - 0.8) < 1e-9,
        {
            "version": weighted_v2["version"],
            "state": weighted2_items["s1"]["state"],
            "aggregate": weighted2_items["s1"]["aggregate"],
        },
    )

    majority_run = run_case("majority-2-1", majority, {"s1": "yes", "s2": "yes"}, "e")
    majority_items = {item["case_ref"]: item for item in majority_run["items"]}
    checks.add(
        "combinator.majority",
        majority_items["s1"]["state"] == "pass"
        and majority_items["s2"]["state"] == "fail"
        and majority_items["s1"]["aggregate"]["passVotes"] == 3
        and majority_items["s2"]["aggregate"]["failVotes"] == 3,
        {
            "s1": majority_items["s1"]["aggregate"],
            "s2": majority_items["s2"]["aggregate"],
        },
    )
    member_details_ok = all(
        item["members"] and all("state" in member and "confidence" in member for member in item["members"])
        for run in runs
        for item in run["items"]
    )
    checks.add(
        "combinator.member_details",
        member_details_ok,
        {"runCount": len(runs), "sample": runs[0]["items"][0]["members"]},
    )
    return {
        "generatedAt": now(),
        "dataset": {
            "id": dataset["id"],
            "version": dataset["_version"]["version"],
            "datasetVersionId": dataset["_version"]["id"],
            "samples": [row["sample_id"] for row in dataset["samples"]],
        },
        "scorers": {
            "exact": eq["id"],
            "regexYes": re_yes["id"],
            "regexNo": re_no["id"],
            "human": human["id"],
        },
        "runs": runs,
    }


def collect_versioning(h: Harness, checks: Checks) -> dict:
    dataset = h.dataset(
        "versioning-eval",
        [{"sample_id": "s1", "input": "greet", "expected": "Hello", "priority": "P0"}],
    )
    scorer = h.scorer("versioned-exact", "exact_match", {"case_sensitive": True})
    profile = h.profile(
        "versioned-profile", "and", [{"scorer_id": scorer["id"]}]
    )
    profile_version = profile["versions"][0]

    run1 = h.spec_run("versioning-before", dataset, profile_version["id"], "f")
    scored1 = h.score(run1, {"s1": "hello"})

    v2 = h.client.ok(
        "POST",
        f"/projects/{h.project_id}/scorers/{scorer['id']}/versions",
        h.token,
        {"config": {"case_sensitive": False}},
        status=201,
    )
    v3 = h.client.ok(
        "POST",
        f"/projects/{h.project_id}/scorers/{scorer['id']}/versions",
        h.token,
        {"rubric": "Documentation rubric added after the fact."},
        status=201,
    )
    trace1 = h.trace(run1)
    run2 = h.spec_run("versioning-after", dataset, profile_version["id"], "9")
    scored2 = h.score(run2, {"s1": "hello"})
    trace2 = h.trace(run2)

    scorer_detail = h.client.ok(
        "GET", f"/projects/{h.project_id}/scorers/{scorer['id']}", h.token
    )
    old_version = h.client.ok(
        "GET", f"/projects/{h.project_id}/scorers/{scorer['id']}/versions/1", h.token
    )
    latest_version = h.client.ok(
        "GET", f"/projects/{h.project_id}/scorers/{scorer['id']}/versions/3", h.token
    )

    weighted = h.profile(
        "versioned-weighted",
        "weighted",
        [{"scorer_id": scorer["id"], "weight": 1}],
        {"threshold": 0.7},
    )
    weighted_v2 = h.client.ok(
        "POST",
        f"/projects/{h.project_id}/scorer-profiles/{weighted['id']}/versions",
        h.token,
        {"config": {"threshold": 0.9}},
        status=201,
    )

    member1 = trace1["items"][0]["trace"]["members"][0]
    member2 = trace2["items"][0]["trace"]["members"][0]
    checks.add(
        "versioning.config_change_creates_version",
        v2["version"] == 2 and v2["config"]["case_sensitive"] is False,
        {"version": v2["version"], "config": v2["config"], "frozenDigest": v2["frozen_digest"]},
    )
    checks.add(
        "versioning.rubric_change_creates_version",
        v3["version"] == 3
        and v3["config"] == v2["config"]
        and v3["rubric_digest"] != v2["rubric_digest"]
        and v3["frozen_digest"] != v2["frozen_digest"],
        {
            "version": v3["version"],
            "config": v3["config"],
            "rubricDigestV2": v2["rubric_digest"],
            "rubricDigestV3": v3["rubric_digest"],
            "frozenDigestV3": v3["frozen_digest"],
        },
    )
    checks.add(
        "versioning.old_run_keeps_old_version",
        trace1["items"][0]["state"] == "fail"
        and member1["scorer_version"] == 1
        and trace1["scorer_versions"][member1["scorer_version_id"]]["version"] == 1
        and trace1["latest_versions"][scorer["id"]]["version"] == 3,
        {
            "state": trace1["items"][0]["state"],
            "memberVersion": member1["scorer_version"],
            "latestVersion": trace1["latest_versions"][scorer["id"]],
        },
    )
    checks.add(
        "versioning.new_run_uses_new_version",
        trace2["items"][0]["state"] == "pass"
        and member2["scorer_version"] == 3,
        {"state": trace2["items"][0]["state"], "memberVersion": member2["scorer_version"]},
    )
    checks.add(
        "versioning.historical_version_retrievable",
        old_version["config"]["case_sensitive"] is True
        and old_version["frozen_digest"] == member1["frozen_digest"]
        and latest_version["config"]["case_sensitive"] is False,
        {
            "oldVersion": 1,
            "oldConfig": old_version["config"],
            "oldFrozenDigest": old_version["frozen_digest"],
            "traceFrozenDigest": member1["frozen_digest"],
            "latestConfig": latest_version["config"],
        },
    )
    checks.add(
        "versioning.profile_change_creates_version",
        weighted["versions"][0]["version"] == 1
        and weighted_v2["version"] == 2
        and weighted_v2["config"]["threshold"] == 0.9,
        {
            "profileVersions": [item["version"] for item in h.client.ok(
                "GET", f"/projects/{h.project_id}/scorer-profiles/{weighted['id']}/versions", h.token
            )],
            "v2Config": weighted_v2["config"],
        },
    )
    return {
        "generatedAt": now(),
        "dataset": {
            "id": dataset["id"],
            "datasetVersionId": dataset["_version"]["id"],
            "version": dataset["_version"]["version"],
        },
        "scorer": {
            "id": scorer["id"],
            "versions": scorer_detail["versions"],
        },
        "profile": {
            "id": profile["id"],
            "versionUsedByBothRuns": profile_version["version"],
            "versionIdUsedByBothRuns": profile_version["id"],
        },
        "runs": [
            {
                "label": "before-config-change",
                "runId": run1["id"],
                "specVersionId": run1["spec_version_id"],
                "summary": scored1["summary"],
                "trace": trace1,
            },
            {
                "label": "after-config-change",
                "runId": run2["id"],
                "specVersionId": run2["spec_version_id"],
                "summary": scored2["summary"],
                "trace": trace2,
            },
        ],
        "oldVersion": old_version,
        "latestVersion": latest_version,
        "weightedProfile": {"id": weighted["id"], "v1": weighted["versions"][0], "v2": weighted_v2},
    }


def collect_endpoint_failure(h: Harness, checks: Checks) -> dict:
    dataset = h.dataset(
        "endpoint-failure-eval",
        [
            {"sample_id": "s1", "input": "q1", "expected": "yes", "priority": "P0"},
            {"sample_id": "s2", "input": "q2", "expected": "no", "priority": "P0"},
        ],
    )
    eq = h.scorer("failure-exact", "exact_match", {})
    dead = h.scorer(
        "failure-dead-judge",
        "llm_judge",
        {
            "endpoint": "http://127.0.0.1:9",
            "model": "missing-model",
            "timeout_s": 0.5,
            "pass_threshold": 0.7,
        },
        rubric="Pass only when the answer is correct.",
    )
    default_profile = h.profile("failure-default", "and", [{"scorer_id": eq["id"]}])["versions"][0]
    judge_profile = h.profile("failure-judge", "and", [{"scorer_id": dead["id"]}])["versions"][0]
    default_binding = h.client.ok(
        "POST",
        f"/projects/{h.project_id}/datasets/{dataset['id']}/scorer-bindings",
        h.token,
        {"profile_id": default_profile["profile_id"]},
        status=201,
    )
    sample_binding = h.client.ok(
        "POST",
        f"/projects/{h.project_id}/datasets/{dataset['id']}/scorer-bindings",
        h.token,
        {"sample_key": "s1", "profile_id": judge_profile["profile_id"]},
        status=201,
    )
    run = h.spec_run("endpoint-failure", dataset, "bindings", "7")
    started = time.monotonic()
    scored = h.score(run, {"s1": "yes", "s2": "no"})
    elapsed_ms = int((time.monotonic() - started) * 1000)
    trace = h.trace(run)
    run_detail = h.client.ok(
        "GET", f"/projects/{h.project_id}/runs/{run['id']}", h.token
    )
    items = {item["case_ref"]: item for item in trace["items"]}
    error_member = items["s1"]["trace"]["members"][0]
    checks.add(
        "endpoint.unavailable_returns_error",
        items["s1"]["state"] == "error"
        and error_member["error"]["code"] == "endpoint_unreachable"
        and bool(error_member["error"]["message"]),
        {
            "state": items["s1"]["state"],
            "error": error_member["error"],
            "memberDetailError": error_member["detail"].get("error"),
        },
    )
    checks.add(
        "endpoint.other_item_passes",
        items["s2"]["state"] == "pass",
        {"state": items["s2"]["state"], "traceOrigin": items["s2"]["trace"]["origin"]},
    )
    checks.add(
        "endpoint.run_continues_and_succeeds",
        scored["summary"]["itemCount"] == 2
        and scored["summary"]["states"]["error"] == 1
        and scored["summary"]["states"]["pass"] == 1
        and run_detail["status"] == "succeeded",
        {
            "summary": scored["summary"],
            "runStatus": run_detail["status"],
            "runItemCount": len(run_detail["items"]),
            "elapsedMs": elapsed_ms,
        },
    )
    return {
        "generatedAt": now(),
        "dataset": {
            "id": dataset["id"],
            "datasetVersionId": dataset["_version"]["id"],
            "version": dataset["_version"]["version"],
        },
        "bindings": {"default": default_binding, "sample": sample_binding},
        "run": {
            "id": run["id"],
            "specVersionId": run["spec_version_id"],
            "status": run_detail["status"],
            "summary": scored["summary"],
        },
        "trace": trace,
        "elapsedMs": elapsed_ms,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run T09 verification and write evidence")
    parser.add_argument("--out", default="../deliverables/evidence")
    args = parser.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    checks = Checks()
    with tempfile.TemporaryDirectory(prefix="aitest-t09-evidence-") as tmp:
        harness = Harness(Path(tmp))
        try:
            builtins = collect_builtins(harness, checks)
            combinators = collect_combinators(harness, checks)
            versioning = collect_versioning(harness, checks)
            endpoint = collect_endpoint_failure(harness, checks)
        finally:
            harness.close()

    payloads = {
        "12-scorers-builtin.json": builtins,
        "13-scorers-combinators.json": combinators,
        "14-scorer-versioning.json": versioning,
        "15-scorer-endpoint-failure.json": endpoint,
    }
    paths = {}
    for filename, payload in payloads.items():
        path = out / filename
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n"
        path.write_text(text, encoding="utf-8")
        paths[filename] = {"path": str(path), "bytes": len(text.encode("utf-8")), "sha256": sha256(text)}

    acceptance = {
        "T09-a1": checks.get("builtin.at_least_five_types")
        and checks.get("builtin.exact_match_pass")
        and checks.get("builtin.regex_pass")
        and checks.get("builtin.json_schema_pass")
        and checks.get("builtin.rule_assertion_pass")
        and checks.get("builtin.semantic_similarity_pass")
        and checks.get("builtin.llm_judge_pass")
        and checks.get("builtin.human_review_undecided"),
        "T09-a2": checks.get("combinator.and")
        and checks.get("combinator.or")
        and checks.get("combinator.weighted")
        and checks.get("combinator.weighted_threshold_change")
        and checks.get("combinator.majority")
        and checks.get("combinator.member_details"),
        "T09-a3": checks.get("versioning.config_change_creates_version")
        and checks.get("versioning.rubric_change_creates_version")
        and checks.get("versioning.old_run_keeps_old_version")
        and checks.get("versioning.new_run_uses_new_version")
        and checks.get("versioning.historical_version_retrievable")
        and checks.get("versioning.profile_change_creates_version"),
        "T09-a4": checks.get("endpoint.unavailable_returns_error")
        and checks.get("endpoint.other_item_passes")
        and checks.get("endpoint.run_continues_and_succeeds"),
    }
    summary = {
        "generatedAt": now(),
        "task": "T09",
        "allChecksPassed": checks.as_dict()["allPassed"],
        "checkCount": len(checks.items),
        "checks": checks.items,
        "acceptance": acceptance,
        "evidence": paths,
        "environment": {
            "python": sys.version.split()[0],
            "database": "temporary SQLite database",
            "http": "real local ThreadingHTTPServer",
            "llmJudge": (
                "deterministic OpenAI-compatible stub for protocol/parse tests; "
                "real closed port for the unavailable-endpoint case. "
                "No real model quality is claimed."
            ),
        },
    }
    summary_text = json.dumps(summary, ensure_ascii=False, indent=2, default=str)
    (out / "t09-summary.json").write_text(summary_text + "\n", encoding="utf-8")
    print(json.dumps({"allChecksPassed": summary["allChecksPassed"], "acceptance": acceptance, "out": str(out)}, ensure_ascii=False, indent=2))
    return 0 if summary["allChecksPassed"] else 2


if __name__ == "__main__":
    sys.exit(main())
