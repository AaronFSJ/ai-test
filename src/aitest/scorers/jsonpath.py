"""Very small JSON path reader used by rule assertions and field extraction.

Supported forms: ``$``, ``$.a.b``, ``a.b``, ``$.a[0]``, ``$['a-b']`` and
``$.items[2].name``. Wildcards, filters and recursive descent are deliberately
not supported; configuration validation rejects them so a typo cannot silently
read the wrong value.
"""

from __future__ import annotations

import re
from typing import Any

from .base import MISSING

_SEGMENT = re.compile(
    r"""
    \.?(?P<name>[A-Za-z0-9_\-]+)      # .name or bare name
    |\[\s*(?P<index>-?\d+)\s*\]        # [0] / [-1]
    |\[\s*(?P<quote>['"])(?P<quoted>.*?)(?P=quote)\s*\]  # ['key']
    """,
    re.VERBOSE,
)


def parse_path(path: Any) -> list[str | int]:
    if path is None or path == "":
        return []
    if not isinstance(path, str):
        raise ValueError("path must be a string")
    text = path.strip()
    if text in ("$", ""):
        return []
    if text.startswith("$"):
        text = text[1:]
    if text.startswith("..") or "*" in text or "?" in text:
        raise ValueError("wildcards and recursive descent are not supported")
    tokens: list[str | int] = []
    position = 0
    while position < len(text):
        match = _SEGMENT.match(text, position)
        if not match or match.end() == position:
            raise ValueError(f"invalid path segment at {position}: {path!r}")
        if match.group("name") is not None:
            tokens.append(match.group("name"))
        elif match.group("index") is not None:
            tokens.append(int(match.group("index")))
        else:
            tokens.append(match.group("quoted"))
        position = match.end()
    return tokens


def read_path(value: Any, path: Any) -> Any:
    if value is MISSING:
        return MISSING
    current = value
    for token in parse_path(path):
        if isinstance(token, int):
            if not isinstance(current, list):
                return MISSING
            try:
                current = current[token]
            except IndexError:
                return MISSING
        else:
            if not isinstance(current, dict) or token not in current:
                return MISSING
            current = current[token]
    return current


def path_exists(value: Any, path: Any) -> bool:
    return read_path(value, path) is not MISSING
