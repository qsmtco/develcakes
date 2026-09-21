# Phase 1 Instructions — Empty-Assistant-Message Fix (Read-Side Filter)

**Master spec:** `/path/to/projects/crabcakes/docs/specs/SPEC-EMPTY-ASSISTANT-MESSAGE-FIX.md`
**Phase:** 1 of 4
**File scope:** 1 file (`models/conversation.py`)
**Estimated delta:** +11 lines net

> **Reminder:** READ ALL FILES BEFORE STARTING. Before writing any code, output a discovery block per `steelFramedCodeWriter.md` Rule 1 + Step 0, trace the data flow per Step 0.5, then implement the hard part first per Rule 2.

---

## What to Change

### File: `models/conversation.py` (358 lines → ~369 lines)

Three edits, all in the same file:

#### Edit 1: Add `import logging`

After the existing `import json` on line 1, add:

```python
import logging
```

#### Edit 2: Add module-level logger

After the `_DEFAULT_ENCODING_NAME` constant definition on line 27 of `models/conversation.py`, and before the `_tiktoken_encoding_for` function definition that begins on line 30, add:

```python
_logger = logging.getLogger(__name__)
```

This is the canonical module-level logger pattern. It goes at the top of the module with the other constants, NOT after the dataclass definitions and NOT inside any function.

#### Edit 3: Replace the ASSISTANT branch in `to_api_messages()`

The current ASSISTANT branch (verified current location: lines 246–260 of `models/conversation.py`) is:

```python
            elif msg.role == MessageRole.ASSISTANT:
                entry: dict = {"role": "assistant", "content": msg.content}
                if msg.tool_calls:
                    entry["tool_calls"] = [
                        {
                            "id": tc.call_id,
                            "type": "function",
                            "function": {
                                "name": tc.tool_name,
                                "arguments": json.dumps(tc.arguments),
                            },
                        }
                        for tc in msg.tool_calls
                    ]
                result.append(entry)
```

Note: spec referenced 244–258, but the actual block now spans 246–260. Use the function/class identifier `to_api_messages` and the `elif msg.role == MessageRole.ASSISTANT:` branch header as the source of truth (per `steelFramedCodeWriter.md` Step 6.8).

Replace it with:

```python
            elif msg.role == MessageRole.ASSISTANT:
                # Defense in depth: strict providers (Cohere, OpenAI tool-loop,
                # Anthropic strict mode) reject {"role":"assistant","content":""}
                # with HTTP 400 "must have non-empty content or tool calls".
                # A corrupt message can exist in conv.messages if it was created
                # before the write-side guard landed, or via some other path we
                # haven't audited. Substitute a descriptive placeholder at the
                # wire boundary so the call succeeds. The original Message stays
                # in conv.messages (audit trail preserved); only the serialized
                # form changes.
                if not msg.content and not msg.tool_calls:
                    _logger.warning(
                        "to_api_messages: empty assistant message at idx=%d "
                        "(role=ASSISTANT, content='', tool_calls=[]) — "
                        "substituting placeholder to satisfy strict providers",
                        len(result),
                    )
                    entry = {
                        "role": "assistant",
                        "content": "[assistant returned no content — placeholder]",
                    }
                else:
                    entry = {"role": "assistant", "content": msg.content}
                    if msg.tool_calls:
                        entry["tool_calls"] = [
                            {
                                "id": tc.call_id,
                                "type": "function",
                                "function": {
                                    "name": tc.tool_name,
                                    "arguments": json.dumps(tc.arguments),
                                },
                            }
                            for tc in msg.tool_calls
                        ]
                result.append(entry)
```

**Critical:** The `else` branch must be byte-equivalent to the pre-fix code (same dict keys, same indentation, same tool_calls structure) so the existing test `test_assistant_message_with_tool_calls` at `tests/test_conversation.py:188` continues to pass.

---

## What NOT to Change

- **Do not** touch any other file. Phase 1 is File 1 only.
- **Do not** add tests in this phase. Phase 3 is tests.
- **Do not** change `agent/runtime.py` in this phase. Phase 2 is the runtime change.
- **Do not** add `_logger.warning()` calls anywhere else. Only inside the new ASSISTANT branch.
- **Do not** reformat, reorder, or "improve" adjacent code (`steelFramedCodeWriter.md` Rule 8).
- **Do not** rename the existing `entry` variable to something else.

---

## Verification Commands (run all, paste full output)

### V1. Confirm existing test still passes (proves else branch unchanged)

```
cd /path/to/projects/crabcakes && pytest tests/test_conversation.py::TestConversationToApiMessages -v 2>&1 | tail -30
```

Expected: all 7 existing tests pass.

### V2. Confirm the existing tool-calls test pattern still matches

```
cd /path/to/projects/crabcakes && grep -n "assistant_message_with_tool_calls" tests/test_conversation.py
cd /path/to/projects/crabcakes && sed -n '188,196p' tests/test_conversation.py
```

