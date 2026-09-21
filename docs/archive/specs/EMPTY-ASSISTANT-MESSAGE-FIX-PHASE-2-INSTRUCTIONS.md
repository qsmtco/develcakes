# Phase 2 Instructions — Empty-Assistant-Message Fix (Write-Side Guard)

**Master spec:** `/path/to/projects/crabcakes/docs/specs/SPEC-EMPTY-ASSISTANT-MESSAGE-FIX.md`
**Phase:** 2 of 4
**File scope:** 1 file (`agent/runtime.py`)
**Estimated delta:** +4 lines net

> **Reminder:** READ ALL FILES BEFORE STARTING. Before writing any code, output a discovery block per `steelFramedCodeWriter.md` Rule 1 + Step 0, trace the data flow per Step 0.5, then implement the hard part first per Rule 2.

---

## Context

Phase 1 shipped the **read-side** fix in `models/conversation.py`: the `to_api_messages()` ASSISTANT branch now substitutes a placeholder when it encounters `content="" AND tool_calls=[]`. This sanitizes the wire payload at the API boundary.

Phase 2 ships the **write-side** guard: at the create-site (`agent/runtime.py:2214`), we substitute a descriptive placeholder string before `add_assistant_message` is called. This prevents future corrupt Messages from being created in `conv.messages`.

**Read-side fix alone is defense-in-depth. Write-side guard is primary prevention.** Together they form a complete solution: no new corrupt messages get created, and any existing ones from before the write-side guard landed are sanitized on the wire.

---

## What to Change

### File: `agent/runtime.py` (currently 2455 lines)

**One edit, exactly one line modified, plus an updated comment.** Target: line 2214.

#### Current code (lines 2210–2217):

```python
                    if not text_content and not response.get("choices"):
                        logger.warning("[tool-loop] sk=%s LLM returned no choices and no content — treating as error",
                                       session_key)
                        conv.add_assistant_message("", [])
                        self._dispatch(self._on_error, session_key,
                                        "Agent returned no content. This may indicate a configuration error "
                                        "or an issue with the LLM provider.")
                        self._auto_save(session_key, conv)
                        return
```

#### Replace `conv.add_assistant_message("", [])` with:

```python
                        # Defense in depth: instead of persisting a corrupt empty
                        # assistant message that downstream providers (Cohere,
                        # strict OpenAI tool-loop, Anthropic strict mode) reject
                        # with HTTP 400 "must have non-empty content or tool calls",
                        # record a descriptive placeholder. The on_error dispatch
                        # below still fires so the user sees the error; this just
                        # prevents the corrupt entry from being saved and re-sent
                        # on subsequent calls.
                        conv.add_assistant_message(
                            "[LLM returned no choices and no content — provider error or malformed response]",
                            [],
                        )
```

**Critical:**
- The `logger.warning(...)` line ABOVE this stays unchanged.
- The `self._dispatch(self._on_error, ...)` line BELOW stays unchanged (user still sees the error).
- The `self._auto_save(session_key, conv)` line BELOW stays unchanged (conv still gets saved).
- The `return` line BELOW stays unchanged.
- The placeholder string is **DIFFERENT** from Phase 1's placeholder on purpose: this one indicates the **creation** event (no LLM response at all), while Phase 1's placeholder indicates a **transit** event (corrupt message survived in conv.messages for some other reason). See the spec's Edge Cases table.

#### What NOT to change:

- Do not modify the `logger.warning(...)` line.
- Do not modify the `self._dispatch(self._on_error, ...)` call.
- Do not modify the `self._auto_save(...)` call.
- Do not modify the `return`.
- Do not touch any other file. Phase 2 is `agent/runtime.py` only.
- Do not reformat, reorder, or "improve" adjacent code (`steelFramedCodeWriter.md` Rule 8).
- Do not add new logging.
- Do not refactor the surrounding `if not tool_calls_raw:` block.

---

## What NOT to Change (file scope)

- **Do not** touch `models/conversation.py`. Phase 1 already shipped; do not re-edit.
- **Do not** add tests in this phase. Phase 3 is tests.
- **Do not** change `agent/runtime.py` anywhere except line 2214.

---

## Verification Commands (run all, paste full output)

### V1. Confirm the pattern is gone and replaced

```
cd /path/to/projects/crabcakes && grep -n 'add_assistant_message("", \[\])' agent/runtime.py
cd /path/to/projects/crabcakes && grep -n 'add_assistant_message' agent/runtime.py
```

Expected: line 2214 (the old pattern) returns 0 hits. The new call appears once at the expected location with the descriptive placeholder.

### V2. Confirm the new placeholder string exists exactly once

```
cd /path/to/projects/crabcakes && grep -n '"\[LLM returned no choices and no content' agent/runtime.py
cd /path/to/projects/crabcakes && grep -c '"\[LLM returned no choices and no content' agent/runtime.py
```

Expected: `count` = 1, line near 2214.

### V3. Confirm surrounding code is unchanged

```
cd /path/to/projects/crabcakes && sed -n '2208,2225p' agent/runtime.py
```

Expected: `logger.warning(...)` line is intact, `self._dispatch(self._on_error, ...)` is intact, `self._auto_save(...)` is intact, `return` is intact. Only the `conv.add_assistant_message(...)` call has changed.

