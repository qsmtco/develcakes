# PHASE 2 — Fix `_stream_anthropic_events`

## Objective
Fix three bugs in `_stream_anthropic_events` (lines 667–775):
1. **W3**: Missing `stream_options` removal from payload
2. **W2**: Missing message/tool conversion — uses raw messages dicts and tool objects instead of calling the new helpers
3. **W4**: Wrong type annotation on `_sse_lines` (`list[bytes]` should be `Iterator[bytes]`)

## Files to Read First
- `/path/to/projects/crabcakes/agent/runtime.py` (lines 400–430, 667–775)
- `/path/to/projects/crabcakes/docs/specs/SPEC-RUNTIME-HARDENING-AUDIT.md` (W2, W3, W4)

## Step 1 — Fix `_sse_lines` Return Type (W4)

Find line 412 and change:
```python
def _sse_lines(resp) -> list[bytes]:
```
to:
```python
def _sse_lines(resp) -> Iterator[bytes]:
```

Also add `Iterator` to the `from typing import` import if not already present (check line 1–20).

## Step 2 — Fix `_stream_anthropic_events` Payload (W3)

In `_stream_anthropic_events`, find the payload construction and **remove** `stream_options`.
The payload currently (around line 692) looks like:
```python
payload = {
    "model": model,
    "messages": messages,   # ← still raw dict here, fixed in Step 3
    "max_tokens": 8192,
    "system": system_prompt,
    "stream": True,
    "stream_options": {"include_usage": True},   # ← REMOVE THIS LINE
    "tools": tools or [],   # ← still raw tool objects, fixed in Step 3
}
```

Remove the `stream_options` line entirely.

## Step 3 — Fix Message and Tool Conversion (W2)

After `base_url`/`api_key`/`model` extraction, before payload construction:

```python
# Convert messages and tools using shared helpers
converted_messages = _convert_messages_for_anthropic(messages)
converted_tools = _convert_tools_for_anthropic(tools) if tools else None
```

Then in the payload, use `converted_messages` and `converted_tools` instead of `messages` and `tools`.

Also remove the inline `build_system_prompt` call that currently exists in the function — the system prompt is now embedded in `converted_messages` by `_convert_messages_for_anthropic`.

## Verification
After editing, run:
```bash
cd /path/to/projects/crabcakes
python3 -c "from agent.runtime import AgentRuntime; print('import ok')"
```

## What NOT to Change
- Do NOT change `_stream_openai_events` or `_stream_minimax_events`
- Do NOT remove `stream_options` from OpenAI/MiniMax — that is handled in PHASE 4
- Do NOT add tests in this phase
