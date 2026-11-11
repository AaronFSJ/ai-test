"""Cost aggregation and quota gate (T16).

The module owns the cost_entry detail rows (model calls, embeddings, runner
duration and manual adjustments), the derived cost_ledger aggregate per Run,
project pricing / per-Run / period quota configuration, and quota evaluation
plus Run termination.

Unknown usage or pricing is stored as NULL with an explicit cost_status of
missing/partial and a missing_reason. It is never rewritten as zero cost.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from . import service

COST_ENTRY_TYPES = ("model_call", "embedding", "runner", "manual")
COST_SOURCES = ("scorer", "runner", "api", "cli", "system")
COST_STATUSES = ("complete", "partial", "missing")
PERIOD_WINDOWS = ("day", "month", "total")

PRICING_KEYS = (
    "tokens_in_per_1m",
    "tokens_out_per_1m",
    "tokens_total_per_1m",
    "gpu_second",
    "currency",
)
RUN_QUOTA_KEYS = (
    "max_tokens_in",
    "max_tokens_out",
    "max_tokens_total",
    "max_gpu_seconds",
    "max_est_cost",
)
PERIOD_QUOTA_KEYS = RUN_QUOTA_KEYS + ("window",)

MISSING_NOTE = (
    "Missing usage or pricing is recorded as NULL with an explicit reason; "
    "it is not accounted as zero cost."
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _new_id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:20]


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_value(text: str | None, default: Any = None) -> Any:
    if text is None:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _number(value: Any, field: str) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise service.ApiError(400, "validation_error", f"'{field}' must be a number")
    if value < 0:
        raise service.ApiError(400, "validation_error", f"'{field}' must be >= 0")
    return value


def _integer(value: Any, field: str) -> int | None:
    result = _number(value, field)
    if result is None:
        return None
    if isinstance(result, float) and not result.is_integer():
        raise service.ApiError(400, "validation_error", f"'{field}' must be an integer")
    return int(result)


def _project_config(conn: sqlite3.Connection, project_id: str) -> dict:
    row = conn.execute("SELECT quota_config FROM project WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise service.ApiError(404, "not_found", "project not found")
    config = _json_value(row["quota_config"], {})
    return config if isinstance(config, dict) else {}


def _cost_config(config: dict) -> dict:
    pricing = config.get("pricing") if isinstance(config.get("pricing"), dict) else {}
    run_quota = config.get("run_quota")
    if not isinstance(run_quota, dict):
        run_quota = config.get("run") if isinstance(config.get("run"), dict) else {}
    period_quota = config.get("period_quota")
    if not isinstance(period_quota, dict):
        period_quota = config.get("period") if isinstance(config.get("period"), dict) else {}
    return {"pricing": pricing, "run_quota": run_quota, "period_quota": period_quota}


def _validate_pricing(values: dict) -> dict:
    if not isinstance(values, dict):
        raise service.ApiError(400, "validation_error", "'pricing' must be an object")
    unknown = set(values) - set(PRICING_KEYS)
    if unknown:
        raise service.ApiError(400, "validation_error", f"unknown pricing key(s): {sorted(unknown)}")
    result: dict[str, Any] = {}
    for key, value in values.items():
        if key == "currency":
            if not isinstance(value, str) or not value.strip():
                raise service.ApiError(400, "validation_error", "'currency' must be a non-empty string")
            result[key] = value.strip()
            continue
        number = _number(value, f"pricing.{key}")
        if number is not None:
            result[key] = number
    return result


def _validate_quota(values: dict, *, scope: str) -> dict:
    allowed = PERIOD_QUOTA_KEYS if scope == "period" else RUN_QUOTA_KEYS
    if not isinstance(values, dict):
        raise service.ApiError(400, "validation_error", f"'{scope}_quota' must be an object")
    unknown = set(values) - set(allowed)
    if unknown:
        raise service.ApiError(400, "validation_error", f"unknown {scope} quota key(s): {sorted(unknown)}")
    result: dict[str, Any] = {}
    for key, value in values.items():
        if key == "window":
            if value not in PERIOD_WINDOWS:
                raise service.ApiError(400, "validation_error", f"'window' must be one of {list(PERIOD_WINDOWS)}")
            result[key] = value
            continue
        number = _number(value, f"{scope}_quota.{key}")
        if number is not None:
            result[key] = number
    return result


def _token_values(usage: dict | None) -> tuple[int | None, int | None, int | None]:
    usage = usage if isinstance(usage, dict) else {}

    def pick(*names: str) -> int | None:
        for name in names:
            value = usage.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
        return None

    tokens_in = pick("prompt_tokens", "input_tokens")
    tokens_out = pick("completion_tokens", "output_tokens")
    tokens_total = pick("total_tokens")
    if tokens_total is None and (tokens_in is not None or tokens_out is not None):
        tokens_total = (tokens_in or 0) + (tokens_out or 0)
    return tokens_in, tokens_out, tokens_total


def _price_tokens(usage: dict | None, pricing: dict) -> dict:
    tokens_in, tokens_out, tokens_total = _token_values(usage)
    cost = None
    required = 0
    priced = 0
    reasons: list[str] = []

    def add(quantity: int | None, price_key: str) -> None:
        nonlocal cost, required, priced
        if quantity is None:
            return
        required += 1
        price = pricing.get(price_key)
        if price is None:
            return
        priced += 1
        cost = (cost or 0.0) + (quantity * float(price) / 1_000_000.0)

    add(tokens_in, "tokens_in_per_1m")
    add(tokens_out, "tokens_out_per_1m")
    if tokens_in is None and tokens_out is None:
        add(tokens_total, "tokens_total_per_1m")

    if tokens_in is None and tokens_out is None and tokens_total is None:
        status = "missing"
        reasons.append("usage_not_reported")
    elif required == 0:
        status = "missing"
        reasons.append("pricing_missing:tokens_total")
    elif priced == 0:
        status = "missing"
        reasons.append("pricing_not_configured")
    elif priced < required:
        status = "partial"
        if tokens_in is not None and "tokens_in_per_1m" not in pricing:
            reasons.append("pricing_missing:tokens_in")
        if tokens_out is not None and "tokens_out_per_1m" not in pricing:
            reasons.append("pricing_missing:tokens_out")
    else:
        status = "complete"
    return {
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tokens_total": tokens_total,
        "est_cost": round(cost, 10) if cost is not None else None,
        "cost_status": status,
        "missing_reason": ";".join(reasons) if reasons else None,
    }


def _price_gpu(gpu_seconds: float | None, pricing: dict) -> dict:
    if gpu_seconds is None:
        return {"gpu_seconds": None, "est_cost": None, "cost_status": "missing", "missing_reason": "gpu_seconds_not_reported"}
    price = pricing.get("gpu_second")
    if price is None:
        return {"gpu_seconds": gpu_seconds, "est_cost": None, "cost_status": "missing", "missing_reason": "pricing_missing:gpu_second"}
    return {"gpu_seconds": gpu_seconds, "est_cost": round(gpu_seconds * float(price), 10), "cost_status": "complete", "missing_reason": None}


def _run_row(conn: sqlite3.Connection, project_id: str, run_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM run WHERE id = ? AND project_id = ?",
        (run_id, project_id),
    ).fetchone()
    if row is None:
        raise service.ApiError(404, "not_found", "run not found")
    return row


def _insert_entry(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    run_id: str,
    entry_type: str,
    source: str,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    tokens_total: int | None = None,
    gpu_seconds: float | None = None,
    est_cost: float | None = None,
    currency: str | None = None,
    cost_status: str = "missing",
    missing_reason: str | None = None,
    run_item_id: str | None = None,
    model: str | None = None,
    usage: dict | None = None,
    pricing: dict | None = None,
    metadata: dict | None = None,
    dedupe_key: str | None = None,
    at: str | None = None,
) -> dict:
    if entry_type not in COST_ENTRY_TYPES:
        raise service.ApiError(400, "validation_error", f"invalid entry_type: {entry_type}")
    if source not in COST_SOURCES:
        raise service.ApiError(400, "validation_error", f"invalid cost source: {source}")
    if cost_status not in COST_STATUSES:
        raise service.ApiError(400, "validation_error", f"invalid cost_status: {cost_status}")
    run = _run_row(conn, project_id, run_id)
    if run_item_id is not None:
        item = conn.execute(
            "SELECT 1 FROM run_item WHERE id = ? AND run_id = ?", (run_item_id, run_id)
        ).fetchone()
        if item is None:
            raise service.ApiError(404, "not_found", "run item not found for this run")
    if dedupe_key:
        existing = conn.execute(
            "SELECT id FROM cost_entry WHERE run_id = ? AND dedupe_key = ?",
            (run_id, dedupe_key),
        ).fetchone()
        if existing is not None:
            return get_entry(conn, project_id, run_id, existing["id"])
    entry_id = _new_id("cst")
    created_at = at or now()
    conn.execute(
        """
        INSERT INTO cost_entry
          (id, project_id, run_id, run_item_id, channel, branch, entry_type,
           source, model, tokens_in, tokens_out, tokens_total, gpu_seconds,
           est_cost, currency, cost_status, missing_reason, usage_json,
           pricing_json, metadata_json, dedupe_key, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entry_id, project_id, run_id, run_item_id, run["channel"], run["branch"],
            entry_type, source, model, tokens_in, tokens_out, tokens_total, gpu_seconds,
            est_cost, currency, cost_status, missing_reason, _json_text(usage or {}),
            _json_text(pricing or {}), _json_text(metadata or {}), dedupe_key, created_at,
        ),
    )
    recompute_ledger(conn, project_id, run_id, at=created_at)
    return get_entry(conn, project_id, run_id, entry_id)


