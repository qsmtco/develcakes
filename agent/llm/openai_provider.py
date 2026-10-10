"""OpenAI-compatible LLM provider (openai, openrouter, zai)."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

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


def _apply_reasoning(payload: dict, caller: str, level: str) -> None:
    """Map the abstract level to this caller's wire format.

    off / "" / unknown caller → omit the field entirely. Runtime already
    collapses supports_reasoning=False to "off"; this is the second guard.
    """
    if not level or level == "off":
        return
    if caller == "openai":
        payload["reasoning_effort"] = level
    elif caller == "openrouter":
        payload["reasoning"] = {"effort": level}


class OpenAIProvider:
    """Handles OpenAI, OpenRouter, and ZAI APIs (all OpenAI-compatible).

    The provider_id is set at construction so a single class serves multiple
    registry entries. The wire protocol is identical; only credentials and
    base_url differ (both passed by the caller).
    """

    def __init__(self, provider_id: str = "openai"):
        self._id = provider_id

    @property
    def provider_id(self) -> str:
        return self._id

    @property
    def response_format(self) -> str:
        return "openai"

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
        """Call OpenAI Chat Completions API (also used by OpenRouter, ZAI)."""
        endpoint = f"{base_url.rstrip('/')}/chat/completions"
        payload = {
            "model": model_id(model),
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        _apply_reasoning(payload, self._id, reasoning_effort)

        body = json.dumps(payload).encode()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        if x_title:
            headers["HTTP-Referer"] = "https://github.com/qsmtco/crabcakes"
            headers["X-Title"] = x_title
        req = urllib.request.Request(
            endpoint,
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urlopen_with_ssl_retry(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"OpenAI API error {e.code} {e.reason}: {body}"
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
        """Yield SSE events from OpenAI Chat Completions streaming API (also used by OpenRouter, ZAI)."""
        endpoint = f"{base_url.rstrip('/')}/chat/completions"
        payload = {
            "model": model_id(model),
            "messages": messages,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        _apply_reasoning(payload, self._id, reasoning_effort)

        body = json.dumps(payload).encode()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        if x_title:
            headers["HTTP-Referer"] = "https://github.com/qsmtco/crabcakes"
            headers["X-Title"] = x_title
        req = urllib.request.Request(
            endpoint,
            data=body,
            headers=headers,
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
            for line in sse_lines(resp):
                ev = parse_sse_line(line)
                if ev is None:
                    continue
                if ev.type == "done":
                    yield SSEEvent(type="done", data={})
                    return
                if ev.type != "raw":
                    continue
                d = ev.data
                # OpenRouter sends top-level error with empty choices:
                #   {"error":{"code":429,...},"choices":[]}
                # This must be caught BEFORE the choice/finish_reason path.
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
                    # OpenRouter sends finish_reason="error" for mid-stream
                    # errors (rate limits, provider failures, content filters).
                    # Also handle "content_filter" which OpenAI defines but
                    # our known set was missing. Without these, the stream
                    # never emits a done event and falls through to the
                    # fallback path, producing an empty response.
                    # See docs/specs/SPEC-OPENROUTER-FINISH-REASON-FIX.md .
                    if finish_reason in ("stop", "tool_calls", "length", "error", "content_filter"):
                        # OpenRouter mid-stream errors carry the error details
                        # in the same SSE chunk alongside finish_reason="error":
                        #   {"error":{"code":429,"message":"Rate limit exceeded"},
                        #    "choices":[{"finish_reason":"error"}]}
                        # Yield a dedicated error event so the runtime can
                        # surface the actual error message to the user.
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
                        usage = d.get("usage")
                        if usage:
                            yield SSEEvent(type="usage", data={"usage": usage})
                        yield SSEEvent(type="done", data={})
                        return
                usage = d.get("usage")
                if usage:
                    yield SSEEvent(type="usage", data={"usage": usage})