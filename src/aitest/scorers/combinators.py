"""Deterministic combinators for scorer profiles.

Supported: strict ``and``, strict ``or``, weighted score with a threshold and
strict-majority voting. ``on_error`` defaults to ``error`` so a model endpoint
failure is never silently turned into pass/fail; an explicit ``ignore`` is
available for profiles that want to decide from the remaining scorers.
"""

from __future__ import annotations

from typing import Any

from .base import ScoreResult, error_result

COMBINATORS = ("and", "or", "weighted", "majority")


class CombinatorConfigError(ValueError):
    """Invalid profile/combinator configuration."""


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def normalize_members(members: Any) -> list[dict]:
    if not isinstance(members, list) or not members:
        raise CombinatorConfigError("members must be a non-empty array")
    normalized: list[dict] = []
    seen: set[str] = set()
    for index, item in enumerate(members):
        if not isinstance(item, dict):
            raise CombinatorConfigError(f"members[{index}] must be an object")
        unknown = sorted(set(item) - {"scorer_id", "version_id", "weight", "label"})
        if unknown:
            raise CombinatorConfigError(
                f"members[{index}] uses unknown key(s): {', '.join(unknown)}"
            )
        scorer_id = item.get("scorer_id")
        if not isinstance(scorer_id, str) or not scorer_id.startswith("scr_"):
            raise CombinatorConfigError(f"members[{index}].scorer_id must be a scorer id")
        if scorer_id in seen:
            raise CombinatorConfigError(f"members[{index}] duplicates scorer {scorer_id}")
        seen.add(scorer_id)
        weight = item.get("weight", 1.0)
        if not _is_number(weight) or float(weight) <= 0:
            raise CombinatorConfigError(f"members[{index}].weight must be a positive number")
        version_id = item.get("version_id")
        if version_id is not None and (
            not isinstance(version_id, str) or not version_id.startswith("scv_")
        ):
            raise CombinatorConfigError(f"members[{index}].version_id must be a scorer version id")
        normalized.append(
            {
                "scorer_id": scorer_id,
                "version_id": version_id,
                "weight": float(weight),
                "label": item.get("label"),
            }
        )
    return normalized


def validate_profile_config(combinator: str, config: Any, members: list[dict]) -> dict:
    if combinator not in COMBINATORS:
        raise CombinatorConfigError(f"combinator must be one of {list(COMBINATORS)}")
    if not isinstance(config, dict):
        raise CombinatorConfigError("profile config must be a JSON object")
    allowed = {"on_error", "threshold", "uncertain_score", "quorum"}
    unknown = sorted(set(config) - allowed)
    if unknown:
        raise CombinatorConfigError(f"profile config uses unknown key(s): {', '.join(unknown)}")
    on_error = config.get("on_error", "error")
    if on_error not in ("error", "ignore"):
        raise CombinatorConfigError("'on_error' must be 'error' or 'ignore'")
    normalized = {"on_error": on_error}
    if combinator == "weighted":
        threshold = config.get("threshold", 0.5)
        if not _is_number(threshold) or not 0 <= float(threshold) <= 1:
            raise CombinatorConfigError("'threshold' must be a number between 0 and 1")
        uncertain_score = config.get("uncertain_score", 0.5)
        if not _is_number(uncertain_score) or not 0 <= float(uncertain_score) <= 1:
            raise CombinatorConfigError("'uncertain_score' must be a number between 0 and 1")
        normalized.update(
            {"threshold": float(threshold), "uncertain_score": float(uncertain_score)}
        )
    elif "threshold" in config or "uncertain_score" in config:
        raise CombinatorConfigError("'threshold' and 'uncertain_score' are only valid for weighted")
    if combinator == "majority":
        quorum = config.get("quorum")
        if quorum is not None and (
            not isinstance(quorum, int) or isinstance(quorum, bool) or quorum < 1
        ):
            raise CombinatorConfigError("'quorum' must be a positive integer")
        if quorum is not None and quorum > len(members):
            raise CombinatorConfigError("'quorum' cannot exceed the number of members")
        normalized["quorum"] = quorum
    elif "quorum" in config:
        raise CombinatorConfigError("'quorum' is only valid for majority")
    return normalized


def _first_error(members: list[dict]) -> dict:
    for member in members:
        if member.get("state") == "error":
            return member.get("error") or {
                "code": "scorer_error",
                "message": "member scorer returned error",
                "retryable": False,
            }
    return {"code": "scorer_error", "message": "member scorer returned error", "retryable": False}