Expected: line numbers and content unchanged.

### V3. Confirm new import is added exactly once

```
cd /path/to/projects/crabcakes && grep -n "^import logging" models/conversation.py
cd /path/to/projects/crabcakes && grep -c "^import logging" models/conversation.py
```

Expected: `count` = 1.

### V4. Confirm new logger is added exactly once and placed correctly

```
cd /path/to/projects/crabcakes && grep -n "^_logger = logging.getLogger" models/conversation.py
cd /path/to/projects/crabcakes && grep -c "^_logger = logging.getLogger" models/conversation.py
```

Expected: `count` = 1, line between 24 and 25 (between `_DEFAULT_ENCODING_NAME` at line 23 and `_tiktoken_encoding_for` at line 26). NOT after any `class` definition.

### V5. Confirm the placeholder substitution works (live simulation)

Run this python snippet — it should print "PASS":

```python
cd /path/to/projects/crabcakes && python3 -c "
import sys; sys.path.insert(0, '.')
import logging; logging.basicConfig(level=logging.WARNING)
from models.conversation import Conversation
c = Conversation(agent_name='Coder')
c.add_assistant_message('', [])
out = c.to_api_messages()
assert out == [{'role': 'assistant', 'content': '[assistant returned no content — placeholder]'}], out
print('PASS:', out)
"
```

Expected: `PASS: [{'role': 'assistant', 'content': '[assistant returned no content — placeholder]'}]`

### V6. Confirm the valid case is preserved (live simulation)

```python
cd /path/to/projects/crabcakes && python3 -c "
import sys; sys.path.insert(0, '.')
import json
from models.conversation import Conversation, ToolCall
c = Conversation(agent_name='Coder')
c.add_assistant_message('', [ToolCall(call_id='c1', tool_name='read_file', arguments={'path': 'a.py'})])
out = c.to_api_messages()
assert out[0]['content'] == ''
assert out[0]['tool_calls'][0]['id'] == 'c1'
assert out[0]['tool_calls'][0]['function']['name'] == 'read_file'
assert out[0]['tool_calls'][0]['function']['arguments'] == json.dumps({'path': 'a.py'})
print('PASS:', out)
"
```

Expected: `PASS:` followed by the dict showing tool_calls preserved with empty content (this is the existing test pattern).

### V7. Line count delta

```
cd /path/to/projects/crabcakes && wc -l models/conversation.py
```

Expected: ~369 lines (was 358; +11 net per spec).

### V8. Pattern sweep (must all return 0)

```
cd /path/to/projects/crabcakes && grep -c 'add_assistant_message("", \[\])' models/conversation.py
cd /path/to/projects/crabcakes && grep -c 'add_assistant_message(""' models/conversation.py
```

Expected: both return `0` (this pattern only exists in `agent/runtime.py:2214`, which is not in scope for Phase 1).

---

## Related-Bug Scan (Step 6.6)

Before reporting done, scan `models/conversation.py` for **other** corrupt-message patterns. Specifically:

1. Are there any other `Message(role=..., content="", ...)` literals in the test file that might break? (Tests are out of scope for Phase 1 — they don't run the production code path. But note any you find for Phase 3.)
2. Does `to_api_messages` have any other branch where the placeholder pattern could be applicable? (USER with empty content? TOOL_RESULT with empty content? — Per spec §1 scope: NO. Only ASSISTANT + empty + no tool_calls.)

If you find any related issue, **do NOT silently fix it** — add it to the COMPLETENESS checklist as:

```
Related issue found — not fixed in this phase: <description>
```

The supervisor decides whether to add a phase.

---

## Reports Required

When complete, reply with:

1. **Discovery block** (DISCOVERY: list of files read + what you learned)
2. **Files changed** with line numbers (paste `git diff --stat`)
3. **Full output** of verification commands V1–V8
4. **COMPLETENESS checklist** (literal `**COMPLETENESS:**` block, mandatory)
5. **Related-bug scan** findings (Step 6.6)
6. **Implementation-choice rationale** (Step 6.7) for any non-obvious choice — for Phase 1 the choices are: (a) where to place `_logger` (top-of-file vs after-classes — chose top-of-file because that's the canonical module-level logger pattern), (b) whether to keep the existing `entry: dict` type annotation — kept it for the valid path, omitted it for the new placeholder path (the new entry is always a `dict[str, str]` which doesn't need annotation). If you made other choices, document them.
7. **Spec drift check** (Step 6.8) — confirm the line numbers in the spec (244–258) versus actual current code (246–260) for the ASSISTANT branch. The drift is 2 lines — minor, but flag it in the report so the supervisor can update the spec in the post-mortem phase.

**Do not declare done** unless ALL of V1–V8 pass and the COMPLETENESS block is present.

---

## End of Phase 1 instructions.

Phase 2 (write-side guard in `agent/runtime.py:2214`) and Phase 3 (regression tests) will be sent in separate `/ask` delegations after Phase 1 audit returns clean.