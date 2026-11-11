"""Acceptance tests for T03: evidence storage, redaction, retention and snapshots."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aitest import db, evidence, http_api, migrations, redaction, reports, service, storage  # noqa: E402

SAMPLE_LOG = "\n".join(
    [
        "2026-11-11T09:00:00Z INFO login user=alice",
        "Authorization: Bearer " + "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
        "contact=13800138000 email=alice@example.com",
        "GET https://files.example.com/report.pdf?X-Amz-Credential=AKIAIOSFODNN7EXAMPLE&X-Amz-Signature=deadbeefcafe&token=abc123&page=2",
        '{"access_token": "sk-live-very-secret", "password": "hunter2", "tokens_in": 100}',
    ]
)


def _iso(delta_days: int = 0) -> str:
    moment = datetime.now(timezone.utc) + timedelta(days=delta_days)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


class T03Fixture:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="aitest-t03-")
        self.root = Path(self.tmp.name)
        self.db_path = str(self.root / "platform.db")
        self.store = storage.ObjectStore(self.root / "objects")
        migrations.migrate(self.db_path)
        self.conn = db.connect(self.db_path)
        alpha = service.create_project(self.conn, "Alpha")
        beta = service.create_project(self.conn, "Beta")
        self.alpha_id = alpha["id"]
        self.beta_id = beta["id"]
        self.alpha_token = service.add_member(self.conn, self.alpha_id, "alice", "member")["token"]
        self.alpha_ro_token = service.add_member(self.conn, self.alpha_id, "alice-ro", "readonly")["token"]
        self.beta_token = service.add_member(self.conn, self.beta_id, "bob", "member")["token"]
        self.actor = service.authenticate(self.conn, self.alpha_token)
        self.ro_actor = service.authenticate(self.conn, self.alpha_ro_token)
        self.beta_actor = service.authenticate(self.conn, self.beta_token)
        self.spec = service.create_spec(self.conn, self.actor, self.alpha_id, {"channel": "B", "name": "alpha-eval"})
        self.v1 = service.add_spec_version(
            self.conn,
            self.actor,
            self.alpha_id,
            self.spec["id"],
            {
                "dataset_version": "eval-2026.11.1",
                "scorer_version": "scorer-v1",
                "model_snapshot": {"model": "qwen2.5-7b", "temperature": 0, "api_key": "sk-config-secret"},
            },
        )
        self.run = service.create_run(
            self.conn,
            self.actor,
            self.alpha_id,
            {"spec_version_id": self.v1["id"], "commit_sha": "c" * 40, "trigger": "ci"},
        )
        self.item_p0 = service.add_run_item(
            self.conn,
            self.actor,
            self.alpha_id,
            self.run["id"],
            {"case_ref": "case-p0", "state": "fail", "priority": "P0", "confidence": 0.9},
        )
        self.item_p1 = service.add_run_item(
            self.conn,
            self.actor,
            self.alpha_id,
            self.run["id"],
            {"case_ref": "case-p1", "state": "uncertain", "priority": "P1"},
        )

    def close(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def add_log(self, item_id: str, content: str = SAMPLE_LOG, filename: str = "run.log") -> dict:
        return evidence.add_evidence(
            self.conn,
            self.store,
            self.actor,
            self.alpha_id,
            self.run["id"],
            item_id,
            {"kind": "log", "filename": filename, "content": content},
        )


class RedactionTests(unittest.TestCase):
    def test_rule_inventory_covers_credentials_and_personal_data(self) -> None:
        rules = redaction.rules_inventory()
        ids = {rule["id"] for rule in rules}
        categories = {rule["category"] for rule in rules}
        for required in (
            "url_query_secret",
            "authorization_header",
            "secret_key_value",
            "phone_cn",
            "email",
            "json_sensitive_key",
        ):
            self.assertIn(required, ids)
        self.assertEqual({"credential", "personal_data"}, categories)
        self.assertTrue(all(rule["description"] for rule in rules))

    def test_sample_log_is_redacted_before_storage(self) -> None:
        redacted, report = redaction.redact_bytes(SAMPLE_LOG.encode("utf-8"), "text/plain")
        text = redacted.decode("utf-8")
        for secret in ("eyJhbGciOiJIUzI1NiJ9", "13800138000", "deadbeefcafe", "abc123", "hunter2", "sk-live-very-secret"):
            self.assertNotIn(secret, text)
        self.assertIn("[REDACTED", text)
        self.assertIn("page=2", text)  # benign parameters survive
        self.assertTrue(report["redacted"])
        self.assertGreaterEqual(report["matches"]["phone_cn"], 1)
        self.assertGreaterEqual(report["matches"]["url_query_secret"], 2)
        self.assertIn("authorization_header", report["matches"])

    def test_json_evidence_redacts_sensitive_keys_and_numeric_pii(self) -> None:
        payload = json.dumps({"token": "secret-token", "phone": 13900139000, "tokens_in": 7}).encode()
        redacted, report = redaction.redact_bytes(payload, "application/json")
        parsed = json.loads(redacted)
        self.assertEqual(parsed["token"], "[REDACTED:JSON_KEY]")
        self.assertEqual(parsed["phone"], "[REDACTED:PHONE]")
        self.assertEqual(parsed["tokens_in"], 7)
        self.assertEqual(report["mode"], "json")


class EvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = T03Fixture()

    def tearDown(self) -> None:
        self.fx.close()

    def test_metadata_search_and_digest_after_redaction(self) -> None:
        log = self.fx.add_log(self.fx.item_p0["id"])
        request = evidence.add_evidence(
            self.fx.conn, self.fx.store, self.fx.actor, self.fx.alpha_id,
            self.fx.run["id"], self.fx.item_p0["id"],
            {"kind": "request", "filename": "request.json", "content": json.dumps({"api_key": "abc", "url": "https://x/y"}), "content_type": "application/json"},
        )
        shot = evidence.add_evidence(
            self.fx.conn, self.fx.store, self.fx.actor, self.fx.alpha_id,
            self.fx.run["id"], self.fx.item_p0["id"],
            {"kind": "screenshot", "filename": "page.png", "content_base64": "iVBORw0KGgo=", "content_type": "image/png"},
        )
        by_run = evidence.list_evidence(self.fx.conn, self.fx.actor, self.fx.alpha_id, run_id=self.fx.run["id"])
        self.assertEqual({record["id"] for record in by_run}, {log["id"], request["id"], shot["id"]})
        by_item = evidence.list_evidence(
            self.fx.conn, self.fx.actor, self.fx.alpha_id, item_id=self.fx.item_p0["id"]
        )
        self.assertEqual(len(by_item), 3)
        by_kind = evidence.list_evidence(
            self.fx.conn, self.fx.actor, self.fx.alpha_id, kind="request"
        )
        self.assertEqual([record["id"] for record in by_kind], [request["id"]])
        by_name = evidence.list_evidence(
            self.fx.conn, self.fx.actor, self.fx.alpha_id, filename_like="page"
        )
        self.assertEqual([record["id"] for record in by_name], [shot["id"]])
        self.assertFalse(log["redaction"]["manualReviewRequired"])
        self.assertTrue(shot["redaction"]["manualReviewRequired"])

        fetched = evidence.read_evidence_content(
            self.fx.conn, self.fx.store, self.fx.actor, self.fx.alpha_id,
            self.fx.run["id"], self.fx.item_p0["id"], log["id"],
        )
        self.assertEqual(hashlib.sha256(fetched["content"]).hexdigest(), log["content_sha256"])
        self.assertNotIn(b"13800138000", fetched["content"])
        self.assertTrue(log["uri"].startswith("object://projects/"))
        self.assertIn(f"/runs/{self.fx.run['id']}/", log["uri"])

    def test_drilldown_from_verdict_fetches_every_evidence_file(self) -> None:
        verdict = service.add_verdict(
            self.fx.conn, self.fx.actor, self.fx.alpha_id, self.fx.run["id"],
            self.fx.item_p0["id"],
            {"state": "fail", "confidence": 0.82, "scorer_trace": {"reason": "value mismatch", "api_key": "hidden"}},
        )
        self.assertEqual(verdict["scorer_trace"]["api_key"], "[REDACTED:JSON_KEY]")
        self.fx.add_log(self.fx.item_p0["id"])
        second = evidence.add_evidence(
            self.fx.conn, self.fx.store, self.fx.actor, self.fx.alpha_id,
            self.fx.run["id"], self.fx.item_p0["id"],
            {"kind": "dom", "filename": "dom.html", "content": "<html>phone 13800138000</html>"},
        )
        drill = evidence.drilldown(
            self.fx.conn, self.fx.ro_actor, self.fx.alpha_id, self.fx.run["id"], self.fx.item_p0["id"]
        )
        self.assertEqual(drill["verdict"]["id"], verdict["id"])
        self.assertEqual(drill["counts"]["evidence"], 2)
        self.assertEqual(set(drill["counts"]["byKind"]), {"log", "dom"})
        for record in drill["evidence"]:
            fetched = evidence.read_evidence_content(
                self.fx.conn, self.fx.store, self.fx.ro_actor, self.fx.alpha_id,
                self.fx.run["id"], self.fx.item_p0["id"], record["id"],
            )
            self.assertEqual(hashlib.sha256(fetched["content"]).hexdigest(), record["content_sha256"])
        dom = evidence.read_evidence_content(
            self.fx.conn, self.fx.store, self.fx.ro_actor, self.fx.alpha_id,
            self.fx.run["id"], self.fx.item_p0["id"], second["id"],
        )
        self.assertNotIn(b"13800138000", dom["content"])

    def test_project_isolation_and_two_tier_permissions(self) -> None:
        record = self.fx.add_log(self.fx.item_p0["id"])
        with self.assertRaises(service.ApiError) as cross:
            evidence.read_evidence_content(
                self.fx.conn, self.fx.store, self.fx.beta_actor, self.fx.alpha_id,
                self.fx.run["id"], self.fx.item_p0["id"], record["id"],
            )
        self.assertEqual(cross.exception.status, 404)
        with self.assertRaises(service.ApiError) as upload:
            evidence.add_evidence(
                self.fx.conn, self.fx.store, self.fx.ro_actor, self.fx.alpha_id,
                self.fx.run["id"], self.fx.item_p0["id"],
                {"kind": "log", "content": "readonly should not write"},
            )
        self.assertEqual(upload.exception.status, 403)
        listed = evidence.list_evidence(self.fx.conn, self.fx.ro_actor, self.fx.alpha_id)
        self.assertEqual(len(listed), 1)
        with self.assertRaises(service.ApiError) as bad_kind:
            evidence.add_evidence(
                self.fx.conn, self.fx.store, self.fx.actor, self.fx.alpha_id,
                self.fx.run["id"], self.fx.item_p0["id"], {"kind": "unknown", "content": "x"},
            )
        self.assertEqual(bad_kind.exception.status, 400)

    def test_database_guard_rejects_cross_project_evidence(self) -> None:
        with self.assertRaises(sqlite3.DatabaseError):
            self.fx.conn.execute(
                """
                INSERT INTO evidence (id, run_item_id, kind, uri, created_at, project_id, run_id)
                VALUES ('evd_cross', ?, 'log', 'object://x', ?, ?, ?)
                """,
                (self.fx.item_p0["id"], _iso(0), self.fx.beta_id, self.fx.run["id"]),
            )
        with self.assertRaises(sqlite3.DatabaseError):
            self.fx.conn.execute(
                """
                INSERT INTO evidence
                  (id, run_item_id, kind, uri, created_at, project_id, run_id, created_by)
                VALUES ('evd_foreign_author', ?, 'log', 'object://x', ?, ?, ?, ?)
                """,
                (
                    self.fx.item_p0["id"], _iso(0), self.fx.alpha_id,
                    self.fx.run["id"], self.fx.beta_actor["id"],
                ),
            )


class RetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = T03Fixture()

    def tearDown(self) -> None:
        self.fx.close()

    def test_defaults_are_readable_and_configurable(self) -> None:
        policy = evidence.get_retention_policy(self.fx.conn, self.fx.actor, self.fx.alpha_id)
        self.assertEqual(policy["policies"]["P0"]["retain_days"], 90)
        self.assertEqual(policy["policies"]["P1"]["retain_days"], 30)
        self.assertEqual(policy["policies"]["P0"]["source"], "default")
        updated = evidence.set_retention_policy(
            self.fx.conn, self.fx.actor, self.fx.alpha_id, {"P0": 5, "P1": 2}
        )
        self.assertEqual(updated["policies"]["P0"]["retain_days"], 5)
        self.assertEqual(updated["policies"]["P1"]["source"], "configured")

    def test_expiry_is_applied_at_ingest_and_cleanup_is_recorded(self) -> None:
        evidence.set_retention_policy(
            self.fx.conn, self.fx.actor, self.fx.alpha_id, {"P0": 0, "P1": 365}
        )
        expired = self.fx.add_log(self.fx.item_p0["id"], filename="expired.log")
        live = self.fx.add_log(self.fx.item_p1["id"], filename="live.log")
        self.assertEqual(expired["retention_class"], "P0")
        self.assertEqual(live["retention_class"], "P1")
        self.assertLessEqual(expired["retention_expires_at"], _iso(1))
        self.assertGreater(live["retention_expires_at"], _iso(30))

        dry = evidence.cleanup_expired_evidence(
            self.fx.conn, self.fx.store, self.fx.actor, self.fx.alpha_id,
            cutoff=_iso(1), dry_run=True,
        )
        self.assertEqual(dry["matched"], 1)
        self.assertEqual(dry["deleted"], 0)
        self.assertTrue(self.fx.store.exists(expired["uri"].removeprefix("object://")))

        applied = evidence.cleanup_expired_evidence(
            self.fx.conn, self.fx.store, self.fx.actor, self.fx.alpha_id, cutoff=_iso(1)
        )
        self.assertEqual(applied["matched"], 1)
        self.assertEqual(applied["deleted"], 1)
        self.assertTrue(applied["records"][0]["object_removed"])
        self.assertFalse(self.fx.store.exists(expired["uri"].removeprefix("object://")))
        with self.assertRaises(service.ApiError) as gone:
            evidence.read_evidence_content(
                self.fx.conn, self.fx.store, self.fx.actor, self.fx.alpha_id,
                self.fx.run["id"], self.fx.item_p0["id"], expired["id"],
            )
        self.assertEqual(gone.exception.status, 410)
        still_there = evidence.read_evidence_content(
            self.fx.conn, self.fx.store, self.fx.actor, self.fx.alpha_id,
            self.fx.run["id"], self.fx.item_p1["id"], live["id"],
        )
        self.assertIn(b"REDACTED", still_there["content"])
        log = evidence.list_cleanup_log(self.fx.conn, self.fx.actor, self.fx.alpha_id)
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["evidence_id"], expired["id"])
        self.assertEqual(log[0]["reason"], "expired")
        again = evidence.cleanup_expired_evidence(
            self.fx.conn, self.fx.store, self.fx.actor, self.fx.alpha_id, cutoff=_iso(1)
        )
        self.assertEqual(again["matched"], 0)
        tombstone = evidence.get_evidence(
            self.fx.conn, self.fx.actor, self.fx.alpha_id, self.fx.run["id"],
            self.fx.item_p0["id"], expired["id"],
        )
        self.assertTrue(tombstone["deleted"])


class ReportSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = T03Fixture()

    def tearDown(self) -> None:
        self.fx.close()

    def _snapshot(self) -> tuple[dict, bytes]:
        service.add_verdict(
            self.fx.conn, self.fx.actor, self.fx.alpha_id, self.fx.run["id"],
            self.fx.item_p0["id"], {"state": "fail", "confidence": 0.9, "scorer_trace": {"score": 0.1}},
        )
        self.fx.add_log(self.fx.item_p0["id"])
        snapshot = reports.create_report_snapshot(
            self.fx.conn, self.fx.store, self.fx.actor, self.fx.alpha_id, self.fx.run["id"]
        )
        content = reports.read_report_content(
            self.fx.conn, self.fx.actor, self.fx.alpha_id, self.fx.run["id"], snapshot["id"]
        )
        return snapshot, content["content"]

    def test_snapshot_is_unchanged_after_scorer_config_change(self) -> None:
        snapshot, before = self._snapshot()
        self.assertEqual(hashlib.sha256(before).hexdigest(), snapshot["content_sha256"])
        parsed = json.loads(before)
        self.assertEqual(parsed["configSnapshot"]["scorerVersion"], "scorer-v1")
        self.assertEqual(parsed["configSnapshot"]["modelSnapshot"]["model"], "qwen2.5-7b")
        self.assertEqual(parsed["summary"]["counts"]["fail"], 1)
        self.assertEqual(parsed["summary"]["evidenceCount"], 1)

        service.add_spec_version(
            self.fx.conn, self.fx.actor, self.fx.alpha_id, self.fx.spec["id"],
            {
                "dataset_version": "eval-2026.12.1",
                "scorer_version": "scorer-v2",
                "model_snapshot": {"model": "qwen3-32b", "temperature": 0.2},
            },
        )
        evidence.set_retention_policy(self.fx.conn, self.fx.actor, self.fx.alpha_id, {"P0": 7})

        after = reports.read_report_content(
            self.fx.conn, self.fx.actor, self.fx.alpha_id, self.fx.run["id"], snapshot["id"]
        )["content"]
        self.assertEqual(before, after)
        self.assertEqual(hashlib.sha256(after).hexdigest(), snapshot["content_sha256"])
        listed = reports.list_report_snapshots(
            self.fx.conn, self.fx.actor, self.fx.alpha_id, self.fx.run["id"]
        )
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["content_sha256"], snapshot["content_sha256"])

    def test_snapshot_rows_are_immutable_and_retention_does_not_shrink_them(self) -> None:
        snapshot, before = self._snapshot()
        with self.assertRaises(sqlite3.DatabaseError):
            self.fx.conn.execute(
                "UPDATE report_snapshot SET content_json = '{}' WHERE id = ?", (snapshot["id"],)
            )
        with self.assertRaises(sqlite3.DatabaseError):
            self.fx.conn.execute("DELETE FROM report_snapshot WHERE id = ?", (snapshot["id"],))
        with self.assertRaises(sqlite3.DatabaseError):
            self.fx.conn.execute(
                """
                INSERT INTO report_snapshot
                  (id, project_id, run_id, spec_version_id, report_version,
                   content_json, content_sha256, storage_key, created_by, created_at)
                VALUES ('rpt_wrong_binding', ?, ?, 'spv_wrong', 99, '{}', ?, 'x', ?, ?)
                """,
                (
                    self.fx.alpha_id, self.fx.run["id"], "0" * 64,
                    self.fx.actor["id"], _iso(0),
                ),
            )
        evidence.set_retention_policy(self.fx.conn, self.fx.actor, self.fx.alpha_id, {"P0": 0})
        evidence.cleanup_expired_evidence(
            self.fx.conn, self.fx.store, self.fx.actor, self.fx.alpha_id, cutoff=_iso(1)
        )
        after = reports.read_report_content(
            self.fx.conn, self.fx.actor, self.fx.alpha_id, self.fx.run["id"], snapshot["id"]
        )["content"]
        self.assertEqual(before, after)
        self.assertEqual(json.loads(after)["summary"]["evidenceCount"], 1)

    def test_snapshot_redacts_credentials_in_config(self) -> None:
        snapshot, before = self._snapshot()
        parsed = json.loads(before)
        self.assertEqual(parsed["configSnapshot"]["modelSnapshot"]["api_key"], "[REDACTED:JSON_KEY]")
        self.assertTrue(parsed["snapshotRedaction"]["redacted"])
        self.assertNotIn(b"sk-config-secret", before)
        self.assertNotIn(b"sk-config-secret", self.fx.store.get(snapshot["storage_key"]))


class HttpIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = T03Fixture()
        self.server = http_api.make_server(
            self.fx.db_path, port=0, storage_root=self.fx.store.root
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.fx.close()

    def call(self, method: str, path: str, token: str | None = None, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"} if data is not None else {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                body = response.read()
                try:
                    return response.status, json.loads(body)
                except json.JSONDecodeError:
                    return response.status, body
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read()
                try:
                    return exc.code, json.loads(body)
                except json.JSONDecodeError:
                    return exc.code, body
            finally:
                exc.close()

    def test_http_drilldown_fetch_and_report_snapshot(self) -> None:
        pid, run_id, item_id, token = (
            self.fx.alpha_id, self.fx.run["id"], self.fx.item_p0["id"], self.fx.alpha_token
        )
        status, verdict = self.call(
            "POST",
            f"/projects/{pid}/runs/{run_id}/items/{item_id}/verdict",
            token,
            {"state": "fail", "confidence": 0.7, "scorer_trace": {"api_key": "hidden"}},
        )
        self.assertEqual(status, 201)
        status, stored = self.call(
            "POST",
            f"/projects/{pid}/runs/{run_id}/items/{item_id}/evidence",
            token,
            {"kind": "log", "filename": "run.log", "content": SAMPLE_LOG},
        )
        self.assertEqual(status, 201)
        self.assertTrue(stored["data"]["redacted"])

        status, drill = self.call(
            "GET", f"/projects/{pid}/runs/{run_id}/items/{item_id}/drilldown", token
        )
        self.assertEqual(status, 200)
        self.assertEqual(drill["data"]["verdict"]["id"], verdict["data"]["id"])
        self.assertEqual(len(drill["data"]["evidence"]), 1)
        content_path = drill["data"]["evidence"][0]["contentPath"]
        status, raw = self.call("GET", content_path, token)
        self.assertEqual(status, 200)
        self.assertIsInstance(raw, bytes)
        self.assertNotIn(b"13800138000", raw)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), stored["data"]["content_sha256"])

        status, search = self.call(
            "GET", f"/projects/{pid}/evidence?run_id={run_id}&kind=log&filename=run", token
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(search["data"]), 1)

        status, created = self.call("POST", f"/projects/{pid}/runs/{run_id}/reports", token)
        self.assertEqual(status, 201)
        snapshot_id = created["data"]["id"]
        status, first = self.call(
            "GET", f"/projects/{pid}/runs/{run_id}/reports/{snapshot_id}/content", token
        )
        self.assertEqual(status, 200)
        service.add_spec_version(
            self.fx.conn, self.fx.actor, self.fx.alpha_id, self.fx.spec["id"],
            {"dataset_version": "v2", "scorer_version": "scorer-v2", "model_snapshot": {"model": "other"}},
        )
        status, second = self.call(
            "GET", f"/projects/{pid}/runs/{run_id}/reports/{snapshot_id}/content", token
        )
        self.assertEqual(status, 200)
        self.assertEqual(first, second)

        status, policy = self.call("GET", f"/projects/{pid}/retention", token)
        self.assertEqual(status, 200)
        self.assertEqual(policy["data"]["policies"]["P0"]["retain_days"], 90)
        status, updated = self.call(
            "POST", f"/projects/{pid}/retention", token, {"P0": 0, "P1": 365}
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["data"]["policies"]["P0"]["retain_days"], 0)
        status, cleanup = self.call(
            "POST", f"/projects/{pid}/evidence/cleanup", token,
            {"cutoff": _iso(1), "dry_run": False},
        )
        self.assertEqual(status, 200)
        # Ingested under the default 90-day policy, so a later policy change is
        # not retroactive and this evidence is not expired yet.
        self.assertEqual(cleanup["data"]["matched"], 0)
        status, rules = self.call("GET", f"/projects/{pid}/redaction-rules", token)
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(rules["data"]["rules"]), 10)

    def test_readonly_can_read_but_not_write_over_http(self) -> None:
        pid, run_id, item_id = self.fx.alpha_id, self.fx.run["id"], self.fx.item_p0["id"]
        status, _ = self.call(
            "POST",
            f"/projects/{pid}/runs/{run_id}/items/{item_id}/evidence",
            self.fx.alpha_ro_token,
            {"kind": "log", "content": "nope"},
        )
        self.assertEqual(status, 403)
        status, _ = self.call(
            "GET", f"/projects/{pid}/runs/{run_id}/items/{item_id}/evidence", self.fx.alpha_ro_token
        )
        self.assertEqual(status, 200)
        status, _ = self.call(
            "GET", f"/projects/{pid}/runs/{run_id}/items/{item_id}/evidence", self.fx.beta_token
        )
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
