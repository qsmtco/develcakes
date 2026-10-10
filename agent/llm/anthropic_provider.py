"""Anthropic Messages API LLM provider."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Iterator

from agent.llm.convert import convert_messages_for_anthropic, convert_tools_for_anthropic
from agent.llm.cost import model_id
from agent.llm.streaming import (
    SSEEvent,
    sse_lines,
    parse_sse_line,
    urlopen_with_ssl_retry,
)

logger = logging.getLogger(__name__)


class AnthropicProvider:
    """Anthropic Messages API.

    Requires message/tool format conversion (system message extraction,
    content-block format). Uses x-api-key header, not Bearer auth.
    """

    @property
    def provider_id(self) -> str:
        return "anthropic"

    @property
    def response_format(self) -> str:
        return "anthropic"

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
        """Call Anthropic Messages API."""
        endpoint = f"{base_url.rstrip('/')}/messages"
        system_msg: str | None = None
        non_system_messages: list[dict] = []
        for msg in messages:
            if msg.get("role") == "system":
                if system_msg is None:
                    content = msg.get("content", "")
                    system_msg = content if isinstance(content, str) else ""
            else:
                non_system_messages.append(msg)
        api_messages = convert_messages_for_anthropic(non_system_messages)

        payload: dict[str, Any] = {
            "model": model_id(model),
            "messages": api_messages,
            "max_tokens": 4096,
        }
        if system_msg:
            payload["system"] = system_msg
        if tools:
            payload["tools"] = convert_tools_for_anthropic(tools)

        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            endpoint,
            data=body,
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        try:
            with urlopen_with_ssl_retry(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Anthropic API error {e.code} {e.reason}: {body}"
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
    ) -> Iterator:
        """Yield SSE events from Anthropic Messages streaming API."""
        endpoint = f"{base_url.rstrip('/')}/messages"
        system_msg: str | None = None
        non_system_messages: list[dict] = []
        for msg in messages:
            if msg.get("role") == "system":
                if system_msg is None:
                    content = msg.get("content", "")
                    system_msg = content if isinstance(content, str) else ""
            else:
                non_system_messages.append(msg)
        api_messages = convert_messages_for_anthropic(non_system_messages)

        payload: dict[str, Any] = {
            "model": model_id(model),
            "messages": api_messages,
            "max_tokens": 4096,
            "stream": True,
        }
        if system_msg:
            payload["system"] = system_msg
        if tools:
            payload["tools"] = convert_tools_for_anthropic(tools)

        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            endpoint,
            data=body,
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        with urlopen_with_ssl_retry(req, timeout=timeout) as resp:
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
                etype = d.get("type", "")
                if etype == "content_block_start":
                    block = d.get("content_block") or {}
                    if block.get("type") == "tool_use":
                        yield SSEEvent(type="tool_call_delta", data={
                            "index": d.get("index", 0),
                            "name": "",
                            "arguments": "",
                            "id": block.get("id", "") or "",
                        })
                elif etype == "content_block_delta":
                    delta = d.get("delta") or {}
                    dtype = delta.get("type", "")
                    if dtype == "text_delta":
                        yield SSEEvent(type="text_delta", data={"content": delta.get("text", "")})
                    elif dtype == "tool_use_delta":
                        idx = d.get("index", 0)
                        fname = delta.get("name") or ""
                        fargs = delta.get("input", "") or ""
                        yield SSEEvent(type="tool_call_delta", data={
                            "index": idx, "name": fname, "arguments": fargs,
                            "id": "",
                        })
                elif etype == "message_delta":
                    usage = d.get("usage")
                    if usage:
                        yield SSEEvent(type="usage", data={"usage": usage})
                elif etype == "message_stop":
                    yield SSEEvent(type="done", data={})
                    return