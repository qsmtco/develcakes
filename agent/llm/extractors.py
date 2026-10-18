"""Response extractors for LLM API responses.

Extracted from agent/runtime.py (Phase B3). Parses tool calls, text content,
and token usage from both OpenAI and Anthropic response formats. Pure functions —
the response format is passed explicitly via the ``response_format`` parameter.

Note: _is_empty_content stays in runtime.py — it is used at non-extractor sites.
"""

from __future__ import annotations

import json
import logging
import uuid

logger = logging.getLogger(__name__)

# Arguments that arrived cut off. The runtime reports this and does not
# run the tool. The key is not sent back to the model.
TRUNCATED_ARGUMENTS = "__truncated_arguments__"


def parse_tool_arguments(args_raw: object) -> dict:
    """Return a dict the tool loop can run, or a one-key truncation marker."""
    if isinstance(args_raw, dict):
        return args_raw
    if not isinstance(args_raw, str) or not args_raw:
        if args_raw in (None, ""):
            return {}
        logger.warning(
            "[extract] tool arguments are incomplete and will be returned as an error: %.200r",
            args_raw,
        )
        return {TRUNCATED_ARGUMENTS: str(args_raw)}
    try:
        parsed = json.loads(args_raw)
    except (json.JSONDecodeError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    logger.warning(
        "[extract] tool arguments are incomplete and will be returned as an error: %.200r",
        args_raw,
    )
    return {TRUNCATED_ARGUMENTS: args_raw}


def is_truncated_arguments(args: object) -> bool:
    """True when parse_tool_arguments kept a call that must not be executed."""
    return (
        isinstance(args, dict)
        and TRUNCATED_ARGUMENTS in args
        and len(args) == 1
    )


def extract_tool_calls(
    response: dict, response_format: str = "openai"
) -> list[tuple[str, str, dict]]:
    """
    Extract tool calls from an API response dict.

    Returns [(call_id, tool_name, arguments)].

    Handles OpenAI, MiniMax, and Anthropic formats.
    """
    calls = []

    if response_format == "openai":
        # OpenAI/MiniMax Chat Completions format
        choices = response.get("choices", [])
        if not choices:
            return []
        delta = choices[0].get("delta", {})
        message = choices[0].get("message", delta)

        if message.get("tool_calls"):
            for tc in message["tool_calls"]:
                func = tc.get("function", {})
                # QTR-FIX: use `or` so that None / empty-string ids fall through
                # to the synthetic fallback instead of propagating as an empty
                # tool_call_id. The previous form `tc.get("id", default)` only
                # substituted when the key was absent — explicit None / "" slipped
                # through and later matched `if not call_id` guards in unexpected
                # places. See `_extract_tool_calls (empty-id fallback)` regression
                # test for the synthetic-id contract.
                call_id = tc.get("id") or f"call_{uuid.uuid4().hex[:8]}"
                name = func.get("name", "")
                # Missing arguments are an empty object. A present non-object
                # is a cut-off stream and must not be dropped.
                present = func.get("arguments")
                args = parse_tool_arguments("{}" if present in (None, "") else present)
                calls.append((call_id, name, args))

    elif response_format == "anthropic":
        # Anthropic Messages API format
        content = response.get("content", [])
        if not isinstance(content, list):
            return []
        for block in content:
            if block.get("type") == "tool_use":
                # QTR-FIX: same None / empty-string fallback as the OpenAI path.
                call_id = block.get("id") or f"call_{uuid.uuid4().hex[:8]}"
                name = block.get("name", "")
                args = block.get("input", {})
                calls.append((call_id, name, args))

    return calls


def extract_text_content(response: dict, response_format: str = "openai") -> str:
    """Extract text content from an API response."""
    if response_format == "openai":
        choices = response.get("choices", [])
        if not choices:
            return ""
        msg = choices[0].get("message", {})
        return msg.get("content", "") or ""

    elif response_format == "anthropic":
        content = response.get("content", [])
        if not isinstance(content, list):
            return ""
        parts = [b.get("text", "") for b in content if b.get("type") == "text"]
        return "".join(parts)

    return ""


def extract_usage(
    response: dict, response_format: str = "openai"
) -> tuple[int, int]:
    """Extract (prompt_tokens, completion_tokens) from API response."""
    usage = response.get("usage")
    if not usage:
        return 0, 0
    if response_format == "anthropic":
        return (
            usage.get("input_tokens", 0),
            usage.get("output_tokens", 0),
        )
    # OpenAI / MiniMax
    return (
        usage.get("prompt_tokens", 0),
        usage.get("completion_tokens", 0),
    )
