"""Anthropic message and tool format converters.

Extracted from agent/runtime.py (Phase B2). Pure functions — convert
OpenAI-format message/tool dicts to Anthropic's content-block format.
No network, no GTK, no state.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


def convert_messages_for_anthropic(messages: list[dict]) -> list[dict]:
    """Convert OpenAI-format message dicts → Anthropic message format.

    Handles:
    - system: stripped, returned as user role with text content
    - assistant with tool_calls: converted to Anthropic content blocks
    - tool: converted to Anthropic tool_result blocks
    - all others: passed through as-is
    """
    api_messages: list[dict] = []
    for msg in messages:
        role = msg.get("role", "")
        if role == "system":
            content_text = msg.get("content", "")
            if content_text:
                api_messages.append({"role": "user", "content": content_text})
        elif role == "assistant" and msg.get("tool_calls"):
            content_blocks: list[dict] = []
            if msg.get("content"):
                content_blocks.append({"type": "text", "text": msg["content"]})
            for tc in msg["tool_calls"]:
                args_str = tc["function"]["arguments"]
                if isinstance(args_str, str):
                    try:
                        args_str = json.loads(args_str)
                    except Exception as e:
                        # Phase 9: log instead of silently passing. Malformed
                        # JSON args may indicate upstream provider corruption;
                        # a debug message lets the developer trace it without
                        # disrupting the message conversion.
                        logger.debug("Failed to parse tool-call args JSON: %s", e)
                content_blocks.append({
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": tc["function"]["name"],
                    "input": args_str,
                })
            api_messages.append({"role": "assistant", "content": content_blocks})
        elif role == "tool":
            api_messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": msg["tool_call_id"],
                    "content": msg["content"],
                }],
            })
        else:
            api_messages.append(msg)
    return api_messages


def _tool_call_ids(message: dict) -> list[str]:
    """Return the well-formed tool-call ids declared by ``message`` (in order)."""
    calls = message.get("tool_calls")
    if not isinstance(calls, list):
        return []
    ids: list[str] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        call_id = call.get("id")
        if isinstance(call_id, str) and call_id:
            ids.append(call_id)
    return ids


def normalize_tool_history(messages: list[dict]) -> list[dict]:
    """Repair tool-call/result pairing in OpenAI-style message lists.

    Rules (idempotent — normalize(normalize(x)) == normalize(x)):
    1. Collect every tool_call id from assistant messages' `tool_calls`.
    2. Drop any `role:"tool"` message whose `tool_call_id` is not in that set.
    3. For every assistant tool_call id with no matching `role:"tool"`
       message, synthesize {"role":"tool","tool_call_id":id,"content":""}
       immediately after that assistant message.
    4. Move each surviving tool result to directly follow the assistant
       message that declared its id (preserving relative order of results
       for the same assistant message).
    Non-tool messages pass through in original relative order. Never mutates
    the input list; returns a new list.
    """
    if not isinstance(messages, list):
        return messages

    # Pass 1: non-tool messages keep their relative order; remember which
    # assistant message declared each id (first declaration wins).
    kept: list[dict] = []
    ids_by_index: dict[int, list[str]] = {}
    declared_at: dict[str, int] = {}
    for msg in messages:
        if not isinstance(msg, dict):
            kept.append(msg)
            continue
        if msg.get("role") == "tool":
            continue
        index = len(kept)
        kept.append(msg)
        if msg.get("role") != "assistant":
            continue
        ids = _tool_call_ids(msg)
        if not ids:
            continue
        ids_by_index[index] = ids
        for call_id in ids:
            declared_at.setdefault(call_id, index)

    # Pass 2: bucket surviving results under their declaring assistant message.
    results_at: dict[int, list[dict]] = {}
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        index = declared_at.get(msg.get("tool_call_id"))
        if index is None:
            logger.debug("normalize_tool_history: dropped orphan tool result")
            continue
        results_at.setdefault(index, []).append(msg)

    # Pass 3: emit each assistant message followed by its results, then by
    # empty results for any call that never got one.
    out: list[dict] = []
    for index, msg in enumerate(kept):
        out.append(msg)
        ids = ids_by_index.get(index)
        if not ids:
            continue
        answered: set[str] = set()
        for result in results_at.get(index, []):
            out.append(result)
            answered.add(result.get("tool_call_id"))
        for call_id in ids:
            if call_id in answered:
                continue
            answered.add(call_id)
            out.append({"role": "tool", "tool_call_id": call_id, "content": ""})
    return out


def convert_tools_for_anthropic(tools: list[dict]) -> list[dict]:
    """Convert OpenAI-format tool dicts → Anthropic tool schema.

    Input:  [{"function": {"name", "description", "parameters"}}}]
    Output: [{"name", "description", "input_schema"}]

    Defensive: defaults missing 'description' to '' and missing/'None'
    'parameters' to {} so malformed upstream tool dicts don't crash
    with KeyError. Anthropic accepts input_schema={} (no required params).
    """
    result: list[dict] = []
    for t in tools:
        fn = t.get("function") or {}
        params = fn.get("parameters")
        if not isinstance(params, dict):
            params = {}
        entry: dict[str, object] = {
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "input_schema": params,
        }
        result.append(entry)
    return result
