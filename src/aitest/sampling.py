"""T10: multi-sample evaluation and consistency aggregation.

The platform does not generate model outputs itself; it scores the outputs it
receives. For a B-channel Run, sampling therefore means one of:

* the caller supplies N concrete outputs per sample (``actual_samples``), which
  is the normal case when the system under test is sampled N times; or
* one output is supplied and the bound scorer is invoked N times, which is
  useful for non-deterministic judges.

Every repetition is kept as an immutable ``run_item_sample`` row. The item
verdict stores the aggregated state plus an explanation. Aggregation never
hides errors: ``error`` repetitions are counted in the explanation and, for
score statistics, excluded from the mean/variance denominator rather than
silently treated as zero.
"""

from __future__ import annotations

import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterable

from .service import ApiError, now

STRATEGIES = ("all_pass", "majority", "expected_variance")
STRATEGY_ALIASES = {
    "all_pass": "all_pass",
    "all": "all_pass",
    "all-pass": "all_pass",
    "every": "all_pass",
    "majority": "majority",
    "majority_pass": "majority",
    "majority-pass": "majority",
    "most": "majority",
    "expected_variance": "expected_variance",
    "score_expectation_variance": "expected_variance",
    "expected-variance": "expected_variance",
    "mean_variance": "expected_variance",
}
STATES = ("pass", "fail", "uncertain", "error")
MAX_SAMPLES = 50
MAX_CONCURRENCY = 32

DEFAULT_CONFIG = {
    "n": 1,
    "strategy": "all_pass",
    "majority_threshold": 0.5,
    "expected_min": 0.8,
    "variance_max": 0.01,
    "pass_threshold": 0.5,
    "concurrency": 4,
}

_ALLOWED_KEYS = set(DEFAULT_CONFIG) | {"temperature", "seed", "batch_size", "note"}


def _number(raw: Any, field: str, *, default: float, minimum: float, maximum: float) -> float:
    value = default if raw is None else raw
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ApiError(400, "validation_error", f"sampling.{field} must be a number")
    value = float(value)
    if not minimum <= value <= maximum:
        raise ApiError(
            400,
            "validation_error",
            f"sampling.{field} must be between {minimum:g} and {maximum:g}",
        )
    return value


def _integer(raw: Any, field: str, *, default: int, minimum: int, maximum: int) -> int:
    value = default if raw is None else raw
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiError(400, "validation_error", f"sampling.{field} must be an integer")
    if not minimum <= value <= maximum:
        raise ApiError(
            400,
            "validation_error",
            f"sampling.{field} must be between {minimum} and {maximum}",
        )
    return value


