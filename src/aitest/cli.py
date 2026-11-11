"""Command line administration entry for the platform skeleton (T01 + T03).

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

from . import db, evidence, http_api, migrations, redaction, reports, service, storage

DEFAULT_DB = os.environ.get("AITEST_DB", "aitest.db")
DEFAULT_STORAGE = os.environ.get("AITEST_STORAGE", "aitest-evidence")


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
            "priority": args.priority,
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


def _store(args: argparse.Namespace) -> storage.ObjectStore:
    return storage.ObjectStore(args.storage_root)


def cmd_add_evidence(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    try:
        actor = _actor(conn, args.token)
        payload: dict[str, Any] = {"kind": args.kind}
        sources = [args.file is not None, args.content is not None, args.content_base64 is not None]
        if sum(sources) != 1:
            raise service.ApiError(
                400, "validation_error", "pass exactly one of --file, --content or --content-base64"
            )
        if args.file is not None:
            raw = Path(args.file).read_bytes()
            try:
                payload["content"] = raw.decode("utf-8")
            except UnicodeDecodeError:
                import base64

                payload["content_base64"] = base64.b64encode(raw).decode("ascii")
        elif args.content is not None:
            payload["content"] = args.content
        else:
            payload["content_base64"] = args.content_base64
        if args.filename is not None:
            payload["filename"] = args.filename
        if args.content_type is not None:
            payload["content_type"] = args.content_type
        record = evidence.add_evidence(
            conn, _store(args), actor, args.project, args.run, args.item, payload
        )
        _print(record)
        return 0
    finally:
        conn.close()


def cmd_list_evidence(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    try:
        actor = _actor(conn, args.token)
        _print(
            evidence.list_evidence(
                conn,
                actor,
                args.project,
                run_id=args.run,
                item_id=args.item,
                kind=args.kind,
                filename_like=args.filename,
                include_deleted=args.include_deleted,
            )
        )
        return 0
    finally:
        conn.close()


def cmd_fetch_evidence(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    try:
        actor = _actor(conn, args.token)
        content = evidence.read_evidence_content(
            conn, _store(args), actor, args.project, args.run, args.item, args.evidence
        )
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content["content"])
        _print(
            {
                "evidence_id": content["evidence_id"],
                "filename": content["filename"],
                "content_type": content["content_type"],
                "bytes": content["bytes"],
                "content_sha256": content["content_sha256"],
                "redacted": content["redacted"],
                "written_to": str(target),
            }
        )
        return 0
    finally:
        conn.close()


def cmd_drilldown(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    try:
        actor = _actor(conn, args.token)
        _print(evidence.drilldown(conn, actor, args.project, args.run, args.item))
        return 0
    finally:
        conn.close()


def cmd_add_verdict(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    try:
        actor = _actor(conn, args.token)
        payload = {
            "state": args.state,
            "confidence": args.confidence,
            "scorer_trace": _json_arg(args.scorer_trace_json, "scorer_trace_json") or {},
        }
        _print(service.add_verdict(conn, actor, args.project, args.run, args.item, payload))
        return 0
    finally:
        conn.close()


def cmd_show_verdict(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    try:
        actor = _actor(conn, args.token)
        _print(service.get_item_verdict(conn, actor, args.project, args.run, args.item))
        return 0
    finally:
        conn.close()


def cmd_create_report(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    try:
        actor = _actor(conn, args.token)
        _print(
            reports.create_report_snapshot(
                conn, _store(args), actor, args.project, args.run
            )
        )
        return 0
    finally:
        conn.close()


def cmd_list_reports(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    try:
        actor = _actor(conn, args.token)
        _print(reports.list_report_snapshots(conn, actor, args.project, args.run))
        return 0
    finally:
        conn.close()


def cmd_show_report(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    try:
        actor = _actor(conn, args.token)
        if not args.snapshot:
            snapshots = reports.list_report_snapshots(conn, actor, args.project, args.run)
            if not snapshots:
                raise service.ApiError(404, "not_found", "no report snapshot for this run")
            snapshot_id = snapshots[0]["id"]
        else:
            snapshot_id = args.snapshot
        if args.content:
            content = reports.read_report_content(
                conn, actor, args.project, args.run, snapshot_id
            )
            sys.stdout.write(content["content"].decode("utf-8"))
            sys.stdout.write("\n")
            return 0
        _print(reports.get_report_snapshot(conn, actor, args.project, args.run, snapshot_id))
        return 0
    finally:
        conn.close()


def cmd_retention_show(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    try:
        actor = _actor(conn, args.token)
        _print(evidence.get_retention_policy(conn, actor, args.project))
        return 0
    finally:
        conn.close()


def cmd_retention_set(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    try:
        actor = _actor(conn, args.token)
        _print(
            evidence.set_retention_policy(
                conn,
                actor,
                args.project,
                {"case_priority": args.priority, "retain_days": args.days},
            )
        )
        return 0
    finally:
        conn.close()


def cmd_cleanup_evidence(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    try:
        actor = _actor(conn, args.token)
        _print(
            evidence.cleanup_expired_evidence(
                conn,
                _store(args),
                actor,
                args.project,
                cutoff=args.cutoff,
                dry_run=args.dry_run,
            )
        )
        return 0
    finally:
        conn.close()


def cmd_cleanup_log(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    try:
        actor = _actor(conn, args.token)
        _print(evidence.list_cleanup_log(conn, actor, args.project))
        return 0
    finally:
        conn.close()


def cmd_redaction_rules(args: argparse.Namespace) -> int:
    _print({"rulesVersion": redaction.RULES_VERSION, "rules": redaction.rules_inventory()})
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    http_api.serve(
        args.db, host=args.host, port=args.port, storage_root=args.storage_root
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aitest", description="AI test platform admin CLI (T01 + T03)")
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
    p.add_argument("--priority", choices=service.RUN_PRIORITIES, default="P1")
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

    p = sub.add_parser("add-evidence", help="redact and store evidence for a run item")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--token", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--run", required=True)
    p.add_argument("--item", required=True)
    p.add_argument("--kind", choices=evidence.EVIDENCE_KINDS, required=True)
    p.add_argument("--file", default=None)
    p.add_argument("--content", default=None)
    p.add_argument("--content-base64", default=None)
    p.add_argument("--filename", default=None)
    p.add_argument("--content-type", default=None)
    p.add_argument("--storage-root", default=DEFAULT_STORAGE)
    p.set_defaults(func=cmd_add_evidence)

    p = sub.add_parser("list-evidence", help="search evidence metadata")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--token", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--run", default=None)
    p.add_argument("--item", default=None)
    p.add_argument("--kind", choices=evidence.EVIDENCE_KINDS, default=None)
    p.add_argument("--filename", default=None)
    p.add_argument("--include-deleted", action="store_true")
    p.set_defaults(func=cmd_list_evidence)

    p = sub.add_parser("fetch-evidence", help="fetch redacted evidence content to a file")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--token", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--run", required=True)
    p.add_argument("--item", required=True)
    p.add_argument("--evidence", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--storage-root", default=DEFAULT_STORAGE)
    p.set_defaults(func=cmd_fetch_evidence)

    p = sub.add_parser("drilldown", help="item -> verdict -> evidence drill-down")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--token", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--run", required=True)
    p.add_argument("--item", required=True)
    p.set_defaults(func=cmd_drilldown)

    p = sub.add_parser("add-verdict", help="record the verdict for a run item")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--token", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--run", required=True)
    p.add_argument("--item", required=True)
    p.add_argument("--state", choices=service.RUN_STATES, required=True)
    p.add_argument("--confidence", type=float, default=None)
    p.add_argument("--scorer-trace-json", default=None)
    p.set_defaults(func=cmd_add_verdict)

    p = sub.add_parser("show-verdict", help="show the verdict for a run item")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--token", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--run", required=True)
    p.add_argument("--item", required=True)
    p.set_defaults(func=cmd_show_verdict)

    p = sub.add_parser("create-report", help="create an immutable report snapshot")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--token", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--run", required=True)
    p.add_argument("--storage-root", default=DEFAULT_STORAGE)
    p.set_defaults(func=cmd_create_report)

    p = sub.add_parser("list-reports", help="list report snapshots for a run")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--token", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--run", required=True)
    p.set_defaults(func=cmd_list_reports)

    p = sub.add_parser("show-report", help="read a report snapshot (optionally raw content)")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--token", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--run", required=True)
    p.add_argument("--snapshot", default=None)
    p.add_argument("--content", action="store_true")
    p.set_defaults(func=cmd_show_report)

    p = sub.add_parser("retention-show", help="show effective evidence retention policy")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--token", required=True)
    p.add_argument("--project", required=True)
    p.set_defaults(func=cmd_retention_show)

    p = sub.add_parser("retention-set", help="set evidence retention days for a priority")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--token", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--priority", choices=("P0", "P1"), required=True)
    p.add_argument("--days", type=int, required=True)
    p.set_defaults(func=cmd_retention_set)

    p = sub.add_parser("cleanup-evidence", help="expire evidence objects past their retention")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--token", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--cutoff", default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--storage-root", default=DEFAULT_STORAGE)
    p.set_defaults(func=cmd_cleanup_evidence)

    p = sub.add_parser("cleanup-log", help="show recorded evidence cleanup actions")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--token", required=True)
    p.add_argument("--project", required=True)
    p.set_defaults(func=cmd_cleanup_log)

    p = sub.add_parser("redaction-rules", help="print the enabled redaction rule inventory")
    p.set_defaults(func=cmd_redaction_rules)

    p = sub.add_parser("serve", help="run the HTTP API and minimal HTML view")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--storage-root", default=DEFAULT_STORAGE)
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
