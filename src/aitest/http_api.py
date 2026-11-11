"""JSON HTTP API and minimal HTML view for the platform skeleton, datasets and scorers."""

from __future__ import annotations

import html
import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import datasets, db, reviews, scoring, service

MAX_BODY_BYTES = 8 * 1024 * 1024


def _query_value(query: dict[str, list[str]], *names: str, default: str | None = None) -> str | None:
    for name in names:
        values = query.get(name)
        if values:
            return values[0]
    return default


def _query_bool(query: dict[str, list[str]], *names: str, default: bool = False) -> bool:
    value = _query_value(query, *names, default=None)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _query_int(
    query: dict[str, list[str]], *names: str, default: int, minimum: int, maximum: int
) -> int:
    value = _query_value(query, *names, default=None)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise service.ApiError(400, "validation_error", f"{names[0]} must be an integer") from exc
    if parsed < minimum or parsed > maximum:
        raise service.ApiError(
            400, "validation_error", f"{names[0]} must be between {minimum} and {maximum}"
        )
    return parsed


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
                    self._handle_runs(conn, actor, project_id, parts, method)
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

                if len(parts) >= 3 and parts[2] == "reviews":
                    self._handle_reviews(conn, actor, project_id, parts, method, query)
                    return

                if len(parts) >= 3 and parts[2] == "calibration-samples":
                    self._handle_calibration_samples(conn, actor, project_id, parts, method, query)
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
    ) -> None:
        if len(parts) == 3:  # /projects/{pid}/runs
            if method == "GET":
                self._send_json(200, {"data": service.list_runs(conn, actor, project_id)})
                return
            if method == "POST":
                payload = self._read_json()
                run = service.create_run(conn, actor, project_id, payload)
                self._send_json(201, {"data": run})
                return
            raise service.ApiError(405, "method_not_allowed", "method not allowed")

        run_id = parts[3]
        if len(parts) == 4:  # /projects/{pid}/runs/{rid}
            if method == "GET":
                service.require_project(actor, project_id)
                self._send_json(200, {"data": service.get_run(conn, project_id, run_id)})
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
            if len(parts) == 7 and parts[6] == "review":
                if method == "POST":
                    payload = self._read_json()
                    result = reviews.record_review(
                        conn, actor, project_id, run_id, parts[5], payload
                    )
                    self._send_json(201, {"data": result})
                    return
                raise service.ApiError(405, "method_not_allowed", "method not allowed")

        if len(parts) == 5 and parts[4] == "review-report":
            if method == "GET":
                self._send_json(
                    200, {"data": reviews.review_report(conn, actor, project_id, run_id)}
                )
                return
            raise service.ApiError(405, "method_not_allowed", "method not allowed")

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

        raise service.ApiError(404, "not_found", "route not found")

    def _handle_reviews(
        self,
        conn: sqlite3.Connection,
        actor: dict,
        project_id: str,
        parts: list[str],
        method: str,
        query: dict[str, list[str]],
    ) -> None:
        if len(parts) == 3:  # /projects/{pid}/reviews
            if method == "GET":
                run_id = _query_value(query, "run_id", "runId")
                item_id = _query_value(query, "item_id", "itemId")
                self._send_json(
                    200,
                    {
                        "data": reviews.list_reviews(
                            conn, actor, project_id, run_id=run_id, item_id=item_id
                        )
                    },
                )
                return
            raise service.ApiError(405, "method_not_allowed", "method not allowed")
        if len(parts) == 4 and parts[3] == "queue":
            if method == "GET":
                kind = _query_value(query, "kind", default="all")
                status = _query_value(query, "status", default="open")
                attribution = _query_value(
                    query, "attribution_label", "attribution-label", default=None
                )
                scorer_type = _query_value(query, "scorer_type", "scorer-type", default=None)
                run_id = _query_value(query, "run_id", "runId", default=None)
                limit = _query_int(query, "limit", default=1000, minimum=1, maximum=1000)
                self._send_json(
                    200,
                    {
                        "data": reviews.review_queue(
                            conn,
                            actor,
                            project_id,
                            kind=kind,
                            status=status,
                            attribution_label=attribution,
                            scorer_type=scorer_type,
                            run_id=run_id,
                            limit=limit,
                        )
                    },
                )
                return
            raise service.ApiError(405, "method_not_allowed", "method not allowed")
        raise service.ApiError(404, "not_found", "route not found")

    def _handle_calibration_samples(
        self,
        conn: sqlite3.Connection,
        actor: dict,
        project_id: str,
        parts: list[str],
        method: str,
        query: dict[str, list[str]],
    ) -> None:
        if method != "GET":
            raise service.ApiError(405, "method_not_allowed", "method not allowed")
        scorer_type = _query_value(query, "scorer_type", "scorer-type", default=None)
        conclusion = _query_value(query, "conclusion", default=None)
        include_unusable = _query_bool(
            query, "include_unusable", "include-unusable", default=False
        )
        run_id = _query_value(query, "run_id", "runId", default=None)
        dataset_id = _query_value(query, "dataset_id", "datasetId", default=None)
        if len(parts) == 3:  # /projects/{pid}/calibration-samples
            self._send_json(
                200,
                {
                    "data": reviews.list_calibration_samples(
                        conn,
                        actor,
                        project_id,
                        scorer_type=scorer_type,
                        conclusion=conclusion,
                        include_unusable=include_unusable,
                        run_id=run_id,
                        dataset_id=dataset_id,
                    )
                },
            )
            return
        if len(parts) == 4 and parts[3] == "export":
            fmt = (_query_value(query, "format", default="jsonl") or "jsonl").strip().lower()
            result = reviews.export_calibration(
                conn,
                actor,
                project_id,
                fmt=fmt,
                scorer_type=scorer_type,
                conclusion=conclusion,
                include_unusable=include_unusable,
                run_id=run_id,
                dataset_id=dataset_id,
            )
            extensions = {"jsonl": "jsonl", "json": "json", "csv": "csv"}
            content_type = {
                "jsonl": "application/jsonl; charset=utf-8",
                "json": "application/json; charset=utf-8",
                "csv": "text/csv; charset=utf-8",
            }[fmt]
            filename = f"calibration-{project_id}.{extensions[fmt]}"
            self._send_text(
                200,
                result["content"],
                content_type,
                {
                    "Content-Disposition": f'attachment; filename="{filename}"',
                    "X-Calibration-Count": str(result["count"]),
                    "X-Calibration-Usable-Count": str(result["usableCount"]),
                    "X-Calibration-Digest": result["contentSha256"],
                },
            )
            return
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

    # -- minimal HTML view ----------------------------------------------

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

        project = service.get_project(conn, actor["project_id"])
        specs = service.list_specs(conn, actor, actor["project_id"])
        runs = service.list_runs(conn, actor, actor["project_id"])
        project_datasets = datasets.list_datasets(conn, actor, actor["project_id"])
        self._send_html(200, _project_page(actor, project, specs, runs, project_datasets))


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
            f"<td>{_esc(run['status'])}</td>"
            f"<td>{_esc(run['created_at'])}</td>"
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
        run_rows.append('<tr><td colspan="7">No runs</td></tr>')
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
<table id="runs"><thead><tr><th>ID</th><th>Channel</th><th>Spec version</th><th>Commit</th><th>Branch</th><th>Status</th><th>Created</th></tr></thead>
<tbody>{''.join(run_rows)}</tbody></table>
<p class="muted">Only this project's data is rendered. Dataset and sample writes are available through the API and CLI for the member role.</p>
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