def normalize_config(raw: Any, *, base: dict | None = None) -> dict:
    """Validate a sampling configuration and apply documented defaults.

    ``raw`` may only contain known keys; an unknown key is a validation error
    instead of silently changing the Run's cost profile. ``base`` supplies
    values for keys omitted from ``raw`` (used to merge a Run's stored config
    with a per-scoring override).
    """

    merged: dict[str, Any] = dict(base or {})
    if raw is not None:
        if not isinstance(raw, dict):
            raise ApiError(400, "validation_error", "'sampling' must be an object")
        unknown = sorted(set(raw) - _ALLOWED_KEYS)
        if unknown:
            raise ApiError(
                400,
                "validation_error",
                f"unknown sampling key(s): {', '.join(unknown)}",
                details={"allowed": sorted(_ALLOWED_KEYS)},
            )
        merged.update(raw)

    n_raw = merged.get("n", merged.get("samples", merged.get("repeats", merged.get("repeat"))))
    n = _integer(n_raw, "n", default=DEFAULT_CONFIG["n"], minimum=1, maximum=MAX_SAMPLES)

    strategy_raw = merged.get("strategy", merged.get("consistency", DEFAULT_CONFIG["strategy"]))
    if not isinstance(strategy_raw, str):
        raise ApiError(400, "validation_error", "sampling.strategy must be a string")
    strategy = STRATEGY_ALIASES.get(strategy_raw.strip().lower())
    if strategy is None:
        raise ApiError(
            400,
            "validation_error",
            f"sampling.strategy must be one of {list(STRATEGIES)}",
            details={"allowed": list(STRATEGIES), "aliases": sorted(STRATEGY_ALIASES)},
        )

    concurrency_raw = merged.get(
        "concurrency", merged.get("workers", merged.get("batch_size"))
    )
    concurrency = _integer(
        concurrency_raw,
        "concurrency",
        default=min(DEFAULT_CONFIG["concurrency"], n),
        minimum=1,
        maximum=MAX_CONCURRENCY,
    )

    config = {
        "n": n,
        "strategy": strategy,
        "majority_threshold": _number(
            merged.get("majority_threshold", merged.get("min_pass_ratio")),
            "majority_threshold",
            default=DEFAULT_CONFIG["majority_threshold"],
            minimum=0.0,
            maximum=1.0,
        ),
        "expected_min": _number(
            merged.get("expected_min", merged.get("mean_min")),
            "expected_min",
            default=DEFAULT_CONFIG["expected_min"],
            minimum=0.0,
            maximum=1.0,
        ),
        "variance_max": _number(
            merged.get("variance_max", merged.get("max_variance")),
            "variance_max",
            default=DEFAULT_CONFIG["variance_max"],
            minimum=0.0,
            maximum=0.25,
        ),
        "pass_threshold": _number(
            merged.get("pass_threshold"),
            "pass_threshold",
            default=DEFAULT_CONFIG["pass_threshold"],
            minimum=0.0,
            maximum=1.0,
        ),
        "concurrency": concurrency,
    }
    if merged.get("temperature") is not None:
        config["temperature"] = _number(
            merged["temperature"], "temperature", default=0.0, minimum=0.0, maximum=2.0
        )
    if merged.get("seed") is not None:
        config["seed"] = _integer(
            merged["seed"], "seed", default=0, minimum=0, maximum=2**31 - 1
        )
    if merged.get("note") is not None:
        if not isinstance(merged["note"], str):
            raise ApiError(400, "validation_error", "sampling.note must be a string")
        config["note"] = merged["note"][:500]
    return config


def config_from_run(raw: Any) -> dict:
    """Normalize a Run's stored config; an empty object means platform defaults."""

    if raw in (None, {}, ""):
        return normalize_config(None)
    return normalize_config(raw)


def sampling_request(payload: dict) -> Any:
    if "sampling" in payload and payload.get("sampling") is not None:
        if "sampling_config" in payload and payload.get("sampling_config") is not None:
            raise ApiError(
                400,
                "validation_error",
                "pass at most one of 'sampling' or 'sampling_config'",
            )
        return payload["sampling"]
    return payload.get("sampling_config")


def merge_run_config(run_config: Any, override: Any) -> dict:
    base = config_from_run(run_config)
    if override is None:
        return base
    # Only keys explicitly present in the override replace the Run values.
    if not isinstance(override, dict):
        raise ApiError(400, "validation_error", "'sampling' must be an object")
    return normalize_config(override, base=base)


def score_of(result: Any) -> float | None:
    """Numeric score used for expectation/variance, independent of the state."""

    detail = getattr(result, "detail", None) or {}
    for key in ("score", "similarity"):
        value = detail.get(key) if isinstance(detail, dict) else None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return round(max(0.0, min(1.0, float(value))), 6)
    value = getattr(result, "score", None)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return round(max(0.0, min(1.0, float(value))), 6)
    return None


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 6) if values else None


def _variance(values: list[float]) -> float | None:
    if not values:
        return None
    mean = sum(values) / len(values)
    return round(sum((value - mean) ** 2 for value in values) / len(values), 6)


def _confidence_for(samples: list[dict], state: str) -> float:
    if not samples:
        return 0.0
    if state == "error":
        return 0.0
    if state == "uncertain":
        pool = samples
    else:
        pool = [sample for sample in samples if sample["state"] == state] or samples
    return round(sum(float(sample.get("confidence") or 0.0) for sample in pool) / len(pool), 6)