def _counts(members: list[dict]) -> dict:
    counts = {"pass": 0, "fail": 0, "uncertain": 0, "error": 0}
    for member in members:
        state = member.get("state")
        if state in counts:
            counts[state] += 1
    return counts


def combine(combinator: str, config: dict, members: list[dict]) -> ScoreResult:
    if combinator not in COMBINATORS:
        return error_result("invalid_combinator", f"unknown combinator: {combinator}")
    if not members:
        return error_result("empty_profile", "profile has no resolved scorers")
    on_error = config.get("on_error", "error")
    errors = [member for member in members if member.get("state") == "error"]
    if errors and on_error == "error":
        first = _first_error(errors)
        return ScoreResult(
            state="error",
            confidence=0.0,
            detail={
                "combinator": combinator,
                "config": config,
                "members": members,
                "aggregate": {"counts": _counts(members), "errorCount": len(errors)},
                "error": first,
            },
            error=first,
        )
    usable = [member for member in members if member.get("state") != "error"]
    counts = _counts(members)

    if not usable:
        first = _first_error(members)
        return ScoreResult(
            state="error",
            confidence=0.0,
            detail={
                "combinator": combinator,
                "config": config,
                "members": members,
                "aggregate": {"counts": counts, "errorCount": len(members)},
                "error": first,
            },
            error=first,
        )

    if combinator == "and":
        if any(member["state"] == "fail" for member in usable):
            state = "fail"
        elif any(member["state"] == "uncertain" for member in usable):
            state = "uncertain"
        else:
            state = "pass"
        confidence = min(float(member.get("confidence", 0.0)) for member in usable)
        aggregate = {"counts": counts, "rule": "fail if any fail, else uncertain if any uncertain, else pass"}
    elif combinator == "or":
        if any(member["state"] == "pass" for member in usable):
            state = "pass"
        elif any(member["state"] == "uncertain" for member in usable):
            state = "uncertain"
        else:
            state = "fail"
        confidence = max(float(member.get("confidence", 0.0)) for member in usable)
        aggregate = {"counts": counts, "rule": "pass if any pass, else uncertain if any uncertain, else fail"}
    elif combinator == "weighted":
        threshold = float(config.get("threshold", 0.5))
        uncertain_score = float(config.get("uncertain_score", 0.5))
        contributions = []
        total_weight = 0.0
        weighted_sum = 0.0
        for member in usable:
            weight = float(member.get("weight", 1.0))
            value = {"pass": 1.0, "fail": 0.0, "uncertain": uncertain_score}[member["state"]]
            total_weight += weight
            weighted_sum += weight * value
            contributions.append(
                {
                    "scorer_id": member.get("scorer_id"),
                    "state": member["state"],
                    "weight": weight,
                    "value": value,
                    "contribution": round(weight * value, 6),
                }
            )
        score = weighted_sum / total_weight if total_weight else 0.0
        state = "pass" if score >= threshold else "fail"
        confidence = min(1.0, 0.5 + abs(score - threshold))
        aggregate = {
            "counts": counts,
            "score": round(score, 6),
            "threshold": threshold,
            "uncertain_score": uncertain_score,
            "totalWeight": round(total_weight, 6),
            "contributions": contributions,
            "rule": "sum(weight * state_score) / sum(weight) >= threshold",
        }
    else:  # majority
        pass_votes = sum(1 for member in usable if member["state"] == "pass")
        fail_votes = sum(1 for member in usable if member["state"] == "fail")
        uncertain_votes = sum(1 for member in usable if member["state"] == "uncertain")
        valid_votes = pass_votes + fail_votes
        quorum = config.get("quorum")
        if quorum is None:
            quorum = valid_votes // 2 + 1 if valid_votes else 1
        if pass_votes >= quorum and pass_votes > fail_votes:
            state = "pass"
        elif fail_votes >= quorum and fail_votes > pass_votes:
            state = "fail"
        else:
            state = "uncertain"
        confidence = min(1.0, 0.5 + (max(pass_votes, fail_votes) / valid_votes) * 0.5) if valid_votes else 0.0
        aggregate = {
            "counts": counts,
            "passVotes": pass_votes,
            "failVotes": fail_votes,
            "uncertainVotes": uncertain_votes,
            "validVotes": valid_votes,
            "quorum": quorum,
            "rule": "a side needs quorum votes and strictly more votes than the other side",
        }

    return ScoreResult(
        state=state,
        confidence=confidence,
        detail={
            "combinator": combinator,
            "config": config,
            "members": members,
            "aggregate": aggregate,
        },
    )
