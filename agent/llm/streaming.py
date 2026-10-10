"""SSE streaming helpers and SSL retry infrastructure.

Extracted from agent/runtime.py (Phase B5). These helpers are consumed by
the provider classes' stream() methods in agent/llm/*_provider.py.

Public API:
    SSEEvent — namedtuple for SSE events
    sse_lines — chunked SSE line reader
    parse_sse_line — SSE line → SSEEvent
    parse_sse_delta — delta dict → SSEEvent list
    first_choice — defensive choices[0] accessor
    urlopen_with_ssl_retry — urllib.urlopen with SSL/DNS retry
    stream_with_ssl_retry — SSE stream retry wrapper
    is_retryable_ssl_error — transient SSL/DNS error detection
    friendly_error_message — raw exception → user-facing message
    RETRYABLE_SSL_ERRORS — frozenset of retryable SSL error tokens
    RETRYABLE_OSERROR_TYPES — tuple of retryable TCP-level + DNS error types
    MAX_SSL_RETRIES — retry budget
    SSL_RETRY_BASE_MS — exponential backoff base in ms
"""

from __future__ import annotations

import http.client
import json
import logging
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import namedtuple
from typing import IO, Iterator, cast
from urllib.parse import urlsplit

from utils.config import get_env

logger = logging.getLogger(__name__)

# ── SSE event types ─────────────────────────────────────────────────────────

SSEEvent = namedtuple("SSEEvent", ["type", "data"])
# Types: 'text_delta', 'tool_call_delta', 'tool_call_done', 'done', 'error', 'usage'


# ── SSE parsing ─────────────────────────────────────────────────────────────

def sse_lines(resp) -> Iterator[bytes]:
    """Read all SSE lines from an HTTP response. Handles chunked transfer encoding."""
    # Read line-by-line (not byte-by-byte) — avoids 100-1000x syscall overhead
    for line in resp:
        yield line.strip()


def parse_sse_line(line: bytes) -> SSEEvent | None:
    """Parse one SSE line into an SSEEvent. Returns None for non-data lines."""
    line = line.strip()
    if not line or line.startswith(b":"):
        return None
    if line.startswith(b"data: "):
        data = line[6:]
    elif line.startswith(b"data:"):
        data = line[5:].lstrip()
    else:
        return None
    if data == b"[DONE]" or data == b"DONE":
        return SSEEvent(type="done", data={})
    try:
        return SSEEvent(type="raw", data=json.loads(data.decode("utf-8")))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.debug(
            "[sse-line] drop malformed frame (%s): %r",
            type(e).__name__,
            line[:200],
        )
        return None


def parse_sse_delta(d: dict) -> list[SSEEvent]:
    """Extract text_delta and tool_call_delta events from an SSE delta dict.

    Shared by OpenAIProvider("openai").stream and MiniMaxProvider().stream.
    The dict is the parsed JSON of an SSE `data:` line whose type is "raw"
    (i.e., it has a `choices` field with a delta).

    finish_reason / usage handling is NOT included — each caller processes
    those inline because OpenAI and MiniMax emit them differently
    (OpenAI: usage in a trailing chunk with empty choices; MiniMax: usage
    inline alongside finish_reason).
    """
    events: list[SSEEvent] = []
    choice = first_choice(d)
    raw_delta = choice.get("delta")
    delta = raw_delta if isinstance(raw_delta, dict) else {}
    content = delta.get("content")
    if content is not None:
        events.append(SSEEvent(type="text_delta", data={"content": content}))
    tc_delta = delta.get("tool_calls", [])
    for tcd in tc_delta:
        idx = tcd.get("index", 0)
        if "function" in tcd:
            fname = tcd["function"].get("name") or ""
            fargs = tcd["function"].get("arguments", "") or ""
            events.append(SSEEvent(type="tool_call_delta", data={
                "index": idx, "name": fname, "arguments": fargs,
                "id": tcd.get("id", "") or "",
            }))
    return events


