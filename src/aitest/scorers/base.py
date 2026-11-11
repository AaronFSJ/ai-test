"""Shared Scorer protocol types.

Every built-in scorer consumes a sample, the actual output produced by the
system under test and an optional execution context. It returns the platform's
four-state verdict (pass / fail / uncertain / error), a confidence and a
machine-readable detail object.

The module is standard-library only, matching the rest of the repository.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

STATES = ("pass", "fail", "uncertain", "error")
SCORER_TYPES = (
    "exact_match",
    "regex",
    "json_schema",
    "rule_assertion",
    "semantic_similarity",
    "llm_judge",
    "human_review",
)


class _Missing:
    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<missing>"


MISSING = _Missing()


@dataclass
class ScoreResult:
    """Normalized output of one scorer invocation."""

    state: str
    confidence: float = 1.0
    detail: dict = field(default_factory=dict)
    error: dict | None = None
    usage: dict | None = None

    def __post_init__(self) -> None:
        if self.state not in STATES:
            raise ValueError(f"invalid scorer state: {self.state!r}")
        self.confidence = round(max(0.0, min(1.0, float(self.confidence))), 6)

    @property
    def score(self) -> float | None:
        """Numeric score used by weighted / majority combinators."""

        if self.state == "pass":
            return 1.0
        if self.state == "fail":
            return 0.0
        if self.state == "uncertain":
            return 0.5
        return None

    def to_dict(self) -> dict:
        data = {
            "state": self.state,
            "confidence": self.confidence,
            "score": self.score,
            "detail": self.detail,
        }
        if self.error is not None:
            data["error"] = self.error
        if self.usage is not None:
            data["usage"] = self.usage
        return data


def error_result(code: str, message: str, *, retryable: bool = False, detail: dict | None = None) -> ScoreResult:
    payload = {"code": code, "message": message, "retryable": bool(retryable)}
    if detail:
        payload["detail"] = detail
    return ScoreResult(
        state="error",
        confidence=0.0,
        detail={"error": payload, **(detail or {})},
        error=payload,
    )


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_ready(value: Any) -> Any:
    """Convert values into JSON-serializable structures without losing types."""

    if value is MISSING:
        return {"__missing__": True}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(key): json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_ready(item) for item in value]
    return repr(value)


def text_of(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is MISSING:
        return ""
    return canonical_json(json_ready(value))


class BaseScorer:
    """Base class for all built-in scorers."""

    type = "base"

    def __init__(
        self,
        *,
        scorer_id: str,
        scorer_name: str,
        version_id: str,
        version: int,
        frozen_digest: str,
        config: dict,
        rubric_text: str | None = None,
        rubric_digest: str | None = None,
    ) -> None:
        self.scorer_id = scorer_id
        self.scorer_name = scorer_name
        self.version_id = version_id
        self.version = version
        self.frozen_digest = frozen_digest
        self.config = config
        self.rubric_text = rubric_text
        self.rubric_digest = rubric_digest

    def evaluate(self, sample: dict, actual: Any, context: dict | None = None) -> ScoreResult:
        raise NotImplementedError

    def trace(self) -> dict:
        return {
            "scorer_id": self.scorer_id,
            "scorer_name": self.scorer_name,
            "scorer_type": self.type,
            "scorer_version_id": self.version_id,
            "scorer_version": self.version,
            "frozen_digest": self.frozen_digest,
            "rubric_digest": self.rubric_digest,
            "config": self.config,
        }


def normalize_state(value: Any, score: Any = None, threshold: float = 0.5) -> str:
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in STATES:
            return lowered
    if isinstance(score, (int, float)):
        return "pass" if float(score) >= threshold else "fail"
    return "uncertain"
