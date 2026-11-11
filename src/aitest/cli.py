"""Command line administration entry for the platform skeleton and T08 datasets.

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
from pathlib import Path
from typing import Any

from . import datasets, db, http_api, migrations, service

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


def cmd_dataset_create(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    try:
        actor = _actor(conn, args.token)
        dataset = datasets.create_dataset(
            conn, actor, args.project, {"name": args.name, "description": args.description}
        )
        _print(dataset)
        return 0
    finally:
        conn.close()


def cmd_dataset_list(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    try:
        actor = _actor(conn, args.token)
        _print(datasets.list_datasets(conn, actor, args.project))
        return 0
    finally:
        conn.close()


def cmd_dataset_show(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    try:
        actor = _actor(conn, args.token)
        if args.version is not None:
            _print(
                datasets.get_dataset_version(
                    conn, actor, args.project, args.dataset, args.version
                )
            )
        else:
            _print(
                datasets.get_dataset(
                    conn,
                    actor,
                    args.project,
                    args.dataset,
                    include_samples=not args.no_samples,
                )
            )
        return 0
    finally:
        conn.close()


def cmd_dataset_import(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    try:
        actor = _actor(conn, args.token)
        try:
            content = Path(args.file).read_text(encoding="utf-8")
        except OSError as exc:
            raise service.ApiError(400, "file_error", f"cannot read import file: {exc}") from exc
        fmt = args.format
        if fmt is None:
            fmt = "csv" if args.file.lower().endswith(".csv") else "jsonl"
        result = datasets.import_samples(
            conn,
            actor,
            args.project,
            args.dataset,
            {"format": fmt, "mode": args.mode, "content": content},
        )
        _print(result)
        return 0
    finally:
        conn.close()


def cmd_dataset_export(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    try:
        actor = _actor(conn, args.token)
        tags = args.tag or []
        priorities = args.priority or []
        if args.version is not None:
            result = datasets.export_version(
                conn,
                actor,
                args.project,
                args.dataset,
                args.version,
                fmt=args.format,
                tags=tags,
                priorities=priorities,
                all_tags=args.all_tags,
            )
        else:
            result = datasets.export_latest(
                conn,
                actor,
                args.project,
                args.dataset,
                fmt=args.format,
                tags=tags,
                priorities=priorities,
                all_tags=args.all_tags,
            )
        output = Path(args.out)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(result["content"], encoding="utf-8")
        summary = {
            key: result[key]
            for key in (
                "format",
                "dataset_id",
                "dataset_name",
                "dataset_version_id",
                "version",
                "sampleCount",
                "sampleDigest",
                "contentSha256",
                "filters",
            )
        }
        summary["path"] = str(output)
        summary["bytes"] = len(result["content"].encode("utf-8"))
        _print(summary)
        return 0
    finally:
        conn.close()


def cmd_dataset_stats(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    try:
        actor = _actor(conn, args.token)
        _print(datasets.dataset_stats(conn, actor, args.project, args.dataset))
        return 0
    finally:
        conn.close()


def _sample_payload(args: argparse.Namespace, *, include_key: bool) -> dict:
    payload: dict[str, Any] = {}
    if include_key and args.sample_key is not None:
        payload["sample_id"] = args.sample_key
    if args.input_json is not None:
        payload["input"] = _json_arg(args.input_json, "input_json")
    if args.expected_json is not None:
        payload["expected"] = _json_arg(args.expected_json, "expected_json")
    if args.context_json is not None:
        payload["context"] = _json_arg(args.context_json, "context_json")
    if args.context_ref is not None:
        payload["context_ref"] = args.context_ref
    if args.priority is not None:
        payload["priority"] = args.priority
    if args.tag is not None:
        payload["tags"] = args.tag
    return payload


def cmd_sample_add(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    try:
        actor = _actor(conn, args.token)
        result = datasets.add_sample(
            conn,
            actor,
            args.project,
            args.dataset,
            _sample_payload(args, include_key=True),
        )
        _print(result)
        return 0
    finally:
        conn.close()


def cmd_sample_update(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    try:
        actor = _actor(conn, args.token)
        result = datasets.update_sample(
            conn,
            actor,
            args.project,
            args.dataset,
            args.id,
            _sample_payload(args, include_key=False),
        )
        _print(result)
        return 0
    finally:
        conn.close()


def cmd_sample_delete(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    try:
        actor = _actor(conn, args.token)
        result = datasets.delete_sample(
            conn, actor, args.project, args.dataset, args.id
        )
        _print(result)
        return 0
    finally:
        conn.close()


def cmd_show_run_samples(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    try:
        actor = _actor(conn, args.token)
        _print(datasets.run_dataset_samples(conn, actor, args.project, args.run))
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

    p = sub.add_parser("dataset-create", help="create a dataset")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--token", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--description", default=None)
    p.set_defaults(func=cmd_dataset_create)

    p = sub.add_parser("dataset-list", help="list datasets with their latest version")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--token", required=True)
    p.add_argument("--project", required=True)
    p.set_defaults(func=cmd_dataset_list)

    p = sub.add_parser("dataset-show", help="show a dataset or one immutable version")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--token", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--dataset", required=True)
    p.add_argument("--version", type=int, default=None)
    p.add_argument("--no-samples", action="store_true")
    p.set_defaults(func=cmd_dataset_show)

    p = sub.add_parser("dataset-import", help="import CSV/JSONL samples with line validation")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--token", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--dataset", required=True)
    p.add_argument("--file", required=True)
    p.add_argument("--format", choices=datasets.FORMATS, default=None)
    p.add_argument("--mode", choices=datasets.MODES, default="abort")
    p.set_defaults(func=cmd_dataset_import)

    p = sub.add_parser("dataset-export", help="export a subset by tag and priority")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--token", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--dataset", required=True)
    p.add_argument("--version", type=int, default=None)
    p.add_argument("--out", required=True)
    p.add_argument("--format", choices=datasets.FORMATS, default="jsonl")
    p.add_argument("--tag", action="append", default=None)
    p.add_argument("--priority", action="append", choices=datasets.PRIORITIES, default=None)
    p.add_argument("--all-tags", action="store_true")
    p.set_defaults(func=cmd_dataset_export)

    p = sub.add_parser("dataset-stats", help="sample counts grouped by priority and tag")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--token", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--dataset", required=True)
    p.set_defaults(func=cmd_dataset_stats)

    p = sub.add_parser("sample-add", help="add one sample and publish a dataset version")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--token", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--dataset", required=True)
    p.add_argument("--sample-key", default=None)
    p.add_argument("--input-json", required=True)
    p.add_argument("--expected-json", default=None)
    p.add_argument("--context-json", default=None)
    p.add_argument("--context-ref", default=None)
    p.add_argument("--priority", choices=datasets.PRIORITIES, default=None)
    p.add_argument("--tag", action="append", default=None)
    p.set_defaults(func=cmd_sample_add)

    p = sub.add_parser("sample-update", help="update one sample and publish a new version")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--token", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--dataset", required=True)
    p.add_argument("--id", required=True)
    p.add_argument("--input-json", default=None)
    p.add_argument("--expected-json", default=None)
    p.add_argument("--context-json", default=None)
    p.add_argument("--context-ref", default=None)
    p.add_argument("--priority", choices=datasets.PRIORITIES, default=None)
    p.add_argument("--tag", action="append", default=None)
    p.set_defaults(func=cmd_sample_update)

    p = sub.add_parser("sample-delete", help="delete one sample and publish a new version")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--token", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--dataset", required=True)
    p.add_argument("--id", required=True)
    p.set_defaults(func=cmd_sample_delete)

    p = sub.add_parser("show-run-samples", help="show the dataset snapshot bound by a Run")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--token", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--run", required=True)
    p.set_defaults(func=cmd_show_run_samples)

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
        error = {"status": exc.status, "code": exc.code, "message": exc.message}
        if exc.details is not None:
            error["details"] = exc.details
        _print({"error": error})
        return 2
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        _print({"error": {"code": "cli_error", "message": str(exc)}})
        return 3


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