def first_choice(d: dict) -> dict:
    """Return choices[0] from an OpenAI-format SSE frame, or {} if missing/empty.

    Defensive against three legitimate frame shapes:
      - {"choices": [...]} — normal delta/finish frame
      - {"choices": [], "usage": {...}} — OpenAI trailing usage frame
      - {} or {"usage": {...}} — keepalive / pre-delta frame

    Replaces the unsafe d.get("choices", [{}])[0] pattern.
    """
    choices = d.get("choices")
    return choices[0] if choices else {}


# ── SSL retry infrastructure ────────────────────────────────────────────────

# Transient SSL/network errors that warrant a retry.
RETRYABLE_SSL_ERRORS = frozenset({
    "SSLV3_ALERT_BAD_RECORD_MAC",
    "SSLV3_ALERT_BAD_RECORD_MD5",
    "TLSV1_ALERT_DECRYPTION_FAILED",
    "TLSV1_ALERT_RECORD_OVERFLOW",
    "SSL_ERROR_SYSCALL",
    # SSLEOFError variants: server drops TLS connection mid-handshake or
    # mid-request. Emitted by MiniMax gateway on long-running streams and
    # by other providers under load. Transient — safe to retry with
    # exponential backoff. See docs/specs/SPEC-SSL-RETRY-FIX.md Layer 1.
    "EOF occurred in violation of protocol",
    "UNEXPECTED_EOF_WHILE_READING",
})

# OSError subclasses that indicate a transient network-level failure
# (NOT ssl.SSLError). ConnectionResetError happens when the peer
# abruptly closes a half-open socket; BrokenPipeError happens when
# writing to a socket the peer has already closed. socket.gaierror is a
# DNS resolution failure (EAI_AGAIN / EAI_FAIL) — see SPEC-DNS-RETRY-1.
# All are safe to retry. MUST be a tuple (not a list) for use with
# `except` and `isinstance`. See docs/specs/SPEC-SSL-RETRY-FIX.md Layer 1.
RETRYABLE_OSERROR_TYPES: tuple[type[Exception], ...] = (
    ConnectionResetError,
    BrokenPipeError,
    TimeoutError,  # Python 3.10+: alias for socket.timeout
    socket.gaierror,  # DNS: EAI_AGAIN "Temporary failure in name resolution"
)

MAX_SSL_RETRIES = 3
SSL_RETRY_BASE_MS = 500

# SPEC-21 SP3: per-host keep-alive. Default OFF (DEVELCAKES_HTTP_KEEPALIVE=1
# to enable). Idle connections only — a request checks a socket out of the
# pool so two agent threads never share one HTTPSConnection.
_POOL: dict[tuple[str, str, int], list[tuple[http.client.HTTPSConnection, float]]] = {}
_POOL_LOCK = threading.Lock()
_POOL_MAX_IDLE = 8
_POOL_IDLE_S = 30.0

_STALE_REUSED = (
    http.client.RemoteDisconnected,
    http.client.CannotSendRequest,
    http.client.BadStatusLine,
    BrokenPipeError,
    ConnectionResetError,
    ssl.SSLError,
)


def _keepalive_enabled() -> bool:
    """SPEC-21 SP3: DEVELCAKES_HTTP_KEEPALIVE=1 enables the pooled fast path.

    Default OFF until measured against a live provider. =1 pools; anything
    else (including unset) is today's urllib urlopen path.
    """
    return get_env("HTTP_KEEPALIVE") == "1"


def _pool_idle_count() -> int:
    return sum(len(bucket) for bucket in _POOL.values())


def _discard_conn(conn: http.client.HTTPSConnection) -> None:
    try:
        conn.close()
    except Exception as e:
        logger.debug("discard pooled conn: %s", e)


def _checkout(
    origin: tuple[str, str, int], timeout: float
) -> tuple[http.client.HTTPSConnection, bool]:
    """Return (connection, reused). Connection is not in the pool while in use."""
    now = time.monotonic()
    with _POOL_LOCK:
        bucket = _POOL.get(origin)
        while bucket:
            conn, parked = bucket.pop()
            if now - parked <= _POOL_IDLE_S:
                return conn, True
            _discard_conn(conn)
        if bucket is not None and not bucket:
            _POOL.pop(origin, None)
    return http.client.HTTPSConnection(origin[1], origin[2], timeout=timeout), False


