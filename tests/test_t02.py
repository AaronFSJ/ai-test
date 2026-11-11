"""Acceptance tests for T02: scheduling, runners and run lifecycle."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aitest import db, http_api, migrations, service  # noqa: E402


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


class T02Fixture:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="aitest-t02-")
        self.db_path = str(Path(self.tmp.name) / "platform.db")
        migrations.migrate(self.db_path)
        conn = db.connect(self.db_path)
        try:
            alpha = service.create_project(conn, "Alpha")
            beta = service.create_project(conn, "Beta")
            self.alpha_id = alpha["id"]
            self.beta_id = beta["id"]
            self.alpha_token = service.add_member(conn, self.alpha_id, "alice", "member")["token"]
            self.alpha_ro_token = service.add_member(conn, self.alpha_id, "alice-ro", "readonly")["token"]
            self.beta_token = service.add_member(conn, self.beta_id, "bob", "member")["token"]
            self.alpha_actor = service.authenticate(conn, self.alpha_token)
            self.beta_actor = service.authenticate(conn, self.beta_token)
            self.alpha_spec = service.create_spec(
                conn, self.alpha_actor, self.alpha_id, {"channel": "A", "name": "alpha-checkout"}
            )
            self.alpha_version = service.add_spec_version(
                conn,
                self.alpha_actor,
                self.alpha_id,
                self.alpha_spec["id"],
                {"repo_path": "github.com/acme/web", "repo_commit": "a" * 40},
            )
            self.beta_spec = service.create_spec(
                conn, self.beta_actor, self.beta_id, {"channel": "A", "name": "beta-checkout"}
            )
            self.beta_version = service.add_spec_version(
                conn,
                self.beta_actor,
                self.beta_id,
                self.beta_spec["id"],
                {"repo_path": "github.com/acme/beta", "repo_commit": "b" * 40},
            )
        finally:
            conn.close()
        self.server = http_api.make_server(self.db_path, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def call(self, method: str, path: str, token: str | None = None, payload=None):
        return api_call(self.base, method, path, token, payload)

    def run_payload(self, **overrides):
        payload = {
            "spec_version_id": self.alpha_version["id"],
            "commit_sha": "c" * 40,
            "branch": "main",
            "trigger": "ci",
        }
        payload.update(overrides)
        return payload

    def create_run(self, token: str | None = None, **overrides):
        return self.call(
            "POST",
            f"/projects/{self.alpha_id}/runs",
            token or self.alpha_token,
            self.run_payload(**overrides),
        )

    def register_runner(self, name: str = "runner-a", **overrides):
        payload = {"name": name}
        payload.update(overrides)
        return self.call("POST", f"/projects/{self.alpha_id}/runners", self.alpha_token, payload)

    def claim(self, runner_token: str):
        return self.call("POST", "/runner/claim", runner_token, {})

    def configure(self, **settings):
        return self.call(
            "POST", f"/projects/{self.alpha_id}/scheduler/config", self.alpha_token, settings
        )


class T02Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = T02Fixture()

    def tearDown(self) -> None:
        self.fx.close()

    def test_idempotency_reuse_force_and_parameter_change(self) -> None:
        status, first = self.fx.create_run()
        self.assertEqual(status, 201, first)
        run_id = first["data"]["id"]
        key = first["data"]["idempotency_key"]
        self.assertFalse(first["data"]["reused"])
        self.assertEqual(first["data"]["queue_position"], 1)
        self.assertEqual(
            [event["to_status"] for event in first["data"]["events"]], ["queued"]
        )

        status, duplicate = self.fx.create_run()
        self.assertEqual(status, 200, duplicate)
        self.assertTrue(duplicate["data"]["reused"])
        self.assertEqual(duplicate["data"]["id"], run_id)
        self.assertEqual(duplicate["data"]["idempotency_key"], key)
        self.assertEqual(duplicate["data"]["reuse"]["reason"], "idempotency_key_match")

        conn = db.connect(self.fx.db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM run").fetchone()[0]
            event_count = conn.execute(
                "SELECT COUNT(*) FROM run_event WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 1)
        self.assertEqual(event_count, 1)

        status, forced = self.fx.create_run(force=True)
        self.assertEqual(status, 201, forced)
        forced_id = forced["data"]["id"]
        self.assertNotEqual(forced_id, run_id)
        self.assertFalse(forced["data"]["reused"])
        self.assertEqual(forced["data"]["rerun_of_run_id"], run_id)
        self.assertEqual(forced["data"]["idempotency_key"], key)

        status, old = self.fx.call(
            "GET", f"/projects/{self.fx.alpha_id}/runs/{run_id}", self.fx.alpha_token
        )
        self.assertEqual(status, 200, old)
        self.assertEqual(old["data"]["superseded_by_run_id"], forced_id)
        self.assertEqual(old["data"]["status"], "cancelled")
        self.assertEqual(old["data"]["cancel"]["reason"], "superseded_by_force_rerun")
        self.assertFalse(old["data"]["idempotency"]["active"])
        self.assertTrue(forced["data"]["idempotency"]["active"])

        status, repeated_after_force = self.fx.create_run()
        self.assertEqual(status, 200, repeated_after_force)
        self.assertEqual(repeated_after_force["data"]["id"], forced_id)
        self.assertTrue(repeated_after_force["data"]["reused"])

        status, changed = self.fx.create_run(sampling_config={"n": 5})
        self.assertEqual(status, 201, changed)
        self.assertNotEqual(changed["data"]["id"], forced_id)
        self.assertNotEqual(changed["data"]["idempotency_key"], key)
        self.assertNotEqual(changed["data"]["params_digest"], forced["data"]["params_digest"])

        status, runs = self.fx.call(
            "GET", f"/projects/{self.fx.alpha_id}/runs", self.fx.alpha_token
        )
        self.assertEqual(status, 200)
        ids = {item["id"] for item in runs["data"]}
        self.assertTrue({run_id, forced_id, changed["data"]["id"]}.issubset(ids))
        self.assertEqual(len(ids), 3)

    def test_cancel_queued_is_never_claimed_and_running_cancel_is_recorded(self) -> None:
        status, created = self.fx.create_run()
        self.assertEqual(status, 201, created)
        run_id = created["data"]["id"]

        status, cancelled = self.fx.call(
            "POST",
            f"/projects/{self.fx.alpha_id}/runs/{run_id}/cancel",
            self.fx.alpha_token,
            {"reason": "new commit on the PR"},
        )
        self.assertEqual(status, 200, cancelled)
        self.assertEqual(cancelled["data"]["status"], "cancelled")
        self.assertEqual(cancelled["data"]["cancel_result"], "cancelled")
        self.assertEqual(cancelled["data"]["cancel"]["source"], "api")
        self.assertEqual(cancelled["data"]["cancel"]["by"], "alice")
        self.assertEqual(cancelled["data"]["cancel"]["reason"], "new commit on the PR")
        self.assertEqual(cancelled["data"]["cancel"]["previous_status"], "queued")

        status, runner = self.fx.register_runner("cancel-runner")
        self.assertEqual(status, 201, runner)
        runner_token = runner["data"]["token"]

        status, claim = self.fx.claim(runner_token)
        self.assertEqual(status, 200, claim)
        self.assertIsNone(claim["data"])
        self.assertEqual(claim["reason"], "no_work")

        status, events = self.fx.call(
            "GET",
            f"/projects/{self.fx.alpha_id}/runs/{run_id}/events",
            self.fx.alpha_token,
        )
        self.assertEqual(status, 200, events)
        self.assertEqual(
            [(item["from_status"], item["to_status"], item["source"]) for item in events["data"]],
            [(None, "queued", "api"), ("queued", "cancelled", "api")],
        )

        status, second = self.fx.create_run(commit_sha="d" * 40)
        self.assertEqual(status, 201, second)
        status, claim = self.fx.claim(runner_token)
        self.assertEqual(status, 200, claim)
        self.assertEqual(claim["reason"], "claimed")
        running_id = claim["data"]["id"]
        self.assertEqual(claim["data"]["status"], "running")

        status, cancelled_running = self.fx.call(
            "POST",
            f"/projects/{self.fx.alpha_id}/runs/{running_id}/cancel",
            self.fx.alpha_token,
            {"reason": "operator stop"},
        )
        self.assertEqual(status, 200, cancelled_running)
        self.assertEqual(cancelled_running["data"]["status"], "cancelled")
        self.assertEqual(cancelled_running["data"]["cancel"]["previous_status"], "running")
        self.assertEqual(
            cancelled_running["data"]["events"][-1]["from_status"], "running"
        )
        self.assertEqual(cancelled_running["data"]["events"][-1]["to_status"], "cancelled")

        status, late_result = self.fx.call(
            "POST",
            f"/runner/runs/{running_id}/complete",
            runner_token,
            {"status": "succeeded"},
        )
        self.assertEqual(status, 409, late_result)

    def test_hard_timeout_and_queue_timeout_are_explicit(self) -> None:
        self.fx.configure(max_concurrency=1, runner_stale_seconds=30)
        status, created = self.fx.create_run(timeout_seconds=1)
        self.assertEqual(status, 201, created)
        run_id = created["data"]["id"]
        status, runner = self.fx.register_runner("timeout-runner")
        runner_token = runner["data"]["token"]
        status, claim = self.fx.claim(runner_token)
        self.assertEqual(status, 200, claim)
        self.assertEqual(claim["data"]["id"], run_id)
        self.assertEqual(claim["data"]["status"], "running")
        self.assertIsNotNone(claim["data"]["deadline_at"])

        time.sleep(1.2)
        # A plain status query must converge an overdue run: no tick call is
        # required for a CI poller to observe the explicit timeout.
        status, timed_out = self.fx.call(
            "GET", f"/projects/{self.fx.alpha_id}/runs/{run_id}", self.fx.alpha_token
        )
        self.assertEqual(status, 200, timed_out)
        self.assertEqual(timed_out["data"]["status"], "timeout")
        status, tick = self.fx.call(
            "POST", f"/projects/{self.fx.alpha_id}/scheduler/tick", self.fx.alpha_token, {}
        )
        self.assertEqual(status, 200, tick)
        self.assertEqual(tick["data"]["scheduler"]["running_count"], 0)
        self.assertEqual(timed_out["data"]["finished_at"] is not None, True)
        transitions = [
            (item["from_status"], item["to_status"], item["reason"])
            for item in timed_out["data"]["events"]
        ]
        self.assertEqual(
            transitions,
            [
                (None, "queued", "created"),
                ("queued", "running", "claimed"),
                ("running", "timeout", "hard_timeout"),
            ],
        )
        self.assertEqual(timed_out["data"]["events"][-1]["source"], "timeout")

        status, queued = self.fx.create_run(commit_sha="e" * 40, queue_timeout_seconds=1)
        self.assertEqual(status, 201, queued)
        queued_id = queued["data"]["id"]
        time.sleep(1.2)
        status, tick = self.fx.call(
            "POST", f"/projects/{self.fx.alpha_id}/scheduler/tick", self.fx.alpha_token, {}
        )
        self.assertEqual(status, 200, tick)
        status, queue_timeout = self.fx.call(
            "GET", f"/projects/{self.fx.alpha_id}/runs/{queued_id}", self.fx.alpha_token
        )
        self.assertEqual(status, 200)
        self.assertEqual(queue_timeout["data"]["status"], "timeout")
        self.assertEqual(
            queue_timeout["data"]["events"][-1]["reason"], "queue_timeout"
        )
        self.assertEqual(queue_timeout["data"]["events"][-1]["source"], "timeout")

    def test_concurrency_limit_and_priority_order_are_observable(self) -> None:
        status, settings = self.fx.configure(max_concurrency=1, runner_stale_seconds=30)
        self.assertEqual(status, 200, settings)
        low = self.fx.create_run(priority=10, commit_sha="1" * 40)[1]["data"]
        high = self.fx.create_run(priority=90, commit_sha="2" * 40)[1]["data"]
        mid = self.fx.create_run(priority=50, commit_sha="3" * 40)[1]["data"]

        status, scheduler = self.fx.call(
            "GET", f"/projects/{self.fx.alpha_id}/scheduler", self.fx.alpha_token
        )
        self.assertEqual(status, 200, scheduler)
        self.assertEqual(scheduler["data"]["max_concurrency"], 1)
        self.assertEqual(scheduler["data"]["queue_depth"], 3)
        self.assertEqual(scheduler["data"]["running_count"], 0)
        self.assertEqual(
            [item["run_id"] for item in scheduler["data"]["priority_order"]],
            [high["id"], mid["id"], low["id"]],
        )
        self.assertEqual(scheduler["data"]["priority_order"][0]["position"], 1)

        status, runner_one = self.fx.register_runner("priority-a")
        status, runner_two = self.fx.register_runner("priority-b")
        token_one = runner_one["data"]["token"]
        token_two = runner_two["data"]["token"]

        status, first = self.fx.claim(token_two)
        self.assertEqual(status, 200, first)
        self.assertEqual(first["data"]["id"], high["id"])
        self.assertEqual(first["data"]["priority"], 90)
        self.assertEqual(first["scheduler"]["running_count"], 1)
        self.assertEqual(first["scheduler"]["queue_depth"], 2)

        status, blocked = self.fx.claim(token_one)
        self.assertEqual(status, 200, blocked)
        self.assertIsNone(blocked["data"])
        self.assertEqual(blocked["reason"], "concurrency_limit_reached")
        self.assertEqual(blocked["scheduler"]["queue_depth"], 2)

        status, completed = self.fx.call(
            "POST", f"/runner/runs/{high['id']}/complete", token_two, {"status": "succeeded"}
        )
        self.assertEqual(status, 200, completed)
        self.assertEqual(completed["data"]["status"], "succeeded")

        status, second = self.fx.claim(token_one)
        self.assertEqual(status, 200, second)
        self.assertEqual(second["data"]["id"], mid["id"])
        self.assertEqual(second["data"]["priority"], 50)
        self.assertEqual(second["scheduler"]["queue_depth"], 1)
        self.assertEqual(
            [item["run_id"] for item in second["scheduler"]["priority_order"]], [low["id"]]
        )

        self.fx.call(
            "POST", f"/runner/runs/{mid['id']}/complete", token_one, {"status": "succeeded"}
        )
        status, third = self.fx.claim(token_one)
        self.assertEqual(third["data"]["id"], low["id"])
        self.assertEqual(third["scheduler"]["queue_depth"], 0)

    def test_stale_runner_is_marked_unavailable_and_skipped(self) -> None:
        self.fx.configure(max_concurrency=2, runner_stale_seconds=1)
        status, live = self.fx.register_runner("live-runner")
        status, dead = self.fx.register_runner("dead-runner")
        live_token = live["data"]["token"]
        dead_token = dead["data"]["token"]
        dead_id = dead["data"]["id"]

        time.sleep(1.2)
        status, heartbeat = self.fx.call(
            "POST", "/runner/heartbeat", live_token, {"status": "online"}
        )
        self.assertEqual(status, 200, heartbeat)
        self.assertEqual(heartbeat["data"]["available"], True)

        status, scheduler = self.fx.call(
            "GET", f"/projects/{self.fx.alpha_id}/scheduler", self.fx.alpha_token
        )
        self.assertEqual(status, 200, scheduler)
        runners = {item["name"]: item for item in scheduler["data"]["runners"]["items"]}
        self.assertEqual(runners["live-runner"]["available"], True)
        self.assertEqual(runners["dead-runner"]["available"], False)
        self.assertEqual(runners["dead-runner"]["effective_status"], "unavailable")
        self.assertEqual(scheduler["data"]["runners"]["available"], 1)

        status, runner_detail = self.fx.call(
            "GET", f"/projects/{self.fx.alpha_id}/runners/{dead_id}", self.fx.alpha_token
        )
        self.assertEqual(status, 200, runner_detail)
        self.assertEqual(runner_detail["data"]["status"], "unavailable")
        self.assertIsNotNone(runner_detail["data"]["heartbeat_age_seconds"])

        status, created = self.fx.create_run()
        self.assertEqual(status, 201, created)
        status, claim = self.fx.claim(live_token)
        self.assertEqual(status, 200, claim)
        self.assertEqual(claim["reason"], "claimed")
        self.assertEqual(claim["data"]["assigned_runner_id"], live["data"]["id"])

        # A stale runner can only come back by sending a heartbeat; the
        # recovery is recorded rather than silently clearing availability.
        status, recovered = self.fx.call(
            "POST", "/runner/heartbeat", dead_token, {"status": "online"}
        )
        self.assertEqual(status, 200, recovered)
        self.assertEqual(recovered["data"]["status"], "online")
        self.assertEqual(recovered["data"]["available"], True)

    def test_readonly_and_cross_project_rejections(self) -> None:
        status, body = self.fx.call(
            "POST",
            f"/projects/{self.fx.alpha_id}/runners",
            self.fx.alpha_ro_token,
            {"name": "readonly-runner"},
        )
        self.assertEqual(status, 403, body)

        status, created = self.fx.create_run()
        run_id = created["data"]["id"]
        status, body = self.fx.call(
            "POST",
            f"/projects/{self.fx.alpha_id}/runs/{run_id}/cancel",
            self.fx.alpha_ro_token,
            {"reason": "no"},
        )
        self.assertEqual(status, 403, body)

        status, body = self.fx.call(
            "POST", f"/projects/{self.fx.alpha_id}/scheduler/tick", self.fx.alpha_ro_token, {}
        )
        self.assertEqual(status, 403, body)

        status, body = self.fx.call(
            "POST",
            f"/projects/{self.fx.alpha_id}/scheduler/config",
            self.fx.alpha_ro_token,
            {"max_concurrency": 9},
        )
        self.assertEqual(status, 403, body)

        status, scheduler = self.fx.call(
            "GET", f"/projects/{self.fx.alpha_id}/scheduler", self.fx.alpha_ro_token
        )
        self.assertEqual(status, 200, scheduler)
        status, runners = self.fx.call(
            "GET", f"/projects/{self.fx.alpha_id}/runners", self.fx.alpha_ro_token
        )
        self.assertEqual(status, 200, runners)

        status, body = self.fx.call(
            "GET", f"/projects/{self.fx.alpha_id}/scheduler", self.fx.beta_token
        )
        self.assertEqual(status, 404, body)
        status, body = self.fx.call(
            "GET", f"/projects/{self.fx.alpha_id}/runners", self.fx.beta_token
        )
        self.assertEqual(status, 404, body)

        status, body = self.fx.call(
            "POST",
            f"/projects/{self.fx.beta_id}/runs",
            self.fx.beta_token,
            {
                "spec_version_id": self.fx.beta_version["id"],
                "commit_sha": "f" * 40,
            },
        )
        self.assertEqual(status, 201, body)
        beta_run = body["data"]["id"]
        status, alpha_runner = self.fx.register_runner("alpha-runner")
        status, body = self.fx.call(
            "POST", f"/runner/runs/{beta_run}/complete", alpha_runner["data"]["token"], {"status": "succeeded"}
        )
        self.assertEqual(status, 404, body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