def _member_model(member: dict) -> str | None:
    detail = member.get("detail") if isinstance(member.get("detail"), dict) else {}
    config = member.get("config") if isinstance(member.get("config"), dict) else {}
    model = detail.get("model")
    if not model:
        model = config.get("model")
    if not model:
        embedding = config.get("embedding")
        if isinstance(embedding, dict):
            model = embedding.get("model")
    return model if isinstance(model, str) and model.strip() else None


def record_model_usage_from_trace(
    conn: sqlite3.Connection,
    project_id: str,
    run_id: str,
    run_item_id: str,
    trace: dict | None,
    *,
    source: str = "scorer",
) -> list[dict]:
    """Record one detail row per model-backed scorer member."""

    trace = trace or {}
    members = trace.get("members") if isinstance(trace.get("members"), list) else []
    config = _cost_config(_project_config(conn, project_id))
    pricing = config["pricing"]
    currency = pricing.get("currency")
    entries: list[dict] = []
    for index, member in enumerate(members):
        if not isinstance(member, dict):
            continue
        scorer_type = member.get("scorer_type")
        member_config = member.get("config") if isinstance(member.get("config"), dict) else {}
        entry_type = None
        if scorer_type == "llm_judge":
            entry_type = "model_call"
        elif scorer_type == "semantic_similarity" and member_config.get("metric") == "embedding":
            entry_type = "embedding"
        if entry_type is None:
            continue
        usage = member.get("usage") if isinstance(member.get("usage"), dict) else None
        model = _member_model(member)
        error = member.get("error") if isinstance(member.get("error"), dict) else None
        metadata = {
            "scorer_id": member.get("scorer_id"),
            "scorer_version_id": member.get("scorer_version_id"),
            "scorer_type": scorer_type,
            "scorer_state": member.get("state"),
            "duration_ms": member.get("duration_ms"),
        }
        dedupe_key = f"item:{run_item_id}:member:{index}"
        has_usage = usage is not None and any(
            usage.get(key) is not None
            for key in ("prompt_tokens", "input_tokens", "completion_tokens", "output_tokens", "total_tokens")
        )
        if not has_usage:
            reason = "usage_not_reported"
            if error is not None:
                reason = "model_call_error:" + str(error.get("code") or "unknown")
            entries.append(
                _insert_entry(
                    conn, project_id=project_id, run_id=run_id, run_item_id=run_item_id,
                    entry_type=entry_type, source=source, model=model, cost_status="missing",
                    missing_reason=reason, usage=usage or {}, pricing=pricing,
                    metadata=metadata, dedupe_key=dedupe_key,
                )
            )
            continue
        priced = _price_tokens(usage, pricing)
        entries.append(
            _insert_entry(
                conn, project_id=project_id, run_id=run_id, run_item_id=run_item_id,
                entry_type=entry_type, source=source, model=model,
                tokens_in=priced["tokens_in"], tokens_out=priced["tokens_out"],
                tokens_total=priced["tokens_total"], est_cost=priced["est_cost"],
                currency=currency, cost_status=priced["cost_status"],
                missing_reason=priced["missing_reason"], usage=usage, pricing=pricing,
                metadata=metadata, dedupe_key=dedupe_key,
            )
        )
    return entries