def _return_to_pool(
    origin: tuple[str, str, int], conn: http.client.HTTPSConnection
) -> None:
    now = time.monotonic()
    with _POOL_LOCK:
        while _pool_idle_count() >= _POOL_MAX_IDLE:
            oldest_origin = None
            oldest_t = None
            for o, bucket in _POOL.items():
                if not bucket:
                    continue
                parked = bucket[0][1]
                if oldest_t is None or parked < oldest_t:
                    oldest_t = parked
                    oldest_origin = o
            if oldest_origin is None:
                break
            old_conn, _unused = _POOL[oldest_origin].pop(0)
            if not _POOL[oldest_origin]:
                del _POOL[oldest_origin]
            _discard_conn(old_conn)
        _POOL.setdefault(origin, []).append((conn, now))


class _PooledResponse:
    """HTTPResponse-shaped wrapper. On close(), drain leftover bytes and
    return the connection to the pool only when the response is reusable
    (2xx, keep-alive, drain reached EOF)."""

    def __init__(
        self,
        resp,
        conn: http.client.HTTPSConnection,
        origin: tuple[str, str, int],
        *,
        returnable: bool,
    ) -> None:
        self._resp = resp
        self._conn = conn
        self._origin = origin
        self._returnable = returnable
        self._closed = False

    def read(self, amt=None):
        return self._resp.read(amt)

    def readline(self, *args, **kwargs):
        return self._resp.readline(*args, **kwargs)

    def __iter__(self):
        return self._resp

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        reusable = self._returnable
        if reusable:
            try:
                if not self._resp.isclosed():
                    sock = getattr(self._conn, "sock", None)
                    old_to = None
                    if sock is not None:
                        old_to = sock.gettimeout()
                        sock.settimeout(1.0)
                    try:
                        leftover = 0
                        while leftover < 65536:
                            chunk = self._resp.read(4096)
                            if not chunk:
                                break
                            leftover += len(chunk)
                        else:
                            reusable = False
                    finally:
                        if sock is not None and old_to is not None:
                            try:
                                sock.settimeout(old_to)
                            except Exception:
                                pass
            except Exception:
                reusable = False
        if reusable:
            # HTTPResponse.close() closes the socket makefile. Leave the
            # response unread-closed so the idle HTTPSConnection can take
            # another request (the body was fully drained above).
            _return_to_pool(self._origin, self._conn)
        else:
            try:
                self._resp.close()
            except Exception:
                pass
            _discard_conn(self._conn)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def __getattr__(self, name):
        return getattr(self._resp, name)


def _origin_and_path(req) -> tuple[tuple[str, str, int], str]:
    parts = urlsplit(req.full_url)
    host = parts.hostname or ""
    port = parts.port or 443
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return ("https", host, port), path


def _request_headers(req) -> dict[str, str]:
    headers = dict(req.header_items())
    dropped = [k for k in headers if k.lower() == "connection"]
    for k in dropped:
        headers.pop(k)
    if not any(k.lower() == "user-agent" for k in headers):
        headers["User-Agent"] = (
            f"Python-urllib/{sys.version_info.major}.{sys.version_info.minor}"
        )
    return headers


def _response_returnable(resp) -> bool:
    conn_hdr = (resp.getheader("Connection") or "").lower()
    if conn_hdr == "close":
        return False
    version = getattr(resp, "version", 11)
    if version < 11 and conn_hdr != "keep-alive":
        return False
    return True


def _eligible(req) -> bool:
    if not _keepalive_enabled():
        return False
    method = (req.get_method() or "").upper()
    if method != "POST":
        return False
    if urlsplit(req.full_url).scheme != "https":
        return False
    if urllib.request.getproxies().get("https"):
        return False
    return True


