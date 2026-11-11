"""Acceptance tests for T08: dataset management, import, versioning and export."""

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

from aitest import db, http_api, migrations, service  # noqa: E402


class DatasetFixture:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="aitest-t08-")
        self.db_path = str(Path(self.tmp.name) / "platform.db")
        migrations.migrate(self.db_path)
        conn = db.connect(self.db_path)
        try:
            alpha = service.create_project(conn, "Alpha")
            beta = service.create_project(conn, "Beta")
            self.alpha_id = alpha["id"]
            self.beta_id = beta["id"]
            self.alpha_token = service.add_member(conn, self.alpha_id, "alice", "member")["token"]
            self.alpha_ro_token = service.add_member(
                conn, self.alpha_id, "alice-ro", "readonly"
            )["token"]
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

    def api(self, method: str, path: str, token: str | None = None, payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                body = response.read().decode("utf-8")
                return response.status, json.loads(body)
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8")
                try:
                    return exc.code, json.loads(body)
                except json.JSONDecodeError:
                    return exc.code, {"raw": body}
            finally:
                exc.close()

    def text(self, path: str, token: str, method: str = "GET"):
        request = urllib.request.Request(
            self.base + path, headers={"Authorization": f"Bearer {token}"}, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return (
                    response.status,
                    response.read().decode("utf-8"),
                    dict(response.headers.items()),
                )
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, exc.read().decode("utf-8"), dict(exc.headers.items())
            finally:
                exc.close()

    def create_dataset(self, name: str = "eval") -> dict:
        status, body = self.api(
            "POST", f"/projects/{self.alpha_id}/datasets", self.alpha_token, {"name": name}
        )
        assert status == 201, body
        return body["data"]

    def import_content(self, dataset_id: str, content: str, fmt: str = "jsonl", mode: str = "abort"):
        return self.api(
            "POST",
            f"/projects/{self.alpha_id}/datasets/{dataset_id}/import",
            self.alpha_token,
            {"format": fmt, "mode": mode, "content": content},
        )


def jsonl(rows: list[dict]) -> str:
    return "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n"


class T08Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = DatasetFixture()

    def tearDown(self) -> None:
        self.fx.close()

    def test_jsonl_import_100_samples_and_invalid_line_location(self) -> None:
        dataset = self.fx.create_dataset()
        rows = [
            {
                "sample_id": f"s{i:03d}",
                "input": {"question": f"q{i}"},
                "expected": f"a{i}",
                "context": {"lang": "zh"},
                "context_ref": f"kb://faq/{i}",
                "priority": "P0" if i % 2 == 0 else "P1",
                "tags": ["smoke"] if i % 5 == 0 else ["regression"],
            }
            for i in range(1, 101)
        ]
        status, body = self.fx.import_content(dataset["id"], jsonl(rows))
        self.assertEqual(status, 201, body)
        result = body["data"]
        self.assertEqual(result["imported"], 100)
        self.assertEqual(result["dataset_version"]["version"], 1)
        self.assertEqual(result["dataset_version"]["sample_count"], 100)
        self.assertEqual(len(result["dataset_version"]["content_digest"]), 64)

        status, version = self.fx.api(
            "GET",
            f"/projects/{self.fx.alpha_id}/datasets/{dataset['id']}/versions/1",
            self.fx.alpha_token,
        )
        self.assertEqual(status, 200, version)
        self.assertEqual(len(version["data"]["samples"]), 100)
        first = version["data"]["samples"][0]
        self.assertEqual(first["sample_id"], "s001")
        self.assertEqual(first["context_ref"], "kb://faq/1")
        self.assertEqual(first["tags"], ["regression"])

        invalid = "\n".join(
            [
                json.dumps({"sample_id": "bad1", "input": "ok"}),
                '{"sample_id": "bad2", "input": ',
                json.dumps({"sample_id": "bad3", "input": "x", "priority": "P9"}),
                json.dumps({"sample_id": "s001", "input": "duplicate"}),
                json.dumps({"sample_id": "bad5", "input": None}),
            ]
        )
        status, body = self.fx.import_content(dataset["id"], invalid)
        self.assertEqual(status, 422, body)
        details = body["error"]["details"]
        self.assertEqual(details["totalLines"], 5)
        self.assertEqual(details["invalidLines"], 4)
        by_line = {}
        for error in details["errors"]:
            by_line.setdefault(error["line"], []).append(error)
        self.assertIn("invalid JSON", by_line[2][0]["reason"])
        self.assertEqual(by_line[3][0]["field"], "priority")
        self.assertIn("already exists", by_line[4][0]["reason"])
        self.assertIn("must not be null", by_line[5][0]["reason"])

        status, dataset_after = self.fx.api(
            "GET", f"/projects/{self.fx.alpha_id}/datasets/{dataset['id']}", self.fx.alpha_token
        )
        self.assertEqual(status, 200)
        self.assertEqual(dataset_after["data"]["sample_count"], 100)
        self.assertEqual(len(dataset_after["data"]["versions"]), 1)

        status, skipped = self.fx.import_content(
            dataset["id"],
            jsonl(
                [
                    {"sample_id": "ok1", "input": 1},
                    {"sample_id": "ok2", "input": 2, "priority": "P0"},
                    {"sample_id": "ok3", "input": 3},
                ]
            )
            + "{broken\n",
            mode="skip",
        )
        self.assertEqual(status, 201, skipped)
        self.assertEqual(skipped["data"]["imported"], 3)
        self.assertEqual(skipped["data"]["skipped"], 1)
        self.assertEqual(skipped["data"]["errors"][0]["line"], 4)
        self.assertEqual(skipped["data"]["dataset_version"]["version"], 2)
        self.assertEqual(skipped["data"]["dataset_version"]["sample_count"], 103)

    def test_sample_edit_creates_version_and_old_run_keeps_old_samples(self) -> None:
        dataset = self.fx.create_dataset()
        content = jsonl(
            [
                {
                    "sample_id": "s1",
                    "input": "old input",
                    "expected": "expected",
                    "priority": "P0",
                    "tags": ["smoke"],
                },
                {"sample_id": "s2", "input": {"q": 2}, "priority": "P1", "tags": ["reg"]},
            ]
        )
        status, body = self.fx.import_content(dataset["id"], content)
        self.assertEqual(status, 201, body)
        dsv1 = body["data"]["dataset_version"]

        status, spec = self.fx.api(
            "POST",
            f"/projects/{self.fx.alpha_id}/specs",
            self.fx.alpha_token,
            {"channel": "B", "name": "eval-spec"},
        )
        self.assertEqual(status, 201, spec)
        spec_id = spec["data"]["id"]
        status, sv1 = self.fx.api(
            "POST",
            f"/projects/{self.fx.alpha_id}/specs/{spec_id}/versions",
            self.fx.alpha_token,
            {
                "dataset_version": dsv1["id"],
                "scorer_version": "scorer-v1",
                "model_snapshot": {"model": "qwen2.5-7b", "temperature": 0},
            },
        )
        self.assertEqual(status, 201, sv1)
        self.assertEqual(sv1["data"]["dataset_version"], dsv1["id"])

        status, run1 = self.fx.api(
            "POST",
            f"/projects/{self.fx.alpha_id}/runs",
            self.fx.alpha_token,
            {"spec_version_id": sv1["data"]["id"], "commit_sha": "a" * 40, "trigger": "ci"},
        )
        self.assertEqual(status, 201, run1)
        run1_id = run1["data"]["id"]
        self.assertEqual(run1["data"]["dataset_version_detail"]["version"], 1)
        self.assertEqual(run1["data"]["dataset_samples"][0]["input"], "old input")

        status, listing = self.fx.api(
            "GET",
            f"/projects/{self.fx.alpha_id}/datasets/{dataset['id']}/samples",
            self.fx.alpha_token,
        )
        self.assertEqual(status, 200)
        s1 = [sample for sample in listing["data"] if sample["sample_id"] == "s1"][0]
        status, updated = self.fx.api(
            "PATCH",
            f"/projects/{self.fx.alpha_id}/datasets/{dataset['id']}/samples/{s1['id']}",
            self.fx.alpha_token,
            {"input": "new input", "context_ref": "kb://updated/1"},
        )
        self.assertEqual(status, 200, updated)
        self.assertTrue(updated["data"]["changed"])
        self.assertEqual(updated["data"]["dataset_version"]["version"], 2)
        self.assertEqual(updated["data"]["dataset_version"]["sample_count"], 2)

        status, old_run = self.fx.api(
            "GET", f"/projects/{self.fx.alpha_id}/runs/{run1_id}", self.fx.alpha_token
        )
        self.assertEqual(status, 200, old_run)
        self.assertEqual(old_run["data"]["spec_version"]["dataset_version"], dsv1["id"])
        self.assertEqual(old_run["data"]["dataset_version_detail"]["version"], 1)
        self.assertEqual(old_run["data"]["dataset_samples"][0]["input"], "old input")
        self.assertIsNone(old_run["data"]["dataset_samples"][0]["context_ref"])

        status, old_snapshot = self.fx.api(
            "GET",
            f"/projects/{self.fx.alpha_id}/runs/{run1_id}/dataset-samples",
            self.fx.alpha_token,
        )
        self.assertEqual(status, 200, old_snapshot)
        self.assertEqual(old_snapshot["data"]["dataset_version"]["version"], 1)
        self.assertEqual(old_snapshot["data"]["samples"][0]["input"], "old input")

        status, current = self.fx.api(
            "GET", f"/projects/{self.fx.alpha_id}/datasets/{dataset['id']}", self.fx.alpha_token
        )
        self.assertEqual(status, 200)
        current_s1 = [sample for sample in current["data"]["samples"] if sample["sample_id"] == "s1"][0]
        self.assertEqual(current_s1["input"], "new input")
        self.assertEqual(current_s1["context_ref"], "kb://updated/1")

        dsv2 = updated["data"]["dataset_version"]
        status, sv2 = self.fx.api(
            "POST",
            f"/projects/{self.fx.alpha_id}/specs/{spec_id}/versions",
            self.fx.alpha_token,
            {
                "dataset_version": dsv2["id"],
                "scorer_version": "scorer-v1",
                "model_snapshot": {"model": "qwen2.5-7b", "temperature": 0},
            },
        )
        self.assertEqual(status, 201, sv2)
        status, run2 = self.fx.api(
            "POST",
            f"/projects/{self.fx.alpha_id}/runs",
            self.fx.alpha_token,
            {"spec_version_id": sv2["data"]["id"], "commit_sha": "b" * 40},
        )
        self.assertEqual(status, 201, run2)
        self.assertEqual(run2["data"]["dataset_samples"][0]["input"], "new input")

        status, old_again = self.fx.api(
            "GET", f"/projects/{self.fx.alpha_id}/runs/{run1_id}", self.fx.alpha_token
        )
        self.assertEqual(status, 200)
        self.assertEqual(old_again["data"]["dataset_samples"][0]["input"], "old input")

    def test_subset_export_reimport_roundtrip(self) -> None:
        dataset = self.fx.create_dataset("roundtrip")
        rows = [
            {"sample_id": "s1", "input": {"q": 1}, "expected": "a1", "priority": "P0", "tags": ["smoke", "core"]},
            {"sample_id": "s2", "input": {"q": 2}, "expected": "a2", "priority": "P0", "tags": ["smoke"]},
            {"sample_id": "s3", "input": {"q": 3}, "expected": "a3", "priority": "P1", "tags": ["smoke"]},
            {"sample_id": "s4", "input": {"q": 4}, "expected": "a4", "priority": "P0", "tags": ["reg"]},
            {"sample_id": "s5", "input": "plain", "expected": None, "priority": "P1", "tags": ["reg"]},
            {"sample_id": "s6", "input": [1, 2], "expected": {"ok": True}, "priority": "P0", "tags": ["core"]},
        ]
        status, body = self.fx.import_content(dataset["id"], jsonl(rows))
        self.assertEqual(status, 201, body)
        original = body["data"]["dataset_version"]

        path = (
            f"/projects/{self.fx.alpha_id}/datasets/{dataset['id']}/versions/1/export"
            "?tag=smoke&priority=P0&format=jsonl"
        )
        status, content, headers = self.fx.text(path, self.fx.alpha_token)
        self.assertEqual(status, 200, content)
        lower = {key.lower(): value for key, value in headers.items()}
        self.assertEqual(lower["x-sample-count"], "2")
        exported = [json.loads(line) for line in content.splitlines() if line.strip()]
        self.assertEqual([row["sample_id"] for row in exported], ["s1", "s2"])
        subset = self.fx.create_dataset("subset")
        status, reimported = self.fx.import_content(subset["id"], content)
        self.assertEqual(status, 201, reimported)
        reimport_version = reimported["data"]["dataset_version"]
        self.assertEqual(reimport_version["content_digest"], lower["x-sample-digest"])
        self.assertEqual(reimport_version["sample_count"], 2)

        status, reexport, headers2 = self.fx.text(
            f"/projects/{self.fx.alpha_id}/datasets/{subset['id']}/export?format=jsonl",
            self.fx.alpha_token,
        )
        self.assertEqual(status, 200, reexport)
        self.assertEqual(reexport, content)
        lower2 = {key.lower(): value for key, value in headers2.items()}
        self.assertEqual(lower2["x-sample-digest"], lower["x-sample-digest"])

        status, csv_content, csv_headers = self.fx.text(
            f"/projects/{self.fx.alpha_id}/datasets/{dataset['id']}/versions/1/export?format=csv",
            self.fx.alpha_token,
        )
        self.assertEqual(status, 200, csv_content)
        self.assertIn("sample_id,input,expected,context,context_ref,priority,tags", csv_content.splitlines()[0])
        csv_dataset = self.fx.create_dataset("csv-roundtrip")
        status, csv_import = self.fx.import_content(csv_dataset["id"], csv_content, fmt="csv")
        self.assertEqual(status, 201, csv_import)
        self.assertEqual(csv_import["data"]["dataset_version"]["content_digest"], original["content_digest"])
        self.assertEqual(csv_import["data"]["dataset_version"]["sample_count"], 6)

    def test_csv_import_valid_and_invalid_line_location(self) -> None:
        import csv
        import io

        dataset = self.fx.create_dataset("csv-eval")
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(["sample_id", "input", "expected", "context", "context_ref", "priority", "tags"])
        writer.writerow(["c1", '{"q": 1}', "a1", '{"lang": "zh"}', "kb://1", "P0", "smoke,core"])
        writer.writerow(["c2", "bad", "a2", "", "", "P9", "reg"])
        writer.writerow(["c3", "short-row"])
        writer.writerow(["c4", "hello", "", "", "kb://4", "p1", "reg|smoke"])
        status, body = self.fx.import_content(dataset["id"], buffer.getvalue(), fmt="csv")
        self.assertEqual(status, 422, body)
        details = body["error"]["details"]
        self.assertEqual(details["format"], "csv")
        lines = {error["line"] for error in details["errors"]}
        self.assertEqual(lines, {3, 4})

        good = io.StringIO(newline="")
        writer = csv.writer(good, lineterminator="\n")
        writer.writerow(["sample_id", "input", "expected", "context", "context_ref", "priority", "tags"])
        writer.writerow(["c1", '{"q": 1}', "a1", '{"lang": "zh"}', "kb://1", "P0", "smoke,core"])
        writer.writerow(["c4", "hello", "", "", "kb://4", "p1", "reg|smoke"])
        status, body = self.fx.import_content(dataset["id"], good.getvalue(), fmt="csv")
        self.assertEqual(status, 201, body)
        self.assertEqual(body["data"]["imported"], 2)
        status, samples = self.fx.api(
            "GET",
            f"/projects/{self.fx.alpha_id}/datasets/{dataset['id']}/samples",
            self.fx.alpha_token,
        )
        self.assertEqual(status, 200)
        by_key = {sample["sample_id"]: sample for sample in samples["data"]}
        self.assertEqual(by_key["c1"]["input"], {"q": 1})
        self.assertEqual(by_key["c1"]["tags"], ["smoke", "core"])
        self.assertEqual(by_key["c4"]["priority"], "P1")
        self.assertEqual(by_key["c4"]["tags"], ["reg", "smoke"])
        self.assertEqual(by_key["c4"]["context_ref"], "kb://4")

    def test_permissions_isolation_and_grouping(self) -> None:
        dataset = self.fx.create_dataset()
        status, body = self.fx.import_content(
            dataset["id"],
            jsonl(
                [
                    {"sample_id": "a1", "input": "x", "priority": "P0", "tags": ["smoke", "core"]},
                    {"sample_id": "a2", "input": "y", "priority": "P0", "tags": ["core"]},
                    {"sample_id": "a3", "input": "z", "priority": "P1", "tags": ["reg"]},
                ]
            ),
        )
        self.assertEqual(status, 201, body)

        status, denied = self.fx.api(
            "POST",
            f"/projects/{self.fx.alpha_id}/datasets",
            self.fx.alpha_ro_token,
            {"name": "denied"},
        )
        self.assertEqual(status, 403, denied)
        status, member_import = self.fx.import_content(dataset["id"], jsonl([{"input": "x"}]))
        self.assertEqual(status, 201, member_import)
        status, denied = self.fx.api(
            "POST",
            f"/projects/{self.fx.alpha_id}/datasets/{dataset['id']}/import",
            self.fx.alpha_ro_token,
            {"format": "jsonl", "content": jsonl([{"input": "x"}])},
        )
        self.assertEqual(status, 403, denied)
        status, sample_list = self.fx.api(
            "GET",
            f"/projects/{self.fx.alpha_id}/datasets/{dataset['id']}/samples",
            self.fx.alpha_token,
        )
        sample = sample_list["data"][0]
        status, denied = self.fx.api(
            "PATCH",
            f"/projects/{self.fx.alpha_id}/datasets/{dataset['id']}/samples/{sample['id']}",
            self.fx.alpha_ro_token,
            {"priority": "P0"},
        )
        self.assertEqual(status, 403, denied)
        status, denied = self.fx.api(
            "DELETE",
            f"/projects/{self.fx.alpha_id}/datasets/{dataset['id']}/samples/{sample['id']}",
            self.fx.alpha_ro_token,
        )
        self.assertEqual(status, 403, denied)

        status, isolated = self.fx.api(
            "GET", f"/projects/{self.fx.alpha_id}/datasets/{dataset['id']}", self.fx.beta_token
        )
        self.assertEqual(status, 404, isolated)
        status, isolated, _ = self.fx.text(
            f"/projects/{self.fx.alpha_id}/datasets/{dataset['id']}/export", self.fx.beta_token
        )
        self.assertEqual(status, 404, isolated)

        status, stats = self.fx.api(
            "GET",
            f"/projects/{self.fx.alpha_id}/datasets/{dataset['id']}/stats",
            self.fx.alpha_token,
        )
        self.assertEqual(status, 200, stats)
        self.assertEqual(stats["data"]["total"], 4)
        self.assertEqual(stats["data"]["by_priority"], {"P0": 2, "P1": 2})
        self.assertEqual(stats["data"]["by_tag"]["core"], 2)
        self.assertEqual(stats["data"]["by_priority_and_tag"]["P0"]["core"], 2)

        status, filtered = self.fx.api(
            "GET",
            f"/projects/{self.fx.alpha_id}/datasets/{dataset['id']}/samples?tag=core&priority=P0",
            self.fx.alpha_token,
        )
        self.assertEqual(status, 200)
        self.assertEqual({sample["sample_id"] for sample in filtered["data"]}, {"a1", "a2"})

        status, updated = self.fx.api(
            "PATCH",
            f"/projects/{self.fx.alpha_id}/datasets/{dataset['id']}/samples/{sample['id']}",
            self.fx.alpha_token,
            {"expected": "expected", "context_ref": "kb://ctx/1", "priority": "P1"},
        )
        self.assertEqual(status, 200, updated)
        self.assertEqual(updated["data"]["dataset_version"]["version"], 3)
        status, deleted = self.fx.api(
            "DELETE",
            f"/projects/{self.fx.alpha_id}/datasets/{dataset['id']}/samples/{sample['id']}",
            self.fx.alpha_token,
        )
        self.assertEqual(status, 200, deleted)
        self.assertEqual(deleted["data"]["dataset_version"]["version"], 4)
        self.assertEqual(deleted["data"]["dataset_version"]["sample_count"], 3)

        status, conflict = self.fx.api(
            "POST", f"/projects/{self.fx.alpha_id}/datasets", self.fx.alpha_token, {"name": "eval"}
        )
        self.assertEqual(status, 409, conflict)
        status, invalid = self.fx.api(
            "POST",
            f"/projects/{self.fx.alpha_id}/datasets/{dataset['id']}/samples",
            self.fx.alpha_token,
            {"input": "x", "priority": "P2"},
        )
        self.assertEqual(status, 400, invalid)


if __name__ == "__main__":
    unittest.main(verbosity=2)
