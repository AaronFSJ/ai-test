#!/usr/bin/env python3
"""Exercise the T03 CLI surface and write a redacted smoke log."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT.parent / "deliverables" / "evidence" / "cli-smoke.txt"))
    args = parser.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="t03-cli-"))
    db = tmp / "cli.db"
    store = tmp / "evidence"
    sample = tmp / "sample.log"
    sample.write_text(
        "Authorization: Bearer abcdefghijklmnop phone=13800138000 "
        "url=https://x.test/a?token=zzz&page=1\n",
        encoding="utf-8",
    )
    env = {**os.environ, "PYTHONPATH": "src"}
    lines: list[str] = []
    token: str | None = None

    def run(label: str, arguments: list[str], truncate: int | None = None):
        nonlocal token
        command = ["python3", "-m", "aitest.cli", *arguments]
        shown = [
            item.replace(token, "$TOKEN") if token and token in item else item
            for item in command
        ]
        shown = [
            item.replace(str(tmp), "$TMP").replace(str(db), "$DB").replace(str(store), "$STORAGE")
            for item in shown
        ]
        proc = subprocess.run(command, cwd=str(ROOT), env=env, capture_output=True, text=True)
        text = proc.stdout.strip()
        if token:
            text = text.replace(token, "$TOKEN")
        text = text.replace(str(tmp), "$TMP").replace(str(db), "$DB").replace(str(store), "$STORAGE")
        if truncate and len(text) > truncate:
            text = text[:truncate] + "\n...[truncated]"
        lines.append(f"$ {' '.join(shown)}\nexit={proc.returncode}\n{text}\n")
        if proc.returncode != 0:
            raise SystemExit(f"command failed: {label}\n{proc.stderr}\n{proc.stdout}")
        stripped = proc.stdout.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            return json.loads(stripped)
        return stripped

    run("migrate", ["migrate", "--db", str(db)], truncate=1200)
    project = run("create-project", ["create-project", "--db", str(db), "--name", "cli-alpha"])
    member = run(
        "add-member",
        ["add-member", "--db", str(db), "--project", project["id"], "--name", "alice", "--role", "member"],
    )
    token = member["token"]
    lines[-1] = lines[-1].replace(token, "$TOKEN")
    spec = run(
        "create-spec",
        ["create-spec", "--db", str(db), "--token", token, "--project", project["id"], "--channel", "B", "--name", "cli-eval"],
    )
    version = run(
        "add-version",
        [
            "add-spec-version", "--db", str(db), "--token", token, "--project", project["id"],
            "--spec", spec["id"], "--dataset-version", "cli-d1", "--scorer-version", "cli-s1",
            "--model-snapshot-json", '{"model":"qwen2.5-7b"}',
        ],
    )
    run_ = run(
        "create-run",
        ["create-run", "--db", str(db), "--token", token, "--project", project["id"], "--spec-version-id", version["id"], "--commit-sha", "c" * 40],
    )
    item = run(
        "add-item",
        ["add-item", "--db", str(db), "--token", token, "--project", project["id"], "--run", run_["id"], "--case-ref", "case-cli", "--state", "fail", "--priority", "P0"],
    )
    run(
        "add-verdict",
        ["add-verdict", "--db", str(db), "--token", token, "--project", project["id"], "--run", run_["id"], "--item", item["id"], "--state", "fail", "--confidence", "0.8"],
    )
    run(
        "add-evidence",
        ["add-evidence", "--db", str(db), "--token", token, "--project", project["id"], "--run", run_["id"], "--item", item["id"], "--kind", "log", "--filename", "cli.log", "--file", str(sample), "--storage-root", str(store)],
        truncate=1800,
    )
    run("list-evidence", ["list-evidence", "--db", str(db), "--token", token, "--project", project["id"], "--run", run_["id"], "--kind", "log"], truncate=2200)
    run("drilldown", ["drilldown", "--db", str(db), "--token", token, "--project", project["id"], "--run", run_["id"], "--item", item["id"]], truncate=2600)
    report = run("create-report", ["create-report", "--db", str(db), "--token", token, "--project", project["id"], "--run", run_["id"], "--storage-root", str(store)], truncate=1200)
    run("show-report", ["show-report", "--db", str(db), "--token", token, "--project", project["id"], "--run", run_["id"], "--snapshot", report["id"], "--content"], truncate=1600)
    run("retention-show", ["retention-show", "--db", str(db), "--token", token, "--project", project["id"]])
    run("retention-set", ["retention-set", "--db", str(db), "--token", token, "--project", project["id"], "--priority", "P1", "--days", "30"])
    run("cleanup-dry-run", ["cleanup-evidence", "--db", str(db), "--token", token, "--project", project["id"], "--dry-run", "--storage-root", str(store)], truncate=1200)
    run("redaction-rules", ["redaction-rules"], truncate=1800)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("T03 CLI smoke (synthetic data; token/paths redacted)\n\n" + "\n".join(lines), encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
