"""Classify provider failures into the SPEC-22 policy classes.

Pure: no GTK, no network, no store. Never raises.
"""

from __future__ import annotations

import logging
import re
import socket
import ssl
import urllib.error
from dataclasses import dataclass
from enum import Enum

_logger = logging.getLogger(__name__)


class FailureClass(str, Enum):
    TRANSIENT = "transient"
    PERMANENT_AUTH = "permanent_auth"
    PERMANENT_MODEL = "permanent_model"
    BAD_REQUEST = "bad_request"
    UNKNOWN = "unknown"


@dataclass
class ProviderFailure:
    session_key: str
    provider: str
    model: str
    failure_class: FailureClass
    status_code: int | None
    detail: str
    stage: str
    attempt: int
    timestamp: float


_CLASS_LABEL = {
    FailureClass.TRANSIENT: "transient",
    FailureClass.PERMANENT_AUTH: "auth",
    FailureClass.PERMANENT_MODEL: "model",
    FailureClass.BAD_REQUEST: "bad request",
    FailureClass.UNKNOWN: "unknown",
}

# Payload we built wrong. Halt, no failover.
_BAD_REQUEST_MARKERS = (
    "invalid json",
    "json decode",
    "could not parse",
    "schema",
    "tool schema",
    "missing required",
    "message structure",
    "must have non-empty",
    "malformed",
    "invalid request body",
    "invalid payload",
)

# This model/provider rejected a parameter or the model id. Advance the chain.
_MODEL_REJECTION_MARKERS = (
    "unknown model",
    "model not found",
    "no such model",
    "not a valid model",
    "unsupported parameter",
    "unexpected parameter",
    "invalid arguments passed to the model",
    "does not support",
    "reasoning_effort",
    "reasoning parameter",
    "thinking parameter",
)

_STATUS_RE = re.compile(
    r"(?:API error|code=|status_code=)\s*(\d{3,4})",
    re.IGNORECASE,
)
_SECRET_RE = re.compile(r"(?:sk-|Bearer\s+)[A-Za-z0-9_\-\.]+", re.IGNORECASE)
_URL_USERINFO_RE = re.compile(r"(https?://)[^\s/@]+:[^\s/@]+@")


def class_label(failure_class: FailureClass) -> str:
    return _CLASS_LABEL.get(failure_class, "unknown")


class ClassifiedProviderError(Exception):
    """String-path provider failure that still carries `_failure` for the UI."""

    def __init__(self, message: str, failure: ProviderFailure) -> None:
        super().__init__(message)
        self._failure = failure


def failure_for_stream(
    *,
    session_key: str,
    provider: str,
    model: str,
    stream_err: object | None,
    stage: str,
) -> ProviderFailure:
    """Build a failure for an empty body or a mid-stream error dict."""
    import time
    if isinstance(stream_err, dict):
        kind = classify_stream_error(stream_err)
        code = stream_err.get("code")
        try:
            status = int(code) if code not in (None, "") else None
        except (TypeError, ValueError):
            status = None
        if status == 0:
            status = None
        detail = sanitize_detail(stream_err.get("message") or "")
    else:
        kind = FailureClass.UNKNOWN
        status = None
        detail = "stream ended with no content and no done event"
    return ProviderFailure(
        session_key=session_key,
        provider=provider,
        model=model,
        failure_class=kind,
        status_code=status,
        detail=detail,
        stage=stage,
        attempt=1,
        timestamp=time.time(),
    )


def sanitize_detail(text: object) -> str:
    """Short, credential-free detail. Never raises."""
    raw = text if isinstance(text, str) else str(text or "")
    try:
        from transport.telegram import redact_log_preview
        raw = redact_log_preview(raw)
    except Exception:
        _logger.debug("detail redaction failed", exc_info=True)
    raw = _SECRET_RE.sub("***", raw)
    raw = _URL_USERINFO_RE.sub(r"\1***@", raw)
    raw = " ".join(raw.split())
    return raw[:200]


def classify_http_400(body: str) -> FailureClass:
    """Split a 400. Schema/payload → bad_request. Parameter/model → permanent_model.
    Neither → unknown. bad_request wins when both lists match, so a schema
    error is never failed over."""
    text = (body or "").lower()
    if any(marker in text for marker in _BAD_REQUEST_MARKERS):
        return FailureClass.BAD_REQUEST
    if any(marker in text for marker in _MODEL_REJECTION_MARKERS):
        return FailureClass.PERMANENT_MODEL
    return FailureClass.UNKNOWN


