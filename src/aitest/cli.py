"""Command line administration entry for the T01 platform skeleton.

Examples:
  python -m aitest.cli migrate --db ./aitest.db
  python -m aitest.cli create-project --db ./aitest.db --name alpha
  python -m aitest.cli add-member --db ./aitest.db --project prj_... --name alice --role member
  python -m aitest.cli serve --db ./aitest.db --port 8080
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from . import db, http_api, migrations, service

DEFAULT_DB = os.environ.get("AITEST_DB", "aitest.db")


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _json_arg(value: str | None, field: str) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise service.ApiError(400, "validation_error", f"{field} must be valid JSON: {exc}") from exc


def _actor(conn, token: str | None) -> dict:
    return service.authenticate(conn, token)


def cmd_migrate(args: argparse.Namespace) -> int:
    _print(migrations.migrate(args.db))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    _print({"database": args.db, "migrations": migrations.migration_status(args.db)})
    return 0


def cmd_create_project(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    try:
        project = service.create_project(
            conn,
            name=args.name,
            default_branch=args.default_branch,
            quota_config=_json_arg(args.quota_json, "quota_json"),
        )
        _print(project)
        return 0
    finally:
        conn.close()


def cmd_list_projects(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    try:
        _print(service.list_projects(conn))
        return 0
    finally:
        conn.close()


def cmd_add_member(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    try:
        member = service.add_member(
            conn,
            project_id=args.project,
            name=args.name,
            role=args.role,
            email=args.email,
        )
        _print(member)
        return 0
    finally:
        conn.close()


def cmd_create_spec(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    try:
        actor = _actor(conn, args.token)
        spec = service.create_spec(
            conn,
            actor,
            args.project,
            {"channel": args.channel, "name": args.name},
        )
        _print(spec)
        return 0
    finally:
        conn.close()


def cmd_add_spec_version(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    try:
        actor = _actor(conn, args.token)
        payload = {
            "params": _json_arg(args.params_json, "params_json") or {},
            "content_ref": args.content_ref,
        }
        if args.repo_path is not None:
            payload["repo_path"] = args.repo_path
        if args.repo_commit is not None:
            payload["repo_commit"] = args.repo_commit
        if args.dataset_version is not None:
            payload["dataset_version"] = args.dataset_version
        if args.scorer_version is not None:
            payload["scorer_version"] = args.scorer_version
        if args.model_snapshot_json is not None:
            payload["model_snapshot"] = _json_arg(args.model_snapshot_json, "model_snapshot_json")
        version = service.add_spec_version(conn, actor, args.project, args.spec, payload)
        _print(version)
        return 0
    finally:
        conn.close()


def cmd_create_run(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    try:
        actor = _actor(conn, args.token)
        payload: dict[str, Any] = {
            "commit_sha": args.commit_sha,
            "branch": args.branch,
            "trigger": args.trigger,
        }
        if args.spec_version_id:
            payload["spec_version_id"] = args.spec_version_id
        if args.spec:
            payload["spec_id"] = args.spec
        if args.version is not None:
            payload["version"] = args.version
        if args.env is not None:
            payload["env"] = args.env
        if args.sampling_config_json is not None:
            payload["sampling_config"] = _json_arg(args.sampling_config_json, "sampling_config_json")
        if args.model_snapshot_json is not None:
            payload["model_snapshot"] = _json_arg(args.model_snapshot_json, "model_snapshot_json")
        if args.baseline_run_id is not None:
            payload["baseline_run_id"] = args.baseline_run_id
        run = service.create_run(conn, actor, args.project, payload)
        _print(run)
        return 0
    finally:
        conn.close()


def cmd_add_item(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    try:
        actor = _actor(conn, args.token)
        payload = {
            "case_ref": args.case_ref,
            "state": args.state,
            "confidence": args.confidence,
            "duration_ms": args.duration_ms,
        }
        item = service.add_run_item(conn, actor, args.project, args.run, payload)
        _print(item)
        return 0
    finally:
        conn.close()


def cmd_list_specs(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    try:
        actor = _actor(conn, args.token)
        _print(service.list_specs(conn, actor, args.project))
        return 0
    finally:
        conn.close()


def cmd_list_runs(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    try:
        actor = _actor(conn, args.token)
        _print(service.list_runs(conn, actor, args.project))
        return 0
    finally:
        conn.close()


def cmd_show_run(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    try:
        actor = _actor(conn, args.token)
        service.require_project(actor, args.project)
        _print(service.get_run(conn, args.project, args.run))
        return 0
    finally:
        conn.close()


def cmd_serve(args: argparse.Namespace) -> int:
    http_api.serve(args.db, host=args.host, port=args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aitest", description="AI test platform T01 admin CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("migrate", help="apply pending migrations")
    p.add_argument("--db", default=DEFAULT_DB)
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("status", help="show applied migrations")
    p.add_argument("--db", default=DEFAULT_DB)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("create-project", help="create a project (local admin action)")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--name", required=True)
    p.add_argument("--default-branch", default="main")
    p.add_argument("--quota-json", default=None)
    p.set_defaults(func=cmd_create_project)

    p = sub.add_parser("list-projects", help="list projects")
    p.add_argument("--db", default=DEFAULT_DB)
    p.set_defaults(func=cmd_list_projects)

    p = sub.add_parser("add-member", help="create a member and print its token once")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--project", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--role", choices=service.ROLES, required=True)
    p.add_argument("--email", default=None)
    p.set_defaults(func=cmd_add_member)

    p = sub.add_parser("create-spec", help="create a spec")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--token", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--channel", choices=service.CHANNELS, required=True)
    p.add_argument("--name", required=True)
    p.set_defaults(func=cmd_create_spec)

    p = sub.add_parser("add-spec-version", help="append an immutable spec version")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--token", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--spec", required=True)
    p.add_argument("--params-json", default=None)
    p.add_argument("--content-ref", default=None)
    p.add_argument("--repo-path", default=None)
    p.add_argument("--repo-commit", default=None)
    p.add_argument("--dataset-version", default=None)
    p.add_argument("--scorer-version", default=None)
    p.add_argument("--model-snapshot-json", default=None)
    p.set_defaults(func=cmd_add_spec_version)

    p = sub.add_parser("create-run", help="create a run bound to an explicit spec version")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--token", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--spec-version-id", default=None)
    p.add_argument("--spec", default=None)
    p.add_argument("--version", type=int, default=None)
    p.add_argument("--commit-sha", required=True)
    p.add_argument("--branch", default=None)
    p.add_argument("--env", default=None)
    p.add_argument("--trigger", choices=service.TRIGGERS, default="manual")
    p.add_argument("--sampling-config-json", default=None)
    p.add_argument("--model-snapshot-json", default=None)
    p.add_argument("--baseline-run-id", default=None)
    p.set_defaults(func=cmd_create_run)

    p = sub.add_parser("add-item", help="add a run item")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--token", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--run", required=True)
    p.add_argument("--case-ref", required=True)
    p.add_argument("--state", choices=service.RUN_STATES, required=True)
    p.add_argument("--confidence", type=float, default=None)
    p.add_argument("--duration-ms", type=int, default=None)
    p.set_defaults(func=cmd_add_item)

    p = sub.add_parser("list-specs", help="list specs in a project")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--token", required=True)
    p.add_argument("--project", required=True)
    p.set_defaults(func=cmd_list_specs)

    p = sub.add_parser("list-runs", help="list runs in a project")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--token", required=True)
    p.add_argument("--project", required=True)
    p.set_defaults(func=cmd_list_runs)

    p = sub.add_parser("show-run", help="show run detail including spec_version")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--token", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--run", required=True)
    p.set_defaults(func=cmd_show_run)

    p = sub.add_parser("serve", help="run the HTTP API and minimal HTML view")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except service.ApiError as exc:
        _print({"error": {"status": exc.status, "code": exc.code, "message": exc.message}})
        return 2
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        _print({"error": {"code": "cli_error", "message": str(exc)}})
        return 3


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
