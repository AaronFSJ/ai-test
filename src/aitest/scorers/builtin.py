"""Built-in scorers: exact match, regex, JSON Schema, rules, similarity,
LLM-as-Judge and a human-review fallback.

Every scorer is deterministic given its inputs except the two model-backed
paths (LLM judge and embedding similarity), whose failures are classified and
returned as an ``error`` verdict instead of raising into the Run loop.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from typing import Any

from .base import (
    MISSING,
    BaseScorer,
    ScoreResult,
    canonical_json,
    error_result,
    json_ready,
    normalize_state,
    text_of,
)
from .jsonpath import read_path
from .llm import ModelEndpointError, chat, embed, extract_json_object
from .schema import SchemaConfigError, validate_instance, validate_schema

_NUMBER = (int, float)
_TOKEN = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")

RULE_OPS = {
    "exists", "not_exists", "eq", "ne", "gt", "gte", "lt", "lte",
    "contains", "not_contains", "matches", "in", "not_in", "type_is",
    "length_eq", "length_gte", "length_lte",
}
VALUE_OPS = RULE_OPS - {"exists", "not_exists"}


class ConfigError(ValueError):
    """Invalid scorer configuration (mapped to HTTP 400 by the service)."""


def _is_number(value: Any) -> bool:
    return isinstance(value, _NUMBER) and not isinstance(value, bool)


def _require_keys(config: dict, allowed: set[str], scorer_type: str) -> None:
    if not isinstance(config, dict):
        raise ConfigError("scorer config must be a JSON object")
    unknown = sorted(set(config) - allowed)
    if unknown:
        raise ConfigError(f"{scorer_type} config uses unknown key(s): {', '.join(unknown)}")


def _bool(config: dict, key: str, default: bool) -> bool:
    value = config.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"'{key}' must be a boolean")
    return value


def _number(config: dict, key: str, default: float, *, low: float, high: float) -> float:
    value = config.get(key, default)
    if not _is_number(value) or not low <= float(value) <= high:
        raise ConfigError(f"'{key}' must be a number between {low:g} and {high:g}")
    return float(value)


def _int(config: dict, key: str, default: int, *, low: int, high: int) -> int:
    value = config.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ConfigError(f"'{key}' must be an integer between {low} and {high}")
    return value


def _text(config: dict, key: str, *, required: bool = False, default: str | None = None) -> str | None:
    value = config.get(key, default)
    if value is None:
        if required:
            raise ConfigError(f"'{key}' is required")
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"'{key}' must be a non-empty string")
    return value.strip()


def _path(config: dict, key: str) -> str | None:
    value = config.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"'{key}' must be a JSON path string")
    if value and value not in ("$", ""):
        read_path({}, value)
    return value


def validate_config(scorer_type: str, config: Any) -> dict:
    """Validate and normalize one scorer configuration."""

    if not isinstance(config, dict):
        raise ConfigError("scorer config must be a JSON object")
    config = json.loads(canonical_json(config))

    if scorer_type == "exact_match":
        _require_keys(
            config,
            {"case_sensitive", "trim", "normalize_whitespace", "expected_path", "actual_path", "numeric_tolerance"},
            scorer_type,
        )
        normalized = {
            "case_sensitive": _bool(config, "case_sensitive", True),
            "trim": _bool(config, "trim", True),
            "normalize_whitespace": _bool(config, "normalize_whitespace", False),
            "expected_path": _path(config, "expected_path"),
            "actual_path": _path(config, "actual_path"),
            "numeric_tolerance": None,
        }
        if config.get("numeric_tolerance") is not None:
            tolerance = config["numeric_tolerance"]
            if not _is_number(tolerance) or tolerance < 0:
                raise ConfigError("'numeric_tolerance' must be a non-negative number")
            normalized["numeric_tolerance"] = float(tolerance)
        return normalized

    if scorer_type == "regex":
        _require_keys(config, {"pattern", "flags", "target", "mode", "path", "negate"}, scorer_type)
        pattern = _text(config, "pattern", required=True)
        flags_text = config.get("flags", "") or ""
        if not isinstance(flags_text, str) or any(char not in "ims" for char in flags_text):
            raise ConfigError("'flags' may only contain 'i', 'm' and 's'")
        flags = 0
        for char, flag in (("i", re.IGNORECASE), ("m", re.MULTILINE), ("s", re.DOTALL)):
            if char in flags_text:
                flags |= flag
        try:
            re.compile(pattern, flags)
        except re.error as exc:
            raise ConfigError(f"'pattern' is not a valid regular expression: {exc}") from exc
        target = config.get("target", "actual")
        if target not in ("actual", "expected"):
            raise ConfigError("'target' must be 'actual' or 'expected'")
        mode = config.get("mode", "search")
        if mode not in ("search", "fullmatch"):
            raise ConfigError("'mode' must be 'search' or 'fullmatch'")
        return {
            "pattern": pattern,
            "flags": flags_text,
            "target": target,
            "mode": mode,
            "path": _path(config, "path"),
            "negate": _bool(config, "negate", False),
        }

    if scorer_type == "json_schema":
        _require_keys(config, {"schema", "parse_string", "actual_path"}, scorer_type)
        if "schema" not in config:
            raise ConfigError("'schema' is required")
        try:
            validate_schema(config["schema"])
        except SchemaConfigError as exc:
            raise ConfigError(str(exc)) from exc
        return {
            "schema": config["schema"],
            "parse_string": _bool(config, "parse_string", True),
            "actual_path": _path(config, "actual_path"),
        }

    if scorer_type == "rule_assertion":
        _require_keys(
            config, {"assertions", "require_all", "on_missing", "on_eval_error"}, scorer_type
        )
        assertions = config.get("assertions")
        if not isinstance(assertions, list) or not assertions:
            raise ConfigError("'assertions' must be a non-empty array")
        normalized_assertions = []
        for index, item in enumerate(assertions):
            if not isinstance(item, dict):
                raise ConfigError(f"assertions[{index}] must be an object")
            unknown = sorted(set(item) - {"path", "op", "value", "message", "source"})
            if unknown:
                raise ConfigError(f"assertions[{index}] uses unknown key(s): {', '.join(unknown)}")
            op = item.get("op")
            if op not in RULE_OPS:
                raise ConfigError(f"assertions[{index}].op must be one of {sorted(RULE_OPS)}")
            path = item.get("path", "$")
            if not isinstance(path, str):
                raise ConfigError(f"assertions[{index}].path must be a string")
            if path not in ("$", ""):
                read_path({}, path)
            source = item.get("source", "actual")
            if source not in ("actual", "expected", "context"):
                raise ConfigError(f"assertions[{index}].source must be actual, expected or context")
            if op in VALUE_OPS and "value" not in item:
                raise ConfigError(f"assertions[{index}].op '{op}' requires 'value'")
            if op == "matches":
                if not isinstance(item["value"], str):
                    raise ConfigError(f"assertions[{index}].value must be a regex string")
                try:
                    re.compile(item["value"])
                except re.error as exc:
                    raise ConfigError(
                        f"assertions[{index}].value is not a valid regex: {exc}"
                    ) from exc
            normalized = {
                "path": path,
                "op": op,
                "message": item.get("message"),
                "source": source,
            }
            if "value" in item:
                normalized["value"] = item["value"]
            normalized_assertions.append(normalized)
        on_missing = config.get("on_missing", "uncertain")
        if on_missing not in ("uncertain", "fail", "error"):
            raise ConfigError("'on_missing' must be uncertain, fail or error")
        on_eval_error = config.get("on_eval_error", "fail")
        if on_eval_error not in ("uncertain", "fail", "error"):
            raise ConfigError("'on_eval_error' must be uncertain, fail or error")
        return {
            "assertions": normalized_assertions,
            "require_all": _bool(config, "require_all", True),
            "on_missing": on_missing,
            "on_eval_error": on_eval_error,
        }

    if scorer_type == "semantic_similarity":
        _require_keys(
            config,
            {"threshold", "metric", "expected_path", "actual_path", "case_sensitive", "embedding"},
            scorer_type,
        )
        metric = config.get("metric", "lexical_cosine")
        if metric not in ("lexical_cosine", "jaccard", "embedding"):
            raise ConfigError("'metric' must be lexical_cosine, jaccard or embedding")
        normalized = {
            "threshold": _number(config, "threshold", 0.8, low=0.0, high=1.0),
            "metric": metric,
            "expected_path": _path(config, "expected_path"),
            "actual_path": _path(config, "actual_path"),
            "case_sensitive": _bool(config, "case_sensitive", False),
            "embedding": None,
        }
        if metric == "embedding":
            embedding = config.get("embedding")
            if not isinstance(embedding, dict):
                raise ConfigError("'embedding' must be an object when metric is 'embedding'")
            unknown = sorted(set(embedding) - {"endpoint", "model", "timeout_s", "api_key_env"})
            if unknown:
                raise ConfigError(f"embedding uses unknown key(s): {', '.join(unknown)}")
            endpoint = embedding.get("endpoint")
            model_name = embedding.get("model")
            if not isinstance(endpoint, str) or not endpoint.startswith(("http://", "https://")):
                raise ConfigError("embedding.endpoint must be an http(s) URL")
            if not isinstance(model_name, str) or not model_name.strip():
                raise ConfigError("embedding.model must be a non-empty string")
            api_key_env = embedding.get("api_key_env")
            if api_key_env is not None and (
                not isinstance(api_key_env, str)
                or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", api_key_env)
            ):
                raise ConfigError("embedding.api_key_env must be an environment variable name")
            normalized["embedding"] = {
                "endpoint": endpoint,
                "model": model_name.strip(),
                "timeout_s": _number(embedding, "timeout_s", 10.0, low=0.1, high=300.0),
                "api_key_env": api_key_env,
            }
        elif "embedding" in config:
            raise ConfigError("'embedding' is only valid when metric is 'embedding'")
        return normalized

    if scorer_type == "llm_judge":
        _require_keys(
            config,
            {
                "endpoint", "model", "pass_threshold", "temperature", "max_tokens",
                "timeout_s", "api_key_env", "system_prompt", "response_format",
                "max_input_chars",
            },
            scorer_type,
        )
        endpoint = _text(config, "endpoint", required=True)
        if not endpoint.startswith(("http://", "https://")):
            raise ConfigError("'endpoint' must be an http(s) URL")
        model_name = _text(config, "model", required=True)
        if "api_key" in config or "token" in config:
            raise ConfigError("literal credentials are not allowed; use 'api_key_env'")
        api_key_env = config.get("api_key_env")
        if api_key_env is not None and (
            not isinstance(api_key_env, str)
            or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", api_key_env)
        ):
            raise ConfigError("'api_key_env' must be an environment variable name")
        response_format = config.get("response_format", "json_object")
        if response_format not in ("json_object", None):
            raise ConfigError("'response_format' must be 'json_object' or null")
        return {
            "endpoint": endpoint,
            "model": model_name,
            "pass_threshold": _number(config, "pass_threshold", 0.7, low=0.0, high=1.0),
            "temperature": _number(config, "temperature", 0.0, low=0.0, high=2.0),
            "max_tokens": _int(config, "max_tokens", 512, low=1, high=8192),
            "timeout_s": _number(config, "timeout_s", 10.0, low=0.1, high=300.0),
            "api_key_env": api_key_env,
            "system_prompt": _text(config, "system_prompt", default=None),
            "response_format": response_format,
            "max_input_chars": _int(config, "max_input_chars", 20000, low=500, high=200000),
        }

    if scorer_type == "human_review":
        _require_keys(config, {"queue", "allow_decision", "decision_path", "default_state"}, scorer_type)
        if config.get("default_state", "uncertain") != "uncertain":
            raise ConfigError("human_review always defaults to 'uncertain'; it cannot auto-pass")
        return {
            "queue": _text(config, "queue", default="manual-review") or "manual-review",
            "allow_decision": _bool(config, "allow_decision", True),
            "decision_path": _path(config, "decision_path") or "$.human_review",
            "default_state": "uncertain",
        }

    raise ConfigError(f"unknown scorer type: {scorer_type}")


def _extract(value: Any, path: str | None) -> Any:
    if path in (None, "", "$"):
        return value
    return read_path(value, path)


def _normalize_string(value: str, config: dict) -> str:
    text = value
    if config.get("trim", True):
        text = text.strip()
    if config.get("normalize_whitespace"):
        text = re.sub(r"\s+", " ", text)
    if not config.get("case_sensitive", True):
        text = text.casefold()
    return text


def _short(value: Any, limit: int = 2000) -> Any:
    ready = json_ready(value)
    if isinstance(ready, str) and len(ready) > limit:
        return ready[:limit] + "..."
    if isinstance(ready, (dict, list)):
        text = canonical_json(ready)
        if len(text) > limit:
            return {"__truncated__": text[:limit]}
    return ready


class ExactMatchScorer(BaseScorer):
    type = "exact_match"

    def evaluate(self, sample: dict, actual: Any, context: dict | None = None) -> ScoreResult:
        expected = _extract(sample.get("expected", MISSING), self.config.get("expected_path"))
        actual_value = _extract(actual, self.config.get("actual_path"))
        if expected is MISSING or expected is None:
            return error_result("missing_expected", "exact_match requires a non-null sample 'expected' value")
        if actual_value is MISSING:
            return error_result("missing_actual", "exact_match could not read the configured actual path")

        compared_expected: Any = expected
        compared_actual: Any = actual_value
        normalized = isinstance(expected, str) and isinstance(actual_value, str)
        if normalized:
            compared_expected = _normalize_string(expected, self.config)
            compared_actual = _normalize_string(actual_value, self.config)

        tolerance = self.config.get("numeric_tolerance")
        if _is_number(expected) and _is_number(actual_value) and tolerance is not None:
            passed = math.isclose(float(expected), float(actual_value), abs_tol=float(tolerance))
        elif _is_number(expected) and _is_number(actual_value):
            passed = float(expected) == float(actual_value)
        elif isinstance(expected, bool) or isinstance(actual_value, bool):
            passed = expected is actual_value
        elif normalized:
            passed = compared_expected == compared_actual
        else:
            passed = canonical_json(json_ready(expected)) == canonical_json(json_ready(actual_value))

        return ScoreResult(
            state="pass" if passed else "fail",
            confidence=1.0,
            detail={
                "expected": _short(expected),
                "actual": _short(actual_value),
                "normalized_expected": _short(compared_expected),
                "normalized_actual": _short(compared_actual),
                "case_sensitive": self.config.get("case_sensitive"),
                "numeric_tolerance": tolerance,
                "matched": passed,
            },
        )


class RegexScorer(BaseScorer):
    type = "regex"

    def evaluate(self, sample: dict, actual: Any, context: dict | None = None) -> ScoreResult:
        source = actual if self.config.get("target") == "actual" else sample.get("expected", MISSING)
        value = _extract(source, self.config.get("path"))
        if value is MISSING:
            return error_result("missing_target", "regex scorer could not read its target value")
        text = text_of(value)
        flags = 0
        for char, flag in (("i", re.IGNORECASE), ("m", re.MULTILINE), ("s", re.DOTALL)):
            if char in (self.config.get("flags") or ""):
                flags |= flag
        pattern = re.compile(self.config["pattern"], flags)
        match = pattern.fullmatch(text) if self.config.get("mode") == "fullmatch" else pattern.search(text)
        matched = match is not None
        passed = (not matched) if self.config.get("negate") else matched
        return ScoreResult(
            state="pass" if passed else "fail",
            confidence=1.0,
            detail={
                "pattern": self.config["pattern"],
                "flags": self.config.get("flags"),
                "mode": self.config.get("mode"),
                "target": self.config.get("target"),
                "negate": bool(self.config.get("negate")),
                "matched": matched,
                "span": list(match.span()) if match else None,
                "value": _short(text),
            },
        )


class JsonSchemaScorer(BaseScorer):
    type = "json_schema"

    def evaluate(self, sample: dict, actual: Any, context: dict | None = None) -> ScoreResult:
        value = _extract(actual, self.config.get("actual_path"))
        if value is MISSING:
            return error_result("missing_actual", "json_schema scorer could not read the actual output")
        if isinstance(value, str) and self.config.get("parse_string", True):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                return ScoreResult(
                    state="fail",
                    confidence=1.0,
                    detail={
                        "reason": "actual output is not valid JSON",
                        "parse_error": {"message": exc.msg, "position": exc.pos},
                        "errors": [
                            {"path": "$", "keyword": "json", "message": f"invalid JSON: {exc.msg}"}
                        ],
                    },
                )
        errors = validate_instance(value, self.config["schema"], "$")
        return ScoreResult(
            state="pass" if not errors else "fail",
            confidence=1.0,
            detail={
                "valid": not errors,
                "errors": errors,
                "schema": self.config["schema"],
                "actual": _short(value),
            },
        )


def _apply_rule(op: str, value: Any, operand: Any) -> bool:
    if op == "eq":
        return value == operand
    if op == "ne":
        return value != operand
    if op in ("gt", "gte", "lt", "lte"):
        if not _is_number(value) or not _is_number(operand):
            raise ValueError("numeric comparison requires numbers")
        if op == "gt":
            return float(value) > float(operand)
        if op == "gte":
            return float(value) >= float(operand)
        if op == "lt":
            return float(value) < float(operand)
        return float(value) <= float(operand)
    if op == "contains":
        if isinstance(value, str):
            return str(operand) in value
        if isinstance(value, (list, tuple)):
            return operand in value
        if isinstance(value, dict):
            return operand in value
        return False
    if op == "not_contains":
        return not _apply_rule("contains", value, operand)
    if op == "matches":
        if not isinstance(value, str):
            raise ValueError("matches requires a string value")
        return re.search(str(operand), value) is not None
    if op == "in":
        if not isinstance(operand, (list, tuple)):
            raise ValueError("'in' requires an array operand")
        return value in operand
    if op == "not_in":
        if not isinstance(operand, (list, tuple)):
            raise ValueError("'not_in' requires an array operand")
        return value not in operand
    if op == "type_is":
        from .schema import _type_name

        return _type_name(value) == operand
    if op in ("length_eq", "length_gte", "length_lte"):
        if not isinstance(value, (str, list, tuple, dict)):
            raise ValueError("length comparison requires a string, array or object")
        if not isinstance(operand, int) or isinstance(operand, bool):
            raise ValueError("length operand must be an integer")
        if op == "length_eq":
            return len(value) == operand
        if op == "length_gte":
            return len(value) >= operand
        return len(value) <= operand
    raise ValueError(f"unsupported rule op: {op}")


def _assertion_outcome(name: str) -> str:
    return {"fail": "failed", "uncertain": "uncertain", "error": "error"}[name]


class RuleAssertionScorer(BaseScorer):
    type = "rule_assertion"

    def evaluate(self, sample: dict, actual: Any, context: dict | None = None) -> ScoreResult:
        results: list[dict] = []
        outcomes: list[str] = []
        for assertion in self.config["assertions"]:
            source_name = assertion["source"]
            source = {
                "actual": actual,
                "expected": sample.get("expected", MISSING),
                "context": (context or {}).get("data", context) if context is not None else MISSING,
            }[source_name]
            op = assertion["op"]
            path = assertion["path"]
            entry: dict = {
                "path": path,
                "op": op,
                "source": source_name,
                "message": assertion.get("message"),
            }
            if op in ("exists", "not_exists"):
                found = read_path(source, path) is not MISSING
                passed = found if op == "exists" else not found
                entry.update({"passed": passed, "actual": found})
                outcomes.append("passed" if passed else "failed")
                results.append(entry)
                continue

            value = read_path(source, path)
            if value is MISSING:
                entry.update({"passed": False, "missing": True, "reason": "path is missing"})
                outcomes.append(_assertion_outcome(self.config["on_missing"]))
                results.append(entry)
                continue
            try:
                passed = _apply_rule(op, value, assertion["value"])
            except ValueError as exc:
                entry.update(
                    {"passed": False, "error": str(exc), "actual": _short(value)}
                )
                outcomes.append(_assertion_outcome(self.config["on_eval_error"]))
                results.append(entry)
                continue
            entry.update(
                {
                    "passed": passed,
                    "actual": _short(value),
                    "value": _short(assertion["value"]),
                }
            )
            outcomes.append("passed" if passed else "failed")
            results.append(entry)

        if "error" in outcomes:
            first = next(item for item in results if not item.get("passed"))
            return error_result(
                "assertion_evaluation_error",
                first.get("error") or first.get("reason") or "rule assertion could not be evaluated",
                detail={"assertions": results},
            )

        if self.config["require_all"]:
            if "failed" in outcomes:
                state = "fail"
            elif "uncertain" in outcomes:
                state = "uncertain"
            else:
                state = "pass"
        else:
            if "passed" in outcomes:
                state = "pass"
            elif "uncertain" in outcomes:
                state = "uncertain"
            else:
                state = "fail"

        return ScoreResult(
            state=state,
            confidence=1.0 if state in ("pass", "fail") else 0.5,
            detail={
                "assertions": results,
                "require_all": self.config["require_all"],
                "counts": dict(Counter(outcomes)),
                "on_missing": self.config["on_missing"],
                "on_eval_error": self.config["on_eval_error"],
            },
        )


def _tokens(text: str, case_sensitive: bool) -> list[str]:
    normalized = text if case_sensitive else text.casefold()
    return _TOKEN.findall(normalized)


def _lexical_cosine(left: str, right: str, case_sensitive: bool) -> float:
    left_tokens = _tokens(left, case_sensitive)
    right_tokens = _tokens(right, case_sensitive)
    if not left_tokens or not right_tokens:
        return 0.0
    left_counts = Counter(left_tokens)
    right_counts = Counter(right_tokens)
    dot = sum(count * right_counts.get(token, 0) for token, count in left_counts.items())
    left_norm = math.sqrt(sum(count * count for count in left_counts.values()))
    right_norm = math.sqrt(sum(count * count for count in right_counts.values()))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def _jaccard(left: str, right: str, case_sensitive: bool) -> float:
    left_set = set(_tokens(left, case_sensitive))
    right_set = set(_tokens(right, case_sensitive))
    if not left_set or not right_set:
        return 0.0
    return len(left_set & right_set) / len(left_set | right_set)


def _cosine_vectors(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


class SemanticSimilarityScorer(BaseScorer):
    type = "semantic_similarity"

    def evaluate(self, sample: dict, actual: Any, context: dict | None = None) -> ScoreResult:
        expected = _extract(sample.get("expected", MISSING), self.config.get("expected_path"))
        actual_value = _extract(actual, self.config.get("actual_path"))
        if expected is MISSING or expected is None:
            return error_result("missing_expected", "semantic_similarity requires a non-null sample 'expected' value")
        if actual_value is MISSING:
            return error_result("missing_actual", "semantic_similarity could not read the actual output")
        expected_text = text_of(expected)
        actual_text = text_of(actual_value)
        case_sensitive = bool(self.config.get("case_sensitive"))
        threshold = float(self.config["threshold"])
        metric = self.config["metric"]
        usage = None
        embedding_meta = None
        if metric == "embedding":
            spec = self.config["embedding"]
            try:
                vectors, meta = embed(
                    endpoint=spec["endpoint"],
                    model=spec["model"],
                    texts=[expected_text, actual_text],
                    timeout_s=spec["timeout_s"],
                    api_key_env=spec.get("api_key_env"),
                )
            except ModelEndpointError as exc:
                return error_result(
                    exc.code,
                    exc.message,
                    retryable=exc.retryable,
                    detail={
                        "endpoint": spec["endpoint"],
                        "model": spec["model"],
                        "metric": "embedding",
                        "httpStatus": exc.status,
                    },
                )
            similarity = _cosine_vectors(vectors[0], vectors[1])
            usage = meta.get("usage")
            embedding_meta = {"latency_ms": meta.get("latency_ms"), "model": spec["model"]}
        elif metric == "jaccard":
            similarity = _jaccard(expected_text, actual_text, case_sensitive)
        else:
            similarity = _lexical_cosine(expected_text, actual_text, case_sensitive)
        similarity = max(0.0, min(1.0, similarity))
        passed = similarity >= threshold
        confidence = min(1.0, 0.5 + abs(similarity - threshold))
        return ScoreResult(
            state="pass" if passed else "fail",
            confidence=confidence,
            detail={
                "metric": metric,
                "similarity": round(similarity, 6),
                "threshold": threshold,
                "matched": passed,
                "expected_text": _short(expected_text, 1000),
                "actual_text": _short(actual_text, 1000),
                "embedding": embedding_meta,
            },
            usage=usage,
        )


DEFAULT_JUDGE_SYSTEM = (
    "You are a strict evaluation judge for an AI test platform. "
    "Read the rubric and the sample. Return exactly one JSON object with keys "
    "\"score\" (a number from 0 to 1), \"verdict\" (\"pass\", \"fail\" or \"uncertain\") "
    "and \"reason\" (a short string). Do not add markdown or extra text."
)


class LLMJudgeScorer(BaseScorer):
    type = "llm_judge"

    def evaluate(self, sample: dict, actual: Any, context: dict | None = None) -> ScoreResult:
        endpoint = self.config["endpoint"]
        model_name = self.config["model"]
        payload = {
            "rubric": self.rubric_text,
            "sample_input": _short(sample.get("input", MISSING), 4000),
            "expected": _short(sample.get("expected", MISSING), 4000),
            "actual": _short(actual, 4000),
            "context": _short(context, 4000) if context is not None else None,
            "sample_id": sample.get("sample_id"),
            "priority": sample.get("priority"),
        }
        user_text = canonical_json(payload)
        limit = int(self.config.get("max_input_chars", 20000))
        if len(user_text) > limit:
            user_text = user_text[:limit] + "...[truncated]"
        system_prompt = self.config.get("system_prompt") or DEFAULT_JUDGE_SYSTEM
        try:
            response = chat(
                endpoint=endpoint,
                model=model_name,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text},
                ],
                timeout_s=float(self.config["timeout_s"]),
                temperature=float(self.config["temperature"]),
                max_tokens=int(self.config["max_tokens"]),
                api_key_env=self.config.get("api_key_env"),
                response_format=self.config.get("response_format"),
            )
            parsed = extract_json_object(response["content"])
        except ModelEndpointError as exc:
            return error_result(
                exc.code,
                exc.message,
                retryable=exc.retryable,
                detail={
                    "endpoint": endpoint,
                    "model": model_name,
                    "httpStatus": exc.status,
                    "rubric_digest": self.rubric_digest,
                },
            )

        raw_score = parsed.get("score")
        score = None
        if raw_score is not None:
            if not _is_number(raw_score) or not 0 <= float(raw_score) <= 1:
                return error_result(
                    "judge_invalid_score",
                    f"judge returned an invalid score: {raw_score!r}",
                    detail={"endpoint": endpoint, "model": model_name, "response": _short(parsed)},
                )
            score = float(raw_score)
        raw_verdict = parsed.get("verdict")
        threshold = float(self.config["pass_threshold"])
        verdict = None
        if isinstance(raw_verdict, str) and raw_verdict.strip().lower() in ("pass", "fail", "uncertain"):
            verdict = raw_verdict.strip().lower()
        if score is None and verdict is None:
            return error_result(
                "judge_invalid_response",
                "judge response has neither a valid score nor a valid verdict",
                detail={"endpoint": endpoint, "model": model_name, "response": _short(parsed)},
            )
        derived = normalize_state(None, score, threshold) if score is not None else "uncertain"
        state = verdict or derived
        conflict = bool(verdict and score is not None and verdict != derived)
        confidence = min(1.0, 0.5 + abs((score if score is not None else (1.0 if state == "pass" else 0.0)) - threshold))
        return ScoreResult(
            state=state,
            confidence=confidence,
            detail={
                "score": score,
                "threshold": threshold,
                "verdict": verdict,
                "derived_verdict": derived,
                "verdict_conflict": conflict,
                "reason": parsed.get("reason"),
                "model": response.get("model") or model_name,
                "endpoint": endpoint,
                "latency_ms": response.get("latency_ms"),
                "rubric_digest": self.rubric_digest,
                "raw_response": _short(response["content"], 4000),
            },
            usage=response.get("usage"),
        )


HUMAN_DECISION_STATES = {
    "confirmed_defect": "fail",
    "false_positive": "pass",
    "sample_error": "uncertain",
}


class HumanReviewScorer(BaseScorer):
    type = "human_review"

    def evaluate(self, sample: dict, actual: Any, context: dict | None = None) -> ScoreResult:
        decision = read_path(actual, self.config.get("decision_path") or "$.human_review")
        if decision is not MISSING and isinstance(decision, dict) and self.config.get("allow_decision", True):
            conclusion = decision.get("conclusion")
            if conclusion in HUMAN_DECISION_STATES:
                state = HUMAN_DECISION_STATES[conclusion]
                return ScoreResult(
                    state=state,
                    confidence=1.0,
                    detail={
                        "queue": self.config["queue"],
                        "decision": _short(decision),
                        "conclusion": conclusion,
                        "mapped_state": state,
                        "reviewer": decision.get("reviewer"),
                    },
                )
        return ScoreResult(
            state="uncertain",
            confidence=0.0,
            detail={
                "queue": self.config["queue"],
                "reason": "human review required; no decision recorded",
                "escalated": True,
                "sample_id": sample.get("sample_id"),
            },
        )


SCORER_CLASSES = {
    "exact_match": ExactMatchScorer,
    "regex": RegexScorer,
    "json_schema": JsonSchemaScorer,
    "rule_assertion": RuleAssertionScorer,
    "semantic_similarity": SemanticSimilarityScorer,
    "llm_judge": LLMJudgeScorer,
    "human_review": HumanReviewScorer,
}


def build_scorer(
    scorer_type: str,
    *,
    scorer_id: str,
    scorer_name: str,
    version_id: str,
    version: int,
    frozen_digest: str,
    config: dict,
    rubric_text: str | None = None,
    rubric_digest: str | None = None,
) -> BaseScorer:
    if scorer_type not in SCORER_CLASSES:
        raise ConfigError(f"unknown scorer type: {scorer_type}")
    return SCORER_CLASSES[scorer_type](
        scorer_id=scorer_id,
        scorer_name=scorer_name,
        version_id=version_id,
        version=version,
        frozen_digest=frozen_digest,
        config=config,
        rubric_text=rubric_text,
        rubric_digest=rubric_digest,
    )