def record_runner_usage(
    conn: sqlite3.Connection,
    project_id: str,
    run_id: str,
    *,
    gpu_seconds: float | None = None,
    duration_seconds: float | None = None,
    runner_id: str | None = None,
    at: str | None = None,
) -> dict:
    """Record Runner duration as GPU seconds with an explicit source note."""

    config = _cost_config(_project_config(conn, project_id))
    pricing = config["pricing"]
    source_note = None
    if gpu_seconds is None and duration_seconds is not None:
        gpu_seconds = max(0.0, float(duration_seconds))
        source_note = "runner_wall_clock_estimate"
    priced = _price_gpu(gpu_seconds, pricing)
    return _insert_entry(
        conn, project_id=project_id, run_id=run_id, entry_type="runner", source="runner",
        gpu_seconds=priced["gpu_seconds"], est_cost=priced["est_cost"],
        currency=pricing.get("currency"), cost_status=priced["cost_status"],
        missing_reason=priced["missing_reason"], pricing=pricing,
        metadata={"runner_id": runner_id, "duration_seconds": duration_seconds,
                  "gpu_seconds_source": source_note or ("reported" if gpu_seconds is not None else None)},
        dedupe_key="runner:complete", at=at,
    )


def record_manual_entry(
    conn: sqlite3.Connection, actor: dict, project_id: str, run_id: str, payload: dict
) -> dict:
    service.require_project(actor, project_id)
    service.require_write(actor)
    config = _cost_config(_project_config(conn, project_id))
    pricing = config["pricing"]
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    for key in ("tokens_in", "tokens_out", "tokens_total"):
        if payload.get(key) is not None:
            usage[key] = payload[key]
    tokens_in = _integer(usage.get("tokens_in"), "tokens_in")
    tokens_out = _integer(usage.get("tokens_out"), "tokens_out")
    tokens_total = _integer(usage.get("tokens_total"), "tokens_total")
    gpu_seconds = _number(payload.get("gpu_seconds"), "gpu_seconds")
    if tokens_total is None and (tokens_in is not None or tokens_out is not None):
        tokens_total = (tokens_in or 0) + (tokens_out or 0)
    if all(value is None for value in (tokens_in, tokens_out, tokens_total, gpu_seconds)):
        raise service.ApiError(400, "validation_error", "provide at least one token or gpu counter")
    explicit_cost = _number(payload.get("est_cost"), "est_cost")
    if explicit_cost is not None:
        cost_status, est_cost, missing_reason = "complete", float(explicit_cost), None
    elif gpu_seconds is not None and tokens_total is None and tokens_in is None and tokens_out is None:
        priced = _price_gpu(gpu_seconds, pricing)
        cost_status, est_cost, missing_reason = priced["cost_status"], priced["est_cost"], priced["missing_reason"]
    else:
        priced = _price_tokens(
            {"prompt_tokens": tokens_in, "completion_tokens": tokens_out, "total_tokens": tokens_total},
            pricing,
        )
        cost_status, est_cost, missing_reason = priced["cost_status"], priced["est_cost"], priced["missing_reason"]
    dedupe_key = payload.get("dedupe_key")
    if dedupe_key is not None and (not isinstance(dedupe_key, str) or not dedupe_key.strip()):
        raise service.ApiError(400, "validation_error", "'dedupe_key' must be a non-empty string")
    return _insert_entry(
        conn, project_id=project_id, run_id=run_id, entry_type="manual",
        source="api" if actor.get("role") == "member" else "cli", model=payload.get("model"),
        tokens_in=tokens_in, tokens_out=tokens_out, tokens_total=tokens_total,
        gpu_seconds=gpu_seconds, est_cost=est_cost,
        currency=payload.get("currency") or pricing.get("currency"), cost_status=cost_status,
        missing_reason=missing_reason, usage=usage, pricing=pricing,
        metadata={"actor": actor.get("name"), "note": payload.get("note")},
        dedupe_key=dedupe_key.strip() if isinstance(dedupe_key, str) else None,
    )


