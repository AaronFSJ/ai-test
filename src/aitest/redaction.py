"""Deterministic redaction applied before evidence and report content is stored.

The platform never stores raw credentials or personal data that its rules can
recognise. Redaction happens in-process, before the object store write; the
stored object and its digest therefore describe the redacted bytes only.

Text evidence is processed with an ordered rule list. Structured JSON evidence
(content type ``application/json``) is walked value-by-value so that numeric
personal data becomes a JSON string instead of corrupting the document.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

RULES_VERSION = "redaction-v1"
PEM_BEGIN = "-----BEGIN " + "[A-Z0-9 ]*PRIVATE KEY" + "-----"
PEM_END = "-----END " + "[A-Z0-9 ]*PRIVATE KEY" + "-----"


@dataclass(frozen=True)
class TextRule:
    id: str
    category: str
    description: str
    replacement: str
    pattern: str
    flags: int = 0


TEXT_RULES: tuple[TextRule, ...] = (
    TextRule(
        id="private_key_block",
        category="credential",
        description="PEM/OpenSSH private key blocks",
        replacement="[REDACTED:PRIVATE_KEY]",
        pattern=PEM_BEGIN + ".*?" + PEM_END,
        flags=re.DOTALL,
    ),
    TextRule(
        id="jwt",
        category="credential",
        description="Standalone JSON Web Tokens",
        replacement="[REDACTED:JWT]",
        pattern=r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\b",
    ),
    TextRule(
        id="aws_access_key",
        category="credential",
        description="AWS access key identifiers",
        replacement="[REDACTED:AWS_KEY]",
        pattern=r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
    ),
    TextRule(
        id="github_token",
        category="credential",
        description="GitHub personal access and app tokens",
        replacement="[REDACTED:GITHUB_TOKEN]",
        pattern=r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b",
    ),
    TextRule(
        id="authorization_header",
        category="credential",
        description="Authorization, proxy-authorization and cookie header values",
        replacement=r"\1[REDACTED:HEADER]",
        pattern=r"(?im)^([ \t]*(?:authorization|proxy-authorization|cookie|set-cookie)[ \t]*:[ \t]*)(?!\[REDACTED).+$",
    ),
    TextRule(
        id="bearer_token",
        category="credential",
        description="Bearer tokens outside a header line",
        replacement=r"\1[REDACTED:BEARER_TOKEN]",
        pattern=r"(?i)\b(Bearer[ \t]+)[A-Za-z0-9._~+/=-]{8,}",
    ),
    TextRule(
        id="basic_auth",
        category="credential",
        description="Basic authentication payloads outside a header line",
        replacement=r"\1[REDACTED:BASIC_AUTH]",
        pattern=r"(?i)\b(Basic[ \t]+)[A-Za-z0-9+/=]{8,}",
    ),
    TextRule(
        id="secret_key_value",
        category="credential",
        description="Password, token, API-key, session and secret key/value pairs",
        replacement=r"\g<key>[REDACTED:SECRET]",
        pattern=(
            r"(?ix)(?P<key>\b(?:password|passwd|pwd|secret|client_secret|"
            r"refresh_token|access_token|id_token|api[_-]?key|apikey|authorization|"
            r"auth|session(?:_id)?|cookie|token)\b[ \t]*[\"']?[ \t]*[:=][ \t]*[\"']?)"
            r"(?!\[?REDACTED)(?P<val>[^\"'\s,;&}\]]+)"
        ),
    ),
    TextRule(
        id="phone_cn",
        category="personal_data",
        description="Mainland China mobile numbers (with optional +86 prefix)",
        replacement="[REDACTED:PHONE]",
        pattern=r"(?<!\d)(?:\+?86[ \t-]?)?1[3-9]\d{9}(?!\d)",
    ),
    TextRule(
        id="phone_e164",
        category="personal_data",
        description="International E.164 phone numbers",
        replacement="[REDACTED:PHONE]",
        pattern=r"(?<!\d)\+\d{8,15}(?!\d)",
    ),
    TextRule(
        id="email",
        category="personal_data",
        description="Email addresses",
        replacement="[REDACTED:EMAIL]",
        pattern=r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
    ),
    TextRule(
        id="id_card_cn",
        category="personal_data",
        description="Mainland China 18-character resident identity numbers",
        replacement="[REDACTED:ID_CARD]",
        pattern=r"(?<!\d)\d{17}[\dXx](?!\d)",
    ),
)


# Query parameters whose values are credentials or signatures. Names are
# compared case-insensitively; the parameter name is preserved to keep the
# evidence intelligible while its value is removed.
SENSITIVE_URL_PARAMS = frozenset(
    {
        "access_token", "api_key", "apikey", "auth", "authorization",
        "awsaccesskeyid", "client_secret", "code", "credential", "id_token",
        "jwt", "key", "password", "refresh_token", "sas", "se", "secret",
        "session", "sessionid", "sig", "signature", "skoid", "sktid", "sp",
        "spr", "sr", "st", "sv", "token", "x-amz-credential",
        "x-amz-security-token", "x-amz-signature",
    }
)

SENSITIVE_KEYS = frozenset(
    {
        "access_key", "access_token", "apikey", "api_key", "auth",
        "authorization", "aws_access_key_id", "aws_secret_access_key",
        "client_secret", "cookie", "credential", "credentials", "id_token",
        "jwt", "password", "passwd", "private_key", "pwd", "refresh_token",
        "secret", "session", "session_id", "sessionid", "set-cookie", "sig",
        "signature", "token", "x-amz-credential", "x-amz-security-token",
        "x-amz-signature",
    }
)

NUMERIC_PII_RULE_IDS = frozenset({"phone_cn", "phone_e164", "id_card_cn"})
URL_RE = re.compile(r"(?i)\bhttps?://[^\s\"'<>]+")
TRAILING_PUNCTUATION = ".,;:!?)]}'\""
_URL_SAFE_VALUE = "REDACTED"


def rules_inventory() -> list[dict]:
    """Human- and machine-readable list of enabled redaction rules."""

    inventory = [
        {
            "id": "url_query_secret",
            "category": "credential",
            "description": (
                "Sensitive URL query parameters (tokens, signatures, SAS and AWS "
                "credential parameters) have their values replaced; names stay."
            ),
            "replacement": _URL_SAFE_VALUE,
            "pattern": "https?://...?<param>=<value>",
        },
        {
            "id": "url_userinfo",
            "category": "credential",
            "description": "Username/password userinfo embedded in an HTTP(S) URL",
            "replacement": "[REDACTED]:[REDACTED]@",
            "pattern": "https?://user:password@host/...",
        },
        {
            "id": "url_fragment",
            "category": "credential",
            "description": "URL fragments are dropped because SPA auth flows can place tokens there",
            "replacement": "(empty fragment)",
            "pattern": "https?://...#<fragment>",
        },
    ]
    inventory.extend(
        {
            "id": rule.id,
            "category": rule.category,
            "description": rule.description,
            "replacement": rule.replacement,
            "pattern": rule.pattern,
        }
        for rule in TEXT_RULES
    )
    inventory.append(
        {
            "id": "json_sensitive_key",
            "category": "credential",
            "description": (
                "Structured JSON keys named like passwords, tokens, API keys, sessions, "
                "cookies, signatures or credentials have their values replaced."
            ),
            "replacement": "[REDACTED:<KEY>]",
            "pattern": "JSON object key match",
        }
    )
    return inventory


def _bump(matches: dict[str, int], rule_id: str, count: int = 1) -> None:
    if count:
        matches[rule_id] = matches.get(rule_id, 0) + count


def _redact_url(raw: str, matches: dict[str, int]) -> str:
    trailing = ""
    while raw and raw[-1] in TRAILING_PUNCTUATION:
        trailing = raw[-1] + trailing
        raw = raw[:-1]
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw + trailing

    netloc = parts.netloc
    if "@" in netloc:
        userinfo, host = netloc.rsplit("@", 1)
        netloc = f"[REDACTED]:[REDACTED]@{host}" if ":" in userinfo else f"[REDACTED]@{host}"
        _bump(matches, "url_userinfo")

    query = parts.query
    if query:
        pairs = parse_qsl(query, keep_blank_values=True)
        changed = False
        new_pairs: list[tuple[str, str]] = []
        for name, value in pairs:
            if name.lower() in SENSITIVE_URL_PARAMS:
                new_pairs.append((name, _URL_SAFE_VALUE))
                changed = True
                _bump(matches, "url_query_secret")
            else:
                new_pairs.append((name, value))
        if changed:
            query = urlencode(new_pairs, doseq=True)

    fragment = parts.fragment
    if fragment:
        fragment = ""
        _bump(matches, "url_fragment")

    return urlunsplit((parts.scheme, netloc, parts.path, query, fragment)) + trailing


def redact_text(text: str) -> tuple[str, dict[str, int]]:
    """Apply URL-aware and text rules; return redacted text and match counts."""

    matches: dict[str, int] = {}

    def _url_sub(match: re.Match[str]) -> str:
        return _redact_url(match.group(0), matches)

    cleaned = URL_RE.sub(_url_sub, text)

    for rule in TEXT_RULES:
        compiled = re.compile(rule.pattern, rule.flags)
        cleaned, count = compiled.subn(rule.replacement, cleaned)
        _bump(matches, rule.id, count)
    return cleaned, matches


def _is_sensitive_key(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    if normalized in SENSITIVE_KEYS:
        return True
    if normalized in {"token_count", "tokens_in", "tokens_out"}:
        return False
    return any(
        normalized.endswith(suffix)
        for suffix in ("_token", "_secret", "_password", "_api_key", "_private_key", "_access_key")
    )


def _redact_json_value(value: Any, matches: dict[str, int]) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if _is_sensitive_key(str(key)) and item not in (None, ""):
                _bump(matches, "json_sensitive_key")
                result[key] = "[REDACTED:JSON_KEY]"
            else:
                result[key] = _redact_json_value(item, matches)
        return result
    if isinstance(value, list):
        return [_redact_json_value(item, matches) for item in value]
    if isinstance(value, str):
        cleaned, text_matches = redact_text(value)
        for rule_id, count in text_matches.items():
            _bump(matches, rule_id, count)
        return cleaned
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        candidate = str(value)
        for rule_id in NUMERIC_PII_RULE_IDS:
            rule = next(item for item in TEXT_RULES if item.id == rule_id)
            if re.fullmatch(rule.pattern, candidate):
                _bump(matches, rule_id)
                return rule.replacement
    return value


def redact_bytes(data: bytes, content_type: str | None) -> tuple[bytes, dict]:
    """Return stored (redacted) bytes plus a redaction report.

    JSON documents are redacted structurally and re-serialised canonically.
    Other textual content is redacted as UTF-8 text. Binary content (for
    example a screenshot) is stored byte-for-byte and explicitly flagged for
    manual review because automated text rules cannot inspect pixels.
    """

    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    is_json = media_type == "application/json" or media_type.endswith("+json")
    is_text = is_json or media_type.startswith("text/") or media_type in {
        "application/xml", "application/x-ndjson", "application/x-www-form-urlencoded",
    }
    base_report = {
        "rulesVersion": RULES_VERSION,
        "bytesBefore": len(data),
    }

    if not is_text:
        return data, {
            **base_report,
            "redacted": False,
            "mode": "binary",
            "matches": {},
            "bytesAfter": len(data),
            "manualReviewRequired": True,
            "note": "Binary evidence is stored unchanged; automated text rules cannot redact pixels.",
        }

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data, {
            **base_report,
            "redacted": False,
            "mode": "binary-invalid-utf8",
            "matches": {},
            "bytesAfter": len(data),
            "manualReviewRequired": True,
            "note": "Declared textual evidence was not valid UTF-8; stored unchanged and flagged.",
        }

    matches: dict[str, int] = {}
    mode = "text"
    if is_json:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            cleaned, matches = redact_text(text)
        else:
            parsed = _redact_json_value(parsed, matches)
            cleaned = json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            mode = "json"
    else:
        cleaned, matches = redact_text(text)

    encoded = cleaned.encode("utf-8")
    return encoded, {
        **base_report,
        "redacted": bool(matches),
        "mode": mode,
        "matches": dict(sorted(matches.items())),
        "bytesAfter": len(encoded),
        "manualReviewRequired": False,
        "note": "Applied before the object store write.",
    }


def redact_json_document(value: Any) -> tuple[Any, dict]:
    """Redact an already-parsed JSON document (used for report snapshots)."""

    matches: dict[str, int] = {}
    redacted = _redact_json_value(value, matches)
    return redacted, {
        "redacted": bool(matches),
        "rulesVersion": RULES_VERSION,
        "matches": dict(sorted(matches.items())),
    }