def aggregate(samples: list[dict], config: dict) -> tuple[str, float, dict]:
    """Aggregate per-repetition results into one item verdict.

    ``samples`` entries need ``state``, ``confidence`` and optionally ``score``.
    """

    if not samples:
        raise ApiError(400, "validation_error", "cannot aggregate an empty sample set")
    counts = {state: 0 for state in STATES}
    for sample in samples:
        if sample.get("state") not in counts:
            raise ApiError(400, "validation_error", f"invalid sample state: {sample.get('state')!r}")
        counts[sample["state"]] += 1
    n = len(samples)
    pass_ratio = round(counts["pass"] / n, 6)
    fail_ratio = round(counts["fail"] / n, 6)
    uncertain_ratio = round(counts["uncertain"] / n, 6)
    error_ratio = round(counts["error"] / n, 6)
    scored = [float(sample["score"]) for sample in samples if sample.get("score") is not None]
    mean_score = _mean(scored)
    variance = _variance(scored)
    strategy = config["strategy"]
    reason = None

    if strategy == "all_pass":
        if counts["fail"]:
            state = "fail"
            reason = "at least one repetition failed"
        elif counts["uncertain"]:
            state = "uncertain"
            reason = "at least one repetition is uncertain"
        elif counts["error"]:
            state = "error"
            reason = "all remaining repetitions are errors; no clean pass set"
        else:
            state = "pass"
            reason = "all repetitions passed"
        rule = "pass only when every repetition is pass; fail dominates, then uncertain, then error"
    elif strategy == "majority":
        threshold = float(config["majority_threshold"])
        required = max(1, math.ceil(n * threshold))
        if counts["error"] == n:
            state = "error"
            reason = "every repetition errored"
        elif counts["pass"] >= required and counts["pass"] > counts["fail"]:
            state = "pass"
            reason = f"{counts['pass']}/{n} pass met majority threshold {threshold:g} ({required} required)"
        elif counts["fail"] >= required and counts["fail"] > counts["pass"]:
            state = "fail"
            reason = f"{counts['fail']}/{n} fail met majority threshold {threshold:g} ({required} required)"
        elif counts["pass"] == counts["fail"] and counts["pass"]:
            state = "uncertain"
            reason = f"tie between pass and fail at {counts['pass']}/{n}; no majority"
        else:
            state = "uncertain"
            reason = (
                f"neither pass ({counts['pass']}/{n}) nor fail ({counts['fail']}/{n}) "
                f"reached threshold {threshold:g} ({required} required)"
            )
        rule = "pass/fail when its count reaches ceil(N * majority_threshold) and exceeds the other; otherwise uncertain"
    elif strategy == "expected_variance":
        expected_min = float(config["expected_min"])
        variance_max = float(config["variance_max"])
        if mean_score is None or not scored:
            state = "error"
            reason = "no non-error repetition produced a numeric score"
        elif mean_score < expected_min:
            state = "fail"
            reason = f"mean score {mean_score:g} is below expected_min {expected_min:g}"
        elif variance is not None and variance > variance_max:
            state = "uncertain"
            reason = f"variance {variance:g} exceeds variance_max {variance_max:g}; results are unstable"
        else:
            state = "pass"
            reason = (
                f"mean score {mean_score:g} >= expected_min {expected_min:g} and "
                f"variance {variance:g} <= variance_max {variance_max:g}"
            )
        rule = (
            "fail when mean score < expected_min; uncertain when variance > variance_max; "
            "otherwise pass. Error repetitions are excluded from mean/variance."
        )
    else:  # pragma: no cover - normalize_config rejects unknown strategies
        raise ApiError(400, "validation_error", f"unknown sampling strategy: {strategy}")

    confidence = _confidence_for(samples, state)
    aggregation = {
        "schemaVersion": 1,
        "strategy": strategy,
        "n": n,
        "counts": counts,
        "ratios": {
            "pass": pass_ratio,
            "fail": fail_ratio,
            "uncertain": uncertain_ratio,
            "error": error_ratio,
        },
        "pass_ratio": pass_ratio,
        "fail_ratio": fail_ratio,
        "mean_score": mean_score,
        "variance": variance,
        "scored_samples": len(scored),
        "error_samples_excluded": counts["error"],
        "thresholds": {
            "majority_threshold": config["majority_threshold"],
            "expected_min": config["expected_min"],
            "variance_max": config["variance_max"],
            "pass_threshold": config["pass_threshold"],
        },
        "state": state,
        "reason": reason,
        "rule": rule,
        "explanation": (
            f"strategy={strategy}; counts={counts}; mean_score={mean_score}; "
            f"variance={variance}; state={state}; reason={reason}"
        ),
    }
    return state, confidence, aggregation