def get_entry(conn: sqlite3.Connection, project_id: str, run_id: str, entry_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM cost_entry WHERE id = ? AND run_id = ? AND project_id = ?",
        (entry_id, run_id, project_id),
    ).fetchone()
    if row is None:
        raise service.ApiError(404, "not_found", "cost entry not found")
    return _entry_dict(row)


def _entry_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["usage"] = _json_value(data.pop("usage_json"), {})
    data["pricing"] = _json_value(data.pop("pricing_json"), {})
    data["metadata"] = _json_value(data.pop("metadata_json"), {})
    return data


def _sum_entries(rows: list) -> dict:
    def total(field: str):
        values = [row[field] for row in rows if row[field] is not None]
        return sum(values) if values else None

    missing = [row for row in rows if row["cost_status"] != "complete"]
    if not rows:
        status = "missing"
    elif not missing:
        status = "complete"
    elif len(missing) == len(rows):
        status = "missing"
    else:
        status = "partial"
    reasons: dict[str, int] = {}
    for row in missing:
        for part in str(row["missing_reason"] or "unspecified").split(";"):
            if part:
                reasons[part] = reasons.get(part, 0) + 1
    est = total("est_cost")
    return {
        "entry_count": len(rows),
        "missing_entries": len(missing),
        "tokens_in": total("tokens_in"),
        "tokens_out": total("tokens_out"),
        "tokens_total": total("tokens_total"),
        "gpu_seconds": total("gpu_seconds"),
        "est_cost": round(est, 10) if est is not None else None,
        "cost_status": status,
        "missing_reasons": reasons,
    }


def _missing_components(
    summary: dict,
    *,
    has_runner_entry: bool,
    has_model_entry: bool,
    expects_gpu: bool,
) -> list[str]:
    components: list[str] = []
    if summary["tokens_total"] is None and has_model_entry:
        components.append("tokens")
    if expects_gpu and not has_runner_entry:
        components.append("gpu_seconds")
    reasons = summary.get("missing_reasons") or {}
    if any("pricing" in reason for reason in reasons):
        components.append("pricing")
    if any("usage_not_reported" in reason for reason in reasons):
        components.append("token_usage")
    return sorted(set(components))


