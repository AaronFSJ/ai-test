"""T09 scorer framework: protocol, built-ins and combinators."""

from .base import (
    MISSING,
    SCORER_TYPES,
    STATES,
    BaseScorer,
    ScoreResult,
    canonical_json,
    error_result,
)
from .builtin import (
    ConfigError,
    SCORER_CLASSES,
    build_scorer,
    validate_config,
)
from .combinators import (
    COMBINATORS,
    CombinatorConfigError,
    combine,
    normalize_members,
    validate_profile_config,
)

__all__ = [
    "MISSING",
    "SCORER_TYPES",
    "STATES",
    "BaseScorer",
    "ScoreResult",
    "canonical_json",
    "error_result",
    "ConfigError",
    "SCORER_CLASSES",
    "build_scorer",
    "validate_config",
    "COMBINATORS",
    "CombinatorConfigError",
    "combine",
    "normalize_members",
    "validate_profile_config",
]
