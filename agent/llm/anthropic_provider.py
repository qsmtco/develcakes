"""Anthropic Messages API LLM provider."""

from __future__ import annotations

import json
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


def _merge_usage(acc: dict, raw: dict) -> bool:
    """Fold Anthropic usage into OpenAI keys.

    True when at least one OpenAI-shaped key was written. Values are
    assigned, not added (Anthropic's output_tokens is cumulative).
    """
    if not isinstance(raw, dict):
        return False
    changed = False
    if isinstance(raw.get("input_tokens"), int) and not isinstance(raw.get("input_tokens"), bool):
        acc["prompt_tokens"] = raw["input_tokens"]
        changed = True
    if isinstance(raw.get("output_tokens"), int) and not isinstance(raw.get("output_tokens"), bool):
        acc["completion_tokens"] = raw["output_tokens"]
        changed = True
    return changed


def _anthropic_max_output_tokens(model: str) -> int:
    """Return a safe max_tokens ceiling for the target Anthropic model."""
    raw = model if isinstance(model, str) else ""
    bare = model_id(raw).strip().lower()
    if bare.startswith((
        "claude-3-opus",
        "claude-3-sonnet",
        "claude-3-haiku",
        "claude-2",
        "claude-instant",
    )) or bare == "claude-3-5-sonnet-20240620":
        return 4096
    if bare.startswith((
        "claude-3-7",
        "claude-sonnet-4",
        "claude-opus-4",
        "claude-haiku-4",
        "claude-4",
    )):
        return 16384
    return 8192


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
            "max_tokens": _anthropic_max_output_tokens(model),
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
                parsed = json.loads(resp.read())
                if isinstance(parsed, dict) and parsed.get("stop_reason") == "max_tokens":
                    parsed["_output_truncated"] = True
                return parsed
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
            "max_tokens": _anthropic_max_output_tokens(model),
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
        usage: dict = {}
        stop_reason: str = ""
        with urlopen_with_ssl_retry(req, timeout=timeout) as resp:
            for line in sse_lines(resp):
                ev = parse_sse_line(line)
                if ev is None:
                    continue
                if ev.type == "done":
                    done_data = {"truncated": True} if stop_reason == "max_tokens" else {}
                    yield SSEEvent(type="done", data=done_data)
                    return
                if ev.type != "raw":
                    continue
                d = ev.data
                etype = d.get("type", "")
                if etype == "message_start":
                    msg = d.get("message")
                    msg_usage = msg.get("usage") if isinstance(msg, dict) else {}
                    if _merge_usage(usage, msg_usage if isinstance(msg_usage, dict) else {}):
                        yield SSEEvent(type="usage", data={"usage": dict(usage)})
                elif etype == "content_block_start":
                    block = d.get("content_block") or {}
                    if block.get("type") == "tool_use":
                        yield SSEEvent(type="tool_call_delta", data={
                            "index": d.get("index", 0),
                            "name": block.get("name", "") or "",
                            "arguments": "",
                            "id": block.get("id", "") or "",
                        })
                elif etype == "content_block_delta":
                    delta = d.get("delta") or {}
                    dtype = delta.get("type", "")
                    if dtype == "text_delta":
                        yield SSEEvent(type="text_delta", data={"content": delta.get("text", "")})
                    elif dtype == "input_json_delta":
                        yield SSEEvent(type="tool_call_delta", data={
                            "index": d.get("index", 0),
                            "name": "",
                            "arguments": delta.get("partial_json", "") or "",
                            "id": "",
                        })
                elif etype == "message_delta":
                    delta_meta = d.get("delta")
                    if isinstance(delta_meta, dict) and isinstance(delta_meta.get("stop_reason"), str):
                        stop_reason = delta_meta["stop_reason"]
                    usage_raw = d.get("usage")
                    if _merge_usage(usage, usage_raw if isinstance(usage_raw, dict) else {}):
                        yield SSEEvent(type="usage", data={"usage": dict(usage)})
                elif etype == "message_stop":
                    done_data = {"truncated": True} if stop_reason == "max_tokens" else {}
                    yield SSEEvent(type="done", data=done_data)
                    return