def recompute_ledger(
    conn: sqlite3.Connection, project_id: str, run_id: str, *, at: str | None = None
) -> dict:
    run = _run_row(conn, project_id, run_id)
    rows = conn.execute(
        "SELECT * FROM cost_entry WHERE run_id = ? ORDER BY created_at, id", (run_id,)
    ).fetchall()
    summary = _sum_entries(list(rows))
    has_runner_entry = any(row["entry_type"] == "runner" for row in rows)
    has_model_entry = any(row["entry_type"] in ("model_call", "embedding", "manual") for row in rows)
    missing_components = _missing_components(
        summary,
        has_runner_entry=has_runner_entry,
        has_model_entry=has_model_entry,
        expects_gpu=bool(run["assigned_runner_id"]) or has_runner_entry,
    )
    currency = next((row["currency"] for row in rows if row["currency"]), None)
    updated_at = at or now()
    existing = conn.execute("SELECT id FROM cost_ledger WHERE run_id = ?", (run_id,)).fetchone()
    values = (
        run["channel"], summary["tokens_in"], summary["tokens_out"], summary["tokens_total"],
        summary["gpu_seconds"], summary["est_cost"], currency, summary["cost_status"],
        summary["missing_entries"], summary["entry_count"], _json_text(missing_components),
        updated_at,
    )
    if existing is None:
        conn.execute(
            """
            INSERT INTO cost_ledger
              (id, run_id, channel, tokens_in, tokens_out, tokens_total,
               gpu_seconds, est_cost, currency, cost_status, missing_entries,
               entry_count, missing_components_json, updated_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (_new_id("clg"), run_id, *values, updated_at),
        )
    else:
        conn.execute(
            """
            UPDATE cost_ledger SET
              channel = ?, tokens_in = ?, tokens_out = ?, tokens_total = ?,
              gpu_seconds = ?, est_cost = ?, currency = ?, cost_status = ?,
              missing_entries = ?, entry_count = ?, missing_components_json = ?,
              updated_at = ?
            WHERE run_id = ?
            """,
            (*values, run_id),
        )
    return ledger_view(conn, project_id, run_id)


def ledger_view(conn: sqlite3.Connection, project_id: str, run_id: str) -> dict:
    _run_row(conn, project_id, run_id)
    row = conn.execute("SELECT * FROM cost_ledger WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        return {
            "tokens_in": None, "tokens_out": None, "tokens_total": None,
            "gpu_seconds": None, "est_cost": None, "currency": None,
            "cost_status": "missing", "missing_entries": 0, "entry_count": 0,
            "missing_components": ["detail_entries"],
            "note": "No cost entries exist for this Run. Unknown cost is not zero cost.",
        }
    data = dict(row)
    data["missing_components"] = _json_value(data.pop("missing_components_json"), [])
    data.pop("id", None)
    data["note"] = MISSING_NOTE
    return data


def get_cost_quota(conn: sqlite3.Connection, actor: dict, project_id: str) -> dict:
    service.require_project(actor, project_id)
    config = _cost_config(_project_config(conn, project_id))
    return {
        "project_id": project_id,
        "pricing": config["pricing"],
        "run_quota": config["run_quota"],
        "period_quota": config["period_quota"],
        "currency": config["pricing"].get("currency"),
        "note": "Unset limits mean unlimited. Pricing is explicit configuration, never assumed.",
    }


def set_cost_quota(conn: sqlite3.Connection, actor: dict, project_id: str, payload: dict) -> dict:
    service.require_project(actor, project_id)
    service.require_write(actor)
    if not isinstance(payload, dict):
        raise service.ApiError(400, "validation_error", "quota payload must be an object")
    unknown = set(payload) - {"pricing", "run_quota", "run", "period_quota", "period"}
    if unknown:
        raise service.ApiError(400, "validation_error", f"unknown quota section(s): {sorted(unknown)}")
    config = _project_config(conn, project_id)
    if "pricing" in payload:
        if payload["pricing"] is None:
            config.pop("pricing", None)
        else:
            existing = config.get("pricing") if isinstance(config.get("pricing"), dict) else {}
            config["pricing"] = {**existing, **_validate_pricing(payload["pricing"])}
    for scope, keys in (("run", ("run_quota", "run")), ("period", ("period_quota", "period"))):
        supplied = next((key for key in keys if key in payload), None)
        if supplied is None:
            continue
        target = "run_quota" if scope == "run" else "period_quota"
        if payload[supplied] is None:
            config.pop(target, None)
            config.pop("run" if scope == "run" else "period", None)
            continue
        existing = config.get(target, config.get("run" if scope == "run" else "period", {}))
        existing = existing if isinstance(existing, dict) else {}
        merged = {**existing, **_validate_quota(payload[supplied], scope=scope)}
        if scope == "period" and "window" not in merged:
            merged["window"] = "day"
        config[target] = merged
        config.pop("run" if scope == "run" else "period", None)
    conn.execute("UPDATE project SET quota_config = ? WHERE id = ?", (_json_text(config), project_id))
    return get_cost_quota(conn, actor, project_id)


def _window_start(window: str, at: datetime) -> datetime | None:
    if window == "total":
        return None
    if window == "month":
        return at.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return at.replace(hour=0, minute=0, second=0, microsecond=0)


def _aggregate(
    conn: sqlite3.Connection, *, project_id: str, run_id: str | None = None,
    since: datetime | None = None,
) -> dict:
    params: list[Any] = [project_id]
    where = "project_id = ?"
    if run_id is not None:
        where += " AND run_id = ?"
        params.append(run_id)
    if since is not None:
        where += " AND created_at >= ?"
        params.append(since.isoformat(timespec="milliseconds").replace("+00:00", "Z"))
    rows = conn.execute(f"SELECT * FROM cost_entry WHERE {where}", params).fetchall()
    summary = _sum_entries(list(rows))
    summary["run_count"] = len({row["run_id"] for row in rows})
    return summary


def _limit_checks(consumed: dict, limits: dict) -> tuple[list[dict], list[str], list[str]]:
    checks: list[dict] = []
    exceeded: list[str] = []
    unknown: list[str] = []
    token_consumed = consumed.get("tokens_total")
    if token_consumed is None and (
        consumed.get("tokens_in") is not None or consumed.get("tokens_out") is not None
    ):
        token_consumed = (consumed.get("tokens_in") or 0) + (consumed.get("tokens_out") or 0)
    mapping = {
        "max_tokens_in": consumed.get("tokens_in"),
        "max_tokens_out": consumed.get("tokens_out"),
        "max_tokens_total": token_consumed,
        "max_gpu_seconds": consumed.get("gpu_seconds"),
        "max_est_cost": consumed.get("est_cost"),
    }
    for key, limit in limits.items():
        if key == "window" or limit is None:
            continue
        value = mapping.get(key)
        check = {"limit": key, "value": value, "max": limit, "exceeded": False, "unknown": False}
        if value is None:
            check["unknown"] = True
            unknown.append(f"{key}:consumed_unknown")
        elif value > limit:
            check["exceeded"] = True
            exceeded.append(f"{key}: consumed {value} > limit {limit}")
        checks.append(check)
    return checks, exceeded, unknown


def evaluate_quota(conn: sqlite3.Connection, project_id: str, run_id: str) -> dict:
    config = _cost_config(_project_config(conn, project_id))
    run_limits = config["run_quota"]
    period_limits = config["period_quota"]
    configured = bool(run_limits) or bool(period_limits)
    window = period_limits.get("window") or "day"
    since = _window_start(window, datetime.now(timezone.utc))
    run_consumed = _aggregate(conn, project_id=project_id, run_id=run_id)
    period_consumed = _aggregate(conn, project_id=project_id, since=since)
    run_checks, run_exceeded, run_unknown = _limit_checks(run_consumed, run_limits)
    period_checks, period_exceeded, period_unknown = _limit_checks(period_consumed, period_limits)
    reasons = [f"run.{item}" for item in run_exceeded] + [f"period.{item}" for item in period_exceeded]
    termination_row = conn.execute(
        "SELECT * FROM cost_quota_event WHERE run_id = ? AND action = 'terminated' "
        "ORDER BY created_at DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    termination = None
    if termination_row is not None:
        termination = {
            "scope": termination_row["scope"],
            "reason": termination_row["reason"],
            "limits": _json_value(termination_row["limit_json"], {}),
            "consumed": _json_value(termination_row["consumed_json"], {}),
            "at": termination_row["created_at"],
        }
    keys = (
        "tokens_in", "tokens_out", "tokens_total", "gpu_seconds", "est_cost",
        "entry_count", "run_count", "missing_entries", "cost_status", "missing_reasons",
    )
    return {
        "configured": configured,
        "run": {
            "limits": run_limits,
            "checks": run_checks,
            "consumed": {key: run_consumed.get(key) for key in keys if key != "run_count"},
        },
        "period": {
            "window": window,
            "window_start": (
                since.isoformat(timespec="milliseconds").replace("+00:00", "Z") if since else None
            ),
            "limits": {key: value for key, value in period_limits.items() if key != "window"},
            "checks": period_checks,
            "consumed": {key: period_consumed.get(key) for key in keys},
        },
        "exceeded": bool(reasons),
        "reasons": reasons,
        "unknown": run_unknown + [f"period.{item}" for item in period_unknown],
        "terminated": termination is not None,
        "status": (
            conn.execute("SELECT status FROM run WHERE id = ?", (run_id,)).fetchone()["status"]
            if conn.execute("SELECT status FROM run WHERE id = ?", (run_id,)).fetchone()
            else None
        ),
        "termination": termination,
        "note": MISSING_NOTE,
    }


def terminate_run_for_quota(
    conn: sqlite3.Connection, project_id: str, run_id: str, quota: dict,
    *, actor: str = "cost_quota", stage: str = "cost_recording",
) -> dict:
    """Terminate a queued/running Run after a hard quota breach.

    The caller owns the transaction. If the Run is already terminal the breach
    is still audited, but the finished status is not rewritten.
    """

    row = _run_row(conn, project_id, run_id)
    at = service.now_ms()
    reasons = quota.get("reasons") or ["unknown"]
    reason = "quota_exceeded:" + ",".join(reasons)
    scope = "period" if any(str(item).startswith("period.") for item in reasons) else "run"
    if row["status"] in service.TERMINAL_RUN_STATUSES:
        _record_quota_event(
            conn, project_id, run_id, scope=scope, action="violation_after_terminal",
            reason=reason, quota=quota,
            detail={"stage": stage, "terminal_status": row["status"]}, at=at,
        )
        quota["terminated"] = False
        quota["status"] = row["status"]
        quota["note"] = (
            "Quota was exceeded after the Run had already reached a terminal status; "
            "the breach is audited but the historical status is preserved."
        )
        return quota
    service._apply_run_transition_locked(
        conn, run_id, "cancelled", source="system", actor=actor, reason="quota_exceeded",
        detail={
            "stage": stage,
            "quota": {
                "reasons": reasons,
                "run": quota.get("run", {}).get("consumed"),
                "period": quota.get("period", {}).get("consumed"),
            },
        },
        at=at,
        fields={
            "finished_at": at, "cancel_source": "quota", "cancel_reason": reason,
            "cancelled_by": actor, "cancelled_at": at, "lease_expires_at": None,
        },
    )
    _record_quota_event(
        conn, project_id, run_id, scope=scope, action="terminated", reason=reason,
        quota=quota, detail={"stage": stage, "previous_status": row["status"]}, at=at,
    )
    quota["terminated"] = True
    quota["status"] = "cancelled"
    quota["termination"] = {
        "scope": scope, "reason": reason,
        "limits": quota.get("run", {}).get("limits") if scope == "run" else quota.get("period", {}).get("limits"),
        "consumed": quota.get("run", {}).get("consumed") if scope == "run" else quota.get("period", {}).get("consumed"),
        "at": at, "stage": stage,
    }
    return quota


def _record_quota_event(
    conn: sqlite3.Connection, project_id: str, run_id: str | None, *, scope: str,
    action: str, reason: str, quota: dict, detail: dict, at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO cost_quota_event
          (id, project_id, run_id, scope, action, reason, limit_json,
           consumed_json, detail_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _new_id("cqe"), project_id, run_id, scope, action, reason,
            _json_text(quota.get("run", {}).get("limits") if scope == "run" else quota.get("period", {}).get("limits", {})),
            _json_text(quota.get("run", {}).get("consumed") if scope == "run" else quota.get("period", {}).get("consumed", {})),
            _json_text(detail), at,
        ),
    )


def enforce_run_quota(
    conn: sqlite3.Connection, project_id: str, run_id: str,
    *, actor: str = "cost_quota", stage: str = "cost_recording",
) -> dict:
    quota = evaluate_quota(conn, project_id, run_id)
    if not quota["configured"] or not quota["exceeded"]:
        return quota
    return terminate_run_for_quota(conn, project_id, run_id, quota, actor=actor, stage=stage)


def run_cost_view(conn: sqlite3.Connection, project_id: str, run_id: str) -> dict:
    run = _run_row(conn, project_id, run_id)
    ledger = ledger_view(conn, project_id, run_id)
    entries = [
        _entry_dict(row)
        for row in conn.execute(
            "SELECT * FROM cost_entry WHERE run_id = ? ORDER BY created_at, id", (run_id,)
        ).fetchall()
    ]
    return {
        "run_id": run_id,
        "project_id": project_id,
        "branch": run["branch"],
        "channel": run["channel"],
        "ledger": ledger,
        "entries": entries,
        "missing": {
            "entry_count": ledger.get("missing_entries", 0),
            "components": ledger.get("missing_components", []),
            "note": MISSING_NOTE,
        },
        "quota": evaluate_quota(conn, project_id, run_id),
        "detail_ref": f"/projects/{project_id}/runs/{run_id}/cost",
    }


def run_entries(conn: sqlite3.Connection, actor: dict, project_id: str, run_id: str) -> dict:
    service.require_project(actor, project_id)
    return run_cost_view(conn, project_id, run_id)


def cost_entries(
    conn: sqlite3.Connection, actor: dict, project_id: str, *,
    run_id: str | None = None, branch: str | None = None, channel: str | None = None,
    date_from: str | None = None, date_to: str | None = None,
) -> dict:
    service.require_project(actor, project_id)
    where = ["e.project_id = ?"]
    params: list[Any] = [project_id]
    if run_id:
        where.append("e.run_id = ?")
        params.append(run_id)
    if branch:
        where.append("e.branch = ?")
        params.append(branch)
    if channel:
        if channel not in ("A", "B"):
            raise service.ApiError(400, "validation_error", "'channel' must be 'A' or 'B'")
        where.append("e.channel = ?")
        params.append(channel)
    if date_from:
        where.append("e.created_at >= ?")
        params.append(date_from)
    if date_to:
        where.append("e.created_at <= ?")
        params.append(date_to)
    rows = conn.execute(
        f"SELECT e.* FROM cost_entry e WHERE {' AND '.join(where)} ORDER BY e.created_at, e.id",
        params,
    ).fetchall()
    entries = [_entry_dict(row) for row in rows]
    summary = _sum_entries(list(rows))
    return {
        "project_id": project_id,
        "filters": {"run_id": run_id, "branch": branch, "channel": channel, "from": date_from, "to": date_to},
        "entryCount": len(entries),
        "entries": entries,
        "summary": summary,
        "missing": {"entryCount": summary["missing_entries"], "reasons": summary["missing_reasons"], "note": MISSING_NOTE},
        "note": MISSING_NOTE,
    }


def _entry_view(entry: dict) -> dict:
    return {key: entry.get(key) for key in (
        "id", "run_id", "run_item_id", "channel", "branch", "entry_type", "source",
        "model", "tokens_in", "tokens_out", "tokens_total", "gpu_seconds",
        "est_cost", "currency", "cost_status", "missing_reason", "created_at",
    )}


def cost_summary(
    conn: sqlite3.Connection, actor: dict, project_id: str, *,
    group_by: list[str] | None = None, branch: str | None = None,
    channel: str | None = None, date_from: str | None = None, date_to: str | None = None,
) -> dict:
    service.require_project(actor, project_id)
    group_by = group_by or ["project", "branch", "channel"]
    allowed = {"project", "branch", "channel"}
    unknown = set(group_by) - allowed
    if unknown:
        raise service.ApiError(400, "validation_error", f"group_by supports {sorted(allowed)}; got {sorted(unknown)}")
    payload = cost_entries(
        conn, actor, project_id, branch=branch, channel=channel,
        date_from=date_from, date_to=date_to,
    )
    entries = payload["entries"]
    field_map = {"project": "project_id", "branch": "branch", "channel": "channel"}
    groups: dict[tuple, list[dict]] = {}
    for entry in entries:
        groups.setdefault(
            tuple(entry.get(field_map[field]) for field in group_by), []
        ).append(entry)
    group_rows = []
    for key, members in sorted(groups.items(), key=lambda item: tuple(str(v) for v in item[0])):
        summary = _sum_entries(members)
        row = {field: value for field, value in zip(group_by, key)}
        row.update({
            "entryCount": summary["entry_count"],
            "runCount": len({entry["run_id"] for entry in members}),
            "tokens_in": summary["tokens_in"],
            "tokens_out": summary["tokens_out"],
            "tokens_total": summary["tokens_total"],
            "gpu_seconds": summary["gpu_seconds"],
            "est_cost": summary["est_cost"],
            "currency": next((entry["currency"] for entry in members if entry["currency"]), None),
            "cost_status": summary["cost_status"],
            "missingEntryCount": summary["missing_entries"],
            "missingReasons": summary["missing_reasons"],
            "entryIds": [entry["id"] for entry in members[:200]],
            "entryIdsTruncated": len(members) > 200,
        })
        row["detail_ref"] = _detail_ref(
            project_id, branch=row.get("branch", branch), channel=row.get("channel", channel),
            date_from=date_from, date_to=date_to,
        )
        group_rows.append(row)
    totals = _sum_entries(entries)
    return {
        "project_id": project_id,
        "groupBy": group_by,
        "filters": payload["filters"],
        "groups": group_rows,
        "totals": {
            "entryCount": totals["entry_count"],
            "runCount": len({entry["run_id"] for entry in entries}),
            "tokens_in": totals["tokens_in"],
            "tokens_out": totals["tokens_out"],
            "tokens_total": totals["tokens_total"],
            "gpu_seconds": totals["gpu_seconds"],
            "est_cost": totals["est_cost"],
            "cost_status": totals["cost_status"],
            "missingEntryCount": totals["missing_entries"],
            "missingReasons": totals["missing_reasons"],
        },
        "missing": {"entryCount": totals["missing_entries"], "reasons": totals["missing_reasons"], "note": MISSING_NOTE},
        "detail_ref": _detail_ref(project_id, branch=branch, channel=channel, date_from=date_from, date_to=date_to),
        "reconciliation": {
            "entriesMatchGroupTotal": sum(group["entryCount"] for group in group_rows) == totals["entry_count"],
            "tokensMatch": sum(group["tokens_total"] or 0 for group in group_rows) == (totals["tokens_total"] or 0),
        },
        "note": MISSING_NOTE,
    }


def _detail_ref(
    project_id: str, *, branch: str | None, channel: str | None,
    date_from: str | None, date_to: str | None,
) -> str:
    from urllib.parse import urlencode

    query = {
        key: value
        for key, value in (("branch", branch), ("channel", channel), ("from", date_from), ("to", date_to))
        if value
    }
    suffix = f"?{urlencode(query)}" if query else ""
    return f"/projects/{project_id}/costs/entries{suffix}"