### V4. Pattern sweep across the whole project (Phase 1 + Phase 2 effect)

```
cd /path/to/projects/crabcakes && grep -rn 'add_assistant_message("", \[\])' agent/ models/ tests/
```

Expected: 0 hits. The pre-fix pattern was only at `agent/runtime.py:2214` and it is now gone.

### V5. Existing test suite still passes

```
cd /path/to/projects/crabcakes && pytest tests/test_conversation.py -v 2>&1 | tail -10
```

Expected: 60 passed.

### V6. Live simulation: prove the new placeholder is what gets persisted

```python
cd /path/to/projects/crabcakes && python3 -c "
import sys; sys.path.insert(0, '.')
from models.conversation import Conversation, MessageRole

# Simulate what runtime.py:2214 now does
c = Conversation(agent_name='Coder')
c.add_assistant_message(
    '[LLM returned no choices and no content — provider error or malformed response]',
    [],
)
msgs = c.messages
print('msg[0].role =', msgs[0].role)
print('msg[0].content =', repr(msgs[0].content))
print('msg[0].tool_calls =', msgs[0].tool_calls)
# Now run it through to_api_messages (Phase 1 read-side) and confirm it
# would NOT be flagged as corrupt (because content is non-empty)
out = c.to_api_messages()
print('to_api_messages output:', out)
assert out[0]['content'] == '[LLM returned no choices and no content — provider error or malformed response]', out
print('PASS: new placeholder is non-empty, passes read-side filter, persists through to wire')
"
```

Expected: `PASS:`. The new placeholder is a non-empty string, so `not msg.content` is False, so Phase 1's read-side filter does NOT fire on this message — it goes through the normal else-branch. ✅ This is the design.

### V7. Live simulation: verify the user still sees the error

```python
cd /path/to/projects/crabcakes && python3 -c "
# We cannot run the full _on_error dispatch without a session_key + agent runtime,
# but we can confirm that the _dispatch call is unchanged in the file.
import subprocess
r = subprocess.run(['grep', '-n', '_dispatch(self._on_error', '/path/to/projects/crabcakes/agent/runtime.py'], capture_output=True, text=True)
print(r.stdout)
assert '2214' not in r.stdout or 'self._dispatch' in r.stdout
print('PASS: _on_error dispatch still called')
"
```

Expected: `self._dispatch(self._on_error, session_key, ...)` is present in the file at a line near 2214.

### V8. Full project grep for any remaining empty-assistant create-sites

```
cd /path/to/projects/crabcakes && grep -rn 'add_assistant_message' agent/ models/
```

Expected: every line is either `add_assistant_message(<non-empty string>, ...)` or `add_assistant_message(<variable>, [tc])` where the variable is text content from the LLM. **No call has both empty string literal AND empty list literal.**

---

## Related-Bug Scan (Step 6.6)

Before reporting done, scan `agent/runtime.py` for **other** places where an assistant message could be created with empty content. Specifically:

1. Are there any OTHER call sites of `add_assistant_message` in `agent/runtime.py` besides line 2214? If yes, do any of them construct an empty assistant message? (Check each.)
2. Are there places where `conv.messages.append(Message(role=MessageRole.ASSISTANT, content="", ...))` happens directly without going through `add_assistant_message`?
3. Is there a path where `text_content` from a streaming response could be `""` but `choices` is non-empty (the existing code only fires the placeholder when `not text_content and not response.get("choices")`)? If yes, that's a related bug — flag it but do NOT fix it in Phase 2.

If you find any related issue, **do NOT silently fix it** — add it to the COMPLETENESS checklist as:

```
Related issue found — not fixed in this phase: <description>
```

The supervisor decides whether to add a phase.

---

## Reports Required

When complete, reply with:

1. **Discovery block** (DISCOVERY: list of files read + what you learned)
2. **Files changed** with line numbers (paste `git diff --stat` and the actual `git show HEAD -- agent/runtime.py` diff)
3. **Full output** of verification commands V1–V8
4. **COMPLETENESS checklist** (literal `**COMPLETENESS:**` block, mandatory)
5. **Related-bug scan** findings (Step 6.6)
6. **Implementation-choice rationale** (Step 6.7) — for Phase 2 the choices are:
   - (a) Why a different placeholder string from Phase 1? (Answer: write-side indicates creation event, read-side indicates transit; both are correct but distinguishable in logs.)
   - (b) Why keep the `logger.warning(...)` line? (Answer: it's the actual error log; the new placeholder is just data persistence. Separation of concerns.)
   - (c) Why keep the `self._dispatch(self._on_error, ...)` call? (Answer: the user still needs to see the error UI; the placeholder is for downstream API correctness, not UX.)
   - If you made other choices, document them.
7. **Spec drift check** (Step 6.8) — confirm the line number 2214 matches the actual current code. If it drifted, flag it.

**Do not declare done** unless ALL of V1–V8 pass and the COMPLETENESS block is present.

---

## End of Phase 2 instructions.

Phase 3 (regression tests in `tests/test_conversation.py`) will be sent in a separate `/ask` delegation after Phase 2 audit returns clean.