def cost_hint(
    config: dict,
    *,
    item_count: int | None = None,
    scorer_calls_per_item: int | None = None,
    token_usage: dict | None = None,
) -> dict:
    n = int(config.get("n") or 1)
    hint: dict[str, Any] = {
        "multiplier": n,
        "note": (
            f"N={n} sampling multiplies scorer invocations and model token usage by {n}; "
            "review quota before running large suites."
        ),
        "tokens_in": None,
        "tokens_out": None,
        "tokens_total": None,
        "tokensNote": (
            "Token counters are null when the bound scorers make no model calls; "
            "missing cost is not recorded as zero."
        ),
    }
    if item_count is not None:
        hint["items"] = item_count
        hint["sampledEvaluations"] = item_count * n
        if scorer_calls_per_item is not None:
            hint["scorerCalls"] = item_count * n * scorer_calls_per_item
    if token_usage:
        for key in ("tokens_in", "tokens_out", "tokens_total"):
            if token_usage.get(key) is not None:
                hint[key] = int(token_usage[key])
        hint["tokensNote"] = "Sum across repetitions; null means the scorers reported no token usage."
    return hint


def run_batch(
    tasks: Iterable[Any],
    worker: Callable[[Any], Any],
    *,
    concurrency: int,
) -> tuple[list[Any], dict]:
    """Run tasks with a bounded worker pool and return real concurrency stats.

    This is the "batch inference" boundary: model-backed scorers are submitted
    as one batch and the pool keeps at most ``concurrency`` requests in flight,
    which is what a continuous-batching endpoint such as vLLM needs to stay
    busy without unbounded fan-out.
    """

    items = list(tasks)
    concurrency = max(1, min(int(concurrency), MAX_CONCURRENCY, max(1, len(items))))
    started = time.monotonic()
    lock = threading.Lock()
    inflight = {"current": 0, "max": 0}

    def wrapped(task: Any) -> Any:
        with lock:
            inflight["current"] += 1
            inflight["max"] = max(inflight["max"], inflight["current"])
        try:
            return worker(task)
        finally:
            with lock:
                inflight["current"] -= 1

    if concurrency == 1 or len(items) <= 1:
        results = [wrapped(task) for task in items]
    else:
        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="aitest-sample") as pool:
            results = list(pool.map(wrapped, items))
    elapsed_ms = int((time.monotonic() - started) * 1000)
    return results, {
        "tasks": len(items),
        "concurrency": concurrency,
        "maxInflight": inflight["max"],
        "elapsedMs": elapsed_ms,
        "mode": "thread_pool" if concurrency > 1 and len(items) > 1 else "sequential",
    }


def model_snapshot(run_snapshot: Any, config: dict, traces: list[dict]) -> dict:
    """Resolve the model snapshot actually used by this scoring pass."""

    models: dict[str, dict] = {}
    for trace in traces:
        for member in trace.get("members") or []:
            detail = member.get("detail") or {}
            member_config = member.get("config") or {}
            model = detail.get("model") or member_config.get("model")
            endpoint = detail.get("endpoint") or member_config.get("endpoint")
            temperature = detail.get("temperature", member_config.get("temperature"))
            if not (model or endpoint):
                continue
            key = "|".join(
                str(part)
                for part in (
                    member.get("scorer_version_id"),
                    member.get("scorer_type"),
                    model,
                    endpoint,
                    temperature,
                )
            )
            models[key] = {
                "scorer_version_id": member.get("scorer_version_id"),
                "scorer_id": member.get("scorer_id"),
                "scorer_type": member.get("scorer_type"),
                "model": model,
                "endpoint": endpoint,
                "temperature": temperature,
            }
    return {
        "run_snapshot": run_snapshot,
        "scorer_models": [models[key] for key in sorted(models)],
        "sampling": {
            "temperature": config.get("temperature"),
            "seed": config.get("seed"),
            "n": config["n"],
            "strategy": config["strategy"],
            "concurrency": config["concurrency"],
        },
        "captured_at": now(),
        "note": (
            "Run-level model snapshot is copied from the Run; scorer_models lists the exact "
            "judge/embedding models and endpoints observed in this scoring pass."
        ),
    }
