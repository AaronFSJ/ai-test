"""Minimal OpenAI-compatible client for the LLM judge and embedding scorer.

Only POST + JSON is needed, so the standard library is sufficient. Errors are
classified instead of stringified: callers map them to an ``error`` verdict and
keep scoring the remaining items.

The optional API key is read from an environment variable named in the scorer
configuration. A literal key in the configuration is rejected by validation, so
keys are never written to the database or to scorer traces.
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from typing import Any


class ModelEndpointError(Exception):
    """Classified model endpoint failure."""

    def __init__(self, code: str, message: str, *, retryable: bool = False, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.status = status

    def as_dict(self) -> dict:
        data = {"code": self.code, "message": self.message, "retryable": self.retryable}
        if self.status is not None:
            data["httpStatus"] = self.status
        return data


def chat_url(endpoint: str) -> str:
    base = endpoint.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def embeddings_url(endpoint: str) -> str:
    base = endpoint.rstrip("/")
    if base.endswith("/embeddings"):
        return base
    if base.endswith("/v1"):
        return base + "/embeddings"
    return base + "/v1/embeddings"


def _post(url: str, payload: dict, timeout_s: float, api_key: str | None) -> tuple[dict, float]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()[:2000]
        status = exc.code
        try:
            decoded = raw.decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover - defensive
            decoded = ""
        if status in (401, 403):
            raise ModelEndpointError(
                "endpoint_auth_error",
                f"model endpoint rejected the credential (HTTP {status})",
                retryable=False,
                status=status,
            ) from exc
        retryable = status == 429 or 500 <= status < 600
        raise ModelEndpointError(
            "endpoint_http_error",
            f"model endpoint returned HTTP {status}: {decoded[:300]}",
            retryable=retryable,
            status=status,
        ) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise ModelEndpointError(
            "endpoint_timeout", f"model endpoint timed out after {timeout_s:g}s", retryable=True
        ) from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise ModelEndpointError(
            "endpoint_unreachable", f"model endpoint is unreachable: {reason}", retryable=True
        ) from exc
    except OSError as exc:
        raise ModelEndpointError(
            "endpoint_unreachable", f"model endpoint is unreachable: {exc}", retryable=True
        ) from exc
    latency_ms = int((time.monotonic() - started) * 1000)
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelEndpointError(
            "endpoint_invalid_response",
            f"model endpoint returned non-JSON content (HTTP {status})",
            retryable=False,
            status=status,
        ) from exc
    if not isinstance(parsed, dict):
        raise ModelEndpointError(
            "endpoint_invalid_response",
            "model endpoint returned a JSON value that is not an object",
            retryable=False,
            status=status,
        )
    return parsed, latency_ms


def chat(
    *,
    endpoint: str,
    model: str,
    messages: list[dict],
    timeout_s: float,
    temperature: float = 0.0,
    max_tokens: int = 512,
    api_key_env: str | None = None,
    response_format: str | None = "json_object",
) -> dict:
    api_key = os.environ.get(api_key_env) if api_key_env else None
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if response_format:
        payload["response_format"] = {"type": response_format}
    body, latency_ms = _post(chat_url(endpoint), payload, timeout_s, api_key)
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ModelEndpointError(
            "endpoint_invalid_response", "chat response has no 'choices' array", retryable=False
        )
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise ModelEndpointError(
            "endpoint_invalid_response", "chat response has empty message content", retryable=False
        )
    return {
        "content": content,
        "latency_ms": latency_ms,
        "usage": body.get("usage") if isinstance(body.get("usage"), dict) else None,
        "model": body.get("model") or model,
    }


def embed(
    *,
    endpoint: str,
    model: str,
    texts: list[str],
    timeout_s: float,
    api_key_env: str | None = None,
) -> tuple[list[list[float]], dict]:
    api_key = os.environ.get(api_key_env) if api_key_env else None
    body, latency_ms = _post(
        embeddings_url(endpoint), {"model": model, "input": texts}, timeout_s, api_key
    )
    data = body.get("data")
    if not isinstance(data, list) or len(data) != len(texts):
        raise ModelEndpointError(
            "endpoint_invalid_response",
            "embeddings response does not contain one vector per input",
            retryable=False,
        )
    vectors: list[list[float]] = []
    for item in data:
        vector = item.get("embedding") if isinstance(item, dict) else None
        if not isinstance(vector, list) or not vector or not all(
            isinstance(value, (int, float)) and not isinstance(value, bool) for value in vector
        ):
            raise ModelEndpointError(
                "endpoint_invalid_response", "embedding vector is missing or malformed", retryable=False
            )
        vectors.append([float(value) for value in vector])
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else None
    return vectors, {"latency_ms": latency_ms, "usage": usage}


def extract_json_object(content: str) -> dict:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ModelEndpointError(
                "endpoint_invalid_response", "judge response does not contain a JSON object"
            )
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ModelEndpointError(
                "endpoint_invalid_response", f"judge response is not valid JSON: {exc.msg}"
            ) from exc
    if not isinstance(parsed, dict):
        raise ModelEndpointError(
            "endpoint_invalid_response", "judge response JSON is not an object"
        )
    return parsed
