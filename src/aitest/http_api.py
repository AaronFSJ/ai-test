"""JSON HTTP API and minimal HTML view for the platform skeleton, datasets and scorers."""

from __future__ import annotations

import html
import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import datasets, db, sampling, scoring, service

MAX_BODY_BYTES = 8 * 1024 * 1024


class ApiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class ApiHandler(BaseHTTPRequestHandler):
    database_path = ""
    server_version = "AITest/0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # pragma: no cover - logging
        return

    def _connect(self) -> sqlite3.Connection:
        return db.connect(self.database_path)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _send_html(self, status: int, body: str) -> None:
        self._send(status, body.encode("utf-8"), "text/html; charset=utf-8")

    def _send_text(self, status: int, body: str, content_type: str, headers: dict | None = None) -> None:
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(raw)

    def _error(self, status: int, code: str, message: str, details: dict | None = None) -> None:
        error = {"code": code, "message": message}
        if details is not None:
            error["details"] = details
        self._send_json(status, {"error": error})

    def _read_json(self) -> dict:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return {}
        try:
            length = int(raw_length)
        except ValueError:
            raise service.ApiError(400, "invalid_body", "Content-Length must be an integer")
        if length < 0 or length > MAX_BODY_BYTES:
            raise service.ApiError(413, "payload_too_large", "request body is too large")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise service.ApiError(400, "invalid_json", f"request body must be valid JSON: {exc}")
        if not isinstance(payload, dict):
            raise service.ApiError(400, "invalid_json", "request body must be a JSON object")
        return payload

    def _token(self, query: dict[str, list[str]]) -> str | None:
        header = self.headers.get("Authorization", "")
        if header.lower().startswith("bearer "):
            token = header[7:].strip()
            if token:
                return token
        values = query.get("token") or []
        return values[0] if values else None

    def _authenticate(self, conn: sqlite3.Connection, query: dict[str, list[str]]) -> dict:
        return service.authenticate(conn, self._token(query))

    # -- routing ---------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._dispatch("POST")

    def do_PATCH(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        parts = [unquote(part) for part in path.split("/") if part]
        query = parse_qs(parsed.query, keep_blank_values=True)
        conn = None
        try:
            conn = self._connect()
            if method == "GET" and parts == ["health"]:
                self._send_json(200, {"status": "ok", "schemaDigest": db.schema_digest(conn)})
                return
            if method == "GET" and path in ("/", "/ui"):
                self._render_ui(conn, query)
                return
            if method == "GET" and len(parts) == 3 and parts[0] == "ui" and parts[1] == "runs":
                self._render_run_ui(conn, parts[2], query)
                return

            # Runner endpoints authenticate with a runner token, not a member
            # token, and are scoped to the runner's own project.
            if parts and parts[0] == "runner":
                self._handle_runner_api(conn, parts, method, query)
                return

            actor = self._authenticate(conn, query)

            if method == "GET" and parts == ["whoami"]:
                self._send_json(200, {"data": actor})
                return
            if method == "GET" and parts == ["projects"]:
                self._send_json(200, {"data": [service.get_project(conn, actor["project_id"])]})
                return

            if len(parts) >= 2 and parts[0] == "projects":
                project_id = parts[1]

                if method == "GET" and len(parts) == 2:
                    service.require_project(actor, project_id)
                    project = service.get_project(conn, project_id)
                    if project is None:
                        raise service.ApiError(404, "not_found", "project not found")
                    self._send_json(200, {"data": project})
                    return

                if method == "GET" and len(parts) == 3 and parts[2] == "members":
                    self._send_json(200, {"data": service.list_members(conn, actor, project_id)})
                    return

                if len(parts) >= 3 and parts[2] == "specs":
                    self._handle_specs(conn, actor, project_id, parts, method)
                    return

                if len(parts) >= 3 and parts[2] == "runs":
                    self._handle_runs(conn, actor, project_id, parts, method, query)
                    return

                if len(parts) >= 3 and parts[2] == "runners":
                    self._handle_runners(conn, actor, project_id, parts, method)
                    return

                if len(parts) >= 3 and parts[2] == "scheduler":
                    self._handle_scheduler(conn, actor, project_id, parts, method)
                    return

                if len(parts) >= 3 and parts[2] == "datasets":
                    self._handle_datasets(conn, actor, project_id, parts, method, query)
                    return

                if len(parts) >= 3 and parts[2] == "scorers":
                    self._handle_scorers(conn, actor, project_id, parts, method)
                    return

                if len(parts) >= 3 and parts[2] == "scorer-profiles":
                    self._handle_scorer_profiles(conn, actor, project_id, parts, method)
                    return

                if len(parts) >= 3 and parts[2] == "sampling":
                    self._handle_sampling(conn, actor, project_id, parts, method)
                    return

            raise service.ApiError(404, "not_found", "route not found")
        except service.ApiError as exc:
            self._error(exc.status, exc.code, exc.message, exc.details)
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            self._error(500, "database_error", f"database error: {exc}")
        except Exception as exc:  # pragma: no cover - defensive
            self._error(500, "internal_error", f"internal error: {exc}")
        finally:
            if conn is not None:
                conn.close()

    def _handle_specs(
        self,
        conn: sqlite3.Connection,
        actor: dict,
        project_id: str,
        parts: list[str],
        method: str,
    ) -> None:
        if len(parts) == 3:  # /projects/{pid}/specs
            if method == "GET":
                self._send_json(200, {"data": service.list_specs(conn, actor, project_id)})
                return
            if method == "POST":
                payload = self._read_json()
                spec = service.create_spec(conn, actor, project_id, payload)
                self._send_json(201, {"data": spec})
                return
            raise service.ApiError(405, "method_not_allowed", "method not allowed")

        spec_id = parts[3]
        if len(parts) == 4:  # /projects/{pid}/specs/{sid}
            if method == "GET":
                service.require_project(actor, project_id)
                self._send_json(200, {"data": service.get_spec(conn, project_id, spec_id)})
                return
            raise service.ApiError(405, "method_not_allowed", "method not allowed")

        if len(parts) >= 5 and parts[4] == "versions":
            if len(parts) == 5:
                if method == "GET":
                    self._send_json(
                        200,
                        {"data": service.list_spec_versions(conn, actor, project_id, spec_id)},
                    )
                    return
                if method == "POST":
                    payload = self._read_json()
                    version = service.add_spec_version(conn, actor, project_id, spec_id, payload)
                    self._send_json(201, {"data": version})
                    return
                raise service.ApiError(405, "method_not_allowed", "method not allowed")

            if len(parts) == 6 and method == "GET":
                try:
                    version_number = int(parts[5])
                except ValueError as exc:
                    raise service.ApiError(400, "validation_error", "version must be an integer") from exc
                version = service.get_spec_version(
                    conn, actor, project_id, spec_id, version_number
                )
                self._send_json(200, {"data": version})
                return

        raise service.ApiError(404, "not_found", "route not found")

    def _handle_scorers(
        self,
        conn: sqlite3.Connection,
        actor: dict,
        project_id: str,
        parts: list[str],
        method: str,
    ) -> None:
        if len(parts) == 3:  # /projects/{pid}/scorers
            if method == "GET":
                self._send_json(200, {"data": scoring.list_scorers(conn, actor, project_id)})
                return
            if method == "POST":
                payload = self._read_json()
                scorer = scoring.create_scorer(conn, actor, project_id, payload)
                self._send_json(201, {"data": scorer})
                return
            raise service.ApiError(405, "method_not_allowed", "method not allowed")

        if len(parts) == 4 and parts[3] == "preview" and method == "POST":
            payload = self._read_json()
            self._send_json(200, {"data": scoring.preview_scorer(conn, actor, project_id, payload)})
            return

        scorer_id = parts[3]
        if len(parts) == 4:
            if method == "GET":
                self._send_json(
                    200, {"data": scoring.get_scorer(conn, actor, project_id, scorer_id)}
                )
                return
            raise service.ApiError(405, "method_not_allowed", "method not allowed")

        if len(parts) >= 5 and parts[4] == "versions":
            if len(parts) == 5:
                if method == "GET":
                    scorer = scoring.get_scorer(conn, actor, project_id, scorer_id)
                    self._send_json(200, {"data": scorer["versions"]})
                    return
                if method == "POST":
                    payload = self._read_json()
                    version = scoring.add_scorer_version(
                        conn, actor, project_id, scorer_id, payload
                    )
                    self._send_json(201, {"data": version})
                    return
                raise service.ApiError(405, "method_not_allowed", "method not allowed")
            if len(parts) == 6 and method == "GET":
                try:
                    version_number = int(parts[5])
                except ValueError as exc:
                    raise service.ApiError(
                        400, "validation_error", "version must be an integer"
                    ) from exc
                self._send_json(
                    200,
                    {
                        "data": scoring.get_scorer_version(
                            conn, actor, project_id, scorer_id, version_number
                        )
                    },
                )
                return
        raise service.ApiError(404, "not_found", "route not found")

    def _handle_scorer_profiles(
        self,
        conn: sqlite3.Connection,
        actor: dict,
        project_id: str,
        parts: list[str],
        method: str,
    ) -> None:
        if len(parts) == 3:  # /projects/{pid}/scorer-profiles
            if method == "GET":
                self._send_json(200, {"data": scoring.list_profiles(conn, actor, project_id)})
                return
            if method == "POST":
                payload = self._read_json()
                profile = scoring.create_profile(conn, actor, project_id, payload)
                self._send_json(201, {"data": profile})
                return
            raise service.ApiError(405, "method_not_allowed", "method not allowed")

        profile_id = parts[3]
        if len(parts) == 4:
            if method == "GET":
                self._send_json(
                    200, {"data": scoring.get_profile(conn, actor, project_id, profile_id)}
                )
                return
            raise service.ApiError(405, "method_not_allowed", "method not allowed")

        if len(parts) >= 5 and parts[4] == "versions":
            if len(parts) == 5:
                if method == "GET":
                    profile = scoring.get_profile(conn, actor, project_id, profile_id)
                    self._send_json(200, {"data": profile["versions"]})
                    return
                if method == "POST":
                    payload = self._read_json()
                    version = scoring.add_profile_version(
                        conn, actor, project_id, profile_id, payload
                    )
                    self._send_json(201, {"data": version})
                    return
                raise service.ApiError(405, "method_not_allowed", "method not allowed")
            if len(parts) == 6 and method == "GET":
                try:
                    version_number = int(parts[5])
                except ValueError as exc:
                    raise service.ApiError(
                        400, "validation_error", "version must be an integer"
                    ) from exc
                self._send_json(
                    200,
                    {
                        "data": scoring.get_profile_version(
                            conn, actor, project_id, profile_id, version_number
                        )
                    },
                )
                return
        raise service.ApiError(404, "not_found", "route not found")

    def _handle_runs(
        self,
        conn: sqlite3.Connection,
        actor: dict,
        project_id: str,
        parts: list[str],
        method: str,
        query: dict[str, list[str]] | None = None,
    ) -> None:
        query = query or {}
        if len(parts) == 3:  # /projects/{pid}/runs
            if method == "GET":
                # A status read is also a scheduler observation point; expire
                # overdue runs so a poller can never observe a permanent hang.
                service.scheduler_maintenance(conn, project_id)
                status = (query.get("status") or [None])[0]
                raw_limit = (query.get("limit") or [None])[0]
                limit = None
                if raw_limit is not None:
                    try:
                        limit = int(raw_limit)
                    except ValueError as exc:
                        raise service.ApiError(400, "validation_error", "limit must be an integer") from exc
                    if limit < 1 or limit > 500:
                        raise service.ApiError(400, "validation_error", "limit must be between 1 and 500")
                self._send_json(
                    200,
                    {"data": service.list_runs(conn, actor, project_id, status=status, limit=limit)},
                )
                return
            if method == "POST":
                payload = self._read_json()
                # Internal rerun linkage is only set by the rerun endpoint.
                payload.pop("_rerun_of_run_id", None)
                run = service.create_run(conn, actor, project_id, payload)
                status = 200 if run.get("reused") else 201
                self._send_json(status, {"data": run})
                return
            raise service.ApiError(405, "method_not_allowed", "method not allowed")

        run_id = parts[3]
        if len(parts) == 4:  # /projects/{pid}/runs/{rid}
            if method == "GET":
                service.require_project(actor, project_id)
                service.scheduler_maintenance(conn, project_id)
                self._send_json(200, {"data": service.get_run(conn, project_id, run_id)})
                return
            raise service.ApiError(405, "method_not_allowed", "method not allowed")

        if len(parts) == 5 and parts[4] == "cancel":
            if method == "POST":
                payload = self._read_json()
                data = service.cancel_run(conn, actor, project_id, run_id, payload, source="api")
                self._send_json(200, {"data": data})
                return
            raise service.ApiError(405, "method_not_allowed", "method not allowed")

        if len(parts) == 5 and parts[4] == "rerun":
            if method == "POST":
                payload = self._read_json()
                data = service.rerun_run(conn, actor, project_id, run_id, payload)
                self._send_json(201, {"data": data})
                return
            raise service.ApiError(405, "method_not_allowed", "method not allowed")

        if len(parts) == 5 and parts[4] == "events":
            if method == "GET":
                service.require_project(actor, project_id)
                service.scheduler_maintenance(conn, project_id)
                self._send_json(
                    200, {"data": service.list_run_events(conn, actor, project_id, run_id)}
                )
                return
            raise service.ApiError(405, "method_not_allowed", "method not allowed")

        if len(parts) >= 5 and parts[4] == "items":
            if len(parts) == 5:
                if method == "GET":
                    self._send_json(
                        200, {"data": service.list_run_items(conn, actor, project_id, run_id)}
                    )
                    return
                if method == "POST":
                    payload = self._read_json()
                    item = service.add_run_item(conn, actor, project_id, run_id, payload)
                    self._send_json(201, {"data": item})
                    return
                raise service.ApiError(405, "method_not_allowed", "method not allowed")
            if len(parts) == 6 and method == "GET":
                item = service.get_run_item(conn, actor, project_id, run_id, parts[5])
                self._send_json(200, {"data": item})
                return

        if len(parts) == 5 and parts[4] == "dataset-samples":
            if method == "GET":
                self._send_json(
                    200,
                    {"data": datasets.run_dataset_samples(conn, actor, project_id, run_id)},
                )
                return
            raise service.ApiError(405, "method_not_allowed", "method not allowed")

        if len(parts) == 5 and parts[4] == "score":
            if method == "POST":
                payload = self._read_json()
                self._send_json(
                    200, {"data": scoring.score_run(conn, actor, project_id, run_id, payload)}
                )
                return
            raise service.ApiError(405, "method_not_allowed", "method not allowed")

        if len(parts) == 5 and parts[4] == "scorer-trace":
            if method == "GET":
                self._send_json(
                    200, {"data": scoring.run_scorer_trace(conn, actor, project_id, run_id)}
                )
                return
            raise service.ApiError(405, "method_not_allowed", "method not allowed")

        if len(parts) == 5 and parts[4] == "sampling":
            if method == "GET":
                self._send_json(
                    200,
                    {"data": service.get_run_sampling(conn, actor, project_id, run_id)},
                )
                return
            raise service.ApiError(405, "method_not_allowed", "method not allowed")

        raise service.ApiError(404, "not_found", "route not found")

    def _handle_dataset_samples(
        self,
        conn: sqlite3.Connection,
        actor: dict,
        project_id: str,
        dataset_id: str,
        parts: list[str],
        method: str,
        query: dict[str, list[str]],
    ) -> None:
        tags, priorities, all_tags = datasets.parse_filters(query)
        if len(parts) == 5:
            if method == "GET":
                samples = datasets.list_samples(
                    conn,
                    actor,
                    project_id,
                    dataset_id,
                    tags=tags,
                    priorities=priorities,
                    all_tags=all_tags,
                )
                self._send_json(200, {"data": samples})
                return
            if method == "POST":
                payload = self._read_json()
                result = datasets.add_sample(conn, actor, project_id, dataset_id, payload)
                self._send_json(201, {"data": result})
                return
            raise service.ApiError(405, "method_not_allowed", "method not allowed")
        if len(parts) == 6:
            sample_id = parts[5]
            if method == "GET":
                self._send_json(
                    200,
                    {"data": datasets.get_sample(conn, actor, project_id, dataset_id, sample_id)},
                )
                return
            if method == "PATCH":
                payload = self._read_json()
                result = datasets.update_sample(
                    conn, actor, project_id, dataset_id, sample_id, payload
                )
                self._send_json(200, {"data": result})
                return
            if method == "DELETE":
                result = datasets.delete_sample(
                    conn, actor, project_id, dataset_id, sample_id
                )
                self._send_json(200, {"data": result})
                return
            raise service.ApiError(405, "method_not_allowed", "method not allowed")
        raise service.ApiError(404, "not_found", "route not found")

    def _handle_dataset_versions(
        self,
        conn: sqlite3.Connection,
        actor: dict,
        project_id: str,
        dataset_id: str,
        parts: list[str],
        method: str,
        query: dict[str, list[str]],
    ) -> None:
        if len(parts) == 5:
            if method == "GET":
                self._send_json(
                    200,
                    {
                        "data": datasets.list_dataset_versions(
                            conn, actor, project_id, dataset_id
                        )
                    },
                )
                return
            raise service.ApiError(405, "method_not_allowed", "method not allowed")
        try:
            version = int(parts[5])
        except ValueError as exc:
            raise service.ApiError(400, "validation_error", "version must be an integer") from exc
        if len(parts) == 6:
            if method == "GET":
                self._send_json(
                    200,
                    {
                        "data": datasets.get_dataset_version(
                            conn, actor, project_id, dataset_id, version
                        )
                    },
                )
                return
            raise service.ApiError(405, "method_not_allowed", "method not allowed")
        if len(parts) == 7 and parts[6] == "export" and method == "GET":
            tags, priorities, all_tags = datasets.parse_filters(query)
            self._send_export(
                datasets.export_version(
                    conn,
                    actor,
                    project_id,
                    dataset_id,
                    version,
                    fmt=self._export_format(query),
                    tags=tags,
                    priorities=priorities,
                    all_tags=all_tags,
                )
            )
            return
        raise service.ApiError(404, "not_found", "route not found")

    def _export_format(self, query: dict[str, list[str]]) -> str:
        fmt = (query.get("format") or ["jsonl"])[0].strip().lower()
        if fmt not in datasets.FORMATS:
            raise service.ApiError(
                400, "validation_error", f"'format' must be one of {list(datasets.FORMATS)}"
            )
        return fmt

    def _send_export(self, result: dict) -> None:
        self._send_text(
            200,
            result["content"],
            result["contentType"],
            {
                "Content-Disposition": f'attachment; filename="{result["filename"]}"',
                "X-Dataset-Version-Id": result["dataset_version_id"],
                "X-Dataset-Version": str(result["version"]),
                "X-Sample-Count": str(result["sampleCount"]),
                "X-Sample-Digest": result["sampleDigest"],
            },
        )

    def _handle_datasets(
        self,
        conn: sqlite3.Connection,
        actor: dict,
        project_id: str,
        parts: list[str],
        method: str,
        query: dict[str, list[str]],
    ) -> None:
        if len(parts) == 3:  # /projects/{pid}/datasets
            if method == "GET":
                self._send_json(200, {"data": datasets.list_datasets(conn, actor, project_id)})
                return
            if method == "POST":
                payload = self._read_json()
                dataset = datasets.create_dataset(conn, actor, project_id, payload)
                self._send_json(201, {"data": dataset})
                return
            raise service.ApiError(405, "method_not_allowed", "method not allowed")

        dataset_id = parts[3]
        if len(parts) == 4:
            if method == "GET":
                self._send_json(
                    200, {"data": datasets.get_dataset(conn, actor, project_id, dataset_id)}
                )
                return
            raise service.ApiError(405, "method_not_allowed", "method not allowed")

        section = parts[4]
        if section == "samples":
            self._handle_dataset_samples(conn, actor, project_id, dataset_id, parts, method, query)
            return
        if section == "versions":
            self._handle_dataset_versions(conn, actor, project_id, dataset_id, parts, method, query)
            return
        if section == "import" and len(parts) == 5:
            if method == "POST":
                payload = self._read_json()
                result = datasets.import_samples(conn, actor, project_id, dataset_id, payload)
                self._send_json(201, {"data": result})
                return
            raise service.ApiError(405, "method_not_allowed", "method not allowed")
        if section == "export" and len(parts) == 5:
            if method == "GET":
                tags, priorities, all_tags = datasets.parse_filters(query)
                self._send_export(
                    datasets.export_latest(
                        conn,
                        actor,
                        project_id,
                        dataset_id,
                        fmt=self._export_format(query),
                        tags=tags,
                        priorities=priorities,
                        all_tags=all_tags,
                    )
                )
                return
            raise service.ApiError(405, "method_not_allowed", "method not allowed")
        if section == "stats" and len(parts) == 5:
            if method == "GET":
                self._send_json(
                    200, {"data": datasets.dataset_stats(conn, actor, project_id, dataset_id)}
                )
                return
            raise service.ApiError(405, "method_not_allowed", "method not allowed")
        if section == "scorer-bindings" and len(parts) == 5:
            if method == "GET":
                self._send_json(
                    200,
                    {"data": scoring.list_bindings(conn, actor, project_id, dataset_id)},
                )
                return
            if method == "POST":
                payload = self._read_json()
                self._send_json(
                    201,
                    {
                        "data": scoring.bind_scorer(
                            conn, actor, project_id, dataset_id, payload
                        )
                    },
                )
                return
            raise service.ApiError(405, "method_not_allowed", "method not allowed")
        if section == "scorer-bindings" and len(parts) == 6:
            if method == "DELETE":
                self._send_json(
                    200,
                    {
                        "data": scoring.delete_binding(
                            conn, actor, project_id, dataset_id, parts[5]
                        )
                    },
                )
                return
            raise service.ApiError(405, "method_not_allowed", "method not allowed")
        raise service.ApiError(404, "not_found", "route not found")

    def _handle_sampling(
        self,
        conn: sqlite3.Connection,
        actor: dict,
        project_id: str,
        parts: list[str],
        method: str,
    ) -> None:
        if len(parts) == 4 and parts[3] == "preview":
            if method != "POST":
                raise service.ApiError(405, "method_not_allowed", "method not allowed")
            payload = self._read_json()
            raw_samples = payload.get("samples")
            if not isinstance(raw_samples, list) or not raw_samples:
                raise service.ApiError(400, "validation_error", "'samples' must be a non-empty array")
            if len(raw_samples) > 1000:
                raise service.ApiError(400, "validation_error", "'samples' is limited to 1000 entries")
            samples = []
            for index, item in enumerate(raw_samples):
                if not isinstance(item, dict):
                    raise service.ApiError(400, "validation_error", f"samples[{index}] must be an object")
                if item.get("state") not in sampling.STATES:
                    raise service.ApiError(
                        400,
                        "validation_error",
                        f"samples[{index}].state must be one of {list(sampling.STATES)}",
                    )
                confidence = item.get("confidence", 1.0)
                if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
                    raise service.ApiError(
                        400, "validation_error", f"samples[{index}].confidence must be a number"
                    )
                score = item.get("score")
                if score is not None:
                    if isinstance(score, bool) or not isinstance(score, (int, float)):
                        raise service.ApiError(
                            400, "validation_error", f"samples[{index}].score must be a number"
                        )
                    if not 0 <= float(score) <= 1:
                        raise service.ApiError(
                            400, "validation_error", f"samples[{index}].score must be between 0 and 1"
                        )
                samples.append(
                    {
                        "state": item["state"],
                        "confidence": max(0.0, min(1.0, float(confidence))),
                        "score": float(score) if score is not None else None,
                    }
                )
            config = sampling.normalize_config(
                payload.get("config", payload.get("sampling"))
            )
            state, confidence, aggregation = sampling.aggregate(samples, config)
            self._send_json(
                200,
                {
                    "data": {
                        "state": state,
                        "confidence": confidence,
                        "aggregation": aggregation,
                        "config": config,
                        "cost": sampling.cost_hint(config, item_count=1),
                    }
                },
            )
            return
        raise service.ApiError(404, "not_found", "route not found")

    def _handle_runners(
        self,
        conn: sqlite3.Connection,
        actor: dict,
        project_id: str,
        parts: list[str],
        method: str,
    ) -> None:
        if len(parts) == 3:  # /projects/{pid}/runners
            if method == "GET":
                self._send_json(200, {"data": service.list_runners(conn, actor, project_id)})
                return
            if method == "POST":
                payload = self._read_json()
                runner = service.register_runner(conn, actor, project_id, payload)
                self._send_json(201, {"data": runner})
                return
            raise service.ApiError(405, "method_not_allowed", "method not allowed")
        if len(parts) == 4:  # /projects/{pid}/runners/{rid}
            if method == "GET":
                runner = service.get_runner(conn, actor, project_id, parts[3])
                self._send_json(200, {"data": runner})
                return
            raise service.ApiError(405, "method_not_allowed", "method not allowed")
        if len(parts) == 5 and parts[4] == "events":
            if method == "GET":
                self._send_json(
                    200,
                    {"data": service.list_runner_events(conn, actor, project_id, parts[3])},
                )
                return
            raise service.ApiError(405, "method_not_allowed", "method not allowed")
        if len(parts) == 5 and parts[4] == "revoke":
            if method == "POST":
                payload = self._read_json()
                runner = service.revoke_runner(conn, actor, project_id, parts[3], payload)
                self._send_json(200, {"data": runner})
                return
            raise service.ApiError(405, "method_not_allowed", "method not allowed")
        raise service.ApiError(404, "not_found", "route not found")

    def _handle_scheduler(
        self,
        conn: sqlite3.Connection,
        actor: dict,
        project_id: str,
        parts: list[str],
        method: str,
    ) -> None:
        if len(parts) == 3:  # /projects/{pid}/scheduler
            if method == "GET":
                self._send_json(200, {"data": service.scheduler_status(conn, actor, project_id)})
                return
            raise service.ApiError(405, "method_not_allowed", "method not allowed")
        if len(parts) == 4 and parts[3] == "tick":
            if method == "POST":
                self._send_json(200, {"data": service.scheduler_tick(conn, actor, project_id)})
                return
            raise service.ApiError(405, "method_not_allowed", "method not allowed")
        if len(parts) == 4 and parts[3] == "config":
            if method == "POST":
                payload = self._read_json()
                settings = service.update_scheduler_settings(conn, actor, project_id, payload)
                self._send_json(200, {"data": settings})
                return
            raise service.ApiError(405, "method_not_allowed", "method not allowed")
        raise service.ApiError(404, "not_found", "route not found")

    def _handle_runner_api(
        self,
        conn: sqlite3.Connection,
        parts: list[str],
        method: str,
        query: dict[str, list[str]],
    ) -> None:
        token = self._token(query)
        if method == "GET" and parts == ["runner", "me"]:
            self._send_json(200, {"data": service.runner_self(conn, token)})
            return
        if method == "POST" and parts == ["runner", "heartbeat"]:
            payload = self._read_json()
            self._send_json(200, {"data": service.runner_heartbeat(conn, token, payload)})
            return
        if method == "POST" and parts == ["runner", "claim"]:
            payload = self._read_json()
            self._send_json(200, service.claim_run(conn, token, payload))
            return
        if len(parts) == 4 and parts[0] == "runner" and parts[1] == "runs":
            run_id = parts[2]
            if parts[3] == "heartbeat" and method == "POST":
                self._send_json(200, {"data": service.runner_run_heartbeat(conn, token, run_id)})
                return
            if parts[3] == "complete" and method == "POST":
                payload = self._read_json()
                self._send_json(200, {"data": service.complete_run(conn, token, run_id, payload)})
                return
        raise service.ApiError(404, "not_found", "route not found")

    # -- minimal HTML view ----------------------------------------------

    def _render_run_ui(
        self, conn: sqlite3.Connection, run_id: str, query: dict[str, list[str]]
    ) -> None:
        token = self._token(query)
        if not token:
            self._send_html(200, _login_page())
            return
        try:
            actor = service.authenticate(conn, token)
        except service.ApiError as exc:
            self._send_html(exc.status, _login_page(exc.message))
            return
        try:
            run = service.get_run(conn, actor["project_id"], run_id)
        except service.ApiError as exc:
            self._send_html(exc.status, _message_page("Not found", exc.message))
            return
        self._send_html(200, _run_detail_page(actor, run, token))

    def _render_ui(self, conn: sqlite3.Connection, query: dict[str, list[str]]) -> None:
        token = self._token(query)
        if not token:
            self._send_html(200, _login_page())
            return
        try:
            actor = service.authenticate(conn, token)
        except service.ApiError as exc:
            self._send_html(exc.status, _login_page(exc.message))
            return

        requested = (query.get("project_id") or [None])[0]
        if requested and requested != actor["project_id"]:
            # Never render another project, even if the caller knows its id.
            self._send_html(404, _message_page("Not found", "Project not found."))
            return

        service.scheduler_maintenance(conn, actor["project_id"])
        project = service.get_project(conn, actor["project_id"])
        specs = service.list_specs(conn, actor, actor["project_id"])
        runs = service.list_runs(conn, actor, actor["project_id"])
        project_datasets = datasets.list_datasets(conn, actor, actor["project_id"])
        self._send_html(200, _project_page(actor, project, specs, runs, project_datasets, token))


def _esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _login_page(message: str | None = None) -> str:
    note = f'<p class="error">{_esc(message)}</p>' if message else ""
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>AI Test Platform</title>
<style>body{{font-family:system-ui,sans-serif;max-width:960px;margin:40px auto;padding:0 16px}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #ddd;padding:6px 8px;text-align:left}}code{{background:#f4f4f4;padding:2px 4px}}.error{{color:#b00020}}</style>
</head><body><h1>AI Test Platform</h1>{note}
<p>Open <code>/ui?token=&lt;member token&gt;</code> with a token created by the CLI.</p>
</body></html>"""


def _message_page(title: str, message: str) -> str:
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>{_esc(title)}</title></head><body><h1>{_esc(title)}</h1><p>{_esc(message)}</p></body></html>"""


def _project_page(
    actor: dict,
    project: dict,
    specs: list[dict],
    runs: list[dict],
    project_datasets: list[dict],
    token: str,
) -> str:
    spec_rows = []
    for spec in specs:
        versions = spec.get("versions") or []
        version_text = ", ".join(f"v{v['version']}" for v in versions) or "-"
        spec_rows.append(
            "<tr>"
            f"<td><code>{_esc(spec['id'])}</code></td>"
            f"<td>{_esc(spec['name'])}</td>"
            f"<td>{_esc(spec['channel'])}</td>"
            f"<td>{_esc(spec['type'])}</td>"
            f"<td>{_esc(version_text)}</td>"
            "</tr>"
        )
    run_rows = []
    for run in runs:
        run_rows.append(
            "<tr>"
            f"<td><code>{_esc(run['id'])}</code></td>"
            f"<td>{_esc(run['channel'])}</td>"
            f"<td>v{_esc(run['spec_version'])}</td>"
            f"<td>{_esc(run['commit_sha'])}</td>"
            f"<td>{_esc(run['branch'])}</td>"
            f"<td>{_esc(run['priority'])}</td>"
            f"<td>{_esc(run.get('queue_position') or '-')}</td>"
            f"<td>{_esc(run.get('assigned_runner_id') or '-')}</td>"
            f"<td>{_esc(run['status'])}</td>"
            f"<td>{_esc(run['created_at'])}</td>"
            f'<td><a href="/ui/runs/{_esc(run["id"])}?token={_esc(token)}">Open</a></td>'
            "</tr>"
        )
    dataset_rows = []
    for dataset in project_datasets:
        latest = dataset.get("latest_version") or {}
        dataset_rows.append(
            "<tr>"
            f"<td><code>{_esc(dataset['id'])}</code></td>"
            f"<td>{_esc(dataset['name'])}</td>"
            f"<td>{_esc(dataset.get('sample_count', 0))}</td>"
            f"<td>{('v' + str(latest['version'])) if latest else '-'}</td>"
            f"<td>{_esc(latest.get('sample_count', '-')) if latest else '-'}</td>"
            f"<td><code>{_esc(latest.get('content_digest', ''))[:12] if latest else '-'}</code></td>"
            "</tr>"
        )
    if not spec_rows:
        spec_rows.append('<tr><td colspan="5">No specs</td></tr>')
    if not run_rows:
        run_rows.append('<tr><td colspan="11">No runs</td></tr>')
    if not dataset_rows:
        dataset_rows.append('<tr><td colspan="6">No datasets</td></tr>')
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{_esc(project['name'])} - AI Test Platform</title>
<style>body{{font-family:system-ui,sans-serif;max-width:1100px;margin:40px auto;padding:0 16px}}table{{border-collapse:collapse;width:100%;margin-bottom:24px}}th,td{{border:1px solid #ddd;padding:6px 8px;text-align:left}}code{{background:#f4f4f4;padding:2px 4px}}.muted{{color:#666}}</style>
</head><body data-project-id="{_esc(project['id'])}" data-role="{_esc(actor['role'])}">
<h1>{_esc(project['name'])}</h1>
<p class="muted">project <code>{_esc(project['id'])}</code> · signed in as {_esc(actor['name'])} ({_esc(actor['role'])})</p>
<h2>Specs</h2>
<table id="specs"><thead><tr><th>ID</th><th>Name</th><th>Channel</th><th>Type</th><th>Versions</th></tr></thead>
<tbody>{''.join(spec_rows)}</tbody></table>
<h2>Datasets</h2>
<table id="datasets"><thead><tr><th>ID</th><th>Name</th><th>Samples</th><th>Latest version</th><th>Version samples</th><th>Digest</th></tr></thead>
<tbody>{''.join(dataset_rows)}</tbody></table>
<h2>Runs</h2>
<table id="runs"><thead><tr><th>ID</th><th>Channel</th><th>Spec version</th><th>Commit</th><th>Branch</th><th>Priority</th><th>Queue</th><th>Runner</th><th>Status</th><th>Created</th><th>Report</th></tr></thead>
<tbody>{''.join(run_rows)}</tbody></table>
<p class="muted">Only this project's data is rendered. Dataset and sample writes are available through the API and CLI for the member role.</p>
</body></html>"""


def _pretty(value) -> str:
    if value is None:
        return "-"
    return _esc(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _run_detail_page(actor: dict, run: dict, token: str) -> str:
    sampling_view = run.get("sampling") or {}
    config = sampling_view.get("effectiveConfig") or sampling_view.get("config") or {}
    cost = sampling_view.get("cost") or {}
    model = sampling_view.get("effectiveModelSnapshot") or sampling_view.get("modelSnapshot")
    summary = sampling_view.get("aggregationSummary") or {}
    item_blocks = []
    for item in run.get("items") or []:
        aggregation = item.get("aggregation") or {}
        counts = aggregation.get("counts") or {}
        samples = item.get("samples") or []
        sample_rows = "".join(
            "<tr>"
            f"<td>{_esc(sample.get('sampleIndex'))}</td>"
            f"<td class=\"state-{_esc(sample.get('state'))}\">{_esc(sample.get('state'))}</td>"
            f"<td>{_esc(sample.get('confidence'))}</td>"
            f"<td>{_esc(sample.get('score'))}</td>"
            f"<td>{_esc(sample.get('durationMs'))}</td>"
            f"<td>{_esc((sample.get('error') or {}).get('code') if sample.get('error') else '-')}</td>"
            f"<td><code>{_esc((sample.get('actualDigest') or '')[:12])}</code></td>"
            "</tr>"
            for sample in samples
        )
        item_blocks.append(
            f'<section class="item" data-case-ref="{_esc(item.get("case_ref"))}" '
            f'data-state="{_esc(item.get("state"))}" '
            f'data-sample-count="{_esc(item.get("sampleCount"))}">'
            f"<h3>{_esc(item.get('case_ref'))} → "
            f'<span class="state-{_esc(item.get("state"))}">{_esc(item.get("state"))}</span> '
            f"<span class=\"muted\">({_esc(item.get('sampleCount'))} samples)</span></h3>"
            f"<p>pass={_esc(counts.get('pass', 0))}, fail={_esc(counts.get('fail', 0))}, "
            f"uncertain={_esc(counts.get('uncertain', 0))}, error={_esc(counts.get('error', 0))}; "
            f"pass_ratio={_esc(aggregation.get('pass_ratio'))}; "
            f"mean_score={_esc(aggregation.get('mean_score'))}; "
            f"variance={_esc(aggregation.get('variance'))}</p>"
            f"<p class=\"muted\">reason: {_esc(aggregation.get('reason'))}</p>"
            f"<p class=\"muted\">rule: {_esc(aggregation.get('rule'))}</p>"
            '<table class="samples"><thead><tr><th>#</th><th>State</th><th>Confidence</th>'
            "<th>Score</th><th>Duration ms</th><th>Error</th><th>Actual digest</th></tr></thead>"
            f"<tbody>{sample_rows}</tbody></table>"
            f"<details><summary>Aggregation JSON</summary><pre>{_pretty(aggregation)}</pre></details>"
            "</section>"
        )
    if not item_blocks:
        item_blocks.append(
            "<p>No scored items yet. Call <code>POST /projects/{pid}/runs/{rid}/score</code> "
            "with sampling parameters and actual outputs.</p>"
        )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{_esc(run.get('id'))} - Run sampling detail</title>
<style>body{{font-family:system-ui,sans-serif;max-width:1200px;margin:40px auto;padding:0 16px}}table{{border-collapse:collapse;width:100%;margin:8px 0 20px}}th,td{{border:1px solid #ddd;padding:6px 8px;text-align:left}}code,pre{{background:#f4f4f4;padding:2px 4px}}pre{{padding:12px;overflow:auto}}.muted{{color:#666}}.state-pass{{color:#0a7f31}}.state-fail{{color:#b00020}}.state-uncertain{{color:#9a6700}}.state-error{{color:#6a1b9a}}.item{{border-top:1px solid #eee;padding-top:12px;margin-top:16px}}</style>
</head><body data-project-id="{_esc(run.get('project_id'))}" data-run-id="{_esc(run.get('id'))}"
 data-sampling-n="{_esc(config.get('n'))}" data-sampling-strategy="{_esc(config.get('strategy'))}">
<p><a href="/ui?token={_esc(token)}">← Project</a></p>
<h1>Run <code>{_esc(run.get('id'))}</code></h1>
<p class="muted">{_esc(run.get('channel'))} channel · status {_esc(run.get('status'))} ·
 spec v{_esc((run.get('spec_version') or {}).get('version'))} · commit {_esc(run.get('commit_sha'))} ·
 branch {_esc(run.get('branch'))}</p>
<h2>Sampling parameters</h2>
<table id="sampling"><tbody>
<tr><th>N (samples per item)</th><td>{_esc(config.get('n'))}</td></tr>
<tr><th>Strategy</th><td>{_esc(config.get('strategy'))}</td></tr>
<tr><th>Majority threshold</th><td>{_esc(config.get('majority_threshold'))}</td></tr>
<tr><th>Expected min (mean score)</th><td>{_esc(config.get('expected_min'))}</td></tr>
<tr><th>Variance max</th><td>{_esc(config.get('variance_max'))}</td></tr>
<tr><th>Concurrency</th><td>{_esc(config.get('concurrency'))}</td></tr>
<tr><th>Temperature</th><td>{_esc(config.get('temperature'))}</td></tr>
<tr><th>Cost multiplier</th><td>{_esc(cost.get('multiplier'))}</td></tr>
<tr><th>Cost note</th><td>{_esc(cost.get('note'))}</td></tr>
<tr><th>Tokens (in/out/total)</th><td>{_esc(cost.get('tokens_in'))} / {_esc(cost.get('tokens_out'))} / {_esc(cost.get('tokens_total'))}</td></tr>
<tr><th>Batch</th><td>{_esc(json.dumps(sampling_view.get('batch'), ensure_ascii=False) if sampling_view.get('batch') else '-')}</td></tr>
</tbody></table>
<h2>Model snapshot</h2>
<pre id="model-snapshot">{_pretty(model)}</pre>
<details><summary>Effective sampling JSON</summary><pre id="effective-sampling">{_pretty(sampling_view.get('effective'))}</pre></details>
<h2>Items</h2>
<p class="muted">items={_esc(summary.get('itemCount'))}; item states=
{_esc(json.dumps(summary.get('itemStates'), ensure_ascii=False, sort_keys=True))};
sample states={_esc(json.dumps(summary.get('sampleStates'), ensure_ascii=False, sort_keys=True))}</p>
<div id="items">{''.join(item_blocks)}</div>
</body></html>"""


def make_server(db_path: str | Path, host: str = "127.0.0.1", port: int = 0) -> ApiServer:
    class BoundHandler(ApiHandler):
        database_path = str(db_path)

    return ApiServer((host, port), BoundHandler)


def serve(db_path: str | Path, host: str = "127.0.0.1", port: int = 8080) -> None:
    server = make_server(db_path, host=host, port=port)
    print(f"serving on http://{host}:{server.server_port} (db={db_path})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover - interactive
        pass
    finally:
        server.server_close()
