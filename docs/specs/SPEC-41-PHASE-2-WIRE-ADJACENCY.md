# SPEC-41 Phase 2 (WU 10.2) — Wire adjacency + hot-window `line`

**Parent spec:** `docs/specs/SPEC-41-FEED-POST-AUDIT-FIXES.md` — read §DISCOVERY
and §2 "Phase 2" first. This file is the complete brief.

**Why:** the compiled wire can separate an assistant's tool calls from their
results. `_compiled_messages` (agent/runtime.py:3492) serializes the
current-turn slice and the tail slice INDEPENDENTLY. `serialize_messages`
(`models/conversation.py:144`) only demotes a result to a user row when it
cannot find the declaring assistant IN THE SAME LIST. Result on the wire: a
tool call with no tool result — the MiniMax 2013 / Anthropic strict
ordering shape we already built `normalize_tool_history` to prevent.
Reproduced live by the Supervisor: the split serialization emitted
`['user','user','assistant','tool(call_write)','user(<demoted>)]` —
`call_read` declared, no tool row.

## Deliverables

1. `agent/runtime.py` `_compiled_messages` (:3492) — ONE pass:
   `messages.extend(serialize_messages(turn + tail, index_base=len(messages)))`
   replacing the two separate `extend(serialize_messages(...))` calls.
   `turn` and `tail` are contiguous slices, so the concatenation is
   `conv.messages[wire_from:]` — same content, one serializer call.
2. `agent/runtime.py` `_maybe_compile` hot-window snapshot (:3769-3777) —
   add `"line": w.get("line")` to the snapshot dict. `_capture_hot_window`
   already stores it; `_compiled_messages` already reads it; the snapshot
   was the only place it was lost. Also make the `where` branch use an
   explicit `is not None` check so the byte branch stays intact.
3. Tests (extend `tests/test_spec24_compiled_turn.py` and/or
   `tests/test_spec40_wire_aware_guard.py`):
   - **split-shape wire test**: build the conversation
     `[user, assistant(call_write+call_read), tool(call_write), user-nudge,
     tool(call_read)]`, run the compiled-message path, assert EVERY declared
     tool_call id has a matching `tool` row in the wire list (no demoted
     `[tool result …]` user rows for declared ids).
   - **hot-window line test**: capture a window with `line=42`, run the
     compile path, assert the replay header says `line=42 limit=…` and NOT
     `offset=0`.
   - a regression that a normal (no-interleave) conversation's wire is
     UNCHANGED by the one-pass switch.

## Read budget (hard limit)
- `agent/runtime.py` windows: :3480-3560 (_compiled_messages), :3760-3800
  (_maybe_compile snapshot), :4130-4175 (_capture_hot_window)
- `models/conversation.py` windows: :140-250 (serialize_messages) — read
  once, it is the contract
- ONE of the two spec test files (grep for the fixture pattern)

## Gate (mandatory protocol)
- Targeted: `xvfb-run -a .venv/bin/pytest tests/test_spec24_compiled_turn.py tests/test_spec40_wire_aware_guard.py -q` — paste the tail.
- Full suite BACKGROUNDED (`nohup xvfb-run -a .venv/bin/pytest -q > /tmp/full41b.log 2>&1 &`), report `gate running pid N`, STOP. Supervisor polls. NEVER poll in-turn.
- Baseline: 47F / 5311P / 5S + Phase 1's new tests + your new tests.
- Lint: no new findings vs HEAD in agent/runtime.py.

## Report
Diff summary (the wire change is ~2 lines — say so explicitly if it is),
targeted output, gate pid, tool-call count. Then STOP.
