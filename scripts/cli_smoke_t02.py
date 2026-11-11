#!/usr/bin/env python3
"""Exercise the T02 CLI on a temporary database and write a redacted log."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REDACT = re.compile(r"(at_[A-Za-z0-9_-]{16,}|rt_[A-Za-z0-9_-]{16,})")
PATH_RE = re.compile(r"/Users/[^\s\"']+")


def redact(text: str) -> str:
    text = REDACT.sub(lambda match: match.group(0)[:8] + "…<redacted>", text)
    return PATH_RE.sub("<local-path>", text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    tmp = tempfile.TemporaryDirectory(prefix="aitest-t02-cli-")
    db = str(Path(tmp.name) / "cli.db")

    def sanitize(text: str) -> str:
        text = text.replace(tmp.name, "<tmp>").replace(str(ROOT), "<repo>")
        return redact(text)
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    lines: list[str] = []
    failures = 0

    def call(*cli_args: str) -> tuple[int, dict | list | None]:
        nonlocal failures
        command = [sys.executable, "-m", "aitest.cli", *cli_args]
        shown = "aitest " + " ".join(sanitize(part) for part in cli_args)
        result = subprocess.run(command, capture_output=True, text=True, env=env)
        lines.append(f"$ {shown}")
        lines.append(f"exit={result.returncode}")
        if result.stdout:
            lines.append(sanitize(result.stdout.rstrip()))
        if result.stderr:
            lines.append("stderr: " + sanitize(result.stderr.rstrip()))
        lines.append("")
        if result.returncode != 0:
            failures += 1
        try:
            return result.returncode, json.loads(result.stdout)
        except json.JSONDecodeError:
            return result.returncode, None

    call("migrate", "--db", db)
    _, project = call("create-project", "--db", db, "--name", "cli-alpha")
    project_id = project["id"]
    _, member = call(
        "add-member", "--db", db, "--project", project_id, "--name", "alice", "--role", "member"
    )
    token = member["token"]
    _, spec = call(
        "create-spec",
        "--db",
        db,
        "--token",
        token,
        "--project",
        project_id,
        "--channel",
        "A",
        "--name",
        "checkout",
    )
    _, version = call(
        "add-spec-version",
        "--db",
        db,
        "--token",
        token,
        "--project",
        project_id,
        "--spec",
        spec["id"],
        "--repo-path",
        "github.com/acme/web",
        "--repo-commit",
        "a" * 40,
    )
    run_args = [
        "create-run",
        "--db",
        db,
        "--token",
        token,
        "--project",
        project_id,
        "--spec-version-id",
        version["id"],
        "--commit-sha",
        "c" * 40,
        "--priority",
        "80",
        "--timeout-seconds",
        "60",
    ]
    call(*run_args)
    call(*run_args)
    call(*run_args, "--force")
    call(
        "set-scheduler",
        "--db",
        db,
        "--token",
        token,
        "--project",
        project_id,
        "--max-concurrency",
        "1",
        "--runner-stale-seconds",
        "30",
    )
    _, runner = call(
        "register-runner",
        "--db",
        db,
        "--token",
        token,
        "--project",
        project_id,
        "--name",
        "cli-runner",
    )
    runner_token = runner["token"]
    call("scheduler-status", "--db", db, "--token", token, "--project", project_id)
    _, claim = call("claim-run", "--db", db, "--runner-token", runner_token)
    claimed_run = claim["data"]["id"]
    call(
        "complete-run",
        "--db",
        db,
        "--runner-token",
        runner_token,
        "--run",
        claimed_run,
        "--status",
        "succeeded",
        "--summary",
        "cli smoke",
    )
    _, created = call(
        "create-run",
        "--db",
        db,
        "--token",
        token,
        "--project",
        project_id,
        "--spec-version-id",
        version["id"],
        "--commit-sha",
        "d" * 40,
    )
    queued_run = created["id"]
    call("cancel-run", "--db", db, "--token", token, "--project", project_id, "--run", queued_run, "--reason", "cli cancel")
    call("run-events", "--db", db, "--token", token, "--project", project_id, "--run", queued_run)
    call("list-runners", "--db", db, "--token", token, "--project", project_id)
    call("heartbeat-runner", "--db", db, "--runner-token", runner_token)
    call("show-run", "--db", db, "--token", token, "--project", project_id, "--run", queued_run)
    call("list-runs", "--db", db, "--token", token, "--project", project_id)

    lines.append(f"commands={len([line for line in lines if line.startswith('$ ')])} failures={failures}")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {out} failures={failures}")
    tmp.cleanup()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