def _try_pooled(req, timeout):
    """One pooled attempt.

    Returns a _PooledResponse, or None to fall back to urllib (ineligible,
    3xx, or a stale reused socket before any response byte). Raises
    HTTPError for status >= 400. Other failures take urllib's exception
    shape so the existing retry loop classifies them.
    """
    if not _eligible(req):
        return None
    origin, path = _origin_and_path(req)
    conn, reused = _checkout(origin, timeout)
    try:
        if reused and getattr(conn, "sock", None) is not None:
            conn.sock.settimeout(timeout)
        headers = _request_headers(req)
        try:
            conn.request("POST", path, body=req.data, headers=headers)
        except Exception as e:
            _discard_conn(conn)
            if reused and isinstance(e, _STALE_REUSED):
                return None
            if isinstance(e, OSError):
                raise urllib.error.URLError(e) from e
            raise
        try:
            resp = conn.getresponse()
        except Exception as e:
            _discard_conn(conn)
            if reused and isinstance(e, _STALE_REUSED):
                return None
            raise
    except urllib.error.URLError:
        raise
    except Exception:
        _discard_conn(conn)
        raise
    status = resp.status
    if 300 <= status < 400:
        try:
            resp.close()
        except Exception:
            pass
        _discard_conn(conn)
        return None
    if status >= 400:
        wrapped = _PooledResponse(resp, conn, origin, returnable=False)
        raise urllib.error.HTTPError(
            req.full_url,
            status,
            resp.reason,
            resp.headers,
            cast("IO[bytes]", wrapped),
        )
    return _PooledResponse(
        resp, conn, origin, returnable=_response_returnable(resp)
    )


def _open(req, timeout):
    """Pooled path when enabled and eligible; otherwise today's urlopen."""
    if _eligible(req):
        pooled = _try_pooled(req, timeout)
        if pooled is not None:
            return pooled
    return urllib.request.urlopen(req, timeout=timeout)


def is_retryable_ssl_error(exc: BaseException) -> bool:
    """Return True if `exc` (or anything in its reason/cause chain) is a
    transient SSL error that should trigger a retry.

    Walks three layers of the exception chain:
      1. `exc` itself
      2. `exc.reason` — populated by `urllib.request.do_open` when it
         wraps an `OSError` (including `ssl.SSLError`) in
         `urllib.error.URLError`. THIS IS THE KEY BUG FIX: the old
         `except ssl.SSLError` never fires when `do_open` wraps the
         SSL error in URLError first.
      3. `exc.__cause__` — populated by `raise X from Y` chains, e.g.
         `raise URLError(ssl.SSLError(...)) from ssl.SSLEOFError(...)`.

    For each candidate:
      - If it is an instance of `RETRYABLE_OSERROR_TYPES`, return True.
        These are TCP-level resets/errors that never produce an SSL reason
        string at all, plus `socket.gaierror` (DNS resolution failure —
        see SPEC-DNS-RETRY-1: transient EAI_AGAIN and permanent NXDOMAIN
        are both retried within budget; the trade-off is documented in
        `urlopen_with_ssl_retry`).
      - If it is an `ssl.SSLError`, check if `str(cand)` contains any
        token from `RETRYABLE_SSL_ERRORS`. Token-match (not isinstance
        check) because `ssl.SSLError` is one class but the reason
        string varies by underlying cause.
      - If it is a string (which happens when URLError is constructed
        with `URLError("EOF occurred in violation of protocol")` —
        .reason is then the raw string, not an exception), token-match
        the string itself. This is the common urllib idiom.

    Returns False for anything else (unclassified network errors,
    configuration errors, etc.) — those should surface to the caller
    immediately.

    See docs/specs/SPEC-SSL-RETRY-FIX.md §Helpers for the contract.
    """
    # String-coerced reason for the outer exception. str(URLError(s))
    # is "<urlopen error s>", but tokens like "EOF occurred in violation
    # of protocol" still appear as substrings of that wrapped form.
    outer_text = str(exc) if exc is not None else ""

    candidates: list[object] = [exc]
    # urllib.error.URLError exposes the underlying error as .reason —
    # sometimes a string (urllib's own "EOF occurred in violation of
    # protocol" path), sometimes an exception (do_open's wrap).
    reason = getattr(exc, "reason", None)
    if reason is not None and reason is not exc:
        candidates.append(reason)
    # `raise X from Y` populates __cause__
    cause = getattr(exc, "__cause__", None)
    if isinstance(cause, BaseException) and cause is not exc:
        candidates.append(cause)

    for cand in candidates:
        # OSError subclass fast-path: ConnectionResetError, BrokenPipeError
        if isinstance(cand, BaseException) and isinstance(cand, RETRYABLE_OSERROR_TYPES):
            return True
        # SSL token-match path: works for ssl.SSLError AND for the string
        # form passed as URLError.reason.
        if isinstance(cand, ssl.SSLError):
            reason_str = str(cand)
            if any(tok in reason_str for tok in RETRYABLE_SSL_ERRORS):
                return True
        elif isinstance(cand, str):
            if any(tok in cand for tok in RETRYABLE_SSL_ERRORS):
                return True
    # Outer-exception string fallback. URLError("EOF...") gives
    # str(exc) == "<urlopen error EOF occurred in violation of protocol>",
    # which still contains the token as a substring.
    if outer_text and any(tok in outer_text for tok in RETRYABLE_SSL_ERRORS):
        return True
    return False


