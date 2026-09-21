# PHASE 6 — Extract MiniMax SSE Helper (W11)

## Objective
Extract the duplicated MiniMax SSE delta parsing into a shared helper so that
`_stream_minimax_events` and `_stream_openai_events` share the same code.

## Files to Read First
- `/path/to/projects/crabcakes/agent/runtime.py` (lines 473–545, 547–665)
- `/path/to/projects/crabcakes/docs/specs/SPEC-RUNTIME-HARDENING-AUDIT.md` (W11)

## Step 1 — Study the Two Stream Functions

Read `_stream_openai_events` (lines 473–545) and `_stream_minimax_events` (547–665).
Both have the same delta-extraction pattern:

```python
d = ev.data
delta = d.get("choices", [{}])[0].get("delta", {})
content = delta.get("content")
if content is not None:
    yield SSEEvent(type="text_delta", data={"content": content})
tc_delta = delta.get("tool_calls", [])
for tcd in tc_delta:
    idx = tcd.get("index", 0)
    if "function" in tcd:
        fname = tcd["function"].get("name") or ""
        fargs = tcd["function"].get("arguments", "") or ""
        yield SSEEvent(type="tool_call_delta", data={
            "index": idx, "name": fname, "arguments": fargs,
            "id": tcd.get("id", "") or "",
        })
```

The only difference: in `_stream_openai_events`, after the tool_calls loop it
also checks for `finish_reason` and yields a `usage` event. MiniMax handles this
differently (inline in its main loop).

## Step 2 — Add the Helper

Add this function after `_parse_sse_line` (around line 445, before `_stream_openai_events`):

```python
def _parse_sse_delta(d: dict) -> list[SSEEvent]:
    """Extract text_delta and tool_call_delta events from an SSE delta dict.

    Shared by _stream_openai_events and _stream_minimax_events.
    The dict is the parsed JSON of an SSE `data:` line whose type is "raw"
    (i.e., it has a `choices` field with a delta).
    """
    events: list[SSEEvent] = []
    delta = d.get("choices", [{}])[0].get("delta", {})
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
```

## Step 3 — Update `_stream_openai_events`

In `_stream_openai_events`, replace the inline delta-extraction block with:
```python
for ev in _stream_openai_events_loop(...):
    yield ev
```

OR replace the inline loop body with:
```python
events = _parse_sse_delta(d)
for e in events:
    yield e
```

**Keep the `finish_reason` / `usage` logic** in `_stream_openai_events` — it is
not part of the shared helper.

## Step 4 — Update `_stream_minimax_events`

Replace the inline delta-extraction block in `_stream_minimax_events` with:
```python
events = _parse_sse_delta(d)
for e in events:
    yield e
```

**Keep the `finish_reason` / `usage` logic** in `_stream_minimax_events` — it
is not part of the shared helper.

## Step 5 — Verify

```bash
cd /path/to/projects/crabcakes
python3 -c "from agent.runtime import AgentRuntime; print('import ok')"
python3 -m py_compile agent/runtime.py && echo "syntax ok"
```

## What NOT to Change
- Do NOT change `_stream_anthropic_events`
- Do NOT change `_call_anthropic`
- Do NOT add tests
