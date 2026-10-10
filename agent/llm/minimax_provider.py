"""MiniMax ChatCompletion v2 LLM provider."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Iterator

from agent.llm.cost import model_id
from agent.llm.streaming import (
    SSEEvent,
    sse_lines,
    parse_sse_line,
    parse_sse_delta,
    first_choice,
    urlopen_with_ssl_retry,
)

logger = logging.getLogger(__name__)


def _handle_finish_frame(d: dict) -> Iterator[SSEEvent]:
    """Process a parsed SSE data frame at finish_reason termination.

    Shared by both the first-line and subsequent-line paths in
    MiniMaxProvider.stream(). Yields delta events, then optional
    error/usage/done events when finish_reason is set.

    Handles both:
    - Top-level provider error (empty choices): {"error":{...},"choices":[]}
    - finish_reason termination: {"choices":[{"finish_reason":"error",...}]}
    """
    # Top-level error with empty choices:
    #   {"error":{"code":429,...},"choices":[]}
    # Must be caught BEFORE the choice/finish_reason path.
    top_error = d.get("error")
    if top_error and isinstance(top_error, dict):
        yield SSEEvent(type="error", data={"error": top_error})
        yield SSEEvent(type="done", data={})
        return
    choice = first_choice(d)
    if choice:
        for out_ev in parse_sse_delta(d):
            yield out_ev
        finish_reason = choice.get("finish_reason")
        # Some providers (e.g. OpenRouter) send finish_reason="error" for
        # mid-stream errors. Handle "content_filter" too.
        if finish_reason in ("stop", "tool_calls", "length", "error", "content_filter"):
            if finish_reason == "error":
                error_data = d.get("error", {})
                if error_data:
                    yield SSEEvent(type="error", data={"error": error_data})
            elif finish_reason == "content_filter":
                yield SSEEvent(type="error", data={"error": {
                    "code": 400,
                    "reason": "content_filter",
                    "message": "Content was filtered by the provider (finish_reason=content_filter).",
                }})
            # BUG #4: yield usage even with a recognized finish_reason,
            # because not every provider sends a separate usage frame.
            usage = d.get("usage")
            if usage:
                yield SSEEvent(type="usage", data={"usage": usage})
            yield SSEEvent(type="done", data={})
        else:
            # BUG #4: still yield usage when finish_reason is not in the
            # recognized set (or None), so usage frames are not dropped.
            usage = d.get("usage")
            if usage:
                yield SSEEvent(type="usage", data={"usage": usage})


class MiniMaxProvider:
    """MiniMax ChatCompletion v2 API.

    Uses the OpenAI-compatible message format but has a different endpoint
    path (/text/chatcompletion_v2), body-level error envelopes, and a different
    finish-detection mechanism.
    """

    @property
    def provider_id(self) -> str:
        return "minimax"

    @property
    def response_format(self) -> str:
        return "openai"  # response shape is OpenAI-compatible

    def call(
        self,
        base_url: str,
        api_key: str,
        model: str,
        messages: list[dict],
        tools: list[dict] | None,
        timeout: float,
        x_title: str = "",
        reasoning_effort: str = "off",
    ) -> dict:
        """Call MiniMax ChatCompletion v2 API."""
        endpoint = f"{base_url.rstrip('/')}/text/chatcompletion_v2"
        payload = {
            "model": model_id(model),
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools

        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            endpoint,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        try:
            with urlopen_with_ssl_retry(req, timeout=timeout) as resp:
                result = json.loads(resp.read())
                # MiniMax returns body-level errors with HTTP 200:
                # {"base_resp":{"status_code":1004,"status_msg":"login fail..."}}
                base_resp = result.get("base_resp") or {}
                status_code = base_resp.get("status_code", 0)
                if status_code != 0:
                    status_msg = base_resp.get("status_msg", "unknown error")
                    raise RuntimeError(
                        f"MiniMax API error (status_code={status_code}): {status_msg}"
                    )
                return result
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"MiniMax API error {e.code} {e.reason}: {body}"
            ) from e

    def stream(
        self,
        base_url: str,
        api_key: str,
        model: str,
        messages: list[dict],
        tools: list[dict] | None,
        timeout: float,
        x_title: str = "",
        reasoning_effort: str = "off",
    ):
        """Yield SSE events from MiniMax ChatCompletion streaming API (OpenAI-compatible)."""
        endpoint = f"{base_url.rstrip('/')}/text/chatcompletion_v2"
        payload = {
            "model": model_id(model),
            "messages": messages,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools

        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            endpoint,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        try:
            resp = urlopen_with_ssl_retry(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                body = "(could not read body)"
            logger.error(
                "Provider HTTP %d from %s (model=%s): %s",
                e.code, req.full_url, model, body[:500],
            )
            raise
        with resp as resp:
            # MiniMax may return a body-level error with HTTP 200 (not SSE).
            first_line = None
            for line in sse_lines(resp):
                if line.strip():
                    first_line = line
                    break
            if first_line is not None:
                try:
                    parsed = json.loads(first_line.decode("utf-8"))
                    base_resp = parsed.get("base_resp", {})
                    status_code = base_resp.get("status_code", 0)
                    if status_code != 0:
                        status_msg = base_resp.get("status_msg", "unknown error")
                        raise RuntimeError(
                            f"MiniMax API error (status_code={status_code}): {status_msg}"
                        )
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass

                ev = parse_sse_line(first_line)
                if ev is not None:
                    if ev.type == "done":
                        yield SSEEvent(type="done", data={})
                        return
                    if ev.type == "raw":
                        had_done = False
                        for out_ev in _handle_finish_frame(ev.data):
                            yield out_ev
                            if out_ev.type == "done":
                                had_done = True
                        if had_done:
                            return

            for line in sse_lines(resp):
                # MiniMax may send base_resp body-level errors mid-stream
                # (HTTP 200 with error envelope after initial text deltas).
                # Must check BEFORE parse_sse_line — non-SSE body-level error
                # lines are not valid SSE and parse_sse_line returns None.
                try:
                    raw = json.loads(line.decode("utf-8"))
                    base_resp = raw.get("base_resp") or {}
                    status_code = base_resp.get("status_code", 0)
                    if status_code != 0:
                        status_msg = base_resp.get("status_msg", "unknown error")
                        raise RuntimeError(
                            f"MiniMax API error (status_code={status_code}): {status_msg}"
                        )
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass

                ev = parse_sse_line(line)
                if ev is None:
                    continue
                if ev.type == "done":
                    yield SSEEvent(type="done", data={})
                    return
                if ev.type != "raw":
                    continue
                had_done = False
                for out_ev in _handle_finish_frame(ev.data):
                    yield out_ev
                    if out_ev.type == "done":
                        had_done = True
                if had_done:
                    return