def friendly_error_message(exc: Exception) -> str:
    """Translate a raw network/SSL exception into a user-facing message.

    Walks the same exception chain as `is_retryable_ssl_error`
    (`exc` → `.reason` → `.__cause__`) so that an `ssl.SSLEOFError` wrapped
    in `urllib.error.URLError` by `do_open` is still classified correctly.

      - `ssl.SSLError` whose text contains "EOF" or "shutdown" →
        "Connection to the AI provider was lost mid-response. Please try
        sending your message again."
      - Other `ssl.SSLError` → "Secure connection error: <reason>. Please
        try again." (rare; included so that any unmapped SSL failure still
        reads as a network problem rather than a Python traceback).
      - `ConnectionResetError` / `BrokenPipeError` →
        "Connection to the AI provider was reset. Please try sending your
        message again."
      - `TimeoutError` →
        "The AI provider took too long to respond (connection timed out
        after retries). The provider may be slow or overloaded. Please try
        sending your message again."
      - `socket.gaierror` →
        "Could not reach the AI provider (DNS lookup failed). Check your
        network connection and try again."
      - Anything else not listed above → `str(exc)` unchanged. Non-network
        errors are not rewritten because the user (or the agent itself)
        often needs the original message (e.g. validation errors).

    See docs/specs/SPEC-SSL-RETRY-FIX.md Layer 3 for the design.
    """
    if exc is None:
        return ""
    chain: list[object] = [exc]
    reason = getattr(exc, "reason", None)
    if reason is not None and reason is not exc:
        chain.append(reason)
    cause = getattr(exc, "__cause__", None)
    if isinstance(cause, BaseException) and cause is not exc:
        chain.append(cause)

    for cand in chain:
        if isinstance(cand, ssl.SSLError):
            text = str(cand)
            lowered = text.lower()
            if "eof" in lowered or "shutdown" in lowered:
                return ("Connection to the AI provider was lost mid-response. "
                        "Please try sending your message again.")
            return f"Secure connection error: {text}. Please try again."
        if isinstance(cand, (ConnectionResetError, BrokenPipeError)):
            return ("Connection to the AI provider was reset. "
                    "Please try sending your message again.")
        if isinstance(cand, TimeoutError):
            return ("The AI provider took too long to respond (connection timed out "
                    "after retries). The provider may be slow or overloaded. "
                    "Please try sending your message again.")
        if isinstance(cand, socket.gaierror):
            return ("Could not reach the AI provider (DNS lookup failed). "
                    "Check your network connection and try again.")
    return str(exc)


