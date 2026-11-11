"""Acceptance tests for T01: data model, isolation, permissions, spec versions."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aitest import db, http_api, migrations, service  # noqa: E402

CORE_TABLES = {
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


def fresh_db(path: str | Path) -> dict:
    return migrations.migrate(path)


class PlatformFixture:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="aitest-t01-")
        self.db_path = str(Path(self.tmp.name) / "platform.db")
        fresh_db(self.db_path)
        conn = db.connect(self.db_path)
        try:
            alpha = service.create_project(conn, "Alpha")
            beta = service.create_project(conn, "Beta")
            self.alpha_id = alpha["id"]
            self.beta_id = beta["id"]
            self.alpha_token = service.add_member(conn, self.alpha_id, "alice", "member")["token"]
            self.alpha_ro_token = service.add_member(conn, self.alpha_id, "alice-ro", "readonly")["token"]
            self.beta_token = service.add_member(conn, self.beta_id, "bob", "member")["token"]
            self.beta_ro_token = service.add_member(conn, self.beta_id, "bob-ro", "readonly")["token"]

            self.alpha_actor = service.authenticate(conn, self.alpha_token)
            self.beta_actor = service.authenticate(conn, self.beta_token)

            self.alpha_spec = service.create_spec(
                conn, self.alpha_actor, self.alpha_id, {"channel": "A", "name": "alpha-checkout"}
            )
            self.alpha_v1 = service.add_spec_version(
                conn,
                self.alpha_actor,
                self.alpha_id,
                self.alpha_spec["id"],
                {"repo_path": "github.com/acme/web", "repo_commit": "a" * 40, "params": {"suite": "smoke"}},
            )
            self.alpha_v2 = service.add_spec_version(
                conn,
                self.alpha_actor,
                self.alpha_id,
                self.alpha_spec["id"],
                {"repo_path": "github.com/acme/web", "repo_commit": "b" * 40, "params": {"suite": "smoke", "retries": 2}},
            )
            self.alpha_run = service.create_run(
                conn,
                self.alpha_actor,
                self.alpha_id,
                {"spec_version_id": self.alpha_v1["id"], "commit_sha": "c" * 40, "trigger": "ci"},
            )

            self.beta_spec = service.create_spec(
                conn, self.beta_actor, self.beta_id, {"channel": "B", "name": "beta-eval"}
            )
            self.beta_v1 = service.add_spec_version(
                conn,
                self.beta_actor,
                self.beta_id,
                self.beta_spec["id"],
                {
                    "dataset_version": "eval-2026.10.1",
                    "scorer_version": "scorer-v1",
                    "model_snapshot": {"model": "qwen2.5-7b", "temperature": 0},
                },
            )
            self.beta_v2 = service.add_spec_version(
                conn,
                self.beta_actor,
                self.beta_id,
                self.beta_spec["id"],
                {
                    "dataset_version": "eval-2026.11.1",
                    "scorer_version": "scorer-v2",
                    "model_snapshot": {"model": "qwen2.5-7b", "temperature": 0.2},
                },
            )
            self.beta_run = service.create_run(
                conn,
                self.beta_actor,
                self.beta_id,
                {"spec_version_id": self.beta_v1["id"], "commit_sha": "d" * 40, "trigger": "manual"},
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
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def api(self, method: str, path: str, token: str | None = None, payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                body = response.read().decode("utf-8")
                return response.status, json.loads(body)
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

    def html(self, token: str, extra: str = ""):
        request = urllib.request.Request(
            self.base + f"/ui?token={token}{extra}", headers={"Accept": "text/html"}
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, exc.read().decode("utf-8")
            finally:
                exc.close()


class T01Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = PlatformFixture()

    def tearDown(self) -> None:
        self.fx.close()

    def test_migrations_repeatable_and_core_schema(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aitest-migrate-") as tmp:
            one = str(Path(tmp) / "one.db")
            two = str(Path(tmp) / "two.db")
            first_one = fresh_db(one)
            first_two = fresh_db(two)
            second_one = fresh_db(one)
            second_two = fresh_db(two)

            # A fresh database applies every migration file exactly once and
            # independent databases converge on the same schema.
            expected_migrations = len(migrations.migration_files())
            self.assertEqual(len(first_one["appliedNow"]), expected_migrations)
            self.assertEqual(len(first_two["appliedNow"]), expected_migrations)
            self.assertEqual(
                [item["version"] for item in first_one["appliedNow"]],
                [item["version"] for item in first_two["appliedNow"]],
            )
            self.assertEqual(len(second_one["appliedNow"]), 0)
            self.assertEqual(len(second_two["appliedNow"]), 0)
            self.assertEqual(first_one["schemaDigest"], second_one["schemaDigest"])
            self.assertEqual(first_two["schemaDigest"], second_two["schemaDigest"])
            self.assertEqual(first_one["schemaDigest"], first_two["schemaDigest"])

            conn = db.connect(one)
            try:
                tables = {
                    row["name"]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                    ).fetchall()
                }
                self.assertTrue(CORE_TABLES.issubset(tables), tables)
                self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM project").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM spec_version").fetchone()[0], 0)
            finally:
                conn.close()

    def test_core_foreign_keys_enforced(self) -> None:
        conn = db.connect(self.fx.db_path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO run (id, project_id, spec_version_id, channel, commit_sha, branch,
                                     sampling_config_json, trigger, status, created_at)
                    VALUES ('run_invalid', ?, 'spv_missing', 'A', 'x', 'main', '{}', 'manual', 'queued', 'now')
                    """,
                    (self.fx.alpha_id,),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO spec_version
                      (id, spec_id, project_id, channel, version, params_json, repo_path, repo_commit,
                       created_by, created_at)
                    VALUES ('spv_wrong_project', ?, ?, 'A', 99, '{}', 'repo', 'commit', ?, 'now')
                    """,
                    (self.fx.alpha_spec["id"], self.fx.beta_id, self.fx.beta_actor["id"]),
                )
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            conn.close()

    def test_project_isolation_in_api_and_ui(self) -> None:
        status, body = self.fx.api("GET", f"/projects/{self.fx.alpha_id}/specs", self.fx.alpha_token)
        self.assertEqual(status, 200)
        names = {spec["name"] for spec in body["data"]}
        self.assertEqual(names, {"alpha-checkout"})
        self.assertNotIn("beta-eval", names)

        status, body = self.fx.api("GET", f"/projects/{self.fx.alpha_id}/runs", self.fx.alpha_token)
        self.assertEqual(status, 200)
        run_ids = {run["id"] for run in body["data"]}
        self.assertIn(self.fx.alpha_run["id"], run_ids)
        self.assertNotIn(self.fx.beta_run["id"], run_ids)
        self.assertTrue(all(run["spec_version"] == 1 for run in body["data"]))

        for path in (
            f"/projects/{self.fx.beta_id}/specs",
            f"/projects/{self.fx.beta_id}/runs",
            f"/projects/{self.fx.beta_id}/runs/{self.fx.beta_run['id']}",
        ):
            status, body = self.fx.api("GET", path, self.fx.alpha_token)
            self.assertEqual(status, 404, (path, body))

        status, body = self.fx.api("GET", "/projects", self.fx.alpha_token)
        self.assertEqual(status, 200)
        self.assertEqual([p["id"] for p in body["data"]], [self.fx.alpha_id])

        status, page = self.fx.html(self.fx.alpha_token)
        self.assertEqual(status, 200)
        self.assertIn("Alpha", page)
        self.assertIn("alpha-checkout", page)
        self.assertIn(self.fx.alpha_run["id"], page)
        self.assertNotIn("beta-eval", page)
        self.assertNotIn(self.fx.beta_run["id"], page)

        status, page = self.fx.html(self.fx.beta_token)
        self.assertEqual(status, 200)
        self.assertIn("Beta", page)
        self.assertIn("beta-eval", page)
        self.assertNotIn("alpha-checkout", page)
        self.assertNotIn(self.fx.alpha_run["id"], page)

        status, page = self.fx.html(self.fx.alpha_token, f"&project_id={self.fx.beta_id}")
        self.assertEqual(status, 404)
        self.assertNotIn("beta-eval", page)

    def test_two_tier_permissions(self) -> None:
        new_spec = {"channel": "A", "name": "readonly-must-not-create"}
        status, body = self.fx.api(
            "POST", f"/projects/{self.fx.alpha_id}/specs", self.fx.alpha_ro_token, new_spec
        )
        self.assertEqual(status, 403, body)
        self.assertEqual(body["error"]["code"], "forbidden")

        status, body = self.fx.api(
            "POST",
            f"/projects/{self.fx.alpha_id}/specs/{self.fx.alpha_spec['id']}/versions",
            self.fx.alpha_ro_token,
            {"repo_path": "github.com/acme/web", "repo_commit": "e" * 40},
        )
        self.assertEqual(status, 403, body)

        status, body = self.fx.api(
            "POST",
            f"/projects/{self.fx.alpha_id}/runs",
            self.fx.alpha_ro_token,
            {"spec_version_id": self.fx.alpha_v2["id"], "commit_sha": "f" * 40},
        )
        self.assertEqual(status, 403, body)

        status, body = self.fx.api(
            "GET", f"/projects/{self.fx.alpha_id}/specs", self.fx.alpha_ro_token
        )
        self.assertEqual(status, 200, body)

        status, body = self.fx.api(
            "POST", f"/projects/{self.fx.alpha_id}/specs", self.fx.alpha_token, {"channel": "A", "name": "member-write"}
        )
        self.assertEqual(status, 201, body)
        created_spec_id = body["data"]["id"]

        status, body = self.fx.api(
            "POST",
            f"/projects/{self.fx.alpha_id}/specs/{created_spec_id}/versions",
            self.fx.alpha_token,
            {"repo_path": "github.com/acme/web", "repo_commit": "e" * 40},
        )
        self.assertEqual(status, 201, body)

        status, body = self.fx.api(
            "POST",
            f"/projects/{self.fx.alpha_id}/runs",
            self.fx.alpha_token,
            {"spec_version_id": body["data"]["id"], "commit_sha": "f" * 40},
        )
        self.assertEqual(status, 201, body)

        # The same denied and allowed calls stay stable on repetition.
        status, _ = self.fx.api(
            "POST", f"/projects/{self.fx.alpha_id}/specs", self.fx.alpha_ro_token, {"channel": "A", "name": "repeat-denied"}
        )
        self.assertEqual(status, 403)

    def test_spec_versions_and_run_binding(self) -> None:
        status, spec = self.fx.api(
            "GET", f"/projects/{self.fx.alpha_id}/specs/{self.fx.alpha_spec['id']}", self.fx.alpha_token
        )
        self.assertEqual(status, 200)
        self.assertEqual([v["version"] for v in spec["data"]["versions"]], [1, 2])

        status, run = self.fx.api(
            "GET",
            f"/projects/{self.fx.alpha_id}/runs/{self.fx.alpha_run['id']}",
            self.fx.alpha_token,
        )
        self.assertEqual(status, 200)
        bound = run["data"]["spec_version"]
        self.assertEqual(bound["id"], self.fx.alpha_v1["id"])
        self.assertEqual(bound["version"], 1)
        self.assertEqual(bound["repo_commit"], "a" * 40)

        # Adding a newer version does not change the historical run binding.
        status, page = self.fx.api(
            "GET",
            f"/projects/{self.fx.alpha_id}/runs/{self.fx.alpha_run['id']}",
            self.fx.alpha_token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(page["data"]["spec_version"]["version"], 1)

        status, run_v2 = self.fx.api(
            "POST",
            f"/projects/{self.fx.alpha_id}/runs",
            self.fx.alpha_token,
            {"spec_id": self.fx.alpha_spec["id"], "version": 2, "commit_sha": "1" * 40},
        )
        self.assertEqual(status, 201, run_v2)
        self.assertEqual(run_v2["data"]["spec_version"]["version"], 2)
        self.assertEqual(run_v2["data"]["spec_version"]["repo_commit"], "b" * 40)

        status, body = self.fx.api(
            "POST",
            f"/projects/{self.fx.alpha_id}/runs",
            self.fx.alpha_token,
            {"spec_id": self.fx.alpha_spec["id"], "commit_sha": "2" * 40},
        )
        self.assertEqual(status, 400, body)

        conn = db.connect(self.fx.db_path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE spec_version SET repo_commit = 'tampered' WHERE id = ?",
                    (self.fx.alpha_v1["id"],),
                )
        finally:
            conn.close()

    def test_channel_specific_spec_fields(self) -> None:
        status, alpha_spec = self.fx.api(
            "GET", f"/projects/{self.fx.alpha_id}/specs/{self.fx.alpha_spec['id']}", self.fx.alpha_token
        )
        self.assertEqual(status, 200)
        a_v1 = alpha_spec["data"]["versions"][0]
        self.assertEqual(a_v1["repo_path"], "github.com/acme/web")
        self.assertEqual(a_v1["repo_commit"], "a" * 40)
        self.assertIsNone(a_v1["dataset_version"])
        self.assertIsNone(a_v1["scorer_version"])
        self.assertIsNone(a_v1["model_snapshot"])

        status, beta_spec = self.fx.api(
            "GET", f"/projects/{self.fx.beta_id}/specs/{self.fx.beta_spec['id']}", self.fx.beta_token
        )
        self.assertEqual(status, 200)
        b_v1 = beta_spec["data"]["versions"][0]
        self.assertEqual(b_v1["dataset_version"], "eval-2026.10.1")
        self.assertEqual(b_v1["scorer_version"], "scorer-v1")
        self.assertEqual(b_v1["model_snapshot"], {"model": "qwen2.5-7b", "temperature": 0})
        self.assertIsNone(b_v1["repo_path"])
        self.assertIsNone(b_v1["repo_commit"])

        status, run = self.fx.api(
            "GET", f"/projects/{self.fx.beta_id}/runs/{self.fx.beta_run['id']}", self.fx.beta_token
        )
        self.assertEqual(status, 200)
        bound = run["data"]["spec_version"]
        self.assertEqual(bound["dataset_version"], "eval-2026.10.1")
        self.assertEqual(bound["scorer_version"], "scorer-v1")
        self.assertEqual(bound["model_snapshot"], {"model": "qwen2.5-7b", "temperature": 0})

        status, body = self.fx.api(
            "POST",
            f"/projects/{self.fx.alpha_id}/specs/{self.fx.alpha_spec['id']}/versions",
            self.fx.alpha_token,
            {"repo_path": "github.com/acme/web", "repo_commit": "3" * 40, "dataset_version": "not-allowed"},
        )
        self.assertEqual(status, 400, body)

        status, body = self.fx.api(
            "POST",
            f"/projects/{self.fx.beta_id}/specs/{self.fx.beta_spec['id']}/versions",
            self.fx.beta_token,
            {
                "dataset_version": "eval-x",
                "scorer_version": "scorer-x",
                "model_snapshot": {"model": "qwen2.5-7b"},
                "repo_path": "not-allowed",
            },
        )
        self.assertEqual(status, 400, body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