def classify_stream_error(error_payload: object) -> FailureClass:
    """OpenRouter-style {code, message}. Never raises."""
    if not isinstance(error_payload, dict):
        return FailureClass.UNKNOWN
    code = error_payload.get("code", 0)
    message = str(error_payload.get("message") or "")
    try:
        code_int = int(code) if code is not None and code != "" else 0
    except (TypeError, ValueError):
        code_int = 0
    if code_int == 0:
        lowered = message.lower()
        if "rate limit" in lowered or "timeout" in lowered or "overloaded" in lowered:
            return FailureClass.TRANSIENT
        return FailureClass.UNKNOWN
    return _class_for_status(code_int, message, from_http=True)


def status_code_of(exc: object) -> int | None:
    if not isinstance(exc, BaseException):
        return None
    status, _body, _from_http = _extract_status(exc)
    return status


def is_provider_exception(exc: object) -> bool:
    """True when this exception came from a provider call, not from app code.

    Status in the chain, a transient network type, or an already-attached
    failure. A bare KeyError/ValueError inside the tool loop is not a
    provider failure — those are only classified when the call boundary
    attaches `_failure` itself.
    """
    if not isinstance(exc, BaseException):
        return False
    if getattr(exc, "_failure", None) is not None:
        return True
    status, _body, _from_http = _extract_status(exc)
    if status is not None:
        return True
    return _chain_is_transient(exc)


def classify_exception(exc: BaseException, provider: str = "", model: str = "") -> FailureClass:
    """Map a raised exception to a class. Walks __cause__/__context__ for HTTPError.
    Never raises. `provider` and `model` are accepted so call sites can pass
    context; classification does not depend on them."""
    del provider, model
    if not isinstance(exc, BaseException):
        return FailureClass.UNKNOWN
    status, body, from_http = _extract_status(exc)
    if status is not None:
        return _class_for_status(status, body, from_http=from_http)
    if _chain_is_transient(exc):
        return FailureClass.TRANSIENT
    lowered = str(exc).lower()
    if "login fail" in lowered or "invalid api key" in lowered or "unauthorized" in lowered:
        return FailureClass.PERMANENT_AUTH
    return FailureClass.UNKNOWN


def _class_for_status(status: int, body: str, *, from_http: bool) -> FailureClass:
    if status in (401, 402, 403):
        return FailureClass.PERMANENT_AUTH
    if status == 404:
        return FailureClass.PERMANENT_MODEL
    if status == 400:
        return classify_http_400(body)
    if status == 429 or (from_http and status >= 500):
        return FailureClass.TRANSIENT
    if not from_http and 500 <= status <= 599:
        return FailureClass.TRANSIENT
    lowered = (body or "").lower()
    if "login fail" in lowered or "invalid api key" in lowered:
        return FailureClass.PERMANENT_AUTH
    return FailureClass.UNKNOWN


def _extract_status(exc: BaseException) -> tuple[int | None, str, bool]:
    """Return (status, body, from_http_error)."""
    body = str(exc)
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, urllib.error.HTTPError):
            code = getattr(current, "code", None)
            try:
                status = int(code) if code is not None else None
            except (TypeError, ValueError):
                status = None
            return status, body, True
        cause = current.__cause__
        context = current.__context__
        nxt = cause if isinstance(cause, BaseException) else None
        if nxt is None and isinstance(context, BaseException):
            nxt = context
        current = nxt
    match = _STATUS_RE.search(body)
    if match:
        return int(match.group(1)), body, False
    return None, body, False


def _chain_is_transient(exc: BaseException) -> bool:
    transient_types = (TimeoutError, ConnectionError, urllib.error.URLError)
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, urllib.error.HTTPError):
            return False
        if isinstance(current, transient_types):
            return True
        if isinstance(current, (ssl.SSLError, socket.gaierror)):
            return True
        cause = current.__cause__ if isinstance(current.__cause__, BaseException) else None
        context = current.__context__ if isinstance(current.__context__, BaseException) else None
        current = cause or context
    return False