def urlopen_with_ssl_retry(req, timeout, *, max_retries=MAX_SSL_RETRIES):
    """Like urllib.request.urlopen but retries on transient SSL/DNS errors.

    Three except clauses trigger a retry attempt (membership decided by
    `is_retryable_ssl_error` which walks the exception chain):

      1. `ssl.SSLError` — raw SSL failure caught directly (reason-text
         token match against RETRYABLE_SSL_ERRORS).
      2. `urllib.error.URLError` — `urllib.request.do_open` wraps the
         underlying `OSError`/`ssl.SSLError`/`socket.gaierror` in `URLError`
         during the request-send phase. The old `except ssl.SSLError` never
         fires here; this clause unwraps via `is_retryable_ssl_error`.
      3. `RETRYABLE_OSERROR_TYPES` — bare (NOT URLError-wrapped)
         `ConnectionResetError` / `BrokenPipeError` / `TimeoutError` /
         `socket.gaierror`, e.g. TCP-level resets on the read side of a
         half-closed connection, or a DNS failure raised before any
         URLError wrapping.

    DNS coverage: ALL `socket.gaierror`s are treated as retryable within
    the existing budget, including permanent failures such as NXDOMAIN
    (errno -2). A permanent failure therefore costs at most `max_retries`
    extra attempts (~5–8s worst case with the default budget: 4 calls
    plus 0.5 + 1.0 + 2.0s of backoff sleeps) before the original
    exception propagates unchanged. That cost is accepted deliberately:
    it is far cheaper than losing an entire agent turn to a ~2-second
    resolver hiccup, and it keeps the classifier free of errno-specific
    special cases that would rot as resolver behaviour varies across
    platforms. (On 2026-09-12 a transient gaierror killed a live agent
    turn because this coverage did not exist; see SPEC-DNS-RETRY-1.)

    Each branch: if not retryable or max attempts reached, re-raise the
    original exception unchanged. Otherwise log a warning and sleep with
    exponential backoff (500ms × 2^attempt).

    See docs/specs/SPEC-SSL-RETRY-FIX.md Layer 1 and
    docs/specs/SPEC-DNS-RETRY-1.md for the design.
    """
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return _open(req, timeout)
        except (ConnectionResetError, BrokenPipeError, TimeoutError, socket.gaierror) as e:
            # Bare OSError subclasses — never SSL-wrapped, retry directly.
            # TimeoutError (Python 3.10+, alias for socket.timeout) fires
            # when the socket read exceeds the timeout. Retryable: the
            # provider may be slow or under load.
            # socket.gaierror fires when the resolver fails before any
            # connection is made (SPEC-DNS-RETRY-1) — urllib usually wraps
            # it in URLError, but a caller using a custom socket can see it
            # raw. Both paths must retry.
            if attempt == max_retries:
                raise
            last_exc = e
            wait_s = (SSL_RETRY_BASE_MS / 1000) * (2 ** attempt)
            logger.warning(
                "[ssl-retry] attempt %d/%d for %s — %s; retrying in %.1fs",
                attempt + 1, max_retries, req.full_url, e, wait_s,
            )
            time.sleep(wait_s)
        except urllib.error.URLError as e:
            # do_open wraps the underlying SSL/OSError in URLError.
            # Walk the chain via is_retryable_ssl_error to decide.
            if not is_retryable_ssl_error(e) or attempt == max_retries:
                raise
            last_exc = e
            reason_str = str(e)
            wait_s = (SSL_RETRY_BASE_MS / 1000) * (2 ** attempt)
            logger.warning(
                "[ssl-retry] attempt %d/%d for %s — %s; retrying in %.1fs",
                attempt + 1, max_retries, req.full_url, reason_str, wait_s,
            )
            time.sleep(wait_s)
        except ssl.SSLError as e:
            # Direct SSL error (bypassed urllib wrapping, e.g. in tests
            # or when called from custom sockets). Token-match against
            # RETRYABLE_SSL_ERRORS.
            reason = str(e)
            is_retryable = any(tok in reason for tok in RETRYABLE_SSL_ERRORS)
            if not is_retryable or attempt == max_retries:
                raise
            last_exc = e
            wait_s = (SSL_RETRY_BASE_MS / 1000) * (2 ** attempt)
            logger.warning(
                "[ssl-retry] attempt %d/%d for %s — %s; retrying in %.1fs",
                attempt + 1, max_retries, req.full_url, reason, wait_s,
            )
            time.sleep(wait_s)
    raise last_exc  # should not reach here


