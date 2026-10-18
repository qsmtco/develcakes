# Bugfix D1 — serialize_messages must enforce tool-call/result adjacency (MiniMax 2013)

**Loop:** emergency fix, PM directive 2026-10-17
**Basis:** live incident — MiniMax status_code=2013 "invalid params, tool call
result does not follow tool call" repeating on every retry.
**Files in scope (exactly two):** `models/conversation.py`
(`serialize_messages`), one test in `tests/test_conversation.py` (or the
nearest existing conversation-serialization test file).

## The bug (verified live)

`serialize_messages` emits messages in stored order. If ANY message lands
between an assistant-with-tool_calls and its tool results — a user nudge
sent mid-loop, a compaction stub, a masked block — the wire sequence is:

```
assistant (tool_calls: [X])
user "please continue"          <- interloper
tool (tool_call_id: X)
```

MiniMax (and other strict providers) require every `tool` message to
directly follow the assistant message carrying its call → HTTP 2013 on
every call. The conversation is bricked; retries send the same invalid
shape. Live transcript measured: 50 of 162 tool results not adjacent to
their call.

## Fix

Rewrite `serialize_messages` to emit, for each assistant message with
tool_calls: the assistant entry, then IMMEDIATELY all tool results whose
`tool_call_id` matches that message's calls (in the message's call order).
Messages that interleave in storage are emitted after the tool-result run.
Rules:

1. Walk stored messages; hold tool results in a pending map
   `call_id -> list[result entries]` (preserving order for duplicates).
2. On an assistant entry with tool_calls: emit it, then pop each matching
   result (in the assistant's call order) and emit directly after. Any
   leftover results for that call that arrive later in storage still
   attach here — first come, first emitted.
3. A tool result whose call_id has NO matching assistant call in the list
   (call was compacted/masked away): do NOT emit a dangling tool row.
   Emit it as a user row prefixed `[tool result {call_id}] — ` so the
   content survives without breaking the contract. Log a warning.
4. Non-interleaved sequences serialize byte-identically to today (pure
   reorder, no other change). Keep the empty-assistant placeholder logic
   and index_base warning alignment intact.
5. `to_api_messages` needs NO change — it delegates to serialize_messages.

## Test

ONE test: build messages `[assistant(tc=[X]), user('nudge'), tool(X,'r')]`
→ serialized output must be `[assistant, tool, user]` — tool adjacent to
its call, interloper after. Plus: a tool result with no matching call
round-trips as a user row, no dangling tool row.

## Rules

- `prompts/steelFramedCodeWriter.md` applies. py_compile touched files.
- No pytest — supervisor runs the suite.
- Deliver COMPLETENESS checklist.