def stream_with_ssl_retry(
    streamer,
    *,
    max_retries: int = MAX_SSL_RETRIES,
    **kwargs,
):
    """Wrap an SSE-event generator and retry the entire stream on transient
    SSL/network failures during body iteration.

    `urlopen_with_ssl_retry` only protects the request-send phase. Once
    `urlopen()` returns and the SSE body is being read line-by-line, an
    `ssl.SSLEOFError` raised from the underlying socket is surfaced directly
    to the caller of `streamer(...)`. This wrapper catches those mid-stream
    drops and re-issues the full streaming call so the connection is
    re-established from scratch.

    Suppresses retry once any `text_delta` event has been yielded (the user
    has already seen partial text; a retry would produce garbled duplicate
    output in the UI). In that case the exception is re-raised so the caller
    surfaces it via `friendly_error_message`.

    Args:
        streamer: A callable matching the `OpenAIProvider("openai").stream` /
            `MiniMaxProvider().stream` / `AnthropicProvider().stream` signature:
            keyword arguments `base_url`, `api_key`, `model`, `messages`,
            `tools`, `timeout`, `x_title`. Called once per attempt.
        max_retries: Total retry budget (default `MAX_SSL_RETRIES`).
        **kwargs: Forwarded verbatim to `streamer`.

    Yields:
        SSEEvent instances as produced by `streamer`.

    Raises:
        ssl.SSLError / ConnectionResetError / BrokenPipeError /
        urllib.error.URLError: if the failure is unretryable (already
            yielded text), not a transient SSL/network error, or the retry
            budget is exhausted.

    See docs/specs/SPEC-SSL-RETRY-FIX.md Layer 2 for the design.
    """
    last_exc: BaseException | None = None
    for attempt in range(max_retries + 1):
        streamed_text = False
        try:
            for ev in streamer(**kwargs):
                if ev.type == "text_delta":
                    streamed_text = True
                yield ev
            return
        except (ConnectionResetError, BrokenPipeError, TimeoutError) as e:
            # TimeoutError (Python 3.10+, alias for socket.timeout) fires
            # during chunked transfer when the socket read exceeds the
            # timeout between SSE chunks. Retryable: the provider may be
            # slow, under load, or the connection is stalling.
            if streamed_text or attempt == max_retries:
                raise
            last_exc = e
            wait_s = (SSL_RETRY_BASE_MS / 1000) * (2 ** attempt)
            logger.warning(
                "[ssl-retry-stream] attempt %d/%d — %s; retrying in %.1fs",
                attempt + 1, max_retries, e, wait_s,
            )
            time.sleep(wait_s)
        except urllib.error.URLError as e:
            if streamed_text or attempt == max_retries:
                raise
            # do_open wraps the underlying SSL/OSError in URLError — walk
            # the chain to decide whether this is a transient SSL drop.
            if not is_retryable_ssl_error(e):
                raise
            last_exc = e
            wait_s = (SSL_RETRY_BASE_MS / 1000) * (2 ** attempt)
            logger.warning(
                "[ssl-retry-stream] attempt %d/%d — %s; retrying in %.1fs",
                attempt + 1, max_retries, e, wait_s,
            )
            time.sleep(wait_s)
        except ssl.SSLError as e:
            reason = str(e)
            is_retryable = any(tok in reason for tok in RETRYABLE_SSL_ERRORS)
            if streamed_text or not is_retryable or attempt == max_retries:
                raise
            last_exc = e
            wait_s = (SSL_RETRY_BASE_MS / 1000) * (2 ** attempt)
            logger.warning(
                "[ssl-retry-stream] attempt %d/%d — %s; retrying in %.1fs",
                attempt + 1, max_retries, reason, wait_s,
            )
            time.sleep(wait_s)
    # All retries exhausted; last_exc is guaranteed to be set because the
    # only way to exit the loop without returning/raising is via the
    # `if attempt == max_retries: raise` branches above (which raise before
    # reassigning last_exc). Defensive raise anyway.
    if last_exc is not None:
        raise last_exc
